"""The raw tmux surface the prompt responder presses through (issue #138;
devloop #127's `Alissa.capture_pane` / `pane_path` / `send_keys` on the
reviewer seat): the pinned argv, the allowlist enforced before any process
is spawned, dry-run sending nothing, and every failure degrading to "" /
False rather than raising -- plus the roster's real tmux name riding along
on `ManagedSession`. Also the reviewer enqueue's spawn stamps (issue #153):
`--task <origin> --repo <owner/name>` on a CLI that accepts both, probed once."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from alissa.tools.github.revloop import alissa as alissa_mod
from alissa.tools.github.revloop.alissa import (
    MANAGED_PREFIX,
    SAFE_SESSION,
    Alissa,
    ManagedSession,
    capture_pane_argv,
    check_session_name,
    pane_path_argv,
    queue_add_accepts_stamps,
    send_keys_argv,
    tmux_socket,
    tmux_target,
)
from alissa.tools.github.revloop.proc import CommandError

NAME = "review-widgets-pr7-r1-abcdef"
TARGET = "=ali-review-widgets-pr7-r1-abcdef:"


def test_capture_pane_runs_the_pinned_argv_and_strips_the_trailing_newline(monkeypatch):
    seen = []
    monkeypatch.setenv("TMUX_TMPDIR", "/home/alissa/.tmux")
    monkeypatch.setattr(alissa_mod, "run", lambda argv, **k: seen.append(list(argv)) or "line 1\nline 2\n\n")
    assert Alissa().capture_pane(NAME, 12) == "line 1\nline 2"
    assert seen == [[
        "tmux", "-S", seen[0][2], "capture-pane", "-p", "-t", TARGET, "-S", "-12",
    ]]
    assert seen[0][2].startswith("/home/alissa/.tmux/tmux-") and seen[0][2].endswith("/default")


def test_capture_pane_and_pane_path_degrade_to_empty(monkeypatch):
    def boom(argv, **k):
        raise CommandError(argv, 1, "can't find pane")
    monkeypatch.setattr(alissa_mod, "run", boom)
    a = Alissa()
    assert a.capture_pane(NAME) == ""
    assert a.pane_path(NAME) == ""
    # an invalid name never reaches tmux either
    seen = []
    monkeypatch.setattr(alissa_mod, "run", lambda argv, **k: seen.append(argv) or "x")
    assert a.capture_pane("-rf") == "" and a.pane_path("a;b") == ""
    assert seen == []


def test_pane_path_reads_the_current_path(monkeypatch):
    seen = []
    monkeypatch.setattr(alissa_mod, "run", lambda argv, **k: seen.append(list(argv)) or "/workspace/x\n")
    assert Alissa().pane_path(NAME) == "/workspace/x"
    assert seen[0][3:] == ["display-message", "-p", "-t", TARGET, "#{pane_current_path}"]


def test_send_keys_presses_only_allowlisted_keys_and_honours_dry_run(monkeypatch, caplog):
    seen = []
    monkeypatch.setattr(alissa_mod, "run", lambda argv, **k: seen.append(list(argv)) or "")
    a = Alissa()
    assert a.send_keys(NAME, "2", "Enter") is True
    assert seen[-1][3:] == ["send-keys", "-t", TARGET, "2", "Enter"]
    with caplog.at_level(logging.INFO):
        assert a.send_keys(NAME, "Escape", dry_run=True) is False
    assert len(seen) == 1 and "[dry-run] would send keys Escape" in caplog.text
    with caplog.at_level(logging.WARNING):
        assert a.send_keys(NAME, "y") is False
        assert a.send_keys(NAME) is False
        assert a.send_keys("bad name", "Enter") is False
    assert len(seen) == 1, "a refused key never spawns a process"
    assert "not in the allowlist" in caplog.text


def test_send_keys_reports_a_tmux_failure_as_not_sent(monkeypatch):
    def boom(argv, **k):
        raise CommandError(argv, 1, "no server running")
    monkeypatch.setattr(alissa_mod, "run", boom)
    assert Alissa().send_keys(NAME, "Enter") is False


def test_tmux_socket_defaults_to_tmp(monkeypatch):
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    assert tmux_socket().startswith("/tmp/tmux-")


def test_argv_builders_pin_the_exact_target_and_the_name_rule():
    assert tmux_target(NAME) == TARGET
    assert tmux_target("ali-x") == "=ali-x:", "an already-prefixed name is not doubled"
    assert tmux_target("review-pr-7") == "=ali-review-pr-7:"
    assert MANAGED_PREFIX == "ali-"
    for bad in ("", "-rf", "has space", "a;b", "x/../y", "x" * 201, None):
        with pytest.raises(ValueError):
            check_session_name(bad)
    assert SAFE_SESSION.match(NAME)
    assert send_keys_argv(NAME, ("Escape",))[-1] == "Escape"
    for bad in (("y",), ("rm -rf /",), ("Enter", "q"), (), ("enter",)):
        with pytest.raises(ValueError):
            send_keys_argv(NAME, bad)
    assert capture_pane_argv(NAME, 0)[-1] == "-1", "at least one line"
    assert pane_path_argv(NAME)[3] == "display-message"


def test_the_roster_carries_the_real_tmux_name_and_a_quiet_time(monkeypatch):
    rows = [
        {"name": NAME, "session": "ali-" + NAME, "status": "busy", "lastActivity": 1_000, "live": True},
        {"name": "review-pr-9", "status": "idle", "live": True},
        {"name": "develop-acme-widgets-i7-a1", "session": "ali-develop-acme-widgets-i7-a1", "status": "busy"},
    ]
    monkeypatch.setattr(alissa_mod, "run_json", lambda argv, **k: rows)
    monkeypatch.setattr(time, "time", lambda: 1_090.0)
    sessions = Alissa().list_review_sessions()
    assert [s.name for s in sessions] == [NAME, "review-pr-9"], "a foreign name is never listed"
    assert sessions[0].session == "ali-" + NAME and sessions[0].tmux_name == "ali-" + NAME
    assert sessions[0].quiet_for == 90.0
    assert sessions[1].session is None and sessions[1].tmux_name == "ali-review-pr-9"
    assert sessions[1].quiet_for is None, "no timestamp reads as unknown, not as 0"
    assert ManagedSession(name="review-pr-1", status="idle").tmux_name == "ali-review-pr-1"


# -- spawn stamps: `queue add --task <origin> --repo <owner/name>` (issue #153) --

# `alissa tmux queue add --help` before and after the Studio lane that adds the
# spawn stamps: 0.3.0 (verbatim below) already had `--task`, for shell
# write-back on a queue item, but no `--repo`.
HELP_WITHOUT_REPO = """Usage: alissa tmux queue add [options] <name> <text...>

