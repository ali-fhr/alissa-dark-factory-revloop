#!/usr/bin/env python3
"""Claude Code `PreToolUse` hook for the Bash tool: refuse the rm shapes that
would open an interactive prompt nobody in an unattended session can answer.

WHY (devloop issue #125, ported to the reviewer seat by issue #138 — the two
images ship the same bytes). Workers and reviewers run
`claude --dangerously-skip-permissions` and STILL stop on Claude Code's critical-path removal check ("Dangerous rm operation on
statically-unresolvable target … Do you want to proceed?"): `rm`/`rmdir` on a
critical or statically-unresolvable target (a glob, an unexpanded variable, a
command substitution, `~`, `/`, the working directory or a parent of it)
prompts in EVERY mode, and neither a `permissions.allow` rule nor a hook
answering `allow` can approve it. It wedged studio lane #1358 twice on
2026-09-19 and devloop #107 for seven hours on 2026-09-03. What a hook CAN do
is `deny` with a reason — Claude Code honours a PreToolUse deny in bypass
mode (docs: hooks-guide → "PreToolUse hooks fire before any permission-mode
check … blocks the tool even in bypassPermissions mode"), the model reads the
reason and retries with a safe command, and no dialog is ever raised.

WHAT IT DOES. Reads the hook JSON on stdin, walks every segment of
`tool_input.command` — `;`, `&&`, `||`, `|`, `&`, newlines, subshells,
`{ }` groups, backticks and `$(…)` — and, when a segment is `rm`/`rmdir`
(directly, through `sudo`/`command`/`env`/`nice`/`nohup`/`exec`/`timeout`,
through `xargs rm`, or as `find … -exec rm`) whose ANY operand is risky,
prints the deny decision and exits 0. The shell reserved words that can
precede a command inside one segment — `do`, `then`, `else`, `elif`, `if`,
`while`, `until`, `!`, a `case … in PATTERN)` prefix — are skipped first, so
the one-line `for d in a b; do rm -rf $d/*; done` is judged like its
multi-line spelling. Risky means: contains a glob character (`*`, `?`, `[`),
a variable (`$X`, `${X}`), a command substitution, starts with `~`, or
resolves to `/`, the session's working directory (`.`), a parent of it
(`..`, anything outside it), or one of the PROTECTED workspace paths judged
against `ALISSA_WORKSPACE_ROOT` from any cwd: the root itself, every direct
child of it (each is a hub — the guard cannot tell a hub from a stray file
without touching the disk, so it protects them all), every hub's `.source`
SUBTREE and every hub's `main` directory. The asymmetry is deliberate:
`main/` is a checkout `git worktree add` rebuilds from `.source` in seconds,
while the bare `.source` is the only local copy of every object and ref,
unpushed branches included — so `rm -rf main/src` from a hub root passes and
`rm -rf .source/objects` does not. A `cd`/`pushd` earlier in the same list
moves the directory relative operands resolve against (the boundary stays
the session cwd); a `cd` to an unresolvable place makes every later relative
operand unresolvable. Everything else: exit 0 with no output — the normal
flow.

DELIBERATELY BROADER than Claude Code's own classifier where cheap: a quoted
glob (`rm '*'`) and a single-quoted `$` are refused too. The cost of a false
deny is one rewritten command; the cost of a false pass is a seven-hour wedge.
`git rm`, `npm rm`, `echo "rm -rf *"`, `grep "rm -rf"`, `find … -delete` and
an `rm` on literal paths inside the worktree all pass.

FAIL-OPEN. Malformed stdin, a non-Bash tool, or ANY exception → exit 0 with
no stdout and one line on stderr: a broken guard must never block work.
Stdlib only, no subprocess, well under 100 ms.

Installed to /usr/local/share/alissa/hooks/ and registered by the entrypoint
(step 3a) unless ALISSA_SHELL_GUARD=off. Exercised by tests-hooks-guard.sh
(the deny/pass table) and test_hooks_guard.py.
"""

import json
import os
import posixpath
import sys

REASON = (
    "Unattended session: this rm shape opens an interactive prompt nobody can "
    "answer. Delete with explicit, literal paths inside your worktree — e.g. "
    "`find <literal-dir> -mindepth 1 -delete`, `git clean -fdx -- "
    "<literal-path>`, or overwrite the files in place. Never rm a glob, a "
    "variable or a path outside the worktree."
)

