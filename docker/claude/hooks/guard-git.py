#!/usr/bin/env python3
"""Claude Code `PreToolUse` hook for the Bash tool: the reviewer seat never
pushes and never merges (issue #195, MDF M7 push safety, GP-3).

WHY. The reviewer seat is read-only by contract (CR6: "You never push and
never delete"), but nothing enforced it: reviewers run
`claude --dangerously-skip-permissions` with a token that may carry write
access (a client may provision a permissive one). On 2026-10-21 an operator
session pushed an empty-tree commit to a default branch via a bare
`git push`. devloop's GP-2 judges each push; a reviewer has no push to
judge, so this port is stricter: EVERY push is refused.

WHAT IT DENIES, in every segment of `tool_input.command` (walked with
guard-shell.py's tokenizer, imported rather than copied: `;`, `&&`, `||`,
`|`, newlines, subshells, `{ }`, backticks and `$(…)`, after wrappers such as
`sudo`/`env`/`timeout`, `git -C <dir>` / `-c k=v`, through `xargs`, and
inside `sh -c` / `bash -c` / `eval` strings):

  * every push: `git push` in any form, and the other git verbs that write a
    remote's refs or objects — `send-pack`, `http-push`, `subtree push`,
    `lfs push` — plus `gh repo sync` (it moves a branch on GitHub);
  * every merge: `gh pr merge`, `gh api … /merges`, a non-GET `gh api` on
    `…/pulls/<n>/merge` (`-X PUT`, or a POST implied by `-f`/`-F`/`--input`)
    or `…/merge-upstream`, and a `gh api graphql` call naming the
    `mergePullRequest` / `enablePullRequestAutoMerge` / `mergeBranch`
    mutations;
  * hook bypass: `git commit --no-verify` (any abbreviation, `-n` in a short
    cluster), `-c core.hooksPath=…` / `--config-env=core.hooksPath=…` on any
    git command, `-c alias.<x>=…` that runs a push, any `git config` naming
    `core.hooksPath` except an explicit read (`--get`, `get`, `--list`, …),
    and an assignment of `GIT_CONFIG_NOSYSTEM` / `GIT_CONFIG_SYSTEM` (they
    switch the system pre-push off) or of a `GIT_CONFIG_*` value carrying
    `core.hooksPath`.

WHAT IT LETS THROUGH. Everything else — `git fetch`, `git worktree add
../REVIEW-x <sha>`, `git log`, `git diff`, `gh pr review`, `gh pr view`,
`gh api …/pulls/<n>/reviews`, `echo "git push"`, `git commit -m "no push"`.

The system pre-push hook (docker/claude/git-hooks/pre-push, every
repository's hook via `git config --system core.hooksPath`) refuses every
push again at git level, for pushes this guard cannot see (a script, a test
suite); the GitHub API merges have no git-level backstop.

FAIL-OPEN, like guard-shell: malformed stdin, a non-Bash tool, a missing
guard-shell.py, or ANY exception → exit 0 with no stdout and one line on
stderr. Stdlib only, no subprocess.

Installed to /usr/local/share/alissa/hooks/ next to guard-shell.py and
registered by the entrypoint (step 3a) in the daemon (reviewer) role.
Exercised by test_git_guard.py.
"""

import importlib.util
import json
import os
import posixpath
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The one message, shared word for word with git-hooks/pre-push.
REASON = "Reviewer seat: reviewers never push or merge — record findings in the review instead."
REASON_TAIL = (
    " Post them with `gh pr review` (or review comments) and leave pushing and "
    "merging to the implementer and the operator."
)

# The quick prefilter: a command that names none of these is never parsed.
TRIGGER = re.compile(r"\b(git|gh|GIT_CONFIG\w*)\b")

