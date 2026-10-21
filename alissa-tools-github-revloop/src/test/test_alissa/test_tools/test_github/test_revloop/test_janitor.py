"""The workspace janitor (issue #167): stale `main/.review-scratch-*` and
`REVIEW-*` folders under each hub are removed, unless a live session or an
open ledger round holds them; nothing else under a hub is ever touched;
dry-run removes nothing; a full volume warns once; fleet vitals carry
`diskUsedPct`. Issue #177: a `REVIEW-*` checkout made INSIDE `<hub>/main` is
reaped too, the grace shrinks under disk pressure, and a pressure episode
records one `janitor.volume_pressure` loop event."""

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
        folder(hub / "main" / "node_modules", age=stale),
        folder(hub / "main" / "review-lowercase", age=stale),
        folder(hub / "main" / "src" / "REVIEW-TASK-1", age=stale),  # nested under main/
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
    (hub / "main" / "REVIEW-TASK-5").symlink_to(outside)
    (hub / "main" / "REVIEW-TASK-6").write_text("a file, not a folder")

    result = run_sweep([hub])

    assert result.removed == []
    for path in keep + [outside]:
        assert path.is_dir(), path
    assert (hub / "main" / ".review-scratch-file").is_file()
    assert (hub / "REVIEW-TASK-3").is_file()
    assert (hub / "main" / ".review-scratch-pr9-r1").is_symlink()
    assert (hub / "main" / "REVIEW-TASK-5").is_symlink()
    assert (hub / "main" / "REVIEW-TASK-6").is_file()
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
def ws(tmp_path, monkeypatch):
    # The volume reads under the warn line unless a test says otherwise, so
    # the pressure grace (issue #177) never depends on the CI machine's disk.
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: 50.0)
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


# -- the hub layout: only {root}/{repo}/main is swept (issue #175) ------------


def _with_template(w, template):
    w.config = Config(workspace_root=w.config.workspace_root,
                      hub_template=template, state_path=w.config.state_db,
                      janitor_interval_minutes=10)


@pytest.mark.parametrize("template", [
    "{root}/{owner}-{repo}/main",
    "{root}/{owner}/{repo}/main",
])
def test_a_non_default_hub_template_sweeps_nothing_and_warns_once(
    ws, caplog, monkeypatch, template
):
    w, _, hub = ws
    _with_template(w, template)
    pair = stale_pair(hub, 7, "TASK-70")
    pruned = []
    monkeypatch.setattr(janitor_mod, "prune_worktrees",
                        lambda hub, runner=None: pruned.append(hub))

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        assert w._run_janitor() is None
        w._janitor_last -= 10 * 60
        assert w._run_janitor() is None

    assert all(p.is_dir() for p in pair)
    assert pruned == [], "no git worktree prune"
    warned = [r for r in caplog.records
              if r.levelno == logging.WARNING and "is not supported" in r.getMessage()]
    assert len(warned) == 1
    assert repr(template) in warned[0].getMessage()
    assert "supports only {root}/{repo}/main" in warned[0].getMessage()
    assert not [r for r in caplog.records if r.getMessage().startswith("janitor: removed")]


def test_the_default_hub_template_is_accepted_and_sweeps_as_before(ws, caplog):
    w, _, hub = ws
    _with_template(w, "{root}/{repo}/main")
    pair = stale_pair(hub, 7, "TASK-70")

    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        result = w._run_janitor()

    assert result is not None and len(result.removed) == 2
    assert not any(p.exists() for p in pair)
    assert not [r for r in caplog.records if "is not supported" in r.getMessage()]


def test_layout_supported_probes_hub_for(tmp_path):
    def check(template, root=tmp_path):
        config = Config(workspace_root=root, hub_template=template)
        return janitor_mod.layout_supported(config.hub_for, root)

    assert check("{root}/{repo}/main")
    assert check("{root}//{repo}/main/")
    assert not check("{root}/{owner}-{repo}/main")
    assert not check("{root}/{owner}/{repo}/main")
    assert not check("{root}/{repo}")
    assert not check("{root}/hubs/{repo}/main")
    assert not check("{root}/{repo}/{unknown}/main"), "an unformattable template"
    assert not check(str(tmp_path / "elsewhere" / "{repo}" / "main"))


def test_under_a_refused_template_the_disk_warning_still_fires(ws, caplog, monkeypatch):
    w, _, _ = ws
    _with_template(w, "{root}/{owner}-{repo}/main")
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: 91.0)

    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert w._run_janitor() is None

    disk = [r for r in caplog.records if "workspace volume" in r.getMessage()]
    assert len(disk) == 1 and "91.0% used (warn at 80%)" in disk[0].getMessage()


# -- the per-sweep bound (issue #175) ------------------------------------------


def thirty_stale(hub):
    return [folder(hub / f"REVIEW-TASK-{n:02d}", age=GRACE + 60) for n in range(30)]


def test_the_default_bound_removes_25_and_defers_5_to_the_next_sweep(tmp_path):
    hub = make_hub(tmp_path)
    folders = thirty_stale(hub)
    runner = Runner()

    first = run_sweep([hub], runner=runner)

    assert len(first.removed) == janitor_mod.MAX_REMOVALS == 25
    assert first.deferred == 5
    assert sum(p.exists() for p in folders) == 5
    assert len(runner.calls) == 1, "the scanned hub is still pruned"

    second = run_sweep([hub])

    assert len(second.removed) == 5 and second.deferred == 0
    assert not any(p.exists() for p in folders)


def test_the_time_budget_stops_the_sweep_after_one_removal(tmp_path):
    hub = make_hub(tmp_path)
    folders = thirty_stale(hub)
    runner = Runner()

    def clock():
        # The budget is spent the moment the first folder is gone.
        return 0.0 if all(p.exists() for p in folders) else janitor_mod.MAX_SWEEP_SECONDS

    result = sweep([hub], Guard(), grace_seconds=GRACE, now=NOW,
                   runner=runner, clock=clock)

    assert len(result.removed) == 1 and result.deferred == 29
    assert sum(p.exists() for p in folders) == 29
    assert len(runner.calls) == 1


