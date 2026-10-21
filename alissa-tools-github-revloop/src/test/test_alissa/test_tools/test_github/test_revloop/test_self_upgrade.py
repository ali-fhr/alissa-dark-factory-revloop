"""Fleet auto-upgrade, the revloop self-hosted path (issue #193, AU-6).

A port of devloop's suite (AU-4) and orcloop's (AU-5): the three seats
behave the same way. What is pinned:

  * parsing -- the vitals answer's `upgrade` block, its absence (an older
    Studio) and a malformed block;
  * drain -- a `drain: true` answer, or a self-hosted target to move to,
    stands every round down through the seat-drain gate (the QUEUED
    `drained` stage, on the early claim and the post-hub re-claim) while
    running reviews are left alone; one INFO line per change of state;
  * only a strictly newer target is wanted -- an older or equal pin never
    drains nor downgrades the seat;
  * exit at idle -- two consecutive polls with no live reviewer session and
    no undispatched queue item write the request file and stop
    `run_forever`;
  * an unserved request -- one this version wrote that no boot acted on,
    or that the boot refused as not newer -- is skipped and reported, never
    asked again;
  * the skip list -- honoured, and cleared by a newer target;
  * managed mode never self-upgrades, paused never upgrades;
  * confirm and report -- the first answered poll on the target confirms it,
    and an `upgradeFailed` report rides the next snapshot exactly once;
  * the entrypoint's boot step (`self_upgrade_boot`, lifted between its
    marker lines and run under bash against a stub `pip`) -- install,
    count boots, roll back (retrying a reinstall that failed), skip -- and
    its exit-75 tail.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from alissa.tools.github.revloop import self_upgrade as mod
from alissa.tools.github.revloop.alissa import ManagedSession
from alissa.tools.github.revloop.alissa_client import AlissaError
from alissa.tools.github.revloop.config import Config
from alissa.tools.github.revloop.fleet_vitals import VITALS_PUSHED, FleetVitalsPusher
from alissa.tools.github.revloop.loop import Action, ReviewWatcher, decision_owed
from alissa.tools.github.revloop.proc import CommandError
from alissa.tools.github.revloop.self_upgrade import (
    EXIT, HOLD_DRAIN, HOLD_UPGRADE, NONE, WAIT, SelfUpgrader, parse_upgrade,
)
from alissa.tools.github.revloop.state import State
from alissa.tools.github.revloop.version import version as running_version
from test_fleet_vitals import FakeSources, FakeVitalsClient
from test_loop import (  # the fakes and the PR scenario the loop tests share
    NUMBER,
    OWNER,
    REPO,
    config,  # noqa: F401 -- fixture, used by name
    make_pr,
    watcher,
)

RUNNING = "0.31.28"
PREVIOUS = "0.31.27"
TARGET = "0.31.40"
WALL = 1_000_000
REPO_ROOT = Path(__file__).resolve().parents[7]
ENTRYPOINT = REPO_ROOT / "docker" / "claude" / "entrypoint.sh"
DIST = "alissa-tools-github-revloop"
DIST_DIR = "alissa_tools_github_revloop"


def answer(mode="self_hosted", version=TARGET, *, auto=True, paused=False,
           drain=False):
    target = None if version is None else {"version": version, "digest": "sha256:ab"}
    return {"seat": "revloop", "receivedAt": 1, "replaced": True, "upgrade": {
        "mode": mode, "target": target, "autoUpgrade": auto, "paused": paused,
        "drain": drain,
    }}


def upgrader(tmp_path, running=RUNNING, **kw):
    return SelfUpgrader(running, directory=str(tmp_path), clock=lambda: 1_800_000_000.0, **kw)


def request_file(tmp_path):
    return tmp_path / mod.REQUEST_FILE


def read_file(tmp_path):
    return json.loads(request_file(tmp_path).read_text())


def write_file(tmp_path, data):
    request_file(tmp_path).write_text(json.dumps(data))


REVIEWER = "review-widgets-pr16-r2-ab12cd"
BUSY = [ManagedSession(name=REVIEWER, status="busy")]
# A name another daemon owns in a shared container: never this seat's.
FOREIGN = [ManagedSession(name="develop-acme-widgets-i7-a1", status="busy")]


class AnsweringClient(FakeVitalsClient):
    """A Studio whose vitals answer is `self.answer` (None: an older Studio,
    no `upgrade` block)."""

    def __init__(self, answer_=None, **kw):
        super().__init__(**kw)
        self.answer = answer_

    def post_fleet_vitals(self, snapshot):
        base = super().post_fleet_vitals(snapshot)
        return self.answer if self.answer is not None else base


def pusher_over(tmp_path, client, up=None):
    cfg = Config(workspace_root=tmp_path, state_path=tmp_path / "state.db",
                 fleet_vitals_enabled=True)
    return FleetVitalsPusher(cfg, FakeSources(), client, clock=lambda: float(WALL),
                             upgrader=up)


# -- parsing ------------------------------------------------------------------

def test_the_upgrade_block_is_parsed():
    parsed = parse_upgrade(answer())
    assert parsed.mode == "self_hosted"
    assert parsed.target == {"version": TARGET, "digest": "sha256:ab"}
    assert (parsed.auto_upgrade, parsed.paused, parsed.drain) == (True, False, False)


@pytest.mark.parametrize("payload", [
    {"seat": "revloop", "receivedAt": 1, "replaced": True},  # an older Studio
    {}, None, [], "upgrade",
])
def test_an_absent_upgrade_block_is_none(payload):
    assert parse_upgrade(payload) is None


@pytest.mark.parametrize("block", [
    {"mode": "hosted", "target": None, "autoUpgrade": True, "paused": False, "drain": False},
    {"mode": "self_hosted", "target": None, "autoUpgrade": True, "paused": "no", "drain": False},
    {"mode": "self_hosted", "target": None, "autoUpgrade": True, "drain": False},
    "self_hosted",
])
def test_a_malformed_block_is_never_read_as_a_partial_one(block):
    assert parse_upgrade({"upgrade": block}) is None


def test_a_null_target_is_no_target():
    parsed = parse_upgrade(answer(version=None))
    assert parsed.target is None and parsed.target_version is None


def test_the_pusher_keeps_the_last_block_across_answers_without_one(tmp_path):
    client = AnsweringClient(answer(paused=True))
    up = upgrader(tmp_path / "up")
    pusher = pusher_over(tmp_path, client, up)
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    assert pusher.upgrade.paused is True and up.answer.paused is True

    client.answer = None  # an older Studio: no block in the answer
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    assert pusher.upgrade.paused is True, "the last value is kept"
    assert up.answer.paused is True


def test_the_console_pusher_reads_the_answer_and_acts_on_nothing(tmp_path):
    pusher = pusher_over(tmp_path, AnsweringClient(answer()))
    assert pusher.upgrader is None
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    assert pusher.upgrade.target_version == TARGET


def test_the_daemon_wires_its_upgrader_into_its_pusher(tmp_path):
    cfg = Config(workspace_root=tmp_path, state_path=tmp_path / "state.db",
                 fleet_vitals_enabled=True)
    w = ReviewWatcher(cfg, github=Gh(), alissa=Al(), state=State(cfg.state_db))
    assert w._fleet_vitals.upgrader is w.self_upgrade
    assert w.self_upgrade.running == running_version.value


# -- drain --------------------------------------------------------------------

def test_drain_holds_in_either_mode(tmp_path):
    for mode in ("managed", "self_hosted"):
        up = upgrader(tmp_path / mode)
        up.observe(answer(mode, RUNNING, drain=True))
        assert up.hold() == HOLD_DRAIN


def test_a_self_hosted_target_holds_and_the_same_version_does_not(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer(version=TARGET))
    assert up.hold() == HOLD_UPGRADE
    up.observe(answer(version=RUNNING))
    assert up.hold() is None


@pytest.mark.parametrize("running, target", [
    (RUNNING, PREVIOUS),   # older: the pin lags a merge that already rolled the seat
    (RUNNING, "0.30.99"),
    (RUNNING, RUNNING),    # equal
    ("0.31.28", "0.31.028"),  # equal numerically, spelled differently
    ("0.10.0", "0.9.9"),   # numeric, not string, order
    ("dev", TARGET),       # running version unparseable: fail closed
    (RUNNING, "latest"),   # target unparseable
])
def test_a_target_at_or_below_the_running_version_is_never_wanted(tmp_path, running, target):
    # A seat rolled from a merge runs ahead of the platform pin until the
    # next GHCR poll; the older pin must neither drain nor downgrade it.
    up = upgrader(tmp_path, running=running)
    up.observe(answer(version=target))
    assert up.wanted() is None
    assert up.hold() is None
    assert up.after_pass([]) == NONE
    assert up.after_pass([]) == NONE
    assert not up.exit_requested and not request_file(tmp_path).exists()


def test_numeric_order_makes_a_two_digit_minor_newer(tmp_path):
    up = upgrader(tmp_path, running="0.9.9")
    up.observe(answer(version="0.10.0"))
    assert up.wanted() == "0.10.0" and up.hold() == HOLD_UPGRADE


def test_no_answer_never_holds(tmp_path):
    up = upgrader(tmp_path)
    up.observe({"seat": "revloop"})
    assert up.hold() is None
    assert up.after_pass([]) == NONE


def test_one_info_line_per_change_of_state_not_per_poll(tmp_path, caplog):
    up = upgrader(tmp_path)
    with caplog.at_level(logging.INFO, logger=mod.__name__):
        for _ in range(3):
            up.observe(answer(drain=True, version=RUNNING))
        for _ in range(3):
            up.observe(answer(version=RUNNING))
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2, lines
    assert "draining" in lines[0] and "drain lifted" in lines[1]


def test_drain_holds_the_round_through_the_seat_drain_gate(config):
    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), [], state=st)
    w.self_upgrade.observe(answer("managed", running_version.value, drain=True))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.QUEUED and d.drained
    assert decision_owed(d)
    assert "managed upgrade" in d.reason
    assert al.enqueued == []
    assert st.read_spawns() == [], "no spawn row: no round number is burned"
    assert st._db.execute("SELECT COUNT(*) FROM spawn_claims").fetchone()[0] == 0

    # Lifted, the very same round spawns.
    w.self_upgrade.observe(answer("managed", running_version.value))
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.SPAWNED and d.round == 1


def test_a_self_hosted_target_holds_the_round_too(config):
    w, _, al = watcher(config, make_pr(), [])
    w.self_upgrade.observe(answer(version="99.0.0"))
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.QUEUED and d.drained
    assert "self-upgrade" in d.reason
    assert al.enqueued == []


def test_a_hold_landing_while_the_hub_is_provisioned_stands_the_round_down(config):
    """The post-hub re-claim reads the hold too: a drain answer that lands
    while the hub is being added enqueues nothing."""
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    real = w._ensure_hub

    def slow(pr, task_ref=None):
        w.self_upgrade.observe(answer("managed", running_version.value, drain=True))
        return real(pr, task_ref=task_ref)

    w._ensure_hub = slow
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.QUEUED and d.drained
    assert "after its hub was provisioned" in d.reason
    assert al.enqueued == [] and st.read_spawns() == []
    assert st._db.execute("SELECT COUNT(*) FROM spawn_claims").fetchone()[0] == 0


def test_drain_blocks_new_spawns_but_not_running_reviews(config):
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    al.sessions = [ManagedSession(name=REVIEWER, status="busy",
                                  last_activity=time.time())]
    w.self_upgrade.observe(answer("managed", running_version.value, drain=True))

    w.poll_once()

    assert al.enqueued == []
    assert al.killed == [], "a running review finishes normally"
    [stage] = st.read_snapshots(1)[0]["stages"]
    assert stage["stage"] == "drained" and stage["owed"] is True
    assert not w.self_upgrade.exit_requested


# -- exit at idle ---------------------------------------------------------------

def test_two_idle_polls_write_the_request_and_exit(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer())
    assert up.after_pass(BUSY) == WAIT
    assert up.after_pass([]) == WAIT
    assert not request_file(tmp_path).exists()
    assert up.after_pass([]) == EXIT
    assert up.exit_requested
    data = read_file(tmp_path)
    assert data["target"] == {"version": TARGET, "digest": "sha256:ab"}
    assert data["previous"] == RUNNING
    assert data["requestedAt"] == "2027-01-15T08:00:00Z"
    assert data["state"] == "requested" and data["confirmed"] is False


def test_a_live_review_between_idle_polls_resets_the_count(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer())
    assert up.after_pass([]) == WAIT
    assert up.after_pass(BUSY) == WAIT
    assert up.after_pass([]) == WAIT
    assert up.after_pass([]) == EXIT


def test_only_a_reviewer_session_counts_as_busy(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer())
    for _ in range(3):
        assert up.after_pass([REVIEWER]) == WAIT
    assert not request_file(tmp_path).exists()
    assert up.after_pass(FOREIGN) == WAIT
    assert up.after_pass(FOREIGN) == EXIT


def test_a_failed_listing_is_never_idle(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer())
    for _ in range(3):
        assert up.after_pass(None) == WAIT
    assert not request_file(tmp_path).exists()


def test_an_undispatched_queue_item_is_not_idle(tmp_path, caplog):
    """An item queued before the hold came on is in no tmux listing yet:
    the seat is not idle until it dispatches. The queue is read only on an
    idle roster."""
    up = upgrader(tmp_path)
    up.observe(answer())
    reads = []

    def queued(n):
        def count():
            reads.append(n)
            return n
        return count

    assert up.after_pass(BUSY, queued(1)) == WAIT
    assert reads == [], "a busy roster never reads the queue"
    assert up.after_pass([], queued(0)) == WAIT
    with caplog.at_level(logging.INFO, logger=mod.__name__):
        assert up.after_pass([], queued(1)) == WAIT
    assert "1 undispatched item(s) in the alissa queue" in caplog.text
    assert up.after_pass([], queued(0)) == WAIT
    assert up.after_pass([], queued(0)) == EXIT


@pytest.mark.parametrize("queued", [lambda: None, lambda: 1 / 0])
def test_an_unreadable_queue_is_never_idle(tmp_path, queued):
    up = upgrader(tmp_path)
    up.observe(answer())
    for _ in range(3):
        assert up.after_pass([], queued) == WAIT
    assert not request_file(tmp_path).exists()


def test_dry_run_never_writes_or_exits(tmp_path):
    up = upgrader(tmp_path, dry_run=True)
    up.observe(answer())
    for _ in range(4):
        assert up.after_pass([]) == WAIT
    assert not request_file(tmp_path).exists() and not up.exit_requested


def test_an_unwritable_request_lifts_the_drain(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    up = SelfUpgrader(RUNNING, directory=str(blocker / "sub"))
    up.observe(answer())
    up.after_pass([])
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        assert up.after_pass([]) == NONE
    assert up.hold() is None and not up.exit_requested
    assert "NOT upgrading" in caplog.text


class Gh:
    """No review requests: the passes below only sweep and push."""

    login = "alissa-app"

    def review_requests(self, repos=()):
        return []


class Al:
    """The reap sweep's listing and the alissa tmux queue, canned."""

    def __init__(self):
        self.sessions: list = []
        self.queues: object = []
        self.killed: list = []

    def list_review_sessions(self):
        return list(self.sessions)

    def list_queues(self):
        return self.queues

    def kill_session(self, name):
        self.killed.append(name)

    def worker_running(self):
        return True