PUSH_VERBS = ("push", "send-pack", "http-push")
PUSH_SUBVERBS = {"subtree": "push", "lfs": "push"}  # `git subtree push`, `git lfs push`
NO_VERIFY = "--no-verify"
# `git commit` short options that consume the rest of their cluster (or the
# next word) as an argument: a `n` after one of them is part of the value.
COMMIT_ARG_SHORTS = "mFCctuS"
# … and those whose value is the NEXT word when nothing follows them.
COMMIT_NEXT_ARG = "mFCct"
# `git commit` long options whose value is the NEXT word.
COMMIT_LONG_ARGS = ("--message", "--file", "--author", "--date", "--template", "--reuse-message",
                    "--reedit-message", "--fixup", "--squash", "--trailer", "--cleanup",
                    "--pathspec-from-file")
CONFIG_READS = ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list", "get", "list")
# git's global options that take the NEXT word as their value.
GIT_OPT_ARGS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env",
                "--attr-source")
HOOKS_PATH = "core.hookspath"
# Assignments that switch the system config (and so the system pre-push) off.
NOSYSTEM_VARS = ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM")
GIT_CONFIG_VAR = re.compile(r"GIT_CONFIG_(PARAMETERS|KEY_\d+|VALUE_\d+)")
# Commands whose arguments are themselves assignments (`export X=1`).
DECLARERS = ("export", "declare", "typeset", "readonly", "local", "env")
SHELLS = ("sh", "bash", "dash", "zsh", "ksh")

# `gh api` options whose value is the NEXT word, and those implying a POST.
GH_API_OPT_ARGS = ("-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header", "--input",
                   "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview")
GH_API_BODY = ("-f", "--raw-field", "-F", "--field", "--input")
GH_MERGE_MUTATIONS = re.compile(r"\b(mergePullRequest|enablePullRequestAutoMerge|mergeBranch)\b")
GH_PR_OPT_ARGS = ("-R", "--repo")