def test_the_bound_spans_hubs_and_every_scanned_hub_is_pruned(tmp_path):
    hubs = [make_hub(tmp_path, "alpha"), make_hub(tmp_path, "beta")]
    for hub in hubs:
        for n in range(3):
            folder(hub / f"REVIEW-TASK-{n}", age=GRACE + 60)
    runner = Runner()

    result = sweep(hubs, Guard(), grace_seconds=GRACE, now=NOW,
                   runner=runner, max_removals=4)

    assert len(result.removed) == 4 and result.deferred == 2
    assert len(runner.calls) == 2


def test_held_and_fresh_folders_are_not_counted_as_deferred(tmp_path):
    hub = make_hub(tmp_path)
    folder(hub / "REVIEW-TASK-1", age=GRACE + 60)
    folder(hub / "REVIEW-TASK-2", age=GRACE + 60)
    folder(hub / "REVIEW-TASK-3", age=60)
    guard = Guard()
    guard.hold("acme/widgets", 9, "TASK-2")

    result = sweep([hub], guard, grace_seconds=GRACE, now=NOW,
                   runner=Runner(), max_removals=0)

    assert result.removed == [] and result.deferred == 1
    assert len(result.held) == 1 and result.fresh == 1


def test_a_dry_run_applies_the_same_bound(tmp_path, caplog):
    hub = make_hub(tmp_path)
    folders = thirty_stale(hub)
    runner = Runner()

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        result = run_sweep([hub], dry_run=True, runner=runner)

    assert len(result.would_remove) == 25 and result.deferred == 5
    assert result.removed == [] and all(p.is_dir() for p in folders)
    assert runner.calls == []
    assert len([r for r in caplog.records
                if r.getMessage().startswith("[dry-run] janitor would remove")]) == 25


def test_the_summary_lines_state_the_deferred_count(ws, caplog):
    w, _, hub = ws
    for n in range(30):
        path = hub / f"REVIEW-TASK-{n:02d}"
        path.mkdir()
        old = time.time() - GRACE * 2
        os.utime(path, (old, old))

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        live = w._run_janitor()

    assert live is not None and live.deferred == 5
    assert any(r.getMessage().startswith("janitor: removed 25 folder(s)")
               and "5 deferred to the next interval" in r.getMessage()
               for r in caplog.records)

    caplog.clear()
    w.config = Config(workspace_root=w.config.workspace_root,
                      state_path=w.config.state_db, dry_run=True)
    w._janitor_last = None
    for n in range(30, 60):
        path = hub / f"REVIEW-TASK-{n:02d}"
        path.mkdir()
        old = time.time() - GRACE * 2
        os.utime(path, (old, old))

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        dry = w._run_janitor()

    assert dry is not None and len(dry.would_remove) == 25 and dry.deferred == 10
    assert any(r.getMessage().startswith("[dry-run] janitor: would remove 25 folder(s)")
               and "10 deferred to the next interval" in r.getMessage()
               for r in caplog.records)


# -- REVIEW-* checkouts INSIDE <hub>/main (issue #177 c1) ---------------------


def test_a_review_checkout_inside_main_is_a_candidate_like_one_beside_it(tmp_path):
    hub = make_hub(tmp_path)
    stale = GRACE * 2
    inside = folder(hub / "main" / "REVIEW-TASK-71", age=stale)
    beside = folder(hub / "REVIEW-TASK-72", age=stale)
    fresh_inside = folder(hub / "main" / "REVIEW-TASK-73", age=60)
    held_inside = folder(hub / "main" / "REVIEW-TASK-74", age=stale)
    main_file = hub / "main" / "README.md"
    main_file.write_text("main's own file")
    guard = Guard()
    guard.hold("acme/widgets", 9, "TASK-74")

    result = run_sweep([hub], guard)

    assert not inside.exists() and not beside.exists()
    assert fresh_inside.is_dir() and held_inside.is_dir()
    assert main_file.is_file()
    assert {p for p, _ in result.removed} == {inside, beside}
    assert [p for p, _ in result.held] == [held_inside]
    kinds = {c.path.name: c.kind for c in janitor_mod.scan_hub(hub)}
    assert kinds["REVIEW-TASK-73"] == janitor_mod.KIND_REVIEW


def test_a_linked_worktree_checkout_goes_through_git_worktree_remove(tmp_path):
    """A `.git` FILE marks a linked worktree: git removes it (here the fake
    runner refuses to, so the folder falls back to rmtree); a plain folder
    and a scratch folder never ask git."""
    hub = make_hub(tmp_path)
    stale = GRACE * 2
    linked = folder(hub / "main" / "REVIEW-TASK-71", age=stale, files=(".git", "a.txt"))
    plain = folder(hub / "REVIEW-TASK-72", age=stale)
    scratch = folder(hub / "main" / ".review-scratch-pr7-r1", age=stale, files=(".git",))
    runner = Runner(fail=True)

    result = run_sweep([hub], runner=runner)

    assert not linked.exists() and not plain.exists() and not scratch.exists()
    assert len(result.removed) == 3 and result.failed == []
    removes = [c for c in runner.calls if "remove" in c]
    assert removes == [[
        "git", "-C", str(hub / ".source"), "worktree", "remove", "--force", str(linked),
    ]]


