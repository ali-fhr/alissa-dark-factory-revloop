"""The workspace janitor (issue #167): stale `main/.review-scratch-*` and
`REVIEW-*` folders under each hub are removed, unless a live session or an
open ledger round holds them; nothing else under a hub is ever touched;
dry-run removes nothing; a full volume warns once; fleet vitals carry
`diskUsedPct`."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from alissa.tools.github.revloop import fleet_vitals
from alissa.tools.github.revloop import janitor as janitor_mod
from alissa.tools.github.revloop.alissa import ManagedSession
from alissa.tools.github.revloop.alissa_client import AlissaError
from alissa.tools.github.revloop.config import Config
from alissa.tools.github.revloop.fleet_vitals import FleetVitalsPusher
from alissa.tools.github.revloop.janitor import Guard, sweep
from alissa.tools.github.revloop.loop import ReviewWatcher
from alissa.tools.github.revloop.proc import CommandError
from alissa.tools.github.revloop.state import State
from test_fleet_vitals import WALL, FakeSources, FakeVitalsClient
from test_loop import FakeAlissa, FakeGitHub, make_pr

NOW = 1_800_000_000.0
DAY = 24 * 3600
GRACE = DAY


def make_hub(root: Path, name: str = "widgets") -> Path:
    hub = root / name
    (hub / ".source").mkdir(parents=True)
    (hub / "main").mkdir()
    return hub


def folder(path: Path, *, age: float, files=("pr.diff",)) -> Path:
    """A folder (with a few direct children) whose newest mtime is `age`
    seconds before NOW."""
    path.mkdir(parents=True, exist_ok=True)
    for name in files:
        (path / name).write_text("x" * 4096)
    stamp = NOW - age
    for child in path.iterdir():
        os.utime(child, (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp))
    return path


class Runner:
    """A fake `proc.run`: records argv, optionally fails."""

    def __init__(self, fail=False):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.fail:
            raise CommandError(argv, 128, "fatal: not a git repository")
        return ""


def run_sweep(hubs, guard=None, *, dry_run=False, runner=None):
    return sweep(
        hubs, guard or Guard(), grace_seconds=GRACE, now=NOW,
        dry_run=dry_run, runner=runner or Runner(),
    )


# -- what is removed, what is kept --------------------------------------------


def test_stale_scratch_and_review_checkout_are_removed_fresh_ones_kept(tmp_path):
    hub = make_hub(tmp_path)
    old_scratch = folder(hub / "main" / ".review-scratch-pr7-r2", age=GRACE + 60)
    old_review = folder(hub / "REVIEW-TASK-70", age=GRACE + 60)
    new_scratch = folder(hub / "main" / ".review-scratch-pr8-r1", age=GRACE - 60)
    new_review = folder(hub / "REVIEW-TASK-80", age=60)

    result = run_sweep([hub])

    assert not old_scratch.exists() and not old_review.exists()
    assert new_scratch.is_dir() and new_review.is_dir()
    assert {p for p, _ in result.removed} == {old_scratch, old_review}
    assert result.freed > 0
    assert result.fresh == 2


def test_a_fresh_file_inside_an_old_folder_renews_it(tmp_path):
    hub = make_hub(tmp_path)
    scratch = folder(hub / "main" / ".review-scratch-pr7-r2", age=GRACE + 60)
    (scratch / "envelope.md").write_text("still writing")
    os.utime(scratch, (NOW - GRACE - 60, NOW - GRACE - 60))
    os.utime(scratch / "envelope.md", (NOW - 30, NOW - 30))

    run_sweep([hub])

    assert scratch.is_dir()


def test_nothing_outside_the_two_patterns_is_ever_touched(tmp_path):
    hub = make_hub(tmp_path)
    stale = GRACE * 10
    keep = [
        folder(hub / "main" / "src", age=stale),
        folder(hub / "main" / ".review-notes", age=stale),
        folder(hub / "main" / "REVIEW-TASK-1", age=stale),  # REVIEW- under main/
        folder(hub / "TASK-12-SOME-WORK", age=stale),
        folder(hub / "review-lowercase", age=stale),
        folder(hub / ".review-scratch-pr7-r1", age=stale),  # scratch beside main/
        folder(hub / ".source" / "worktrees" / "REVIEW-TASK-2", age=stale),
        folder(hub / "main" / "sub" / ".review-scratch-pr7-r1", age=stale),  # nested
    ]
    (hub / "main" / ".review-scratch-file").write_text("a file, not a folder")
    (hub / "REVIEW-TASK-3").write_text("a file, not a folder")
    outside = folder(tmp_path / "elsewhere", age=stale)
    (hub / "main" / ".review-scratch-pr9-r1").symlink_to(outside)
    (hub / "REVIEW-TASK-4").symlink_to(outside)

    result = run_sweep([hub])

    assert result.removed == []
    for path in keep + [outside]:
        assert path.is_dir(), path
    assert (hub / "main" / ".review-scratch-file").is_file()
    assert (hub / "REVIEW-TASK-3").is_file()
    assert (hub / "main" / ".review-scratch-pr9-r1").is_symlink()
    assert (outside / "pr.diff").is_file()


def test_discover_hubs_finds_only_directories_with_a_source(tmp_path):
    hub = make_hub(tmp_path, "widgets")
    other = make_hub(tmp_path, "studio.alissa.app")
    (tmp_path / ".revloop").mkdir()
    (tmp_path / "plain").mkdir()
    (tmp_path / "linked").symlink_to(hub)

    assert janitor_mod.discover_hubs(tmp_path) == [other, hub]


# -- the guard: live sessions and open rounds ----------------------------------


def test_a_held_pr_keeps_its_scratch_and_its_tasks_checkout(tmp_path):
    hub = make_hub(tmp_path)
    stale = GRACE * 2
    held_scratch = folder(hub / "main" / ".review-scratch-pr7-r2", age=stale)
    held_other_round = folder(hub / "main" / ".review-scratch-pr7-r1", age=stale)
    held_review = folder(hub / "REVIEW-TASK-70", age=stale)
    free_scratch = folder(hub / "main" / ".review-scratch-pr8-r1", age=stale)
    free_review = folder(hub / "REVIEW-TASK-80", age=stale)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")

    result = run_sweep([hub], guard)

    assert held_scratch.is_dir() and held_other_round.is_dir() and held_review.is_dir()
    assert not free_scratch.exists() and not free_review.exists()
    assert len(result.held) == 3


def test_a_round_without_a_task_ref_holds_every_checkout_in_its_hub_only(tmp_path):
    hub = make_hub(tmp_path, "widgets")
    other = make_hub(tmp_path, "gadgets")
    stale = GRACE * 2
    mine = folder(hub / "REVIEW-TASK-80", age=stale)
    theirs = folder(other / "REVIEW-TASK-80", age=stale)
    guard = Guard()
    guard.hold("acme/widgets", 7, None)

    run_sweep([hub, other], guard)

    assert mine.is_dir()
    assert not theirs.exists()


def test_a_nameless_session_holds_its_pr_in_every_hub(tmp_path):
    hub = make_hub(tmp_path, "widgets")
    other = make_hub(tmp_path, "gadgets")
    stale = GRACE * 2
    a = folder(hub / "main" / ".review-scratch-pr7-r1", age=stale)
    b = folder(other / "main" / ".review-scratch-pr7-r1", age=stale)
    c = folder(other / "main" / ".review-scratch-pr9-r1", age=stale)
    guard = Guard()
    guard.hold(None, 7)

    run_sweep([hub, other], guard)

    assert a.is_dir() and b.is_dir()
    assert not c.exists()


def test_hubs_match_a_dotted_repo_through_the_session_slug(tmp_path):
    hub = make_hub(tmp_path, "studio.alissa.app")
    scratch = folder(hub / "main" / ".review-scratch-pr2005-r3", age=GRACE * 2)
    guard = Guard()
    guard.hold("studio-alissa-app", 2005, "TASK-1")  # a session-name slug

    run_sweep([hub], guard)

    assert scratch.is_dir()


# -- dry run, prune ------------------------------------------------------------


def test_dry_run_logs_the_would_be_removals_and_deletes_nothing(tmp_path, caplog):
    hub = make_hub(tmp_path)
    scratch = folder(hub / "main" / ".review-scratch-pr7-r2", age=GRACE * 2)
    review = folder(hub / "REVIEW-TASK-70", age=GRACE * 2)
    runner = Runner()

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        result = run_sweep([hub], dry_run=True, runner=runner)

    assert scratch.is_dir() and review.is_dir()
    assert result.removed == []
    assert {p for p, _ in result.would_remove} == {scratch, review}
    assert runner.calls == [], "dry-run prunes nothing either"
    lines = [r.getMessage() for r in caplog.records if "[dry-run]" in r.getMessage()]
    assert any(str(scratch) in line for line in lines)
    assert any(str(review) in line for line in lines)


def test_each_hub_is_pruned_in_its_source(tmp_path):
    hubs = [make_hub(tmp_path, "widgets"), make_hub(tmp_path, "gadgets")]
    runner = Runner()

    result = run_sweep(hubs, runner=runner)

    assert runner.calls == [
        ["git", "-C", str(h / ".source"), "worktree", "prune"] for h in hubs
    ]
    assert result.pruned == 2


def test_a_failed_prune_is_a_warning_not_a_raise(tmp_path, caplog):
    hub = make_hub(tmp_path)
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        result = run_sweep([hub], runner=Runner(fail=True))
    assert result.pruned == 0
    assert any("worktree prune failed" in r.getMessage() for r in caplog.records)


def _git(*args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def test_real_git_the_removed_worktrees_records_are_pruned(tmp_path):
    """Against real git: a scratch folder's `wt/` worktree and a REVIEW-
    checkout, both off the hub's bare `.source`, are removed and their
    records pruned; `main/` stays registered."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README.md").write_text("hi")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-q", "-m", "seed", cwd=seed)
    root = tmp_path / "ws"
    hub = root / "widgets"
    hub.mkdir(parents=True)
    _git("clone", "-q", "--bare", str(seed), str(hub / ".source"), cwd=tmp_path)
    source = hub / ".source"
    _git("worktree", "add", "-q", str(hub / "main"), "main", cwd=source)
    scratch = hub / "main" / ".review-scratch-pr7-r2"
    scratch.mkdir()
    _git("worktree", "add", "-q", "--detach", str(scratch / "wt"), "main", cwd=source)
    _git("worktree", "add", "-q", "--detach", str(hub / "REVIEW-TASK-70"), "main", cwd=source)
    (hub / "REVIEW-TASK-70" / "untracked.txt").write_text("a reviewer's note")
    old = time.time() - GRACE * 2
    for path in (scratch, scratch / "wt", hub / "REVIEW-TASK-70",
                 hub / "REVIEW-TASK-70" / "untracked.txt", hub / "REVIEW-TASK-70" / ".git",
                 hub / "REVIEW-TASK-70" / "README.md"):
        os.utime(path, (old, old), follow_symlinks=False)

    result = sweep(
        janitor_mod.discover_hubs(root), Guard(),
        grace_seconds=GRACE, now=time.time(),
    )

    assert {p.name for p, _ in result.removed} == {scratch.name, "REVIEW-TASK-70"}
    listed = subprocess.run(
        ["git", "-C", str(source), "worktree", "list", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "REVIEW-TASK-70" not in listed and ".review-scratch" not in listed
    assert f"worktree {hub / 'main'}" in listed
    assert (hub / "main" / "README.md").is_file()


# -- the watcher: guard from the roster and the ledger, interval, disk ---------


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    hub = make_hub(root, "widgets")
    config = Config(
        workspace_root=root,
        hub_template="{root}/{repo}/main",
        state_path=tmp_path / "state.db",
        janitor_interval_minutes=10,
    )
    alissa = FakeAlissa()
    w = ReviewWatcher(
        config, github=FakeGitHub(make_pr(), []), alissa=alissa,
        state=State(config.state_db),
    )
    return w, alissa, hub


def stale_pair(hub, number, task_ref):
    now = time.time()
    scratch = hub / "main" / f".review-scratch-pr{number}-r1"
    review = hub / f"REVIEW-{task_ref}"
    for path in (scratch, review):
        path.mkdir()
        (path / "pr.diff").write_text("diff")
        old = now - GRACE * 2
        os.utime(path / "pr.diff", (old, old))
        os.utime(path, (old, old))
    return scratch, review


def test_a_live_session_holds_its_round_through_the_ledger(ws):
    w, alissa, hub = ws
    session = "review-widgets-pr7-r2-abc123"
    w.state.record_spawn(repo="acme/widgets", number=7, round_=2, head_sha="s",
                         session=session, task_ref="TASK-70")
    # An old spawn row (past the stale window): only the LIVE session holds it.
    w.state._db.execute("UPDATE spawns SET spawned_at = ?", (int(time.time()) - DAY,))
    w.state._db.commit()
    alissa.sessions = [ManagedSession(name=session, status="busy")]
    held = stale_pair(hub, 7, "TASK-70")
    free = stale_pair(hub, 8, "TASK-80")

    result = w._run_janitor()

    assert all(p.is_dir() for p in held)
    assert not any(p.exists() for p in free)
    assert result is not None and len(result.removed) == 2


def test_a_skill_session_without_a_ledger_row_holds_by_name(ws):
    w, alissa, hub = ws
    alissa.sessions = [ManagedSession(name="review-pr-7", status="busy")]
    scratch, review = stale_pair(hub, 7, "TASK-70")

    w._run_janitor()

    assert scratch.is_dir()
    assert review.is_dir(), "no task ref known: every checkout in the hub is held"


def test_an_open_ledger_round_holds_with_no_session_listed(ws):
    w, _, hub = ws
    w.state.record_spawn(repo="acme/widgets", number=7, round_=1, head_sha="s",
                         session="review-widgets-pr7-r1-aaaaaa", task_ref="TASK-70")
    held = stale_pair(hub, 7, "TASK-70")

    w._run_janitor()

    assert all(p.is_dir() for p in held)


def test_a_reaped_round_holds_nothing(ws):
    w, _, hub = ws
    session = "review-widgets-pr7-r1-aaaaaa"
    w.state.record_spawn(repo="acme/widgets", number=7, round_=1, head_sha="s",
                         session=session, task_ref="TASK-70")
    w.state.record_reap(session)
    pair = stale_pair(hub, 7, "TASK-70")

    w._run_janitor()

    assert not any(p.exists() for p in pair)


def test_an_owed_verdict_post_holds_its_round(ws):
    w, _, hub = ws
    session = "review-widgets-pr7-r1-aaaaaa"
    w.state.record_spawn(repo="acme/widgets", number=7, round_=1, head_sha="s",
                         session=session, task_ref="TASK-70")
    w.state.record_reap(session)
    w.state.note_verdict_post_owed("acme/widgets", 7, 1, "s")
    held = stale_pair(hub, 7, "TASK-70")

    w._run_janitor()

    assert all(p.is_dir() for p in held)


def test_an_unlistable_roster_skips_the_sweep(ws, caplog):
    w, alissa, hub = ws

    def boom():
        raise CommandError(["alissa", "tmux", "ls"], 1, "no server")

    alissa.list_review_sessions = boom
    pair = stale_pair(hub, 7, "TASK-70")

    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert w._run_janitor() is None

    assert all(p.is_dir() for p in pair)
    assert any("skipping this sweep" in r.getMessage() for r in caplog.records)


def test_the_sweep_runs_once_per_interval_and_logs_one_info_line(ws, caplog):
    w, _, hub = ws
    stale_pair(hub, 7, "TASK-70")

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        first = w._run_janitor()
        second = w._run_janitor()

    assert first is not None and second is None
    lines = [r for r in caplog.records if r.getMessage().startswith("janitor: removed")]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert "removed 2 folder(s)" in lines[0].getMessage()

    w._janitor_last -= 10 * 60
    assert w._run_janitor() is not None


def test_a_disabled_janitor_never_sweeps(ws):
    w, _, hub = ws
    w.config = Config(workspace_root=w.config.workspace_root,
                      state_path=w.config.state_db, janitor_enabled=False)
    pair = stale_pair(hub, 7, "TASK-70")

    assert w._run_janitor() is None
    assert all(p.is_dir() for p in pair)


def test_the_watcher_dry_run_removes_nothing_and_says_what_it_would(ws, caplog):
    w, _, hub = ws
    w.config = Config(workspace_root=w.config.workspace_root,
                      state_path=w.config.state_db, dry_run=True)
    pair = stale_pair(hub, 7, "TASK-70")

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        result = w._run_janitor()

    assert all(p.is_dir() for p in pair)
    assert result is not None and len(result.would_remove) == 2
    assert any(r.getMessage().startswith("[dry-run] janitor: would remove 2 folder(s)")
               for r in caplog.records)


@pytest.mark.parametrize("pct, warns", [(79.9, False), (80.0, True), (97.5, True)])
def test_disk_at_or_above_the_threshold_logs_one_warning(ws, caplog, monkeypatch, pct, warns):
    w, _, _ = ws
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: pct)

    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        w._run_janitor()

    disk = [r for r in caplog.records if "workspace volume" in r.getMessage()]
    assert len(disk) == (1 if warns else 0)
    if warns:
        assert disk[0].levelno == logging.WARNING
        assert f"{pct:.1f}% used (warn at 80%)" in disk[0].getMessage()


def test_a_zero_threshold_never_warns(ws, caplog, monkeypatch):
    w, _, _ = ws
    w.config = Config(workspace_root=w.config.workspace_root,
                      state_path=w.config.state_db, janitor_warn_pct=0)
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: 99.0)
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        w._run_janitor()
    assert not [r for r in caplog.records if "workspace volume" in r.getMessage()]