REMOVERS = ("rm", "rmdir")
# Wrappers that run the word after them (and their options) as the command.
# `env`/`sudo` also take NAME=VALUE / `-u user` style words, handled below.
WRAPPERS = ("sudo", "doas", "command", "builtin", "exec", "env", "nice",
            "nohup", "ionice", "stdbuf", "timeout", "time", "chronic")
# Wrapper options that consume the NEXT word as their argument.
WRAPPER_OPT_ARGS = {
    "sudo": ("-u", "-g", "-C", "-p", "-h", "-r", "-t", "-U", "-T", "-D"),
    "doas": ("-u", "-C"),
    "env": ("-u", "-C", "-S"),
    "nice": ("-n",),
    "ionice": ("-c", "-n", "-p"),
    "stdbuf": ("-i", "-o", "-e"),
    "timeout": ("-s", "-k"),
}
FIND_EXEC = ("-exec", "-execdir", "-ok", "-okdir")
# Reserved words that precede a command inside ONE segment (`; do rm …`,
# `; then rm …`, `! rm …`). `{`/`}` are already transparent in tokenize;
# `fi`/`done`/`esac` only ever close a segment and need no handling.
RESERVED = ("if", "then", "else", "elif", "while", "until", "do", "!")

F_GLOB, F_VAR, F_SUBST, F_TILDE = "glob", "var", "subst", "tilde"


class Word:
    __slots__ = ("text", "flags", "inner")

    def __init__(self):
        self.text = ""
        self.flags = set()
        self.inner = []  # command-substitution bodies, analysed recursively

    def risky_syntax(self):
        return bool(self.flags & {F_GLOB, F_VAR, F_SUBST, F_TILDE})


def _matching(s, i, open_ch, close_ch):
    """Index of the bracket closing the one at s[i], honouring quotes and
    nesting; len(s) when unterminated."""
    depth = 0
    j = i
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = s.find("'", j + 1)
            j = n if k < 0 else k + 1
            continue
        if c == '"':
            j = _skip_dquote(s, j)
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n


def _skip_dquote(s, i):
    """Index just past the double-quoted string opening at s[i]."""
    j = i + 1
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
        elif c == "$" and j + 1 < n and s[j + 1] == "(":
            j = _matching(s, j + 1, "(", ")") + 1
        elif c == '"':
            return j + 1
        else:
            j += 1
    return n


def tokenize(s):
    """Split a shell command into a flat list of ('op', text), ('sub', inner)
    and ('word', Word) tokens. Quotes never split; `( … )` is one subshell
    token; `{`/`}` group braces are transparent."""
    tokens = []
    n = len(s)
    i = 0
    cur = None

    def flush():
        nonlocal cur
        if cur is not None:
            tokens.append(("word", cur))
            cur = None

    def word():
        nonlocal cur
        if cur is None:
            cur = Word()
        return cur

    while i < n:
        c = s[i]
        if c in " \t\r":
            flush()
            i += 1
        elif c == "\n" or c == ";":
            flush()
            tokens.append(("op", c))
            i += 1
        elif c == "&":
            flush()
            if s.startswith("&&", i):
                tokens.append(("op", "&&"))
                i += 2
            elif s.startswith("&>", i):  # redirection, keep as a word
                w = word()
                w.text += "&>"
                i += 2
            else:
                tokens.append(("op", "&"))
                i += 1
        elif c == "|":
            flush()
            if s.startswith("||", i):
                tokens.append(("op", "||"))
                i += 2
            elif s.startswith("|&", i):
                tokens.append(("op", "|&"))
                i += 2
            else:
                tokens.append(("op", "|"))
                i += 1
        elif c == "(" and cur is None:
            j = _matching(s, i, "(", ")")
            tokens.append(("sub", s[i + 1:j]))
            i = j + 1
        elif c == ")" and cur is None:
            i += 1
        elif c in "{}" and cur is None and (i + 1 >= n or s[i + 1] in " \t\n;&|"):
            i += 1
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                j = n
            w = word()
            body = s[i + 1:j]
            w.text += body
            if any(ch in body for ch in "*?["):
                w.flags.add(F_GLOB)
            if "$" in body or "`" in body:
                w.flags.add(F_VAR)
            i = j + 1
        elif c == '"':
            j = _skip_dquote(s, i)
            _scan_dquote(s[i + 1:j - 1] if s[j - 1:j] == '"' else s[i + 1:j], word())
            i = j
        elif c == "\\":
            w = word()
            if i + 1 < n:
                w.text += s[i + 1]
            i += 2
        elif c == "$":
            w = word()
            if s.startswith("$(", i):
                j = _matching(s, i + 1, "(", ")")
                w.flags.add(F_SUBST)
                w.inner.append(s[i + 2:j])
                w.text += "$(…)"
                i = j + 1
            else:
                w.flags.add(F_VAR)
                w.text += c
                i += 1
        elif c == "`":
            j = s.find("`", i + 1)
            if j < 0:
                j = n
            w = word()
            w.flags.add(F_SUBST)
            w.inner.append(s[i + 1:j])
            w.text += "`…`"
            i = j + 1
        else:
            w = word()
            if c in "*?[":
                w.flags.add(F_GLOB)
            if c == "~" and w.text == "":
                w.flags.add(F_TILDE)
            w.text += c
            i += 1
    flush()
    return tokens