class AnsweringPusher:
    """The daemon's pusher, reduced to its answer: every pass's POST is
    answered with a self-hosted target."""

    def __init__(self, upgrader_, version="99.0.0"):
        self.upgrader = upgrader_
        self.version = version

    def push_once(self, *, heartbeat_at, queue_depth=None):
        self.upgrader.observe(answer(version=self.version))
        return VITALS_PUSHED


def daemon(tmp_path):
    cfg = Config(workspace_root=tmp_path, state_path=tmp_path / "state.db")
    al = Al()
    w = ReviewWatcher(cfg, github=Gh(), alissa=al, state=State(cfg.state_db))
    w._fleet_vitals = AnsweringPusher(w.self_upgrade)
    return w, al


def test_the_daemon_exits_after_two_idle_passes(tmp_path, monkeypatch, caplog):
    """End to end over `run_forever`: a pusher that answers with a
    self-hosted target and an empty roster -- the second pass writes the
    request and `run_forever` returns (the CLI then exits 0)."""
    w, _ = daemon(tmp_path)
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    with caplog.at_level(logging.INFO):
        w.run_forever()
    assert w.self_upgrade.exit_requested
    assert len(sleeps) == 1, "one sleep between the two passes, none after the exit"
    data = json.loads(Path(w.self_upgrade.path).read_text())
    assert data["target"]["version"] == "99.0.0"
    assert data["previous"] == running_version.value
    summaries = [r.getMessage() for r in caplog.records if "poll summary" in r.getMessage()]
    assert summaries[0].endswith("; upgrade: wait") and summaries[1].endswith("; upgrade: exit")


