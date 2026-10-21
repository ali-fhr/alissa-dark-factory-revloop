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
  * every push by API (round 1 of PR #196): a non-GET `gh api` on
    `…/git/(refs|commits|trees|blobs|tags)` or `…/contents/…`, `gh pr
    update-branch` and a non-GET `…/pulls/<n>/update-branch`, the GraphQL
    ref and commit mutations (`createRef`, `updateRef(s)`, `deleteRef`,
    `createCommitOnBranch`, `updatePullRequestBranch`), and any `gh api
    graphql` call whose query the guard cannot read (`-F query=@file`,
    `--input`, a query from `$VAR` / `$(…)`);
  * the same API writes sent with `curl` / `wget` to a literal
    `api.github.com` URL (round 2) — any case, with or without a scheme,
    with userinfo (`https://x:$TOKEN@api.github.com/…`) or a port, as a
    word or `--url=` (issue #201), with a scheme of one to three slashes
    (`https:/…`, `https:///…`, which curl normalises; issue #203): a non-GET method — explicit, or implied
    by a body (`-d`, `--json`, `-F`, `-T`, `--post-data`, …) — on the paths
    above, a GraphQL ref or merge mutation in the body, or a GraphQL body
    read from a file (`@…`) or built by `$(…)`;
  * every gh form, whatever flags come before the subcommand (round 2): gh
    is a cobra CLI and finds `pr merge` in `gh -R o/r pr merge 7` and `api`
    in `gh -X PUT api …`, so the guard looks the subcommand up the same way;
    and every `gh alias set` / `gh alias import` (issue #201): gh appends
    the call's own words to an alias's expansion, so `gh alias set m pr`
    then `gh m merge 7` rebuilds a denied command from two harmless-looking
    ones, and `gh alias set s -` reads the expansion from stdin. A reviewer
    has no need to define aliases; `gh alias list` / `delete` pass;
  * every merge: `gh pr merge`, `gh api … /merges`, a non-GET `gh api` on
    `…/pulls/<n>/merge` (`-X PUT`, or a POST implied by `-f`/`-F`/`--input`)
    or `…/merge-upstream`, and a `gh api graphql` call naming the
    `mergePullRequest` / `enablePullRequestAutoMerge` / `mergeBranch`
    mutations;
  * hook bypass: `git commit --no-verify` (any abbreviation, `-n` in a short
    cluster), `-c core.hooksPath=…` / `--config-env=core.hooksPath=…` on any
    git command, an alias that runs a push (`-c alias.<x>=…` or a `git
    config` write of `alias.<x>`), any `git config` naming
    `core.hooksPath` except an explicit read (`--get`, `get`, `--list`, …),
    and an assignment of `GIT_CONFIG_NOSYSTEM` / `GIT_CONFIG_SYSTEM` (they
    switch the system pre-push off) or of a `GIT_CONFIG_*` value carrying
    `core.hooksPath`.

WHAT IT LETS THROUGH. Everything else — `git fetch`, `git worktree add
../REVIEW-x <sha>`, `git log`, `git diff`, `gh pr review`, `gh pr view`,
`gh api …/pulls/<n>/reviews`, `gh api …/contents/<path>` (a read), GraphQL
queries, `gh alias list`, `echo "git push"`, `git commit -m "no push"`.

The system pre-push hook (docker/claude/git-hooks/pre-push, every
repository's hook via `git config --system core.hooksPath`) refuses every
push again at git level, for pushes this guard cannot see (a script, a test
suite); the GitHub API merges and ref writes have no git-level backstop.
Neither layer is airtight: this guard reads only the Bash command text, so a
push it cannot see (`… | sh`, `watch git push`, a `python3 -c` string, a
script) that also passes `--no-verify` skips the pre-push too. The same
holds for API writes the command does not spell out: a URL in a variable, a
`curl -K` config file, another HTTP client (httpie, a `python3 -c` request).

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
TRIGGER = re.compile(r"\b(git|gh|GIT_CONFIG\w*)\b|(?i:api\.github\.com)")

PUSH_VERBS = ("push", "send-pack", "http-push")
PUSH_IN_ALIAS = re.compile(r"\b(push|send-pack|http-push)\b")
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
# GraphQL mutations that write a ref or a commit: pushes by another name.
GH_REF_MUTATIONS = re.compile(
    r"\b(createRef|updateRef|updateRefs|deleteRef|createCommitOnBranch|updatePullRequestBranch)\b")
# REST paths whose non-GET methods write git objects or refs (`git/trees` →
# `git/commits` → `PATCH git/refs` lands a commit on a branch with no git push),
# or commit a file to a branch (`PUT …/contents/<path>`).
GH_GIT_WRITE_PATH = re.compile(r"(^|/)git/(refs|commits|trees|blobs|tags)(/|$)|(^|/)contents(/|$)")
GH_FIELD = ("-F", "--field")  # the typed field: `@file` reads its value from a file
# gh (cobra) words that never take the next word as a value while it looks
# up a subcommand; any other `--flag` (no `=`) or two-letter `-x` does.
COBRA_BOOL = ("-h", "--help", "--version")

# curl / wget straight to the REST or GraphQL API (round 2 of PR #196).
# Hostnames are case-insensitive, and curl sends a URL's userinfo
# (`https://x:$TOKEN@api.github.com/…`) as Basic auth (issue #201). curl
# also normalises a scheme written with one or three slashes (`https:/…`,
# `https:///…`) to `https://` (issue #203).
GH_API_URL = re.compile(r"(https?:/{1,3})?([^/\s]*@)?api\.github\.com\.?(:\d+)?(/|$)", re.I)
CURL_BODY = ("--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode", "--json", "--form",
             "--form-string")
# curl long options whose value is the NEXT word.
CURL_LONG_ARGS = CURL_BODY + ("--request", "--upload-file", "--header", "--url", "--user", "--output", "--config",
                              "--user-agent", "--referer", "--cookie", "--cookie-jar", "--max-time",
                              "--connect-timeout", "--retry", "--proxy", "--cacert", "--cert", "--key",
                              "--write-out", "--oauth2-bearer", "--resolve", "--connect-to", "--range",
                              "--dump-header", "--limit-rate", "--max-filesize")
# curl short options that take a value (the rest of the cluster, else the next word).
CURL_ARG_SHORTS = "XdFTHouAbceKxwrmCEyYzDUQPt"
WGET_BODY = ("--post-data", "--post-file", "--body-data", "--body-file")
WGET_OPT_ARGS = WGET_BODY + ("--method", "--header", "--output-document", "--user", "--password")


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


def _cobra_subcommand(args):
    """The index of the subcommand gh (cobra) runs, or None. Cobra skips
    flags while it looks the subcommand up: a `--flag` without `=` or a
    two-letter `-x` takes the next word as its value (unless it is a known
    boolean such as `--help`), and `--` ends the lookup. So `gh -R o/r pr
    merge`, `gh -X PUT api …` and `gh --jq .x api …` all reach their
    subcommand — checked against gh 2.102."""
    i, n = 0, len(args)
    while i < n:
        t = args[i].text
        if t == "--":
            return None
        if t and not t.startswith("-"):
            return i
        if "=" not in t and t not in COBRA_BOOL and (t.startswith("--") or len(t) == 2):
            i += 2
        else:
            i += 1
    return None


def _api_url_path(text):
    """The API path of a curl / wget word naming api.github.com (bare, or
    as `--url=…`), or None. The host comes off whether or not a scheme is
    written, so `api.github.com/graphql` is judged as `graphql`; lower-cased,
    since the guard's paths are."""
    if text.startswith("--url="):
        text = text[len("--url="):]
    m = GH_API_URL.match(text)
    if not m:
        return None
    return _endpoint_path(text[m.end():].split("#", 1)[0]).lower()


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
                if posixpath.basename(w.text) in ("git", "gh", "curl", "wget"):
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
        if head in ("curl", "wget"):
            return self.check_http(head, args)
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
        if key.startswith("alias.") and PUSH_IN_ALIAS.search(value):
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
        if any(w.text in CONFIG_READS for w in args):
            return None
        lowered = [w.text.lower() for w in args]
        for k, t in enumerate(lowered):
            # `git config --global alias.p 'push --no-verify'`: the persistent
            # form of `-c alias.p=push`; a bare `git config alias.p` is a read.
            if t.startswith("alias.") and any(PUSH_IN_ALIAS.search(v) for v in lowered[k + 1:]):
                return self.reason(f"`git config {args[k].text} …` makes an alias that runs a push")
        if not any(t == HOOKS_PATH or t.startswith(HOOKS_PATH + "=") for t in lowered):
            return None
        return self.reason("`git config core.hooksPath` changes the seat's git hooks")

    # -- gh ---------------------------------------------------------------------
    def check_gh(self, args):
        # gh (cobra) finds each subcommand past any flags written before it —
        # `gh -R o/r pr merge 7`, `gh -X PUT api …` — so the subcommand is the
        # first positional by cobra's rule, never simply the first word.
        k = _cobra_subcommand(args)
        if k is None:
            return None
        sub, lead, rest = args[k].text, args[:k], args[k + 1:]
        if sub == "pr":
            j = _cobra_subcommand(rest)
            verb = rest[j].text if j is not None else None
            if verb == "merge":
                return self.reason("`gh pr merge` merges a pull request")
            if verb == "update-branch":
                return self.reason("`gh pr update-branch` writes a commit onto the pull request's branch")
            return None
        if sub == "repo":
            j = _cobra_subcommand(rest)
            if j is not None and rest[j].text == "sync":
                return self.reason("`gh repo sync` pushes a branch")
            return None
        if sub == "api":
            # the leading words are flags and their values: never the endpoint
            return self.check_gh_api(list(lead) + list(rest), lead=len(lead))
        if sub == "alias":
            return self.check_gh_alias(rest)
        return None

    def check_gh_alias(self, args):
        """Every `gh alias set` and `gh alias import` (issue #201). gh appends
        the call's words to the expansion, so no expansion can be judged on
        its own: `gh alias set m pr` then `gh m merge 7` is `gh pr merge 7`,
        `gh alias set a api` then `gh a -X PUT …/merge` a merge, and
        `gh alias set s -` reads the expansion from stdin. `list` and
        `delete` pass."""
        j = _cobra_subcommand(args)
        if j is None:
            return None
        verb = args[j].text
        if verb == "import":
            return self.reason("`gh alias import` defines aliases the guard cannot read")
        if verb == "set":
            return self.reason("`gh alias set` defines an alias the call's own words can turn into a "
                               "denied command (gh appends them to the expansion)")
        return None

    def check_gh_api(self, args, lead=0):
        method = None
        body = False
        endpoint = None
        texts = []
        unread_query = False  # a GraphQL query whose text the guard cannot see
        j, n = 0, len(args)
        while j < n:
            w = args[j]
            t = w.text
            texts.append(t)
            name, eq, attached = t.partition("=")
            if t.startswith("-X") and len(t) > 2:
                method = t[2:]
            elif name == "--method" and eq:
                method = attached
            elif t in GH_API_OPT_ARGS:
                nxt = args[j + 1] if j + 1 < n else None
                value = nxt.text if nxt else ""
                texts.append(value)
                if t in ("-X", "--method"):
                    method = value
                if t in GH_API_BODY:
                    body = True
                    unread_query |= self._unread_query(t, value, nxt)
                j += 1
            elif name in GH_API_BODY and eq:
                body = True
                unread_query |= self._unread_query(name, attached, w)
            elif not t.startswith("-") and endpoint is None and j >= lead:
                endpoint = t
            j += 1
        if endpoint is None:
            return None
        method = (method or ("POST" if body else "GET")).upper()
        return self.judge_api("gh api", method, _endpoint_path(endpoint), texts, unread_query)

    def judge_api(self, tool, method, path, texts, unread_query):
        """The deny reason for one GitHub API call (`gh api`, or curl / wget
        to api.github.com), or None."""
        if path == "graphql":
            if any(GH_MERGE_MUTATIONS.search(t) for t in texts):
                return self.reason("a GraphQL merge mutation merges a pull request")
            if any(GH_REF_MUTATIONS.search(t) for t in texts):
                return self.reason("a GraphQL ref or commit mutation writes to a branch")
            if unread_query:
                hint = "`-f query='…'`" if tool == "gh api" else "the request body"
                return self.reason("a GraphQL query the guard cannot read (`@file`, `--input`, `$…`) may "
                                   f"write a branch; inline it with {hint}")
            return None
        if path == "merges" or path.endswith("/merges"):
            return self.reason(f"`{tool} …/merges` merges a branch")
        if method not in ("GET", "HEAD"):
            if re.search(r"(^|/)pulls/[^/]+/merge$", path):
                return self.reason(f"`{tool} -X {method} …/merge` merges a pull request")
            if path.endswith("/merge-upstream"):
                return self.reason(f"`{tool} …/merge-upstream` pushes a branch")
            if re.search(r"(^|/)pulls/[^/]+/update-branch$", path):
                return self.reason(f"`{tool} …/update-branch` writes a commit onto the pull request's branch")
            if GH_GIT_WRITE_PATH.search(path):
                return self.reason(f"`{tool} -X {method} {path}` writes git objects or refs (a push by another name)")
        return None

    # -- curl / wget to the GitHub API ------------------------------------------
    def check_http(self, head, args):
        """The same REST and GraphQL writes sent straight to api.github.com.
        Only a URL spelled out in the command is seen (a URL in a variable or
        a `curl -K` config file is not)."""
        paths = [p for p in (_api_url_path(w.text) for w in args) if p is not None]
        if not paths:
            return None
        method = None
        body = False
        get = False
        unread = False
        texts = [w.text for w in args]
        j, n = 0, len(args)
        while j < n:
            w = args[j]
            t = w.text
            nxt = args[j + 1] if j + 1 < n else None
            name, eq, attached = t.partition("=")
            if t.startswith("--"):
                if head == "wget":
                    value = attached if eq else (nxt.text if nxt and name in WGET_OPT_ARGS else "")
                    used = nxt if not eq else w
                    if name == "--method":
                        method = value
                    elif name in WGET_BODY:
                        body = True
                        unread |= name.endswith("-file") or self._unread_body(value, used)
                else:
                    value = attached if eq else (nxt.text if nxt and name in CURL_LONG_ARGS else "")
                    used = nxt if not eq else w
                    if name == "--request":
                        method = value
                    elif name in ("--get",):
                        get = True
                    elif name == "--head":
                        method = method or "HEAD"
                    elif name == "--upload-file":
                        method, body = method or "PUT", True
                    elif name in CURL_BODY:
                        body = True
                        unread |= self._unread_body(value, used)
                if not eq and name in (WGET_OPT_ARGS if head == "wget" else CURL_LONG_ARGS):
                    j += 1
            elif head == "curl" and t.startswith("-") and len(t) > 1:
                # a short cluster: `-sX PATCH`, `-XPATCH`, `-d @body.json`
                for k, ch in enumerate(t[1:], start=1):
                    if ch == "G":
                        get = True
                    elif ch == "I":
                        method = method or "HEAD"
                    if ch not in CURL_ARG_SHORTS:
                        continue
                    tail = t[k + 1:]
                    value, used = (tail, w) if tail else ((nxt.text, nxt) if nxt else ("", None))
                    if ch == "X":
                        method = value
                    elif ch == "T":
                        method, body = method or "PUT", True
                    elif ch in "dF":
                        body = True
                        unread |= self._unread_body(value, used)
                    if not tail:
                        j += 1
                    break
            j += 1
        if not method:
            method = "GET" if get or not body else "POST"
        for path in paths:
            hit = self.judge_api(head, method.upper(), path, texts, unread)
            if hit:
                return hit
        return None

    def _unread_body(self, value, word):
        """True when a request body hides its text: read from a file (`@…`)
        or built by a `$(…)` expansion. A readable GraphQL body always
        carries a `{` (JSON, and the query inside it)."""
        if value.startswith("@") or (word is not None and self.gs.F_SUBST in word.flags):
            return True
        return "{" not in value

    def _unread_query(self, opt, value, word):
        """True when this body option hides a GraphQL query's text: `--input`
        (the whole body from a file or stdin), `-F query=@file`, or a query
        value built from a `$VAR` / `$(…)` expansion. The tokenizer flags a
        single-quoted `$` too (GraphQL's own `$var`), so a variable is told
        by content: every operation the guard can read carries a `{`."""
        if opt == "--input":
            return True
        key, eq, val = value.partition("=")
        if key != "query" or not eq:
            return False
        if opt in GH_FIELD and val.startswith("@"):
            return True
        if word is not None and self.gs.F_SUBST in word.flags:
            return True
        return "{" not in val

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
