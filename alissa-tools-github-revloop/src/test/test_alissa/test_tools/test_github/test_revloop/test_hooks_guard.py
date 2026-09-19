"""Pinning tests for the container's Claude Code hooks (docker/claude/hooks,
issue #138 -- devloop #125's guard and marker, ported to the reviewer seat
with the SAME deny/pass table), driven the way claude drives them: JSON on
stdin, decision on stdout, exit code.

docker/claude/tests-hooks-guard.sh carries the issue's deny/pass table for
CI's shell job; this file runs the same scripts from pytest so the unit job
covers them too, and adds the finer shapes — cwd tracking across `cd`,
subshell scoping, redirections, wrapper commands, the protected hub paths —
that a bash table is awkward for.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[7]
HOOKS = REPO_ROOT / "docker" / "claude" / "hooks"
GUARD = HOOKS / "guard-shell.py"
NOTE = HOOKS / "note-waiting.py"
CLEAR = HOOKS / "clear-waiting.py"
CWD = "/workspace/some-repo/TASK-1-DESC"
REASON_HEAD = "Unattended session: this rm shape opens an interactive prompt nobody can answer."

pytestmark = pytest.mark.skipif(shutil.which("python3") is None, reason="the hooks are python3 scripts")


def guard(command, cwd=CWD, tool="Bash", env=None, raw=None):
    payload = raw if raw is not None else json.dumps({
        "session_id": "s1", "cwd": cwd, "hook_event_name": "PreToolUse",
        "tool_name": tool, "tool_input": {"command": command},
    })
    e = {"PATH": os.environ.get("PATH", ""), "ALISSA_WORKSPACE_ROOT": "/workspace"}
    e.update(env or {})
    return subprocess.run(["python3", str(GUARD)], input=payload, capture_output=True, text=True, env=e)


def denied(command, **kw):
    done = guard(command, **kw)
    assert done.returncode == 0, done
    assert done.stdout.strip(), f"expected a deny for {command!r}, got nothing"
    d = json.loads(done.stdout)["hookSpecificOutput"]
    assert d["hookEventName"] == "PreToolUse"
    assert d["permissionDecision"] == "deny"
    assert d["permissionDecisionReason"].startswith(REASON_HEAD)
    return d["permissionDecisionReason"]


def passed(command, **kw):
    done = guard(command, **kw)
    assert done.returncode == 0, done
    assert done.stdout == "", f"expected silence for {command!r}, got {done.stdout!r}"
    assert done.stderr == "", done.stderr


def test_the_scripts_are_executable():
    for f in (GUARD, NOTE, CLEAR):
        assert os.access(f, os.X_OK), f
        assert f.read_text().startswith("#!/usr/bin/env python3\n"), f


@pytest.mark.parametrize("command", [
    # the issue's table
    "rm -rf sales/screenshots/x/*",
    'rm -rf "$S"/shot',
    "cd a && rm -rf ./*",
    "(rm -rf $PWD/tmp)",
    "rm -rf ~/x",
    "rm -rf /",
    "rm -rf ..",
    "find . -name '*.png' -exec rm {} +",
    "ls | xargs rm",
    "sudo rm -rf build/*",
    # every other path the guard walks
    "rm -rf .",
    "rm -rf ./",
    "rm -rf /workspace",
    "rm -rf /workspace/some-repo",
    "rm -rf /workspace/some-repo/main",
    "rm -rf /workspace/some-repo/.source",
    "rm -rf /workspace/some-repo/.source/refs",
    "rm -rf /workspace/other-repo/TASK-9-Y/build",
    "rm -rf ../sibling",
    "rm -rf a/../../x",
    "cd .. && rm -rf TASK-1-DESC",
    "cd /tmp; rm -rf scratch",
    "cd $DIR && rm -rf out",
    "cd && rm -rf out",
    "cd - && rm -rf out",
    "pushd .. && rm -rf x",
    "echo $(rm -rf $x)",
    'echo `rm -f "$f"`',
    "rm -f `ls`",
    "rm -f $(ls)",
    "true || rm -rf ${DIR}/x",
    "rm -rf x; rm -rf y/?",
    "make && { rm -rf out/[ab]; }",
    "FOO=1 env -u X rm -rf x/*",
    "command rm -rf x/*",
    "nohup rm -rf x/* &",
    "timeout -k 2 5 rm -rf x/*",
    "sudo -u alissa rm -rf x/*",
    "/bin/rm -rf x/*",
    "rmdir ../sibling",
    "rm -rf /etc/passwd",
    "find build -exec rm -rf {} \\;",
    "find build -execdir rm {} +",
    "find . -type f | xargs -0 rm -f",
    "xargs -I{} rm -f {} < list.txt",
    'rm -rf "a b"/*',
    "rm -rf '*'",
    "rm -rf x 2>/dev/null; rm -rf y/*",
    "rm -rf x/*\n",
    "echo start\nrm -rf x/*\necho done",
    "rm -rf -- */",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf \"${TMPDIR:-/tmp}/x\"",
    # reserved words inside one segment (PR #126 review round 1)
    "for d in build dist; do rm -rf $d/*; done",
    "for f in *.png; do rm -rf $f; done",
    "if [ -d build ]; then rm -rf build/*; fi",
    "while read f; do rm -rf $f; done < list",
    "until [ -z \"$x\" ]; do rm -rf $x; done",
    "if x; then y; else rm -rf *; fi",
    "if x; then y; elif z; then rm -rf *; fi",
    "if rm -rf *; then echo gone; fi",
    "! rm -rf out/*",
    "case $x in a) rm -rf $x/*;; esac",
    "case $x in a) echo ok;; b|c) rm -rf $x/*;; esac",
    "case $f in *.png) rm -f $f;; esac",
    "case $x in (a) rm -rf $x/*;; esac",
    "do sudo rm -rf $d/*",
    "then FOO=1 rm -rf $d/*",
    "for d in a b; do cd $d; rm -rf out; done",
])
def test_denies(command):
    reason = denied(command)
    assert "find <literal-dir> -mindepth 1 -delete" in reason
    assert "Refused: `" in reason


@pytest.mark.parametrize("command", [
    # the issue's table
    "rm -f sales/screenshots/x/a.png",
    "rm -rf build/cache",
    "git rm -r --cached x",
    "npm rm lodash",
    'echo "rm -rf *"',
    'grep -r "rm -rf" .',
    "find build -mindepth 1 -delete",
    # beyond it
    "rm -rf x 2>/dev/null",
    "rm -f a.txt > /dev/null 2>&1 && echo ok",
    "rm -f a.txt 2>&1 | tee log",
    "rm -rf -- a b c",
    "rm -rf ./build",
    "rm -rf build/",
    "rm -rf a/../b",
    "cd sub && rm -rf out",
    "cd sub; cd deeper && rm -rf out",
    "(cd sub && rm -rf out)",
    "(cd .. ) && rm -rf out",
    "cd /workspace/some-repo/TASK-1-DESC/sub && rm -rf out",
    "timeout 5 rm -f a.txt",
    "sudo rm -f a.txt",
    "env FOO=bar rm -f a.txt",
    "rm -f 'a b.txt'",
    'rm -f "a b.txt"',
    "rm -f a\\ b.txt",
    "rm -rf /workspace/some-repo/TASK-1-DESC/build",
    "ls *.png | grep x; rm -f a.png",
    'find . -name "*.png" -delete',
    "find . -name '*.png' -print",
    "git clean -fdx -- build",
    'cat "$HOME/notes" | grep rm',
    "ls",
    "echo 'rm -rf ~'",
    "python -c 'import shutil; shutil.rmtree(\"x\")'",
    "rm -rf TASK-1-DESC/foo",
    "rm -v -f -- -weird-name",
    "rm",  # no operand: `rm: missing operand`, no prompt
    "",
    "   ",
    # reserved words with literal, in-worktree operands
    "if [ -d build ]; then rm -rf build; fi",
    "for f in a b; do echo $f; done; rm -rf build/cache",
    "while true; do sleep 1; done",
    "case $x in a) rm -rf build/cache;; esac",
    "! test -d build",
    "echo $(x)",
])
def test_passes(command):
    passed(command)


def test_one_line_loop_is_judged_like_its_multi_line_spelling():
    one_line = "for d in build dist; do rm -rf $d/*; done"
    multi_line = "for d in build dist; do\n  rm -rf $d/*\ndone"
    assert denied(one_line).endswith("Refused: `do rm -rf $d/*`.")
    assert denied(multi_line).endswith("Refused: `rm -rf $d/*`.")


REVIEW_CWD = "/workspace/some-repo/REVIEW-TASK-500"


@pytest.mark.parametrize("command", [
    "rm -rf build/cache",
    "rm -rf /workspace/some-repo/REVIEW-TASK-500/node_modules",
    "find build -mindepth 1 -delete",
    "git clean -fdx -- build",
])
def test_the_reviewers_own_checkout_is_an_ordinary_worktree(command):
    """The reviewer seat's cwd is the throwaway `REVIEW-<task>` checkout the
    review skill lets it make (issue #138): a literal in-checkout removal
    passes exactly as it does in a developer's `TASK-*` worktree."""
    passed(command, cwd=REVIEW_CWD)


@pytest.mark.parametrize("command", [
    "rm -rf ../main",
    "rm -rf ../.source",
    "rm -rf /workspace/some-repo/main",
    "rm -rf ../TASK-1-DESC",
    "rm -rf build/*",
    "rm -rf $TMPDIR",
])
def test_the_reviewer_cannot_reach_past_its_checkout(command):
    denied(command, cwd=REVIEW_CWD)


def test_cwd_of_the_hub_root_protects_source_and_main():
    hub = "/workspace/some-repo"
    denied("rm -rf .source", cwd=hub)
    denied("rm -rf main", cwd=hub)
    denied("rm -rf .source/objects", cwd=hub)
    passed("rm -rf TASK-2-OLD/build", cwd=hub)
    passed("rm -rf main/src", cwd=hub)  # a checkout is rebuilt from .source; the bare clone is not


def test_cwd_at_the_workspace_root_protects_every_hub():
    """The degenerate case (PR #126 review round 1): with the cwd AT the
    workspace root the hubs are INSIDE the boundary, so only the structural
    protection can refuse them — the boundary rule alone fails every deny."""
    root = "/workspace"
    denied("rm -rf some-repo", cwd=root)
    denied("rm -rf /workspace/some-repo", cwd=root)
    denied("rm -rf some-repo/.source", cwd=root)
    denied("rm -rf some-repo/.source/objects", cwd=root)
    denied("rm -rf /workspace/some-repo/main", cwd=root)
    denied("rm -rf ./some-repo/", cwd=root)
    denied("rm -rf some-file.txt", cwd=root)  # a direct child: the guard cannot tell it from a hub
    denied("rm -rf .", cwd=root)
    passed("rm -rf some-repo/TASK-9-Y/build", cwd=root)
    passed("rm -rf some-repo/main/src", cwd=root)
    passed("rm -rf some-repo/TASK-9-Y", cwd=root)


def test_cwd_outside_the_workspace_root_keeps_the_boundary_rule():
    denied("rm -rf /workspace/some-repo", cwd="/tmp/scratch")
    denied("rm -rf ../x", cwd="/tmp/scratch")
    passed("rm -rf build", cwd="/tmp/scratch")


def test_workspace_root_env_moves_the_protected_paths():
    env = {"ALISSA_WORKSPACE_ROOT": "/srv/ws"}
    cwd = "/srv/ws/repo/TASK-3-Z"
    denied("rm -rf /srv/ws", cwd=cwd, env=env)
    denied("rm -rf /srv/ws/repo", cwd=cwd, env=env)
    denied("rm -rf /srv/ws/repo/main", cwd=cwd, env=env)
    passed("rm -rf build", cwd=cwd, env=env)


def test_reason_names_the_refused_segment():
    reason = denied("echo hi && rm -rf out/* && echo done")
    assert reason.endswith("Refused: `rm -rf out/*`.")


def test_non_bash_tool_is_ignored():
    done = guard("rm -rf *", tool="Write")
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""


@pytest.mark.parametrize("raw", ["not json", "", "[]", "42", '{"tool_name": "Bash", "cwd": "/x"}',
                                 '{"tool_name": "Bash", "tool_input": {"command": 7}}'])
def test_fails_open(raw):
    done = guard("", raw=raw)
    assert done.returncode == 0
    assert done.stdout == ""
    assert done.stderr.count("\n") <= 1


def test_malformed_stdin_logs_one_line():
    done = guard("", raw="not json")
    assert done.stderr.startswith("guard-shell: letting the command through")
    assert done.stderr.count("\n") == 1


def test_missing_cwd_falls_back_to_the_process_cwd(tmp_path):
    raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}})
    done = subprocess.run(["python3", str(GUARD)], input=raw, capture_output=True, text=True,
                          cwd=str(tmp_path), env={"PATH": os.environ.get("PATH", "")})
    assert done.returncode == 0 and done.stdout == ""