def test_real_git_a_checkout_made_inside_main_is_removed_and_unregistered(tmp_path):
    """Against real git, the incident's shape: a reviewer in `<hub>/main`
    runs `git worktree add REVIEW-TASK-71`, which lands INSIDE `main/`. The
    sweep removes it through `git worktree remove --force` (its record goes
    with it), and `main/` -- its files, its registration -- is untouched."""
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
    _git("worktree", "add", "-q", "--detach", "REVIEW-TASK-71", "main", cwd=hub / "main")
    checkout = hub / "main" / "REVIEW-TASK-71"
    (checkout / "node_modules").mkdir()
    (checkout / "node_modules" / "dep.js").write_text("module.exports = 1")
    old = time.time() - GRACE * 2
    for path in (checkout, checkout / ".git", checkout / "README.md", checkout / "node_modules"):
        os.utime(path, (old, old), follow_symlinks=False)
    calls = []

    def recording(argv, **kwargs):
        calls.append(list(argv))
        return janitor_mod.run(argv, **kwargs)

    result = sweep(
        janitor_mod.discover_hubs(root), Guard(),
        grace_seconds=GRACE, now=time.time(), runner=recording,
    )

    assert [p for p, _ in result.removed] == [checkout]
    assert not checkout.exists()
    assert any(c[3:6] == ["worktree", "remove", "--force"] for c in calls)
    listed = subprocess.run(
        ["git", "-C", str(source), "worktree", "list", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "REVIEW-TASK-71" not in listed
    assert f"worktree {hub / 'main'}" in listed
    assert (hub / "main" / "README.md").is_file()


def test_the_reviewer_rule_says_where_the_checkout_goes():
    """c4: the rule names the place beside `main/` -- the name trust seeds --
    and the janitor reaps that name in both places."""
    from alissa.tools.github.revloop.loop import _REVIEWER_RULE
    from alissa.tools.github.revloop.trust import review_checkout_name

    assert "BESIDE `main/`, never inside it" in _REVIEWER_RULE
    assert "git worktree add ../REVIEW-<review task ref>" in _REVIEWER_RULE
    assert review_checkout_name("TASK-71") == "REVIEW-TASK-71"


# -- the pressure grace (issue #177 c2) ----------------------------------------

HOUR = 3600
PRESSURE_GRACE = 2 * HOUR


def pressure_sweep(hubs, guard=None, *, under_pressure, dry_run=False, **kw):
    return sweep(
        hubs, guard or Guard(), grace_seconds=GRACE, now=NOW, runner=Runner(),
        dry_run=dry_run, pressure_grace_seconds=PRESSURE_GRACE,
        under_pressure=under_pressure, **kw,
    )


def test_without_pressure_the_day_grace_holds(tmp_path):
    hub = make_hub(tmp_path)
    young = folder(hub / "main" / "REVIEW-TASK-1", age=5 * HOUR)

    result = pressure_sweep([hub], under_pressure=lambda: False)

    assert young.is_dir() and result.removed == [] and result.fresh == 1
    assert result.pressure is False


def test_under_pressure_the_grace_drops_and_held_folders_stay(tmp_path):
    hub = make_hub(tmp_path)
    past = folder(hub / "main" / "REVIEW-TASK-1", age=3 * HOUR)
    scratch = folder(hub / "main" / ".review-scratch-pr8-r1", age=3 * HOUR)
    inside = folder(hub / "REVIEW-TASK-2", age=1 * HOUR)
    held = folder(hub / "main" / "REVIEW-TASK-3", age=10 * HOUR)
    held_scratch = folder(hub / "main" / ".review-scratch-pr9-r1", age=GRACE * 3)
    guard = Guard()
    guard.hold("acme/widgets", 9, "TASK-3")

    result = pressure_sweep([hub], guard, under_pressure=lambda: True)

    assert not past.exists() and not scratch.exists()
    assert inside.is_dir(), "younger than the pressure grace"
    assert held.is_dir() and held_scratch.is_dir(), "held is never removed"
    assert result.pressure is True and result.pressure_removed == 2
    assert result.fresh == 1 and len(result.held) == 2


def test_under_pressure_the_oldest_go_first_until_the_volume_recovers(tmp_path):
    hub = make_hub(tmp_path)
    other = make_hub(tmp_path, "gadgets")
    oldest = folder(other / "main" / "REVIEW-TASK-9", age=8 * HOUR)
    middle = folder(hub / "REVIEW-TASK-5", age=6 * HOUR)
    newest = folder(hub / "main" / "REVIEW-TASK-1", age=4 * HOUR)
    stale = folder(hub / "main" / ".review-scratch-pr7-r1", age=GRACE + 60)

    # Pressed until two folders are gone.
    def pressed():
        return sum(p.exists() for p in (oldest, middle, newest, stale)) > 2

    result = pressure_sweep([hub, other], under_pressure=pressed)

    assert not stale.exists(), "past the day grace: removed whatever the volume"
    assert not oldest.exists()
    assert middle.is_dir() and newest.is_dir()
    assert [p for p, _ in result.removed] == [stale, oldest]
    assert result.pressure_removed == 1 and result.fresh == 2
    assert result.left == 2


def test_a_dry_run_under_pressure_reports_every_eligible_folder(tmp_path):
    hub = make_hub(tmp_path)
    a = folder(hub / "main" / "REVIEW-TASK-1", age=3 * HOUR)
    b = folder(hub / "REVIEW-TASK-2", age=4 * HOUR)
    asked = []

    def pressed():
        asked.append(1)
        return True

    result = pressure_sweep([hub], under_pressure=pressed, dry_run=True)

    assert a.is_dir() and b.is_dir()
    assert [p for p, _ in result.would_remove] == [b, a]
    assert result.pressure_removed == 2 and len(asked) == 1


def test_the_pressure_grace_never_exceeds_the_normal_grace(tmp_path):
    hub = make_hub(tmp_path)
    young = folder(hub / "REVIEW-TASK-1", age=2 * HOUR)

    result = sweep([hub], Guard(), grace_seconds=HOUR, now=NOW, runner=Runner(),
                   pressure_grace_seconds=5 * HOUR, under_pressure=lambda: True)

    assert not young.exists() and result.pressure_removed == 0


def test_the_pressure_grace_config_default_env_and_floor(tmp_path, monkeypatch):
    from alissa.tools.github.revloop import config as config_mod

    assert Config(workspace_root=tmp_path).janitor_pressure_grace_minutes == 120
    assert config_mod.DEFAULT_JANITOR_PRESSURE_GRACE_MINUTES == 120
    assert config_mod.env_janitor(
        {"ALISSA_REVIEW_JANITOR_PRESSURE_GRACE_MINUTES": "45"}
    ) == {"janitor_pressure_grace_minutes": 45}
    assert config_mod.env_janitor(
        {"ALISSA_REVIEW_JANITOR_PRESSURE_GRACE_MINUTES": ""}
    ) == {}
    with pytest.raises(ValueError, match="ALISSA_REVIEW_JANITOR_PRESSURE_GRACE_MINUTES"):
        config_mod.env_janitor({"ALISSA_REVIEW_JANITOR_PRESSURE_GRACE_MINUTES": "soon"})
    with pytest.raises(ValueError, match="janitor_pressure_grace_minutes must be >= 1"):
        Config.build(tmp_path, {"janitor_pressure_grace_minutes": 0}, environ={})
    assert Config.build(
        tmp_path, {"janitor_pressure_grace_minutes": 30},
        environ={"ALISSA_REVIEW_JANITOR_PRESSURE_GRACE_MINUTES": "45"},
    ).janitor_pressure_grace_minutes == 45, "the env outranks the file"


# -- the watcher under pressure: grace, one event per episode (c2, c3) --------


def young_checkout(hub, name="REVIEW-TASK-71", hours=3):
    path = hub / "main" / name
    path.mkdir()
    (path / "pr.diff").write_text("x" * 4096)
    old = time.time() - hours * HOUR
    for each in (path / "pr.diff", path):
        os.utime(each, (old, old))
    return path


def volume_pings(w):
    return [r for r in w.state.read_pings()
            if str(r["kind"]).startswith("volume-pressure:")]


def test_the_watcher_shrinks_the_grace_while_the_volume_is_pressed(ws, monkeypatch, caplog):
    w, _, hub = ws
    checkout = young_checkout(hub)
    # Pressed while the checkout is on disk; its removal frees the volume.
    monkeypatch.setattr(janitor_mod, "disk_used_pct",
                        lambda path: 95.0 if checkout.exists() else 70.0)
    beside = hub / "REVIEW-TASK-72"
    beside.mkdir()

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        result = w._run_janitor()

    assert result is not None and result.pressure is True
    assert not checkout.exists()
    assert beside.is_dir(), "fresh even under pressure"
    assert any("the grace dropped to 120 minute(s)" in r.getMessage()
               for r in caplog.records)
    assert len(volume_pings(w)) == 1
    assert w._janitor_pressure_since is None, "back under the line: episode closed"


def test_one_volume_pressure_event_per_episode_and_recovery_closes_it(ws, monkeypatch):
    w, _, hub = ws
    pct = {"now": 97.5}
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: pct["now"])
    young_checkout(hub)
    t0 = int(time.time())

    w._run_janitor(now=t0)
    w._janitor_last -= 10 * 60
    w._run_janitor(now=t0 + 600)

    rows = volume_pings(w)
    assert len(rows) == 1, "one event per episode, not per sweep"
    assert rows[0]["repo"] == "" and rows[0]["number"] == 0
    started, used, warn, freed, left = rows[0]["kind"].split(":")[1:]
    assert (started, used, warn) == (str(t0), "97.5", "80")
    assert int(freed) > 0 and left == "0"

    pct["now"] = 60.0
    w._janitor_last -= 10 * 60
    w._run_janitor(now=t0 + 1200)
    assert w._janitor_pressure_since is None

    pct["now"] = 85.0
    w._janitor_last -= 10 * 60
    w._run_janitor(now=t0 + 1800)
    assert len(volume_pings(w)) == 2, "a new episode is a new event"


def test_the_event_is_recorded_even_when_the_sweep_cannot_run(ws, monkeypatch):
    w, alissa, _ = ws
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: 99.1)

    def boom():
        raise CommandError(["alissa", "tmux", "ls"], 1, "no server")

    alissa.list_review_sessions = boom

    assert w._run_janitor(now=NOW) is None

    rows = volume_pings(w)
    assert len(rows) == 1 and rows[0]["kind"].endswith(":0:")