def test_disk_used_pct_reads_the_real_volume(tmp_path):
    pct = janitor_mod.disk_used_pct(tmp_path)
    assert pct is not None and 0.0 <= pct <= 100.0
    assert janitor_mod.disk_used_pct(tmp_path / "missing") is None


# -- fleet vitals --------------------------------------------------------------


def _vitals_config(tmp_path):
    return Config(workspace_root=tmp_path, state_path=tmp_path / "state.db",
                  fleet_vitals_enabled=True)


def test_fleet_vitals_carry_disk_used_pct(tmp_path, monkeypatch):
    seen = []

    def fake(path):
        seen.append(Path(path))
        return 42.5

    monkeypatch.setattr(fleet_vitals, "volume_used_pct", fake)
    client = FakeVitalsClient()
    pusher = FleetVitalsPusher(_vitals_config(tmp_path), FakeSources(), client,
                               clock=lambda: float(WALL))

    pusher.push_once(heartbeat_at=WALL)

    (sent,) = client.snapshots
    assert sent["diskUsedPct"] == 42.5
    assert seen == [tmp_path.resolve()]


def test_fleet_vitals_disk_used_pct_is_real_and_null_when_unreadable(tmp_path, monkeypatch):
    client = FakeVitalsClient()
    pusher = FleetVitalsPusher(_vitals_config(tmp_path), FakeSources(), client)
    pusher.push_once(heartbeat_at=WALL)
    assert isinstance(client.snapshots[0]["diskUsedPct"], float)

    monkeypatch.setattr(fleet_vitals, "volume_used_pct", lambda path: None)
    pusher.push_once(heartbeat_at=WALL)
    assert client.snapshots[1]["diskUsedPct"] is None


def test_a_studio_refusing_disk_used_pct_gets_the_snapshot_without_it(tmp_path):
    class Refusing(FakeVitalsClient):
        def post_fleet_vitals(self, snapshot):
            if "diskUsedPct" in snapshot:
                self.calls.append("refused")
                raise AlissaError(400, {"error": "unknown key: diskUsedPct"})
            return super().post_fleet_vitals(snapshot)

    client = Refusing()
    pusher = FleetVitalsPusher(_vitals_config(tmp_path), FakeSources(), client)

    assert pusher.push_once(heartbeat_at=WALL) == fleet_vitals.VITALS_PUSHED

    assert client.calls == ["refused", "vitals"]
    assert "diskUsedPct" not in client.snapshots[0]