def _load_shell_guard():
    """guard-shell.py's module (its name has a dash: no plain import)."""
    path = os.path.join(HERE, "guard-shell.py")
    spec = importlib.util.spec_from_file_location("alissa_guard_shell", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prefix_of(name, full, minimum):
    """True when `name` abbreviates the long option `full` (git accepts any
    unique prefix); shorter than `minimum` never matches."""
    return len(name) >= minimum and name.startswith("--") and full.startswith(name)


def _short_cluster(text, letter, arg_shorts):
    """True when the short-option cluster `-xyz` carries `letter` before any
    option that takes the rest of the cluster as its value."""
    if not text.startswith("-") or text.startswith("--") or len(text) < 2:
        return False
    for ch in text[1:]:
        if ch == letter:
            return True
        if ch in arg_shorts:
            return False
    return False


def _assignment(text):
    """`(NAME, value)` for a `NAME=value` word, else None."""
    name, eq, value = text.partition("=")
    if eq and name and name.replace("_", "a").isalnum() and not name[:1].isdigit():
        return name, value
    return None


def _endpoint_path(text):
    """`/repos/o/r/pulls/1/merge?x=1` → `repos/o/r/pulls/1/merge`."""
    path = text.split("?", 1)[0].strip("/")
    if "://" in path:  # a full URL: keep the path after the host
        path = path.split("://", 1)[1].partition("/")[2]
    return path


class GitGuard:
    def __init__(self, gs):
        self.gs = gs

    @staticmethod
    def reason(detail):
        return f"{REASON} ({detail}){REASON_TAIL}"

    # -- one segment ------------------------------------------------------------
    def check_segment(self, words):
        """The deny reason for one segment, or None."""
        gs = self.gs
        words = gs._strip_redirects(words)
        cmd = gs._unwrap(gs._strip_reserved(words))
        assignments = words[:len(words) - len(cmd)]
        if cmd and posixpath.basename(cmd[0].text) in DECLARERS:
            assignments = words
        for w in assignments:
            hit = self.check_assignment(w.text)
            if hit:
                return hit
        if not cmd or cmd[0].flags & {gs.F_VAR, gs.F_SUBST}:
            return None
        head = posixpath.basename(cmd[0].text)
        args = cmd[1:]
        if head == "xargs":  # `… | xargs git push`: judge the command it runs
            for k, w in enumerate(args):
                if posixpath.basename(w.text) in ("git", "gh"):
                    return self.check_segment(args[k:])
            return None
        if head in SHELLS:  # `bash -c 'git push'`, `sh -ec "…"`
            for a, b in zip(args, args[1:]):
                if _short_cluster(a.text, "c", ""):
                    return self._inner(b.text)
            return None
        if head == "eval":
            return self._inner(" ".join(w.text for w in args))
        if head == "git":
            return self.check_git(args)
        if head == "gh":
            return self.check_gh(args)
        return None

    def _inner(self, command):
        """The reason for a command string run by a shell, or None."""
        hit = self.check(command)
        return hit[0] if hit else None

    def check_assignment(self, text):
        found = _assignment(text)
        if found is None:
            return None
        name, value = found
        if name in NOSYSTEM_VARS:
            return self.reason(f"setting `{name}` switches the system pre-push hook off")
        if GIT_CONFIG_VAR.fullmatch(name) and "hookspath" in value.lower():
            return self.reason(f"`{name}` carrying `core.hooksPath` bypasses the seat's git hooks")
        return None

    # -- git --------------------------------------------------------------------
    def check_git(self, args):
        gs = self.gs
        i, n = 0, len(args)
        while i < n:
            w = args[i]
            t = w.text
            if w.flags & {gs.F_VAR, gs.F_SUBST} or not t.startswith("-"):
                break
            name, eq, attached = t.partition("=")
            value = attached if eq else (args[i + 1].text if t in GIT_OPT_ARGS and i + 1 < n else "")
            if name == "-c":
                hit = self.check_config_pair(value)
                if hit:
                    return hit
            elif name == "--config-env" and value.lower().startswith(HOOKS_PATH + "="):
                return self.reason("`--config-env=core.hooksPath=…` bypasses the seat's git hooks")
            i += 2 if t in GIT_OPT_ARGS else 1
        if i >= n:
            return None
        sub = args[i].text
        rest = args[i + 1:]
        if sub in PUSH_VERBS:
            return self.reason(f"`git {sub}` writes to a remote")
        if sub in PUSH_SUBVERBS and any(w.text == PUSH_SUBVERBS[sub] for w in rest):
            return self.reason(f"`git {sub} push` writes to a remote")
        if sub == "commit":
            return self.check_commit(rest)
        if sub == "config":
            return self.check_config(rest)
        return None

    def check_config_pair(self, pair):
        key, _, value = pair.partition("=")
        key = key.lower()
        if key == HOOKS_PATH:
            return self.reason("`-c core.hooksPath=…` bypasses the seat's git hooks")
        if key.startswith("alias.") and re.search(r"\b(push|send-pack|http-push)\b", value):
            return self.reason(f"`-c {key}=…` runs a push")
        return None

    def check_commit(self, args):
        skip = False
        for w in args:
            t = w.text
            if skip:  # the value of `-m`, `--file`, …: a message may say anything
                skip = False
                continue
            if t == "--":
                break
            if t in COMMIT_LONG_ARGS:
                skip = True
                continue
            if _prefix_of(t.split("=", 1)[0], NO_VERIFY, 6):
                return self.reason(f"`git commit {t}` skips the git hooks")
            if _short_cluster(t, "n", COMMIT_ARG_SHORTS):
                return self.reason(f"`git commit {t}` skips the git hooks (`-n` is `--no-verify`)")
            if t.startswith("-") and not t.startswith("--") and t[-1] in COMMIT_NEXT_ARG:
                skip = not any(ch in COMMIT_ARG_SHORTS for ch in t[1:-1])  # `-am msg`; `-mfoo` carries it
        return None

    def check_config(self, args):
        lowered = [w.text.lower() for w in args]
        if not any(t == HOOKS_PATH or t.startswith(HOOKS_PATH + "=") for t in lowered):
            return None
        if any(w.text in CONFIG_READS for w in args):
            return None
        return self.reason("`git config core.hooksPath` changes the seat's git hooks")

    # -- gh ---------------------------------------------------------------------
    def check_gh(self, args):
        if not args:
            return None
        sub = args[0].text
        if sub == "pr":
            positional = self._positional(args[1:], GH_PR_OPT_ARGS)
            if positional[:1] == ["merge"]:
                return self.reason("`gh pr merge` merges a pull request")
            return None
        if sub == "repo":
            if self._positional(args[1:], ())[:1] == ["sync"]:
                return self.reason("`gh repo sync` pushes a branch")
            return None
        if sub == "api":
            return self.check_gh_api(args[1:])
        return None

    @staticmethod
    def _positional(args, opt_args):
        out = []
        skip = False
        for w in args:
            if skip:
                skip = False
            elif w.text in opt_args:
                skip = True
            elif not w.text.startswith("-"):
                out.append(w.text)
        return out

    def check_gh_api(self, args):
        method = None
        body = False
        endpoint = None
        texts = []
        j, n = 0, len(args)
        while j < n:
            t = args[j].text
            texts.append(t)
            name, eq, attached = t.partition("=")
            if t.startswith("-X") and len(t) > 2:
                method = t[2:]
            elif name == "--method" and eq:
                method = attached
            elif t in GH_API_OPT_ARGS:
                value = args[j + 1].text if j + 1 < n else ""
                texts.append(value)
                if t in ("-X", "--method"):
                    method = value
                if t in GH_API_BODY:
                    body = True
                j += 1
            elif name in GH_API_BODY and eq:
                body = True
            elif not t.startswith("-") and endpoint is None:
                endpoint = t
            j += 1
        if endpoint is None:
            return None
        method = (method or ("POST" if body else "GET")).upper()
        path = _endpoint_path(endpoint)
        if path == "graphql":
            if any(GH_MERGE_MUTATIONS.search(t) for t in texts):
                return self.reason("a GraphQL merge mutation merges a pull request")
            return None
        if path == "merges" or path.endswith("/merges"):
            return self.reason("`gh api …/merges` merges a branch")
        if method != "GET":
            if re.search(r"(^|/)pulls/[^/]+/merge$", path):
                return self.reason(f"`gh api -X {method} …/merge` merges a pull request")
            if path.endswith("/merge-upstream"):
                return self.reason("`gh api …/merge-upstream` pushes a branch")
        return None

    # -- the walk ---------------------------------------------------------------
    def check(self, command):
        """Walk every segment (and every substitution / subshell inside it)."""
        segment = []
        for kind, value in self.gs.tokenize(command) + [("op", ";")]:
            if kind == "op":
                hit = self.check_segment(segment)
                if hit:
                    return hit, " ".join(w.text for w in segment)
                segment = []
            elif kind == "sub":
                hit = self.check(value)
                if hit:
                    return hit
            else:
                for inner in value.inner:
                    hit = self.check(inner)
                    if hit:
                        return hit
                segment.append(value)
        return None


def decide(data, gs=None):
    """The deny reason for one hook payload, or None to let it through."""
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return None
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not TRIGGER.search(command):
        return None
    gs = gs or _load_shell_guard()
    hit = GitGuard(gs).check(command)
    if hit is None:
        return None
    reason, text = hit
    return f"{reason} Refused: `{text.strip()}`."


def main():
    try:
        data = json.load(sys.stdin)
        reason = decide(data)
    except Exception as exc:  # fail OPEN: a broken guard must never block work
        sys.stderr.write(f"guard-git: letting the command through — {type(exc).__name__}: {exc}\n")
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