def test_a_live_review_or_a_queued_item_holds_the_daemon_exit(tmp_path):
    w, al = daemon(tmp_path)
    al.sessions = [ManagedSession(name=REVIEWER, status="busy", last_activity=time.time())]
    for _ in range(3):
        w.poll_once()
    assert not w.self_upgrade.exit_requested
    assert al.killed == []

    al.sessions = []
    al.queues = [{"name": "default", "items": [{"session": f"ali-{REVIEWER}"}]}]
    for _ in range(3):
        w.poll_once()
    assert not w.self_upgrade.exit_requested

    al.queues[0]["items"][0]["lastDispatchedAt"] = 1
    w.poll_once()
    w.poll_once()
    assert w.self_upgrade.exit_requested


def test_a_failed_session_listing_holds_the_daemon_exit(tmp_path):
    w, al = daemon(tmp_path)

    def broken():
        raise CommandError(["alissa", "tmux", "ls"], 1, "boom")

    al.list_review_sessions = broken
    for _ in range(3):
        w.poll_once()
    assert not w.self_upgrade.exit_requested


# -- the skip list --------------------------------------------------------------

def test_a_skipped_version_is_never_held_for_or_requested(tmp_path):
    write_file(tmp_path, {"state": "rolled_back", "skip": [TARGET]})
    up = upgrader(tmp_path)
    up.observe(answer())
    assert up.hold() is None
    for _ in range(3):
        assert up.after_pass([]) == NONE
    assert read_file(tmp_path)["state"] == "rolled_back"