def _scan_dquote(body, w):
    """Double quotes: `$…` and backticks still expand; globs do not, but the
    guard refuses them anyway (see the module docstring)."""
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == "\\" and i + 1 < n:
            w.text += body[i + 1]
            i += 2
        elif c == "$":
            if body.startswith("$(", i):
                j = _matching(body, i + 1, "(", ")")
                w.flags.add(F_SUBST)
                w.inner.append(body[i + 2:j])
                w.text += "$(…)"
                i = j + 1
            else:
                w.flags.add(F_VAR)
                w.text += c
                i += 1
        elif c == "`":
            j = body.find("`", i + 1)
            if j < 0:
                j = n
            w.flags.add(F_SUBST)
            w.inner.append(body[i + 1:j])
            w.text += "`…`"
            i = j + 1
        else:
            if c in "*?[":
                w.flags.add(F_GLOB)
            w.text += c
            i += 1


def _is_redirect(text):
    t = text.lstrip("0123456789")
    return t.startswith((">", "<", "&>"))


def _strip_redirects(words):
    out = []
    skip = False
    for w in words:
        if skip:
            skip = False
            continue
        if _is_redirect(w.text):
            t = w.text.lstrip("0123456789")
            if t in (">", ">>", "<", "<<", "<<<", "&>", "&>>", ">|", ">&", "<&"):
                skip = True  # a bare operator: the target is the next word
            continue
        out.append(w)
    return out


def _is_assignment(text):
    name, eq, _ = text.partition("=")
    return bool(eq) and name.replace("_", "a").isalnum() and not name[:1].isdigit()


def _strip_reserved(words):
    """Drop the reserved words that can precede a command in one segment, and
    a `case` arm's prefix — `case WORD in PATTERN)`, or the bare `PATTERN)`
    that heads the segment after `;;` — so the command after them is judged.
    A `$(…)` head is left alone: it is a substitution, not a pattern."""
    i = 0
    n = len(words)
    while i < n:
        w = words[i]
        if w.text in RESERVED and not w.flags:
            i += 1
        elif w.text == "case" and not w.flags:
            i += 1
            while i < n and words[i].text != "in":
                i += 1
            i += 1
        elif w.text.endswith(")") and F_SUBST not in w.flags:
            i += 1
        else:
            break
    return words[i:]


def _unwrap(words):
    """Drop leading assignments and wrapper commands (`sudo -u x`, `env A=b`,
    `timeout 5`, …). Returns the words from the real command on."""
    i = 0
    n = len(words)
    while i < n and _is_assignment(words[i].text):
        i += 1
    while i < n:
        head = posixpath.basename(words[i].text)
        if head not in WRAPPERS:
            break
        i += 1
        takes_arg = WRAPPER_OPT_ARGS.get(head, ())
        while i < n:
            t = words[i].text
            if t == "--":
                i += 1
                break
            if t.startswith("-"):
                i += 1
                if t in takes_arg and i < n:
                    i += 1
                continue
            if head == "env" and _is_assignment(t):
                i += 1
                continue
            if head == "timeout":
                i += 1  # the DURATION
            break
    return words[i:]


class Cwd:
    """The directory relative operands resolve against, tracked across `cd`.
    `path` is None once a `cd` made it unresolvable."""

    def __init__(self, path):
        self.path = path

    def copy(self):
        return Cwd(self.path)