Enqueue a command/message for a session (the session may not exist yet)

Options:
  -a, --agent <profile>  Agent the worker should use if it must create the
                         session
  -c, --cwd <dir>        Working directory the worker should use if it must
                         create the session
  --stop-on-fail         Pause this queue when a shell item exits nonzero
                         (sticky, like --agent)
  -t, --task <ref>       Link the item to an Alissa task (TASK-<n>, id, or URL)
                         — the worker writes shell results back to it
  --json                 Output raw JSON
  -h, --help             display help for command
"""
HELP_WITH_STAMPS = HELP_WITHOUT_REPO.replace(
    "  --json", "  --repo <owner/name>    Stamp the session's repository\n  --json"
)


def stamp_calls(monkeypatch, help_answer):
    """Record every `run`; the probe answers `help_answer` (or raises it)."""
    recorded = []

    def fake_run(argv, **kwargs):
        recorded.append(list(argv))
        if "--help" in argv:
            if isinstance(help_answer, Exception):
                raise help_answer
            return help_answer
        return ""

    monkeypatch.setattr(alissa_mod, "run", fake_run)
    return recorded


def probes(recorded):
    return [argv for argv in recorded if "--help" in argv]


def enqueues(recorded):
    return [argv for argv in recorded
            if argv[:4] == ["alissa", "tmux", "queue", "add"] and "--help" not in argv]


def stamped_enqueue(al, session="s", **overrides):
    kwargs = dict(
        session=session, directive="review it", cwd=Path("/hub/main"), agent="claude",
        task_ref="TASK-4242", repo="Acme/Widgets",
    )
    kwargs.update(overrides)
    al.enqueue_reviewer(**kwargs)


def test_both_stamps_go_on_a_cli_that_accepts_them(monkeypatch):
    recorded = stamp_calls(monkeypatch, HELP_WITH_STAMPS)
    stamped_enqueue(Alissa())

    assert probes(recorded) == [["alissa", "tmux", "queue", "add", "--help"]]
    (argv,) = enqueues(recorded)
    assert argv == [
        "alissa", "tmux", "queue", "add", "s", "--agent", "claude", "--cwd", "/hub/main",
        "--task", "TASK-4242", "--repo", "acme/widgets", "review it",
    ], "the repo is canonical lowercase and the directive stays last"
    assert ["alissa", "tmux", "queue", "set", "s", "respawn", "off"] in recorded


def test_neither_stamp_goes_on_a_cli_without_repo(monkeypatch):
    """Both or neither: 0.3.0's `--task` is the older write-back link, and the
    enqueue must not fail on a CLI that has no `--repo`."""
    recorded = stamp_calls(monkeypatch, HELP_WITHOUT_REPO)
    stamped_enqueue(Alissa())

    (argv,) = enqueues(recorded)
    assert "--task" not in argv and "--repo" not in argv
    assert argv[-1] == "review it"


def test_the_stamp_probe_runs_once_per_process(monkeypatch):
    for help_text, stamped in ((HELP_WITH_STAMPS, True), (HELP_WITHOUT_REPO, False)):
        recorded = stamp_calls(monkeypatch, help_text)
        al = Alissa()
        for session in ("a", "b", "c"):
            stamped_enqueue(al, session)

        assert len(probes(recorded)) == 1
        assert [("--task" in argv) for argv in enqueues(recorded)] == [stamped] * 3


def test_a_stamp_probe_that_cannot_run_enqueues_unstamped_and_probes_again(monkeypatch):
    """A probe failure is no verdict on the CLI: this spawn goes unstamped and
    the next one asks again."""
    recorded = stamp_calls(monkeypatch, CommandError(["alissa"], -1, "timed out"))
    al = Alissa()
    stamped_enqueue(al, "a")
    stamped_enqueue(al, "b")

    assert len(probes(recorded)) == 2
    assert len(enqueues(recorded)) == 2
    assert all("--task" not in argv and "--repo" not in argv for argv in enqueues(recorded))


def test_an_unstamped_enqueue_never_probes(monkeypatch):
    recorded = stamp_calls(monkeypatch, HELP_WITH_STAMPS)
    stamped_enqueue(Alissa(), task_ref=None, repo=None)

    assert probes(recorded) == []
    assert enqueues(recorded)[0][-1] == "review it"


def test_only_the_stamps_given_are_passed(monkeypatch):
    """No origin found (no review task title, no body reference): the repo
    still goes, `--task` does not."""
    recorded = stamp_calls(monkeypatch, HELP_WITH_STAMPS)
    stamped_enqueue(Alissa(), task_ref=None)

    (argv,) = enqueues(recorded)
    assert "--task" not in argv
    assert argv[-3:] == ["--repo", "acme/widgets", "review it"]


def test_a_stamped_dry_run_runs_nothing_not_even_the_probe(monkeypatch, caplog):
    recorded = stamp_calls(monkeypatch, HELP_WITH_STAMPS)
    with caplog.at_level(logging.INFO, logger="alissa.tools.github.revloop.alissa"):
        stamped_enqueue(Alissa(), dry_run=True)

    assert recorded == []
    assert any("--task TASK-4242 --repo acme/widgets <directive>" in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("help_text, expected", [
    (HELP_WITH_STAMPS, True),
    (HELP_WITHOUT_REPO, False),
    ("", False),
    # Named in another option's description is not an offer of the flag.
    (HELP_WITHOUT_REPO.replace("Output raw JSON", "Output raw JSON (see --repo)"), False),
    # Nor is a longer flag that merely starts with it.
    (HELP_WITHOUT_REPO.replace("  --json", "  --repository <x>  y\n  --json"), False),
])
def test_queue_add_accepts_stamps(help_text, expected):
    assert queue_add_accepts_stamps(help_text) is expected