def test_a_skip_written_mid_drain_stops_the_exit(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer())
    assert up.after_pass([]) == WAIT
    write_file(tmp_path, {"skip": [TARGET]})
    assert up.after_pass([]) == NONE
    assert up.hold() is None


def test_a_newer_target_clears_the_skip_list_an_older_one_does_not(tmp_path):
    write_file(tmp_path, {"skip": [TARGET], "state": "rolled_back"})
    up = upgrader(tmp_path)
    up.observe(answer(version="0.31.35"))
    assert read_file(tmp_path)["skip"] == [TARGET]
    up.observe(answer(version="0.31.41"))
    assert read_file(tmp_path)["skip"] == []
    assert up.hold() == HOLD_UPGRADE


def test_a_new_request_keeps_the_skip_list(tmp_path):
    write_file(tmp_path, {"skip": ["0.31.33"], "state": "rolled_back"})
    up = upgrader(tmp_path, running="0.31.20")
    up.observe(answer(version="0.31.30"))  # not newer than 0.31.33: the list stays
    up.after_pass([])
    assert up.after_pass([]) == EXIT
    assert read_file(tmp_path)["skip"] == ["0.31.33"]


# -- a request nothing served ------------------------------------------------

def test_a_request_the_boot_step_never_served_is_not_repeated(tmp_path, caplog):
    """Successive processes on the same version, the boot step never acting
    on the file. The first asks; the second finds its own unserved request,
    skips the target, reports it and stays up."""
    first = upgrader(tmp_path)
    first.observe(answer())
    first.after_pass([])
    assert first.after_pass([]) == EXIT
    for _ in range(2):
        nxt = upgrader(tmp_path)
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            nxt.observe(answer())
        assert nxt.hold() is None
        for _ in range(3):
            assert nxt.after_pass([]) == NONE
        assert not nxt.exit_requested
    assert caplog.text.count("was never served") == 1, "one WARNING, by the second process"
    data = read_file(tmp_path)
    assert data["state"] == "unserved" and data["skip"] == [TARGET]
    assert data["upgradeFailed"]["version"] == TARGET
    assert "boot step never acted" in data["upgradeFailed"]["reason"]
    assert nxt.report() == data["upgradeFailed"]


def test_an_install_the_daemon_does_not_run_is_unserved_too(tmp_path):
    write_file(tmp_path, {"target": {"version": TARGET}, "previous": RUNNING,
                          "state": "installed", "confirmed": False, "boots": 1})
    up = upgrader(tmp_path)
    up.observe(answer())
    assert up.hold() is None
    data = read_file(tmp_path)
    assert data["state"] == "unserved" and data["skip"] == [TARGET]
    assert f"still runs {RUNNING}" in data["upgradeFailed"]["reason"]


def test_a_request_the_boot_refused_as_not_newer_is_unserved_too(tmp_path):
    write_file(tmp_path, {"target": {"version": TARGET}, "previous": RUNNING,
                          "state": "not_newer", "confirmed": False, "boots": 0})
    up = upgrader(tmp_path)
    up.observe(answer())
    assert up.hold() is None
    assert up.after_pass([]) == NONE and up.after_pass([]) == NONE
    data = read_file(tmp_path)
    assert data["state"] == "unserved" and data["skip"] == [TARGET]
    assert "not newer than the installed version" in data["upgradeFailed"]["reason"]