def test_a_dry_run_records_no_volume_pressure_row(ws, monkeypatch, caplog):
    w, _, _ = ws
    w.config = Config(workspace_root=w.config.workspace_root,
                      state_path=w.config.state_db, dry_run=True)
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: 95.0)

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        w._run_janitor(now=NOW)

    assert volume_pings(w) == []
    assert any(r.getMessage().startswith("[dry-run] janitor: would record volume-pressure:")
               for r in caplog.records)


def test_a_zero_warn_line_never_presses_or_records(ws, monkeypatch):
    w, _, hub = ws
    w.config = Config(workspace_root=w.config.workspace_root,
                      state_path=w.config.state_db, janitor_warn_pct=0)
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: 99.0)
    checkout = young_checkout(hub)

    result = w._run_janitor(now=time.time())

    assert result is not None and result.pressure is False
    assert checkout.is_dir() and volume_pings(w) == []


def test_a_failed_ledger_write_leaves_the_episode_unopened(ws, monkeypatch, caplog):
    w, _, _ = ws
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: 95.0)
    real = w.state.record_ping

    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(w.state, "record_ping", broken)
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        w._run_janitor(now=time.time())
    assert w._janitor_pressure_since is None
    assert any("could not record the volume-pressure episode" in r.getMessage()
               for r in caplog.records)

    monkeypatch.setattr(w.state, "record_ping", real)
    w._janitor_last -= 10 * 60
    w._run_janitor(now=time.time())
    assert len(volume_pings(w)) == 1, "the next sweep records it"