class Guard:
    def __init__(self, boundary, workspace_root):
        self.boundary = posixpath.normpath(boundary) if boundary else None
        self.workspace_root = posixpath.normpath(workspace_root)

    def protected(self, target):
        """Judged structurally against the workspace root, from ANY cwd (a
        cwd AT the root must protect the hubs too): the root, every direct
        child of it (a hub), every hub's `.source` subtree, every hub's
        `main` directory. `target` is normalized and absolute."""
        rel = posixpath.relpath(target, self.workspace_root)
        if rel == ".":
            return True
        parts = rel.split("/")
        if parts[0] == "..":
            return False
        if len(parts) == 1 or parts[1] == ".source":
            return True
        return len(parts) == 2 and parts[1] == "main"

    # -- operand judgement ---------------------------------------------------
    def risky_operand(self, w, cwd):
        if w.risky_syntax():
            return True
        text = w.text
        if text == "":
            return True
        if text.startswith("/"):
            target = posixpath.normpath(text)
        else:
            if cwd.path is None:
                return True
            target = posixpath.normpath(posixpath.join(cwd.path, text))
        if target == "/" or self.boundary is None:
            return True
        if target == self.boundary or not target.startswith(self.boundary + "/"):
            return True
        return self.protected(target)

    def _operands(self, args):
        ops = []
        end_of_opts = False
        for w in args:
            if end_of_opts:
                ops.append(w)
            elif w.text == "--":
                end_of_opts = True
            elif w.text.startswith("-") and not w.risky_syntax():
                continue
            else:
                ops.append(w)
        return ops

    # -- segments ------------------------------------------------------------
    def check_segment(self, words, cwd):
        """Return the offending segment text, or None."""
        words = _strip_redirects(words)
        cmd = _unwrap(_strip_reserved(words))
        if not cmd:
            return None
        head = posixpath.basename(cmd[0].text)
        if cmd[0].flags & {F_VAR, F_SUBST}:
            head = ""
        if head in REMOVERS:
            if any(self.risky_operand(w, cwd) for w in self._operands(cmd[1:])):
                return " ".join(w.text for w in words)
            return None
        if head == "xargs":
            if any(posixpath.basename(w.text) in REMOVERS for w in cmd[1:]):
                return " ".join(w.text for w in words)
            return None
        if head == "find":
            for a, b in zip(cmd, cmd[1:]):
                if a.text in FIND_EXEC and posixpath.basename(b.text) in REMOVERS:
                    return " ".join(w.text for w in words)
            return None
        if head in ("cd", "pushd"):
            self._track_cd(cmd[1:], cwd)
        return None

    def _track_cd(self, args, cwd):
        targets = [w for w in args if not (w.text.startswith("-") and len(w.text) > 1)]
        if not targets:
            cwd.path = None  # $HOME: outside the worktree, and unknown here
            return
        w = targets[0]
        if w.risky_syntax() or w.text in ("-", "") or cwd.path is None and not w.text.startswith("/"):
            cwd.path = None
            return
        cwd.path = posixpath.normpath(w.text if w.text.startswith("/") else posixpath.join(cwd.path, w.text))

    def check(self, command, cwd):
        """Walk every segment (and every substitution / subshell inside it)."""
        segment = []
        for kind, value in tokenize(command) + [("op", ";")]:
            if kind == "op":
                hit = self.check_segment(segment, cwd)
                if hit:
                    return hit
                segment = []
            elif kind == "sub":
                hit = self.check(value, cwd.copy())
                if hit:
                    return hit
            else:
                for inner in value.inner:
                    hit = self.check(inner, cwd.copy())
                    if hit:
                        return hit
                segment.append(value)
        return None


def decide(data):
    """The deny reason for one hook payload, or None to let it through."""
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return None
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()
    workspace_root = os.environ.get("ALISSA_WORKSPACE_ROOT", "").strip() or "/workspace"
    hit = Guard(cwd, workspace_root).check(command, Cwd(cwd))
    if hit is None:
        return None
    return f"{REASON} Refused: `{hit.strip()}`."


def main():
    try:
        data = json.load(sys.stdin)
        reason = decide(data)
    except Exception as exc:  # fail OPEN: a broken guard must never block work
        sys.stderr.write(f"guard-shell: letting the command through — {type(exc).__name__}: {exc}\n")
        return 0
    if reason is None:
        return 0
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