@pytest.mark.parametrize("data", [
    {"previous": "0.31.10", "state": "requested"},    # another version asked
    {"previous": "0.31.10", "state": "not_newer"},    # another version's stale ask
    {"previous": RUNNING, "state": "rolled_back"},    # already a failure
    {"previous": RUNNING, "state": "requested", "confirmed": True},
])
def test_only_this_versions_pending_request_is_unserved(tmp_path, data):
    write_file(tmp_path, {"target": {"version": TARGET}, "skip": [], **data})
    up = upgrader(tmp_path)
    up.observe(answer())
    assert read_file(tmp_path)["state"] == data["state"]


def test_an_unserved_request_that_cannot_be_rewritten_still_lifts_the_drain(tmp_path, monkeypatch):
    write_file(tmp_path, {"target": {"version": TARGET}, "previous": RUNNING,
                          "state": "requested", "confirmed": False})

    def refuse(path, data):
        raise OSError("read-only file system")

    monkeypatch.setattr(mod, "write_request", refuse)
    up = upgrader(tmp_path)
    up.observe(answer())
    assert up.hold() is None
    assert up.after_pass([]) == NONE and up.after_pass([]) == NONE


# -- managed and paused -----------------------------------------------------------

def test_managed_mode_never_self_upgrades(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer("managed", TARGET))
    assert up.hold() is None, "a managed seat drains only when told to"
    for _ in range(4):
        assert up.after_pass([]) == NONE
    up.observe(answer("managed", TARGET, drain=True))
    assert up.hold() == HOLD_DRAIN
    for _ in range(4):
        assert up.after_pass([]) == NONE
    assert not request_file(tmp_path).exists() and not up.exit_requested


def test_paused_never_upgrades(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer(paused=True))
    assert up.hold() is None
    for _ in range(4):
        assert up.after_pass([]) == NONE
    assert not request_file(tmp_path).exists()


def test_a_pause_landing_mid_drain_lifts_it_before_the_exit(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer())
    assert up.after_pass([]) == WAIT
    up.observe(answer(paused=True))
    assert up.hold() is None
    assert up.after_pass([]) == NONE
    assert not request_file(tmp_path).exists()


def test_auto_upgrade_off_never_upgrades(tmp_path):
    up = upgrader(tmp_path)
    up.observe(answer(auto=False))
    assert up.hold() is None
    assert up.after_pass([]) == NONE and up.after_pass([]) == NONE


# -- confirm and report -------------------------------------------------------------

def test_the_first_answer_on_the_target_confirms_it(tmp_path):
    write_file(tmp_path, {"target": {"version": RUNNING}, "previous": PREVIOUS,
                          "state": "installed", "confirmed": False, "boots": 1})
    up = upgrader(tmp_path)
    up.observe(answer(version=RUNNING))
    data = read_file(tmp_path)
    assert data["confirmed"] is True and data["state"] == "confirmed"
    assert data["boots"] == 1, "the entrypoint's keys are kept"


def test_a_skipped_target_in_a_pending_state_is_never_confirmed(tmp_path):
    write_file(tmp_path, {"target": {"version": RUNNING}, "previous": PREVIOUS,
                          "state": "installed", "confirmed": False, "skip": [RUNNING]})
    upgrader(tmp_path).observe(answer(version=RUNNING))
    assert read_file(tmp_path)["confirmed"] is False


def test_an_answer_on_another_version_does_not_confirm(tmp_path):
    write_file(tmp_path, {"target": {"version": TARGET}, "state": "installed",
                          "confirmed": False})
    upgrader(tmp_path).observe(answer(version=TARGET))
    assert read_file(tmp_path)["confirmed"] is False


@pytest.mark.parametrize("skip", [[RUNNING], []], ids=["skipped", "state-only"])
@pytest.mark.parametrize("state", ["rolled_back", "rollback_failed", "install_failed"])
def test_a_failed_target_the_daemon_still_runs_is_never_confirmed(tmp_path, state, skip):
    """A rollback that could not reinstall `previous` leaves the daemon on
    the target: it must not mark that target confirmed."""
    write_file(tmp_path, {"target": {"version": RUNNING}, "previous": PREVIOUS,
                          "state": state, "confirmed": False, "skip": skip})
    upgrader(tmp_path).observe(answer(version=RUNNING))
    data = read_file(tmp_path)
    assert data["confirmed"] is False and data["state"] == state


def test_the_upgrade_failed_report_rides_the_next_snapshot_once(tmp_path):
    up_dir = tmp_path / "up"
    up_dir.mkdir()
    write_file(up_dir, {"state": "rolled_back", "skip": [TARGET],
                        "upgradeFailed": {"version": TARGET, "reason": "not\nconfirmed"},
                        "upgradeFailedReported": False})
    client = AnsweringClient()
    pusher = pusher_over(tmp_path, client, upgrader(up_dir))
    pusher.push_once(heartbeat_at=WALL)
    pusher.push_once(heartbeat_at=WALL)
    first, second = client.snapshots
    assert first["upgradeFailed"] == {"version": TARGET, "reason": "not confirmed"}
    assert "upgradeFailed" not in second
    assert read_file(up_dir)["upgradeFailedReported"] is True


def test_a_failed_push_leaves_the_report_owed(tmp_path):
    up_dir = tmp_path / "up"
    up_dir.mkdir()
    write_file(up_dir, {"upgradeFailed": {"version": TARGET, "reason": "x"}})
    client = AnsweringClient(fail_with=AlissaError(502, "bad gateway"))
    pusher_over(tmp_path, client, upgrader(up_dir)).push_once(heartbeat_at=WALL)
    assert not read_file(up_dir).get("upgradeFailedReported")


