"""Pinning tests for the reviewer seat's PreToolUse git guard
(docker/claude/hooks/guard-git.py, issue #195, MDF M7 push safety, GP-3),
driven the way claude drives it: JSON on stdin, decision on stdout, exit code.

The reviewer seat never pushes and never merges, so the deny table is every
push form (whatever the remote, refspec, flags or branch — no repository is
consulted), every merge form, and the hook bypasses that would switch the
system pre-push off; the pass table is what a reviewer does all day — fetch,
worktree add, log, diff, `gh pr review`, `gh pr view`. The pre-push hook (the
git-level half) is pinned by docker/claude/tests-hooks-guard.sh section 5.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[7]
HOOKS = REPO_ROOT / "docker" / "claude" / "hooks"
GIT_GUARD = HOOKS / "guard-git.py"
PRE_PUSH = REPO_ROOT / "docker" / "claude" / "git-hooks" / "pre-push"
CWD = "/workspace/some-repo/REVIEW-some-repo-pr7-r1"
REASON_HEAD = "Reviewer seat: reviewers never push or merge — record findings in the review instead."

pytestmark = pytest.mark.skipif(shutil.which("python3") is None, reason="the guard is a python3 script")


def run_guard(command, tool="Bash", raw=None, script=GIT_GUARD):
    payload = raw if raw is not None else json.dumps({
        "session_id": "s1", "cwd": CWD, "hook_event_name": "PreToolUse",
        "tool_name": tool, "tool_input": {"command": command},
    })
    env = {"PATH": os.environ.get("PATH", "")}
    return subprocess.run(["python3", str(script)], input=payload, capture_output=True, text=True, env=env)


def denied(command):
    done = run_guard(command)
    assert done.returncode == 0, done
    assert done.stdout.strip(), f"expected a deny for {command!r}, got nothing (stderr {done.stderr!r})"
    d = json.loads(done.stdout)["hookSpecificOutput"]
    assert d["hookEventName"] == "PreToolUse"
    assert d["permissionDecision"] == "deny"
    reason = d["permissionDecisionReason"]
    assert reason.startswith(REASON_HEAD), reason
    assert "gh pr review" in reason, reason  # says what to do instead
    assert " Refused: `" in reason and reason.endswith("`."), reason  # names the offending segment
    return reason


def passed(command):
    done = run_guard(command)
    assert done.returncode == 0, done
    assert done.stdout == "", f"expected silence for {command!r}, got {done.stdout!r}"
    assert done.stderr == "", done.stderr


def test_the_guard_is_executable_and_imports_the_shell_guards_tokenizer():
    assert os.access(GIT_GUARD, os.X_OK)
    text = GIT_GUARD.read_text()
    assert text.startswith("#!/usr/bin/env python3\n")
    assert '"guard-shell.py"' in text
    assert "def tokenize" not in text  # reused by import, never copied


def test_the_pre_push_hook_carries_the_same_message():
    assert REASON_HEAD in PRE_PUSH.read_text()


# -- every push ----------------------------------------------------------------

PUSHES = [
    # the incident's shape, and the ones a TASK branch would make
    "git push",
    "git push origin",
    "git push -u origin HEAD",
    "git push origin HEAD:refs/heads/TASK-1-x",
    "git push origin TASK-1-x",
    "git push origin main",
    "git push --force origin main",
    "git push -f",
    "git push --force-with-lease",
    "git push origin +HEAD:main",
    "git push --mirror",
    "git push --all origin",
    "git push --tags",
    "git push origin --delete TASK-1-x",
    "git push origin :TASK-1-x",
    "git push --no-verify",
    "git push --dry-run origin HEAD",
    'git push origin "$BRANCH"',
    "git push https://github.com/acme/app.git HEAD:main",
    # through git's global options
    "git -C /workspace/some-repo/main push",
    "git -C ../x push origin HEAD",
    "git -c push.default=matching push",
    "git --git-dir=/tmp/x.git push",
    "git --no-pager push",
    # the other verbs that write a remote
    "git send-pack https://github.com/acme/app.git main",
    "git http-push https://example.com/app.git main",
    "git subtree push --prefix=lib origin lib",
    "git lfs push origin main",
    "gh repo sync",
    "gh repo sync acme/app --branch main --force",
    # in every segment shape the shell guard walks
    "git commit -qam x ; git push",
    "git add -A && git push",
    "false || git push",
    "(cd /tmp && git push)",
    "echo $(git push)",
    "echo `git push`",
    "if true; then git push; fi",
    "{ git push; }",
    "make\ngit push origin HEAD",
    # through wrappers, xargs, shells, eval
    "sudo git push",
    "env GIT_TRACE=1 git push",
    "timeout 30 git push",
    "GIT_SSH_COMMAND=ssh git push",
    "command git push",
    "echo origin | xargs git push",
    "echo origin | xargs -n1 git push",
    "bash -c 'git push origin HEAD'",
    'sh -c "git fetch && git push"',
    "bash -ec 'git push'",
    "eval git push",
    "/usr/bin/git push",
]


@pytest.mark.parametrize("command", PUSHES)
def test_every_push_is_denied(command):
    denied(command)


# -- every merge ---------------------------------------------------------------

MERGES = [
    "gh pr merge",
    "gh pr merge 7",
    "gh pr merge 7 --squash --delete-branch",
    "gh pr merge --auto --merge 7",
    "gh pr merge -R acme/app 7 --admin",
    "gh pr -R acme/app merge 7",
    "gh api repos/acme/app/merges -f base=main -f head=TASK-1-x",
    "gh api /repos/acme/app/merges --input body.json",
    "gh api -X POST repos/acme/app/merges",
    "gh api -X PUT repos/acme/app/pulls/7/merge",
    "gh api -XPUT repos/acme/app/pulls/7/merge",
    "gh api --method PUT /repos/acme/app/pulls/7/merge",
    "gh api --method=put repos/acme/app/pulls/7/merge -f merge_method=squash",
    "gh api repos/acme/app/pulls/7/merge -f merge_method=squash",
    'gh api -X PUT "repos/$REPO/pulls/$PR/merge"',
    "gh api -X POST repos/acme/app/merge-upstream -f branch=main",
    "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }'",
    "gh api graphql -F query='mutation { enablePullRequestAutoMerge(input: {}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { mergeBranch(input: {}) { clientMutationId } }'",
    "gh pr view 7 && gh pr merge 7",
]


@pytest.mark.parametrize("command", MERGES)
def test_every_merge_is_denied(command):
    denied(command)


# -- every push by API (PR #196 round 1) -------------------------------------------

API_PUSHES = [
    # REST ref writes: force-move, create, delete a branch
    "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc -F force=true",
    "gh api --method=patch /repos/o/r/git/refs/heads/main -f sha=abc",
    "gh api -X POST repos/o/r/git/refs -f ref=refs/heads/x -f sha=abc",
    "gh api repos/o/r/git/refs -f ref=refs/heads/x -f sha=abc",
    "gh api -X DELETE repos/o/r/git/refs/heads/main",
    "gh api -XDELETE https://api.github.com/repos/o/r/git/refs/tags/v1",
    'gh api -X PATCH "repos/$REPO/git/refs/heads/$BRANCH" -f sha="$SHA"',
    "gh api repos/{owner}/{repo}/git/refs -f ref=refs/heads/x -f sha=abc",
    # the incident's sequence: trees → commits → refs
    "gh api repos/o/r/git/trees -f 'tree[][path]=x'",
    "gh api -X POST repos/o/r/git/commits -f message=m -f tree=abc",
    "gh api repos/o/r/git/blobs -f content=x",
    "gh api repos/o/r/git/tags -f tag=v1 -f object=abc -f type=commit",
    "gh api repos/o/r/git/trees --input tree.json",
    # a file committed to (or removed from) a branch
    "gh api repos/o/r/contents/x -X PUT -f message=m -f content=eA==",
    "gh api -X PUT repos/o/r/contents/docs/a.md -f message=m -f content=eA== -f branch=main",
    "gh api -X DELETE repos/o/r/contents/x -f message=m -f sha=abc",
    # a merge commit onto the PR's branch
    "gh pr update-branch 7",
    "gh pr update-branch 7 --rebase",
    "gh api -X PUT repos/o/r/pulls/7/update-branch",
    # GraphQL ref and commit mutations
    "gh api graphql -f query='mutation { updateRef(input: {refId: \"x\", oid: \"y\", force: true}) { ref { name } } }'",
    "gh api graphql -f query='mutation { updateRefs(input: {repositoryId: \"x\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { createRef(input: {}) { ref { name } } }'",
    "gh api graphql -f query='mutation { deleteRef(input: {refId: \"x\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation($i: X!) { createCommitOnBranch(input: $i) { commit { oid } } }'",
    "gh api graphql -f query='mutation { updatePullRequestBranch(input: {}) { clientMutationId } }'",
    # GraphQL whose query text never reaches the guard
    "gh api graphql -F query=@q.graphql",
    "gh api graphql --field=query=@q.graphql",
    "gh api graphql -F query=@- < q.graphql",
    "gh api graphql --input body.json",
    'gh api graphql -f query="$Q"',
    'gh api graphql -f query="$(cat q.graphql)"',
]


@pytest.mark.parametrize("command", API_PUSHES)
def test_every_push_by_api_is_denied(command):
    denied(command)


def test_the_api_push_reason_names_the_path():
    reason = denied("gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc")
    assert "`gh api -X PATCH repos/o/r/git/refs/heads/main` writes git objects or refs" in reason, reason
    reason = denied("gh api graphql -F query=@q.graphql")
    assert "cannot read" in reason and "-f query='…'" in reason, reason


# -- hook bypasses ---------------------------------------------------------------

BYPASSES = [
    "git commit --no-verify -m x",
    "git commit --no-ver -m x",
    "git commit -n -m x",
    "git commit -anm x",
    "git commit -m x --no-verify",
    "git -c core.hooksPath=/dev/null commit -m x",
    "git -c core.hooksPath= fetch",
    "git -c CORE.HOOKSPATH=/tmp status",
    "git --config-env=core.hooksPath=HOOKS fetch",
    "git --config-env core.hooksPath=HOOKS fetch",
    "git -c alias.p=push p",
    "git -c 'alias.ship=!git push origin HEAD' ship",
    # the persistent alias (PR #196 round 1)
    "git config --global alias.p 'push --no-verify'",
    "git config alias.p push",
    "git config --local alias.ship '!git push origin HEAD'",
    "git config set --global alias.p push",
    "git config --add alias.sp send-pack",
    "git config --global ALIAS.P 'push -f'",
    "git config core.hooksPath /dev/null",
    "git config core.hooksPath",
    "git config --global core.hooksPath /tmp/h",
    "git config --unset core.hooksPath",
    "git config --system --unset-all core.hooksPath",
    "git config set core.hooksPath /tmp/h",
    "git config unset core.hooksPath",
    "git config CORE.hooksPath /tmp/h",
    "GIT_CONFIG_NOSYSTEM=1 ./scripts/release.sh",
    "GIT_CONFIG_SYSTEM=/dev/null make publish",
    "export GIT_CONFIG_NOSYSTEM=1",
    "env GIT_CONFIG_NOSYSTEM=1 npm run release",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp make",
    "export GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/tmp'\"",
]


@pytest.mark.parametrize("command", BYPASSES)
def test_every_hook_bypass_is_denied(command):
    denied(command)


def test_the_reason_names_the_offending_segment_and_the_shape():
    reason = denied("git fetch origin && git push origin HEAD:main")
    assert reason.endswith("Refused: `git push origin HEAD:main`."), reason
    assert "`git push` writes to a remote" in reason
    reason = denied("gh pr merge 7 --squash")
    assert "`gh pr merge` merges a pull request" in reason
    assert reason.endswith("Refused: `gh pr merge 7 --squash`."), reason
    reason = denied("git commit -n -m x")
    assert "skips the git hooks" in reason


# -- what a reviewer does all day ----------------------------------------------

PASSES = [
    # the issue's pass rows
    "git fetch",
    "git fetch origin pull/7/head",
    "git fetch --all --prune",
    "git worktree add ../REVIEW-x 0123abc",
    "git worktree add --detach ../REVIEW-some-repo-pr7-r1 origin/TASK-1-x",
    "git log",
    "git log --oneline origin/main..HEAD",
    "git log --all --graph",
    'gh pr review 7 --comment -b "[minor] x"',
    "gh pr review 7 --request-changes --body-file review.md",
    "gh pr review 7 --approve",
    "gh pr view",
    "gh pr view 7 --json mergeable,mergeStateStatus,reviewDecision",
    # and the rest of a review
    "git diff origin/main...HEAD",
    "git show HEAD --stat",
    "git status",
    "git blame src/x.py",
    "git rev-parse HEAD",
    "git worktree remove ../REVIEW-x",
    "git worktree list",
    "git checkout --detach origin/TASK-1-x",
    "git config --get core.hooksPath",
    "git config get core.hooksPath",
    "git config --list",
    "git config user.name",
    "git -c core.pager=cat log",
    "git -C /workspace/some-repo/main fetch origin",
    "git commit -m 'never --no-verify, never push'",
    "git commit -m -n",
    'git commit -F msg.txt',
    "git remote -v",
    "git ls-remote origin",
    "gh pr diff 7",
    "gh pr checks 7",
    "gh pr list --search 'is:open'",
    "gh pr comment 7 --body 'see review'",
    "gh issue view 195",
    "gh api repos/acme/app/pulls/7",
    "gh api repos/acme/app/pulls/7/merge",
    "gh api repos/acme/app/pulls/7/reviews -f event=COMMENT -f body=x",
    "gh api repos/acme/app/pulls/7/comments --paginate",
    "gh api -X POST repos/acme/app/issues/7/comments -f body='do not merge yet'",
    "gh api graphql -f query='query { repository(owner: \"a\", name: \"b\") { pullRequest(number: 7) { merged } } }'",
    # reads of what the API-push rows write (PR #196 round 1)
    "gh api -X POST repos/acme/app/pulls/7/reviews -f event=REQUEST_CHANGES -f body=x",
    "gh api repos/acme/app/pulls/7/reviews --input review.json",
    "gh api repos/acme/app/git/refs/heads/main",
    "gh api repos/acme/app/git/ref/heads/main --jq .object.sha",
    "gh api repos/acme/app/git/commits/abc",
    "gh api repos/acme/app/git/trees/abc?recursive=1",
    "gh api repos/acme/app/contents/README.md",
    "gh api -X GET repos/acme/app/contents/docs -f ref=TASK-1-x",
    "gh api graphql -f query='query($n: Int!) { node(id: $n) { id } }' -F n=7",
    "gh api graphql -F owner=a -f query='query($owner: String!) { repositoryOwner(login: $owner) { id } }'",
    "gh pr view 7 --json headRefName",
    "git config --get-regexp alias",
    "git config --global --get alias.p",
    "git config alias.p",
    "git config --global alias.st status",
    'echo "git push --force"',
    "grep -rn 'git push' docs/",
    "grep -rn 'gh pr merge' .",
    "cat CHANGELOG.md | grep push",
    "pytest -k push",
    "ls",
    "GIT_TRACE=1 git fetch",
    "export GIT_PAGER=cat",
    "bash -c 'git log -1'",
    "echo x | xargs git show",
    "python3 -c 'print(1)'",
]


@pytest.mark.parametrize("command", PASSES)
def test_review_work_passes(command):
    passed(command)


# -- fail-open and the hook contract ---------------------------------------------

def test_malformed_and_foreign_payloads_pass_silently():
    for raw in ("not json", "", "[]", '{"tool_name": "Bash"}', '{"tool_name": "Bash", "tool_input": {}}'):
        done = run_guard("", raw=raw)
        assert done.returncode == 0, raw
        assert done.stdout == "", raw
    done = run_guard("git push", tool="Write")
    assert (done.returncode, done.stdout) == (0, "")
    err = run_guard("", raw="not json").stderr
    assert err.startswith("guard-git: letting the command through"), err
    assert len(err.strip().splitlines()) == 1


def test_a_missing_shell_guard_fails_open(tmp_path):
    lone = tmp_path / "guard-git.py"
    lone.write_text(GIT_GUARD.read_text())
    done = run_guard("git push", script=lone)
    assert done.returncode == 0
    assert done.stdout == ""
    assert "guard-git: letting the command through" in done.stderr


def test_a_command_naming_no_git_is_never_parsed(monkeypatch):
    spec = importlib.util.spec_from_file_location("alissa_guard_git", GIT_GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def boom():
        raise AssertionError("parsed a command that names no git")

    monkeypatch.setattr(module, "_load_shell_guard", boom)
    payload = {"tool_name": "Bash", "cwd": CWD, "tool_input": {"command": "rm -f a && ls | sort"}}
    assert module.decide(payload) is None


def test_the_budget():
    import time
    start = time.monotonic()
    for _ in range(5):
        denied("git fetch && gh pr review 7 --approve && git push origin HEAD")
    assert (time.monotonic() - start) / 5 < 0.5