# --- the waiting marker ---------------------------------------------------

def notify(env, session_id="sess-abc", kind="permission_prompt", script=NOTE, raw=None):
    payload = raw if raw is not None else json.dumps({
        "session_id": session_id, "cwd": CWD, "hook_event_name": "Notification",
        "message": "Claude needs your permission", "notification_type": kind,
    })
    e = {"PATH": os.environ.get("PATH", "")}
    e.update(env)
    return subprocess.run(["python3", str(script)], input=payload, capture_output=True, text=True, env=e)


def test_marker_is_written_then_cleared(tmp_path):
    wd = tmp_path / "nested" / ".waiting"  # missing: the hook creates it
    env = {"ALISSA_WAITING_DIR": str(wd)}
    done = notify(env)
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    marker = wd / "sess-abc.json"
    d = json.loads(marker.read_text())
    assert d == {
        "session": "sess-abc", "sessionId": "sess-abc", "cwd": CWD, "kind": "permission_prompt",
        "message": "Claude needs your permission", "at": d["at"],
    }
    assert d["at"].endswith("+00:00")
    assert sorted(p.name for p in wd.iterdir()) == ["sess-abc.json"]  # no temp file left
    notify(env, kind="idle_prompt")
    assert json.loads(marker.read_text())["kind"] == "idle_prompt"
    done = notify(env, script=CLEAR, raw=json.dumps({"session_id": "sess-abc", "cwd": CWD,
                                                     "hook_event_name": "UserPromptSubmit", "prompt": "go"}))
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    assert not marker.exists()
    done = notify(env, script=CLEAR, raw=json.dumps({"session_id": "sess-abc", "cwd": CWD,
                                                     "hook_event_name": "PostToolUse", "tool_name": "Bash"}))
    assert done.returncode == 0 and done.stderr == ""  # absent marker: not an error