def test_an_old_studio_refusing_the_report_leaves_it_owed(tmp_path):
    up_dir = tmp_path / "up"
    up_dir.mkdir()
    write_file(up_dir, {"upgradeFailed": {"version": TARGET, "reason": "x"}})

    class OldStudio(AnsweringClient):
        def post_fleet_vitals(self, snapshot):
            if "upgradeFailed" in snapshot:
                raise AlissaError(400, {"error": "Unrecognized key(s) in object: 'upgradeFailed'"})
            return super().post_fleet_vitals(snapshot)

    client = OldStudio()
    pusher = pusher_over(tmp_path, client, upgrader(up_dir))
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    (body,) = client.snapshots
    assert "upgradeFailed" not in body
    assert not read_file(up_dir).get("upgradeFailedReported")


# -- the entrypoint's boot step --------------------------------------------------

bash_only = pytest.mark.skipif(shutil.which("bash") is None, reason="the entrypoint is bash")

STUB_PIP = """#!/usr/bin/env bash
echo "$*" >> "$STUB_PIP_LOG"
spec="${@: -1}"
version="${spec#*==}"
case " ${STUB_PIP_FAIL:-} " in
  *" ${version} "*) echo "ERROR: No matching distribution found for ${spec}" >&2; exit 1 ;;
esac
find "$FAKE_SITE" -mindepth 1 -maxdepth 1 -name 'alissa_tools_github_revloop-*' -exec rm -rf {} +
d="$FAKE_SITE/alissa_tools_github_revloop-${version}.dist-info"
mkdir -p "$d"
printf 'Metadata-Version: 2.1\\nName: alissa-tools-github-revloop\\nVersion: %s\\n' "$version" > "$d/METADATA"
"""


def lift(start, end):
    lines = ENTRYPOINT.read_text().splitlines()
    i = lines.index(start)
    j = lines.index(end)
    return "\n".join(lines[i:j + 1])