# -- an unknown-ref round's hold is bounded by its spawn time (issue #185) ----

STALE = 90 * 60  # config.STALE_ROUND_SECONDS


def test_an_unknown_ref_round_holds_only_checkouts_made_after_its_spawn(tmp_path):
    hub = make_hub(tmp_path)
    spawned = NOW - 3 * DAY
    after = folder(hub / "REVIEW-TASK-81", age=2 * DAY)
    within_skew = folder(hub / "main" / "REVIEW-TASK-82",
                         age=3 * DAY + janitor_mod.HOLD_SKEW_SECONDS - 60)
    before = folder(hub / "REVIEW-TASK-83", age=4 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, None, since=spawned)

    result = run_sweep([hub], guard)

    assert after.is_dir() and within_skew.is_dir()
    assert not before.exists(), "older than the round: it cannot be the round's"
    assert {p for p, _ in result.held} == {after, within_skew}
    assert all(why == janitor_mod.HUB_HOLD_BOUNDED for _, why in result.held)


def test_the_earliest_unknown_ref_round_bounds_the_hub_and_an_unknown_spawn_holds_all(tmp_path):
    hub = make_hub(tmp_path)
    mid = folder(hub / "REVIEW-TASK-81", age=3 * DAY)
    old = folder(hub / "REVIEW-TASK-82", age=5 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, None, since=NOW - 2 * DAY)
    guard.hold("acme/widgets", 8, None, since=NOW - 4 * DAY)

    run_sweep([hub], guard)

    assert mid.is_dir() and not old.exists()

    guard.hold("acme/widgets", 9, None)  # no spawn time: today's hub-wide hold
    older = folder(hub / "REVIEW-TASK-83", age=6 * DAY)
    result = run_sweep([hub], guard)
    assert older.is_dir() and mid.is_dir()
    assert janitor_mod.HUB_HOLD_UNBOUNDED in {why for _, why in result.held}