def test_marker_is_named_after_the_tmux_session(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "tmux").write_text("#!/usr/bin/env bash\necho ali-review-widgets-pr7-r1-abcdef\n")
    (fake_bin / "tmux").chmod(0o755)
    wd = tmp_path / ".waiting"
    env = {"ALISSA_WAITING_DIR": str(wd), "TMUX": "/tmp/tmux-1000/default,1,0",
           "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
    notify(env)
    marker = wd / "ali-review-widgets-pr7-r1-abcdef.json"
    assert json.loads(marker.read_text())["session"] == "ali-review-widgets-pr7-r1-abcdef"
    notify(env, script=CLEAR, raw=json.dumps({"session_id": "sess-abc", "hook_event_name": "UserPromptSubmit"}))
    assert not marker.exists()


def test_marker_uses_alissa_tmux_session_without_asking_tmux(tmp_path):
    """The managed seat exports ALISSA_TMUX_SESSION; with it set the hooks
    spawn nothing — a tmux on PATH that would fail is never consulted."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "tmux").write_text("#!/usr/bin/env bash\nexit 1\n")
    (fake_bin / "tmux").chmod(0o755)
    wd = tmp_path / ".waiting"
    env = {"ALISSA_WAITING_DIR": str(wd), "TMUX": "/tmp/tmux-1000/default,1,0",
           "ALISSA_TMUX_SESSION": "ali-review-pr-7-r2",
           "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
    done = notify(env)
    assert done.returncode == 0 and done.stderr == ""
    marker = wd / "ali-review-pr-7-r2.json"
    assert json.loads(marker.read_text())["session"] == "ali-review-pr-7-r2"
    done = notify(env, script=CLEAR, raw=json.dumps({"session_id": "sess-abc", "hook_event_name": "PostToolUse"}))
    assert done.returncode == 0 and done.stderr == ""
    assert not marker.exists()
    # Blank is unset: the tmux fallback, then session_id when tmux fails.
    notify(dict(env, ALISSA_TMUX_SESSION="  "))
    assert (wd / "sess-abc.json").exists()


def test_marker_falls_back_to_session_id_when_tmux_fails(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "tmux").write_text("#!/usr/bin/env bash\nexit 1\n")
    (fake_bin / "tmux").chmod(0o755)
    wd = tmp_path / ".waiting"
    env = {"ALISSA_WAITING_DIR": str(wd), "TMUX": "/tmp/tmux-1000/default,1,0",
           "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
    notify(env)
    assert (wd / "sess-abc.json").exists()


def test_marker_default_dir_is_under_workspace():
    assert "/workspace/.waiting" in NOTE.read_text()
    assert "/workspace/.waiting" in CLEAR.read_text()


def test_marker_name_is_filesystem_safe(tmp_path):
    wd = tmp_path / ".waiting"
    notify({"ALISSA_WAITING_DIR": str(wd)}, session_id="../../etc/evil")
    names = sorted(p.name for p in wd.iterdir())
    assert names == ["_.._etc_evil.json"], names  # slashes replaced, leading dots stripped
    assert not (tmp_path / "etc").exists()


@pytest.mark.parametrize("script", [NOTE, CLEAR])
def test_marker_hooks_fail_open(tmp_path, script):
    done = notify({"ALISSA_WAITING_DIR": str(tmp_path / "w")}, script=script, raw="not json")
    assert done.returncode == 0 and done.stdout == ""
    assert done.stderr.count("\n") == 1
    done = notify({"ALISSA_WAITING_DIR": str(tmp_path / "w")}, script=script, raw="[]")
    assert done.returncode == 0 and done.stdout == ""