class Seat:
    """A scratch container: an upgrade dir, a fake site-packages holding the
    installed dist's metadata, and a stub `pip` that records its argv and
    swaps that metadata."""

    def __init__(self, root: Path, installed: str):
        self.dir = root / "upgrade"
        self.dir.mkdir()
        self.site = root / "site"
        self.site.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        pip = self.bin / "pip"
        pip.write_text(STUB_PIP)
        pip.chmod(0o755)
        self.log = root / "pip.log"
        self.fail = ""
        d = self.site / f"{DIST_DIR}-{installed}.dist-info"
        d.mkdir()
        (d / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {installed}\n")

    def write(self, data):
        (self.dir / "revloop.json").write_text(json.dumps(data))

    def read(self):
        return json.loads((self.dir / "revloop.json").read_text())

    def pip_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def env(self):
        return {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": str(self.site),
            "HOME": str(self.dir.parent),
            "ALISSA_UPGRADE_DIR": str(self.dir),
            "SELF_UPGRADE_RETRY_DELAY": "0",
            "STUB_PIP_LOG": str(self.log),
            "STUB_PIP_FAIL": self.fail,
            "FAKE_SITE": str(self.site),
        }

    def boot(self):
        script = "set -euo pipefail\nlog() { printf '[entrypoint] %s\\n' \"$*\" >&2; }\n"
        script += lift("# >>> self_upgrade_boot", "# <<< self_upgrade_boot")
        script += "\nself_upgrade_boot '' || exit 7\n"
        return subprocess.run(["bash", "-c", script], env=self.env(),
                              capture_output=True, text=True, timeout=60)


@pytest.fixture
def seat(tmp_path):
    return Seat(tmp_path, installed=PREVIOUS)


def request(version=TARGET, previous=PREVIOUS):
    return {"target": {"version": version, "digest": "sha256:ab"}, "previous": previous,
            "requestedAt": "2027-01-15T08:00:00Z", "state": "requested",
            "confirmed": False, "boots": 0, "skip": []}


def test_the_boot_step_runs_in_the_root_pass_for_the_daemon_role_before_the_firewall():
    text = ENTRYPOINT.read_text()
    root = text.index('if [ "$(id -u)" = "0" ]; then')
    call = text.index('    self_upgrade_boot "${RUNTIME_USER}"')
    firewall = text.index('raising egress firewall')
    drop = text.index('exec gosu "${RUNTIME_USER}"')
    assert root < call < firewall < drop
    assert 'if [ "${CONTAINER_ROLE}" = "daemon" ]; then\n    self_upgrade_boot' in text
    assert '|| log "WARN: the boot-time self-upgrade step failed' in text, (
        "a failed step never stops the boot"
    )


@bash_only
def test_boot_without_a_request_does_nothing(seat):
    proc = seat.boot()
    assert proc.returncode == 0, proc.stderr
    assert seat.pip_calls() == [] and proc.stderr == ""


@bash_only
def test_boot_installs_the_target_then_counts_boots_then_rolls_back(seat):
    seat.write(request())
    proc = seat.boot()
    assert proc.returncode == 0, proc.stderr
    (call,) = seat.pip_calls()
    assert call.endswith(f"{DIST}=={TARGET}")
    data = seat.read()
    assert data["state"] == "installed" and data["boots"] == 1
    assert isinstance(data["firstBootTs"], (int, float))

    # Boot 2, still unconfirmed: already installed, counted, nothing reinstalled.
    assert seat.boot().returncode == 0
    assert len(seat.pip_calls()) == 1
    assert seat.read()["boots"] == 2

    # Boot 3: unconfirmed on 2 boots -- roll back to previous, skip, report.
    proc = seat.boot()
    assert proc.returncode == 0, proc.stderr
    assert seat.pip_calls()[-1].endswith(f"{DIST}=={PREVIOUS}")
    data = seat.read()
    assert data["state"] == "rolled_back"
    assert data["skip"] == [TARGET]
    assert data["upgradeFailed"]["version"] == TARGET
    assert "not confirmed after 2 boots" in data["upgradeFailed"]["reason"]
    assert data["upgradeFailedReported"] is False

    # Rolled back is final: the next boot touches nothing.
    calls = len(seat.pip_calls())
    assert seat.boot().returncode == 0
    assert len(seat.pip_calls()) == calls


@bash_only
def test_boot_rolls_back_an_attempt_older_than_ten_minutes(seat):
    data = request()
    data.update(state="installed", boots=1, firstBootTs=time.time() - 700)
    seat.write(data)
    proc = seat.boot()
    assert proc.returncode == 0, proc.stderr
    data = seat.read()
    assert data["state"] == "rolled_back" and data["skip"] == [TARGET]
    assert "10 min" in data["upgradeFailed"]["reason"]


@bash_only
def test_a_rollback_whose_reinstall_fails_is_retried_next_boot(seat):
    seat.write(request())
    seat.boot()
    seat.boot()
    seat.fail = PREVIOUS
    proc = seat.boot()  # boot 3: rollback, PyPI refuses `previous`
    assert proc.returncode == 0, proc.stderr
    data = seat.read()
    assert data["state"] == "rollback_failed" and data["skip"] == [TARGET]
    assert f"could not reinstall {PREVIOUS}" in data["upgradeFailed"]["reason"]
    assert (seat.site / f"{DIST_DIR}-{TARGET}.dist-info").exists()

    seat.fail = ""
    calls = len(seat.pip_calls())
    assert seat.boot().returncode == 0  # boot 4: the retry succeeds
    assert seat.pip_calls()[-1].endswith(f"{DIST}=={PREVIOUS}")
    assert len(seat.pip_calls()) == calls + 1
    data = seat.read()
    assert data["state"] == "rolled_back" and data["skip"] == [TARGET]
    assert "not confirmed after 2 boots" in data["upgradeFailed"]["reason"]
    assert data["upgradeFailedReported"] is False

    calls = len(seat.pip_calls())
    assert seat.boot().returncode == 0
    assert len(seat.pip_calls()) == calls


@bash_only
def test_a_rollback_report_already_sent_is_not_owed_again_unchanged(tmp_path):
    seat = Seat(tmp_path, installed=TARGET)  # the failed rollback left it on the target
    data = request()
    data.update(state="rollback_failed", boots=2, skip=[TARGET],
                rollbackReason="not confirmed after 2 boots",
                upgradeFailed={"version": TARGET, "reason": "x"},
                upgradeFailedReported=True)
    seat.write(data)
    seat.fail = PREVIOUS
    seat.boot()
    first = seat.read()
    assert first["state"] == "rollback_failed"
    assert first["upgradeFailedReported"] is False, "the reason changed: owed again"
    first["upgradeFailedReported"] = True
    seat.write(first)
    seat.boot()
    assert seat.read()["upgradeFailedReported"] is True, "same report: not re-sent"


@bash_only
@pytest.mark.parametrize("target, previous", [
    ("0.31.26", "0.31.25"),  # older than the installed 0.31.27
    (PREVIOUS, "0.31.20"),   # equal to it: nothing to install anyway
    ("0.31.28", "0.31.29"),  # newer than installed, not newer than the asker
])
def test_boot_never_installs_a_target_that_is_not_newer(seat, target, previous):
    seat.write(request(version=target, previous=previous))
    proc = seat.boot()
    assert proc.returncode == 0, proc.stderr
    assert seat.pip_calls() == []
    data = seat.read()
    if target == PREVIOUS:
        # Already installed: an ordinary boot on the target, counted.
        assert data["state"] == "installed"
    else:
        assert data["state"] == "not_newer"
        assert "not newer" in proc.stderr
        assert data["skip"] == [] and "upgradeFailed" not in data
        assert seat.boot().returncode == 0 and seat.pip_calls() == []


@bash_only
def test_a_request_refused_as_not_newer_is_not_repeated(tmp_path):
    """The daemon imports RUNNING while the root pass's metadata reports a
    newer version, and Studio pins one in between. The first process asks
    and the boot refuses it as not newer; the next process skips and
    reports the target instead of asking again, for ever."""
    seat = Seat(tmp_path, installed="0.31.45")
    first = SelfUpgrader(RUNNING, directory=str(seat.dir))
    first.observe(answer(version=TARGET))
    first.after_pass([])
    assert first.after_pass([]) == EXIT
    assert seat.boot().returncode == 0
    assert seat.read()["state"] == "not_newer"
    for _ in range(2):
        nxt = SelfUpgrader(RUNNING, directory=str(seat.dir))
        nxt.observe(answer(version=TARGET))
        assert nxt.hold() is None
        assert [nxt.after_pass([]) for _ in range(3)] == [NONE] * 3
        assert seat.boot().returncode == 0
    data = seat.read()
    assert data["state"] == "unserved" and data["skip"] == [TARGET]
    assert "refused" in data["upgradeFailed"]["reason"]
    assert seat.pip_calls() == []


@bash_only
def test_a_failed_install_boots_the_installed_version_and_skips_the_target(seat):
    seat.fail = TARGET
    seat.write(request())
    proc = seat.boot()
    assert proc.returncode == 0, proc.stderr
    assert len(seat.pip_calls()) == 3, "bounded retries"
    data = seat.read()
    assert data["state"] == "install_failed" and data["skip"] == [TARGET]
    assert "No matching distribution" in data["upgradeFailed"]["reason"]
    assert (seat.site / f"{DIST_DIR}-{PREVIOUS}.dist-info").exists()


@bash_only
def test_a_confirmed_request_is_left_alone(seat):
    data = request()
    data.update(confirmed=True, state="confirmed")
    seat.write(data)
    assert seat.boot().returncode == 0
    assert seat.pip_calls() == []


@bash_only
def test_a_target_that_is_not_a_version_is_never_handed_to_pip(seat):
    seat.write(request(version="1.0 --index-url=http://evil"))
    assert seat.boot().returncode == 0
    assert seat.pip_calls() == []
    assert seat.read()["state"] == "invalid"


@bash_only
def test_an_unwritable_request_file_never_fails_the_boot(seat):
    """A full or read-only volume: the step's write fails, the function
    returns non-zero, and the caller's `|| log WARN` keeps the boot going."""
    seat.write(request())
    seat.dir.chmod(0o500)
    try:
        if os.access(seat.dir, os.W_OK):
            pytest.skip("running as root: the directory stays writable")
        proc = seat.boot()
    finally:
        seat.dir.chmod(0o700)
    assert proc.returncode == 7, "the step reports the failure to its caller"


@bash_only
def test_the_full_cycle_daemon_request_boot_install_confirm(seat):
    """The two halves together: the daemon writes the request at idle, the
    boot installs it, the daemon on the new version confirms it, and the
    next boot is a no-op."""
    old = SelfUpgrader(PREVIOUS, directory=str(seat.dir))
    old.observe(answer())
    old.after_pass([])
    assert old.after_pass([]) == EXIT
    assert seat.boot().returncode == 0
    assert seat.read()["state"] == "installed"
    new = SelfUpgrader(TARGET, directory=str(seat.dir))
    new.observe(answer())
    assert new.hold() is None
    assert seat.read()["confirmed"] is True
    calls = len(seat.pip_calls())
    assert seat.boot().returncode == 0
    assert len(seat.pip_calls()) == calls


@bash_only
@pytest.mark.parametrize("state, rc, expected", [
    ("requested", "0", 75), ("confirmed", "0", 0), ("requested", "3", 3),
])
def test_the_container_exits_75_after_a_self_upgrade_exit(seat, state, rc, expected):
    script = "log() { printf '[entrypoint] %s\\n' \"$*\" >&2; }\n"
    script += f"DAEMON_RC={rc}\n"
    script += lift("# >>> self_upgrade_exit", "# <<< self_upgrade_exit")
    script += '\nexit "${DAEMON_RC}"\n'
    # The file is written by json.dump(indent=2), as both writers do.
    (seat.dir / "revloop.json").write_text(json.dumps({"state": state}, indent=2))
    proc = subprocess.run(["bash", "-c", script], env=seat.env(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == expected


def test_both_writers_write_the_spelling_the_exit_check_greps(tmp_path):
    mod.write_request(str(tmp_path / "revloop.json"), {"state": "requested"})
    assert '"state": "requested"' in (tmp_path / "revloop.json").read_text()
    assert "json.dump(data, fh, indent=2, sort_keys=True)" in lift(
        "# >>> self_upgrade_boot", "# <<< self_upgrade_boot")
    assert os.environ.get("ALISSA_UPGRADE_DIR"), "conftest scrubs the real directory"


# -- the request directory follows the workspace volume (issue #199) -------------

def upgrade_dir_env(env):
    """Run the entrypoint's export block, then read the variable from a child
    process — as the daemon (and the gosu pass) would see it."""
    script = lift("# >>> upgrade_dir_env", "# <<< upgrade_dir_env")
    script = 'WORKSPACE_ROOT="${ALISSA_WORKSPACE_ROOT:-/workspace}"\n' + script
    script += '\nbash -c \'printf %s "${ALISSA_UPGRADE_DIR-unset}"\'\n'
    base = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    proc = subprocess.run(["bash", "-c", script], env={**base, **env},
                          capture_output=True, text=True, timeout=30, check=True)
    return proc.stdout


@bash_only
@pytest.mark.parametrize("env, expected", [
    ({}, mod.DEFAULT_UPGRADE_DIR),
    ({"ALISSA_WORKSPACE_ROOT": "/data/ws"}, "/data/ws/.alissa-upgrade"),
    ({"ALISSA_WORKSPACE_ROOT": "/data/ws", "ALISSA_UPGRADE_DIR": "/elsewhere"}, "/elsewhere"),
    ({"ALISSA_WORKSPACE_ROOT": "/data/ws", "ALISSA_UPGRADE_DIR": ""}, "/data/ws/.alissa-upgrade"),
])
def test_the_upgrade_dir_is_exported_from_the_workspace_root(env, expected):
    assert upgrade_dir_env(env) == expected


def test_the_upgrade_dir_is_exported_before_anything_reads_it():
    text = ENTRYPOINT.read_text()
    root = text.index('WORKSPACE_ROOT="${ALISSA_WORKSPACE_ROOT:-/workspace}"\n')
    export = text.index("# >>> upgrade_dir_env")
    assert root < export, "the export reads WORKSPACE_ROOT"
    assert export < text.index('if [ "$(id -u)" = "0" ]; then'), "before the root pass"
    assert export < text.index('    self_upgrade_boot "${RUNTIME_USER}"'), "before step 0a"
    assert export < text.index("alissa-revloop --workspace-root"), "before the daemon starts"
    assert export < text.index("# >>> self_upgrade_exit"), "before the exit check"