def test_a_known_ref_hold_is_unchanged_by_the_spawn_bound(tmp_path):
    hub = make_hub(tmp_path)
    mine = folder(hub / "REVIEW-TASK-70", age=5 * DAY)
    other = folder(hub / "REVIEW-TASK-80", age=5 * DAY)
    scratch = folder(hub / "main" / ".review-scratch-pr7-r1", age=5 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70", since=NOW - DAY)

    result = run_sweep([hub], guard)

    assert mine.is_dir(), "a known ref holds its checkout whatever its age"
    assert scratch.is_dir()
    assert not other.exists(), "a known ref never holds the rest of the hub"
    assert guard.all_checkouts == {}
    assert len(result.held) == 2


def test_the_backstop_voids_a_hub_wide_hold_on_checkouts_older_than_the_stale_window(tmp_path):
    hub = make_hub(tmp_path)
    stale_checkout = folder(hub / "REVIEW-TASK-81", age=STALE + 60)
    young_one = folder(hub / "main" / "REVIEW-TASK-82", age=STALE - 60)
    known = folder(hub / "REVIEW-TASK-70", age=3 * DAY)
    scratch = folder(hub / "main" / ".review-scratch-pr7-r1", age=3 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    guard.hold("acme/widgets", 8, None)  # hub-wide, no spawn time

    without = sweep([hub], guard, grace_seconds=60, now=NOW, runner=Runner())
    assert stale_checkout.is_dir() and young_one.is_dir()
    assert without.backstop is False and without.backstop_released == 0

    result = sweep([hub], guard, grace_seconds=60, now=NOW, runner=Runner(),
                   stale_round_seconds=STALE)

    assert result.backstop is True and result.backstop_released == 1
    assert not stale_checkout.exists()
    assert young_one.is_dir(), "inside the stale window a live round may own it"
    assert known.is_dir() and scratch.is_dir(), "known-ref and PR holds are never voided"


def test_held_bytes_are_counted_per_hub_and_reason_and_cached(tmp_path, monkeypatch):
    hub = make_hub(tmp_path)
    folder(hub / "REVIEW-TASK-70", age=3 * DAY, files=("a", "b"))
    folder(hub / "REVIEW-TASK-81", age=2 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    guard.hold("acme/widgets", 8, None, since=NOW - 3 * DAY)
    sizes: dict = {}
    walks = []
    real = janitor_mod.held_bytes_of
    monkeypatch.setattr(janitor_mod, "held_bytes_of",
                        lambda p, stop, progress=None: walks.append(p) or real(p, stop, progress))

    first = sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(), sizes=sizes)

    assert set(first.held_bytes) == {
        "widgets: TASK-70 has a live session or open round",
        f"widgets: {janitor_mod.HUB_HOLD_BOUNDED}",
    }
    assert all(size > 0 for size in first.held_bytes.values())
    assert first.top_held(1)[0][0].startswith("widgets: TASK-70"), "two files outweigh one"
    assert len(walks) == 2 and len(sizes) == 2

    second = sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(), sizes=sizes)
    assert second.held_bytes == first.held_bytes
    assert len(walks) == 2, "an unchanged held folder is not walked again"
    assert second.held_cached == 2 and first.held_cached == 0
    assert "widgets: TASK-70" in janitor_mod.held_summary(second)
    assert "2 held size(s) cached, up to 60 min old" in janitor_mod.held_summary(second)
    assert "cached" not in janitor_mod.held_summary(first)


def test_held_folders_past_the_time_budget_go_unsized(tmp_path):
    hub = make_hub(tmp_path)
    folder(hub / "REVIEW-TASK-70", age=3 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")

    result = sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(),
                   max_seconds=0.0)

    assert len(result.held) == 1 and result.held_unsized == 1 and result.held_bytes == {}
    assert "1 held folder(s) not sized" in janitor_mod.held_summary(result)


def test_held_sizes_are_re_walked_once_the_cache_entry_ages_out(tmp_path):
    hub = make_hub(tmp_path)
    held = folder(hub / "REVIEW-TASK-70", age=3 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    sizes: dict = {}
    first = sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(), sizes=sizes)
    # An `npm ci` deep in the tree: the folder's newest mtime does not move.
    deep = held / "node_modules" / "pkg"
    deep.mkdir(parents=True)
    (deep / "index.js").write_text("x" * 65536)
    os.utime(held / "node_modules", (NOW - 3 * DAY, NOW - 3 * DAY))
    os.utime(held, (NOW - 3 * DAY, NOW - 3 * DAY))

    fresh = sweep([hub], guard, grace_seconds=GRACE, now=NOW + 60, runner=Runner(),
                  sizes=sizes)
    assert fresh.held_bytes == first.held_bytes and fresh.held_cached == 1

    aged = sweep([hub], guard, grace_seconds=GRACE,
                 now=NOW + janitor_mod.SIZE_CACHE_SECONDS, runner=Runner(), sizes=sizes)
    assert aged.held_cached == 0
    assert aged.held_total > first.held_total, "an aged entry is walked again"
    assert next(iter(sizes.values()))[2] == NOW + janitor_mod.SIZE_CACHE_SECONDS


def test_held_folders_are_sized_after_removals_so_sizing_never_defers_one(tmp_path, monkeypatch):
    hub = make_hub(tmp_path)
    folder(hub / "REVIEW-TASK-70", age=5 * DAY)  # held, and oldest: first in line
    stale = folder(hub / "REVIEW-TASK-81", age=3 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    now = [0.0]
    events = []

    def slow_walk(path, stop, progress=None):
        events.append(("size", path.name))
        now[0] += 1000.0  # one walk eats the whole budget
        return 4096

    real_remove = janitor_mod._remove

    def remove(candidate, runner):
        events.append(("remove", candidate.path.name))
        real_remove(candidate, runner)

    monkeypatch.setattr(janitor_mod, "held_bytes_of", slow_walk)
    monkeypatch.setattr(janitor_mod, "_remove", remove)

    result = sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(),
                   max_seconds=120.0, clock=lambda: now[0])

    assert not stale.exists() and result.deferred == 0
    assert events == [("remove", "REVIEW-TASK-81"), ("size", "REVIEW-TASK-70")]
    assert result.held_total == 4096 and result.held_unsized == 0


def test_held_sizing_gets_only_the_budget_the_removals_left(tmp_path):
    hub = make_hub(tmp_path)
    folder(hub / "REVIEW-TASK-70", age=3 * DAY)
    folder(hub / "REVIEW-TASK-81", age=4 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    ticks = iter([0.0, 0.0, 200.0, 200.0, 200.0])  # the removal runs the budget out

    result = sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(),
                   max_seconds=120.0, clock=lambda: next(ticks, 200.0))

    assert len(result.removed) == 1
    assert result.held_unsized == 1 and result.held_bytes == {}


def test_a_held_walk_stops_mid_tree_at_the_budget(tmp_path):
    root = tmp_path / "big"
    for i in range(5):
        (root / f"d{i}" / "deeper").mkdir(parents=True)
        (root / f"d{i}" / "deeper" / "f").write_text("x" * 4096)
    asked = []

    def stop():
        asked.append(1)
        return len(asked) > 3

    assert janitor_mod.held_bytes_of(root, stop) is None, "no partial figure"
    assert len(asked) == 4
    assert janitor_mod.held_bytes_of(root, lambda: False) == janitor_mod.dir_bytes(root) > 0


def tree(path: Path, dirs: int) -> Path:
    """A held checkout with `dirs` subdirectories, one file each, so a walk
    asks its stop question once per directory."""
    for i in range(dirs):
        (path / f"d{i}").mkdir(parents=True)
        (path / f"d{i}" / "f").write_text("x" * 4096)
    return path


class Ticks:
    """A clock that moves one second each time it is read, so the sweep's
    budget buys a fixed number of directory reads."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def test_an_abandoned_held_walk_no_longer_starves_the_folders_behind_it(tmp_path):
    # Issue #189, the review probe of PR #188: `big` (oldest, so first in
    # line) needs more than one sweep's budget, `small` queues behind it.
    hub = make_hub(tmp_path)
    big = folder(tree(hub / "REVIEW-TASK-70", 30), age=4 * DAY)
    small = folder(hub / "REVIEW-TASK-81", age=2 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    guard.hold("acme/widgets", 8, "TASK-81")
    big_key = "widgets: TASK-70 has a live session or open round"
    small_key = "widgets: TASK-81 has a live session or open round"
    sizes: dict = {}
    clock = Ticks()

    def one_sweep():
        return sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(),
                     max_seconds=10.0, clock=clock, sizes=sizes)

    first = one_sweep()
    assert first.held_bytes == {} and first.held_unsized == 2
    assert first.held_resuming == 1, "big's walk is kept, small never began"
    assert isinstance(sizes[big], janitor_mod.HeldWalk) and small not in sizes
    assert "(1 walk(s) resume next sweep)" in janitor_mod.held_summary(first)
    kept = sizes[big].total
    assert kept > 0

    second = one_sweep()
    assert second.held_bytes == {small_key: janitor_mod.dir_bytes(small)}, \
        "small is sized ahead of the walk that was cut short"
    assert second.held_unsized == 1 and second.held_resuming == 1
    assert sizes[big].total > kept, "big's walk carried on, it did not restart"

    sweeps = [first, second]
    while isinstance(sizes.get(big), janitor_mod.HeldWalk):
        sweeps.append(one_sweep())
        assert len(sweeps) < 10, "big's walk never finished"
    done = sweeps[-1]
    assert done.held_bytes[big_key] == janitor_mod.dir_bytes(big)
    assert done.held_unsized == 0 and done.held_resuming == 0
    assert sizes[big] == (NOW - 4 * DAY, janitor_mod.dir_bytes(big), NOW, NOW)
    assert all(sw.held_bytes.get(small_key) for sw in sweeps[1:]), \
        "small stays sized (from the cache) on every sweep after the first"


def test_two_held_walks_cut_short_take_turns_so_neither_starves(tmp_path):
    # Issue #191, the review probe of PR #190: `big` needs more than
    # SIZE_CACHE_SECONDS of sweeps, so its figure expires the sweep after it
    # lands and its walk starts over. `young` must still get its turns.
    hub = make_hub(tmp_path)
    big = folder(tree(hub / "REVIEW-TASK-70", 60), age=4 * DAY)
    young = folder(tree(hub / "REVIEW-TASK-81", 20), age=2 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    guard.hold("acme/widgets", 8, "TASK-81")
    big_key = "widgets: TASK-70 has a live session or open round"
    young_key = "widgets: TASK-81 has a live session or open round"
    sizes: dict = {}
    clock = Ticks()
    sweeps = []
    for i in range(25):
        sweeps.append(sweep([hub], guard, grace_seconds=GRACE, now=NOW + 600 * i,
                            runner=Runner(), max_seconds=10.0, clock=clock, sizes=sizes))

    assert any(sw.held_bytes.get(young_key) for sw in sweeps), "young was never sized"
    young_at = next(i for i, sw in enumerate(sweeps) if sw.held_bytes.get(young_key))
    assert young_at < 8, "young waits only its share of turns, not big's whole walk"
    assert sweeps[young_at].held_bytes[young_key] == janitor_mod.dir_bytes(young)
    assert any(sw.held_bytes.get(big_key) == janitor_mod.dir_bytes(big) for sw in sweeps), \
        "taking turns still lets big finish"


def test_kept_walks_go_least_recently_advanced_first(tmp_path, monkeypatch):
    hub = make_hub(tmp_path)
    old = folder(hub / "REVIEW-TASK-70", age=4 * DAY)
    new = folder(hub / "REVIEW-TASK-81", age=2 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    guard.hold("acme/widgets", 8, "TASK-81")
    sizes: dict = {
        old: janitor_mod.HeldWalk(NOW - 4 * DAY, NOW - 600, 0, [os.fspath(old)], advanced=NOW - 60),
        new: janitor_mod.HeldWalk(NOW - 2 * DAY, NOW - 600, 0, [os.fspath(new)], advanced=NOW - 600),
    }
    walked = []
    monkeypatch.setattr(janitor_mod, "held_bytes_of",
                        lambda p, stop, progress=None: walked.append(p.name))

    sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(), sizes=sizes)

    assert walked == ["REVIEW-TASK-81", "REVIEW-TASK-70"], "the walk that waited longest goes first"
    assert sizes[old].advanced == sizes[new].advanced == NOW


def test_a_finished_long_walk_restarts_behind_the_folders_never_cut_short(tmp_path):
    # Issue #197, the review probe of PR #192: every kept walk now finishes
    # in turn, and its figure expires an hour on. Its restart must not sort
    # to the front and take the leftover budget from the small folder `d`.
    hub = make_hub(tmp_path)
    folder(tree(hub / "REVIEW-TASK-70", 60), age=4 * DAY)
    folder(tree(hub / "REVIEW-TASK-71", 40), age=3 * DAY)
    folder(tree(hub / "REVIEW-TASK-72", 20), age=2 * DAY)
    d = folder(hub / "REVIEW-TASK-73", age=1 * DAY)
    guard = Guard()
    for number, ref in enumerate(("TASK-70", "TASK-71", "TASK-72", "TASK-73"), 1):
        guard.hold("acme/widgets", number, ref)
    d_key = "widgets: TASK-73 has a live session or open round"
    sizes: dict = {}
    clock = Ticks()
    sweeps = [sweep([hub], guard, grace_seconds=GRACE, now=NOW + 600 * i, runner=Runner(),
                    max_seconds=10.0, clock=clock, sizes=sizes) for i in range(40)]

    unsized = [i for i, sw in enumerate(sweeps) if not sw.held_bytes.get(d_key)]
    # The first three sweeps each walk one big tree before any is known to be
    # long; at PR #192's head d was also unsized on sweeps 9, 16, 29 and 36.
    assert unsized == [0, 1, 2]
    assert sweeps[-1].held_bytes[d_key] == janitor_mod.dir_bytes(d)
    for ref in ("TASK-70", "TASK-71", "TASK-72"):
        key = f"widgets: {ref} has a live session or open round"
        size = janitor_mod.dir_bytes(hub / f"REVIEW-{ref}")
        assert any(sw.held_bytes.get(key) == size for sw in sweeps), f"{ref}'s walk still finishes"


def test_an_expired_figure_from_a_walk_cut_short_keeps_a_kept_walks_place(tmp_path, monkeypatch):
    hub = make_hub(tmp_path)
    long_one = folder(hub / "REVIEW-TASK-70", age=4 * DAY)
    quick = folder(hub / "REVIEW-TASK-71", age=3 * DAY)
    young = folder(hub / "REVIEW-TASK-81", age=2 * DAY)
    guard = Guard()
    for number, ref in enumerate(("TASK-70", "TASK-71", "TASK-81"), 1):
        guard.hold("acme/widgets", number, ref)
    expired = NOW - janitor_mod.SIZE_CACHE_SECONDS - 1
    sizes: dict = {
        # began three sweeps before the one it finished in: cut short
        long_one: (NOW - 4 * DAY, 4096, expired - 1800, expired),
        # began and finished in one sweep: never cut short
        quick: (NOW - 3 * DAY, 4096, expired, expired),
    }
    walked = []
    monkeypatch.setattr(janitor_mod, "held_bytes_of",
                        lambda p, stop, progress=None: walked.append(p.name) or 4096)

    sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(), sizes=sizes)

    assert walked == ["REVIEW-TASK-71", "REVIEW-TASK-81", "REVIEW-TASK-70"], \
        "the long walk's restart goes behind the folders never cut short"
    assert sizes[long_one] == (NOW - 4 * DAY, 4096, NOW, NOW), "finished in one sweep this time"


def test_a_restart_of_a_walk_cut_short_is_kept_when_no_budget_is_left(tmp_path):
    hub = make_hub(tmp_path)
    big = folder(hub / "REVIEW-TASK-70", age=4 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    expired = NOW - janitor_mod.SIZE_CACHE_SECONDS - 1
    sizes: dict = {big: (NOW - 4 * DAY, 4096, expired - 1800, expired)}

    starved = sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(),
                    max_seconds=0.0, sizes=sizes)

    assert starved.held_unsized == 1 and starved.held_resuming == 1
    kept = sizes[big]
    assert isinstance(kept, janitor_mod.HeldWalk), "it stays at the back, not dropped to the front"
    assert kept.total == 0 and kept.stack == [os.fspath(big)] and kept.advanced == expired


def test_a_held_walk_cut_short_restarts_when_its_folder_mtime_moves(tmp_path):
    hub = make_hub(tmp_path)
    big = folder(tree(hub / "REVIEW-TASK-70", 30), age=4 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    sizes: dict = {}
    sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(),
          max_seconds=10.0, clock=Ticks(), sizes=sizes)
    assert isinstance(sizes[big], janitor_mod.HeldWalk)
    sizes[big].total += 10 ** 9  # a stale partial figure must not survive
    folder(big, age=3 * DAY, files=("new.diff",))

    later = sweep([hub], guard, grace_seconds=GRACE, now=NOW + 60, runner=Runner(),
                  sizes=sizes)

    assert later.held_total == janitor_mod.dir_bytes(big)
    assert sizes[big] == (NOW - 3 * DAY, janitor_mod.dir_bytes(big), NOW + 60, NOW + 60)


def test_a_held_walk_cut_short_is_kept_when_no_budget_is_left_to_resume_it(tmp_path):
    hub = make_hub(tmp_path)
    big = folder(tree(hub / "REVIEW-TASK-70", 30), age=4 * DAY)
    guard = Guard()
    guard.hold("acme/widgets", 7, "TASK-70")
    sizes: dict = {}
    sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(),
          max_seconds=10.0, clock=Ticks(), sizes=sizes)
    kept = sizes[big]

    starved = sweep([hub], guard, grace_seconds=GRACE, now=NOW, runner=Runner(),
                    max_seconds=0.0, sizes=sizes)

    assert starved.held_unsized == 1 and starved.held_resuming == 1
    assert sizes[big] is kept, "the walk keeps its place for a sweep with budget"


def test_a_held_walk_resumes_from_its_progress(tmp_path):
    root = tree(tmp_path / "big", 6)
    progress = janitor_mod.HeldWalk(0.0, NOW, 0, [os.fspath(root)])
    asked = []

    def stop():
        asked.append(1)
        return len(asked) > 3

    assert janitor_mod.held_bytes_of(root, stop, progress) is None
    assert progress.total > 0 and progress.stack, "the walk left its place"
    assert janitor_mod.held_bytes_of(root, lambda: False, progress) == janitor_mod.dir_bytes(root)


def test_the_watcher_bounds_a_ledger_round_without_a_task_ref_by_its_spawn(ws, caplog):
    w, alissa, hub = ws
    session = "review-widgets-pr7-r1-abc123"
    w.state.record_spawn(repo="acme/widgets", number=7, round_=1, head_sha="s",
                         session=session, task_ref=None)
    spawned = int(time.time()) - 3 * DAY
    w.state._db.execute("UPDATE spawns SET spawned_at = ?", (spawned,))
    w.state._db.commit()
    alissa.sessions = [ManagedSession(name=session, status="busy")]
    newer = young_checkout(hub, "REVIEW-TASK-81", hours=2 * 24)
    older = young_checkout(hub, "REVIEW-TASK-82", hours=4 * 24)

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        result = w._run_janitor()

    assert newer.is_dir(), "made after the round spawned: it may be the round's"
    assert not older.exists(), "older than the round's spawn: reclaimed"
    assert result is not None and result.held_total > 0
    summary = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("janitor: removed 1 folder(s)")]
    assert summary and f"widgets: {janitor_mod.HUB_HOLD_BOUNDED} = " in summary[0]


@pytest.mark.parametrize("pct, released", [(94.9, False), (95.0, True)])
def test_the_watcher_backstop_releases_a_rowless_sessions_hub_wide_hold(
    ws, caplog, monkeypatch, pct, released
):
    w, alissa, hub = ws
    monkeypatch.setattr(janitor_mod, "disk_used_pct", lambda path: pct)
    alissa.sessions = [ManagedSession(name="review-pr-7", status="busy")]
    scratch, review = stale_pair(hub, 7, "TASK-70")

    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        result = w._run_janitor()

    assert scratch.is_dir(), "the PR's own scratch stays held"
    assert review.exists() is not released
    assert result is not None and result.backstop is released
    warns = [r for r in caplog.records if "hub-wide holds are void" in r.getMessage()]
    assert len(warns) == (1 if released else 0)
    if released:
        assert warns[0].levelno == logging.WARNING
        assert "1 released" in warns[0].getMessage()
        assert "PR #7 has a live session or open round = " in warns[0].getMessage()
