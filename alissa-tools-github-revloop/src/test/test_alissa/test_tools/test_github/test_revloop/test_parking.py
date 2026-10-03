"""Seat parking for the reviewer seat (issue #157, lane L5 of studio.alissa.app
docs/design/managed-dark-factory-seat-parking.md §2.1, §2.2, §2.4): the idle
block, the drain flag every round spawn honours, and the console's
`/action/drain` / `/action/undrain` handshake.

The acceptance criteria are devloop L4's c1-c5, for rounds:

* c1 `owed` is exact per decision (table-tested over every Action and the
  sites that override the default);
* c2 a drained seat spawns no round;
* c3 the flag clears on its TTL and at the daemon's boot;
* c4 park -> wake -> the first pass raises no `stalled`;
* c5 a drain that holds pushes a vitals snapshot carrying `idle.drainedAt`
  before it answers.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

import pytest

from alissa.tools.github.revloop import parking
from alissa.tools.github.revloop.alissa import ManagedSession
from alissa.tools.github.revloop.alissa_client import AlissaError
from alissa.tools.github.revloop.config import Config
from alissa.tools.github.revloop.fleet_vitals import (
    IDLE_REPROBE_S,
    VITALS_PUSHED,
    FleetVitalsPusher,
)
from alissa.tools.github.revloop.loop import (
    STALE_ROUND_SECONDS,
    STALLED_DEFER_MULTIPLE,
    Action,
    Decision,
    ReviewWatcher,
    decision_owed,
    stalled_kind,
)
from alissa.tools.github.revloop.proc import CommandError
from alissa.tools.github.revloop.state import State
from alissa.tools.github.revloop.webui.auth import Auth
from alissa.tools.github.revloop.webui.server import App
from alissa.tools.github.revloop.webui.sources import Sources

from test_loop import (  # the fakes and the PR scenario the loop tests share
    NUMBER,
    OWNER,
    REPO,
    SLUG,
    config,  # noqa: F401 -- fixture, used by name
    make_pr,
    operator_comments,
    review,
    running_check,
    watcher,
)
from test_webui_server import _login, _req, live  # noqa: F401 -- fixture

from alissa.tools.github.revloop.ghclient import CheckContext, rollup_of


# -- c1: owed is exact per decision ------------------------------------------

@pytest.mark.parametrize("action,owed", [
    (Action.SPAWNED, True),
    (Action.IN_FLIGHT, True),
    (Action.QUEUED, True),
    (Action.AWAITING_POST, True),
    (Action.POSTED, True),
    (Action.ABANDONED, True),
    (Action.ESCALATED, True),
    (Action.CONVERGED, False),
    (Action.CAPPED, False),
    (Action.SKIPPED, False),
])
def test_every_action_has_its_default_verdict(action, owed):
    assert decision_owed(Decision(action, "why", 1)) is owed


def test_an_explicit_verdict_beats_the_actions_default():
    assert decision_owed(Decision(Action.SKIPPED, "cooldown", 2, owed=True)) is True
    assert decision_owed(Decision(Action.IN_FLIGHT, "x", 2, owed=False)) is False


def test_every_action_value_is_classified_once():
    """A new Action must be decided on purpose: either it is in OWED_STAGES
    or it is one of the three settled ones."""
    settled = {Action.CONVERGED, Action.CAPPED, Action.SKIPPED}
    for action in Action:
        assert (action.value in parking.OWED_STAGES) is (action not in settled)


def test_a_fresh_round_is_owed_with_its_stale_window(config):
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)

    spawned = w.evaluate(OWNER, REPO, NUMBER)
    assert spawned.action is Action.SPAWNED and decision_owed(spawned)

    before = int(time.time())
    in_flight = w.evaluate(OWNER, REPO, NUMBER)
    assert in_flight.action is Action.IN_FLIGHT
    assert decision_owed(in_flight)
    kind, until = in_flight.timer
    assert kind == parking.TIMER_STALE_WINDOW
    assert before + STALE_ROUND_SECONDS - 2 <= until <= int(time.time()) + STALE_ROUND_SECONDS


def test_a_converged_pr_is_settled(config):
    w, _, _ = watcher(config, make_pr(), [review("APPROVED")])
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.CONVERGED
    assert decision_owed(d) is False and d.timer is None


def test_a_draft_is_settled(config):
    w, _, _ = watcher(config, make_pr(draft=True), [])
    assert decision_owed(w.evaluate(OWNER, REPO, NUMBER)) is False


def test_a_cap_already_escalated_is_settled_and_its_page_is_owed_once(config):
    st = State(config.state_db)
    reviews = [review("CHANGES_REQUESTED")] * 3
    w, _, _ = watcher(config, make_pr(), reviews, state=st)

    page = w.evaluate(OWNER, REPO, NUMBER)
    assert page.action is Action.ESCALATED and decision_owed(page)
    again = w.evaluate(OWNER, REPO, NUMBER)
    assert again.action is Action.CAPPED and decision_owed(again) is False


def test_a_round_waiting_on_ci_is_owed_with_a_checks_hold_timer(config):
    w, gh, _ = watcher(config, make_pr(), [], state=State(config.state_db))
    gh.default_rollup = rollup_of(
        [running_check("test"), CheckContext("lint", "success")]
    )

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.QUEUED and d.checks_held
    assert decision_owed(d)
    assert d.timer[0] == parking.TIMER_CHECKS_HOLD
    assert d.timer[1] > time.time()


def test_a_round_waiting_for_a_slot_is_owed(config):
    w, _, al = watcher(config, make_pr(), [], state=State(config.state_db))
    al.sessions = [
        ManagedSession(f"review-widgets-pr{n}-r1-aaaaaa", "busy", time.time())
        for n in range(1, 10)
    ]
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.QUEUED and not d.checks_held
    assert decision_owed(d)


def test_the_post_verdict_cooldown_is_owed_with_its_clock(config):
    """The request still stands and the round is decided when the cooldown
    ends -- a moment nothing on GitHub marks, so it is the seat's clock."""
    now = int(time.time())
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10))
    w, gh, _ = watcher(
        config, make_pr(sha="0d9d66b7", requested=("alissa-app",)),
        [review("CHANGES_REQUESTED", sha="0d9d66b7", at=at)],
    )
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED and "cooldown" in d.reason
    assert decision_owed(d)
    assert d.timer == (
        parking.TIMER_STALE_WINDOW, now - 10 + config.verdict_cooldown_s
    )


def test_a_request_older_than_the_verdict_is_settled(config):
    """Waiting on a FRESH re-request is waiting on a human, whose re-request
    moves the PR's `updated_at` and wakes a parked seat by itself."""
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    older = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))
    w, gh, _ = watcher(
        config, make_pr(sha="0d9d66b7", requested=("alissa-app",)),
        [review("CHANGES_REQUESTED", sha="0d9d66b7", at=old)],
    )
    gh.request_events = [("alissa-app", older)]
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.SKIPPED and "fresh re-request" in d.reason
    assert decision_owed(d) is False


def test_a_round_with_no_hub_is_owed(config, tmp_path):
    cfg = Config(
        workspace_root=tmp_path, hub_template="{root}/nowhere/{repo}",
        state_path=tmp_path / "state.db", round_cap=3,
    )
    w, _, al = watcher(cfg, make_pr(), [])
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.SKIPPED and al.enqueued == []
    assert decision_owed(d), "unknown is never idle"


def test_a_failed_evaluation_is_owed(config):
    w, gh, _ = watcher(config, make_pr(), [])

    def boom(*a, **k):
        raise CommandError(["gh"], 1, "boom")

    gh.pull_request = boom
    [(slug, d)] = w.poll_once()
    assert d.action is Action.SKIPPED and decision_owed(d)


def test_the_poll_snapshot_carries_the_verdict_and_the_timer(config):
    st = State(config.state_db)
    w, _, _ = watcher(config, make_pr(), [], state=st)
    w.poll_once()  # spawns round 1
    w.poll_once()  # round 1 in flight

    [stage] = st.read_snapshots(1)[0]["stages"]
    assert stage["stage"] == "in-flight"
    assert stage["owed"] is True
    assert stage["timer"]["kind"] == parking.TIMER_STALE_WINDOW
    idle = parking.idle_block(
        st.read_snapshots(1)[0], live=0, managed=0, queued=0, drain=None,
        now=time.time(),
    )
    assert idle["owed"] == 1
    assert idle["timers"] == [{
        "kind": "stale_window", "until": parking.iso(stage["timer"]["until"]),
        "ref": f"{SLUG}#{NUMBER}",
    }]


def test_a_pass_with_nothing_owed_reads_zero(config):
    st = State(config.state_db)
    w, _, _ = watcher(config, make_pr(), [review("APPROVED")], state=st)
    w.poll_once()
    idle = parking.idle_block(
        st.read_snapshots(1)[0], live=0, managed=0, queued=0, drain=None,
        now=time.time(),
    )
    assert idle["owed"] == 0 and idle["timers"] == []
    assert parking.idle_refusals(idle, poll_interval=60, now=time.time()) == []


# -- the idle block and D2 ----------------------------------------------------

def test_no_pass_yet_is_unknown_not_idle():
    idle = parking.idle_block(
        None, live=0, managed=0, queued=0, drain=None, now=1_000,
    )
    assert idle["owed"] is None and idle["passAt"] is None
    refusals = parking.idle_refusals(idle, poll_interval=60, now=1_000)
    assert "owed unknown (no completed pass)" in refusals
    assert "no completed pass" in refusals


def test_every_unknown_count_is_a_refusal():
    idle = parking.idle_block(
        {"ts": 1_000, "stages": []}, live=None, managed=None, queued=None,
        drain=None, now=1_000,
    )
    assert parking.idle_refusals(idle, poll_interval=60, now=1_000) == [
        "live unknown", "managed unknown", "queued unknown",
    ]


def test_a_stale_pass_is_stalled_not_idle():
    idle = parking.idle_block(
        {"ts": 1_000, "stages": []}, live=0, managed=0, queued=0, drain=None,
        now=1_200,
    )
    assert parking.idle_refusals(idle, poll_interval=60, now=1_121) == [
        "last pass 121s ago (over 2 × 60s)",
    ]
    assert parking.idle_refusals(idle, poll_interval=60, now=1_120) == []


def test_a_pre_parking_stage_falls_back_to_its_stages_default():
    for stage, owed in (("spawned", True), ("checks-held", True),
                        ("drained", True), ("converged", False),
                        ("skipped", False), ("capped", False)):
        assert parking.stage_owed({"stage": stage}) is owed


def test_a_malformed_timer_is_ignored():
    assert parking.stage_timer({"timer": {"kind": "nap", "until": 5}}) is None
    assert parking.stage_timer({"timer": {"kind": "checks_hold", "until": True}}) is None
    assert parking.stage_timer({"timer": "soon"}) is None


def test_queued_counts_only_undispatched_items():
    queues = [
        {"name": "default", "items": [
            {"session": "a"}, {"session": "b", "lastDispatchedAt": 99},
        ]},
        {"name": "other", "items": [{"session": "c"}]},
        "junk",
    ]
    assert parking.queued_items(queues) == 2
    assert parking.queued_items(None) is None
    assert parking.queued_items({"items": []}) is None


def test_roster_counts_follow_the_vitals_sessions_rule():
    rows = [
        {"live": True, "managed": True},
        {"live": True, "managed": False},
        {"live": False, "managed": True},
    ]
    assert parking.roster_counts(rows) == (2, 2)
    assert parking.roster_counts(None) == (None, None)


# -- c2: a drained seat spawns no round ---------------------------------------

def test_a_drained_seat_spawns_nothing_and_the_round_stays_owed(config):
    st = State(config.state_db)
    st.set_drain(300, "park")
    w, gh, al = watcher(config, make_pr(), [], state=st)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.QUEUED and d.drained
    assert decision_owed(d)
    assert "drained for parking" in d.reason and "park" in d.reason
    assert al.enqueued == []
    assert st.read_spawns() == [], "no spawn row: no round number is burned"
    assert gh.issue_store == [], "no activity line"
    assert st._db.execute("SELECT COUNT(*) FROM spawn_claims").fetchone()[0] == 0


def test_a_drained_respawn_is_held_too(config):
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    st._db.execute(
        "UPDATE spawns SET spawned_at=?",
        (int(time.time()) - STALE_ROUND_SECONDS - 60,),
    )
    st._db.commit()
    st.set_drain(300, "park")

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.QUEUED and d.drained
    assert len(al.enqueued) == 1, "the dead round is not re-enqueued while drained"


def test_the_round_spawns_once_the_drain_is_lifted(config):
    st = State(config.state_db)
    st.set_drain(300, "park")
    w, _, al = watcher(config, make_pr(), [], state=st)
    assert w.evaluate(OWNER, REPO, NUMBER).drained
    st.clear_drain()

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED and d.round == 1
    assert st._db.execute("SELECT COUNT(*) FROM spawn_claims").fetchone()[0] == 0, (
        "the claim is released once the spawn lands"
    )


def test_the_claim_is_released_when_the_spawn_raises(config):
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)

    def boom(**kw):
        raise RuntimeError("enqueue failed")

    al.enqueue_reviewer = boom
    with pytest.raises(RuntimeError):
        w.evaluate(OWNER, REPO, NUMBER)
    assert st._db.execute("SELECT COUNT(*) FROM spawn_claims").fetchone()[0] == 0


def test_a_drain_landing_mid_spawn_sees_the_claim(config):
    """The race the claim closes: the drain lands while the spawn is between
    its gate and its ledger write. The drain must see the claim (and the
    console then refuses it), never a seat that looks idle."""
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)
    seen = {}

    def enqueue(**kw):
        with State(config.state_db) as console:
            seen["drain"], seen["claims"] = console.set_drain(300, "park")
        al.enqueued.append(kw)

    al.enqueue_reviewer = enqueue
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert [c["session"] for c in seen["claims"]] == [al.enqueued[0]["session"]]


def test_a_ledger_that_cannot_prove_the_seat_undrained_stands_the_spawn_down(config):
    st = State(config.state_db)
    w, _, al = watcher(config, make_pr(), [], state=st)

    def broken(session, now=None):
        raise sqlite3.OperationalError("database is locked")

    st.claim_spawn = broken
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.QUEUED and d.drained
    assert "could not prove" in d.reason
    assert al.enqueued == []


def test_dry_run_reads_the_flag_and_writes_no_claim(config, tmp_path):
    import dataclasses

    st = State(config.state_db)
    dry = dataclasses.replace(config, dry_run=True)
    w, _, al = watcher(dry, make_pr(), [], state=st)
    claims = []
    st.claim_spawn = lambda *a, **k: claims.append(a)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert claims == []
    st.set_drain(300, "park")
    assert w.evaluate(OWNER, REPO, NUMBER).drained


def test_a_drained_pass_records_the_stage_and_never_pages_as_a_stall(config, caplog):
    st = State(config.state_db)
    st.set_drain(300, "park")
    w, _, _ = watcher(config, make_pr(), [], state=st)

    with caplog.at_level(logging.INFO):
        w.poll_once()

    [stage] = st.read_snapshots(1)[0]["stages"]
    assert stage["stage"] == "drained" and stage["owed"] is True
    assert not any("deferred" in r.message and "sessions live" in r.message
                   for r in caplog.records), "a drain is not slot back-pressure"


# -- c3: the flag clears on its TTL and at boot -------------------------------

def test_the_flag_expires_on_its_ttl(tmp_path):
    st = State(tmp_path / "state.db")
    st.set_drain(300, "park", now=1_000)
    assert st.drain(now=1_299)["reason"] == "park"
    assert st.drain(now=1_300) is None
    assert st.claim_spawn("s", now=1_299) is not None
    assert st.claim_spawn("s", now=1_300) is None


def test_a_re_drain_restarts_the_ttl(tmp_path):
    st = State(tmp_path / "state.db")
    st.set_drain(300, "first", now=1_000)
    st.set_drain(300, "second", now=1_200)
    assert st.drain(now=1_450)["reason"] == "second"


def test_the_daemon_boot_clears_the_drain_and_every_claim(config):
    st = State(config.state_db)
    st.set_drain(300, "park")
    st.claim_spawn("review-widgets-pr7-r1-aaaaaa")
    st.set_drain(300, "park")
    w, _, _ = watcher(config, make_pr(), [], state=st)

    def stop():
        raise KeyboardInterrupt

    w.poll_once = stop
    w.run_forever()

    assert st.drain() is None
    assert st._db.execute("SELECT COUNT(*) FROM spawn_claims").fetchone()[0] == 0


def test_building_a_watcher_does_not_clear_the_drain(config):
    """`--once` and `--pr` build a watcher too; a hand-run diagnostic must
    not lift a drain orcloop holds on the running daemon."""
    st = State(config.state_db)
    st.set_drain(300, "park")
    watcher(config, make_pr(), [], state=st)
    assert st.drain() is not None


def test_a_dry_run_boot_leaves_the_drain_alone(config):
    import dataclasses

    st = State(config.state_db)
    st.set_drain(300, "park")
    w, _, _ = watcher(dataclasses.replace(config, dry_run=True), make_pr(), [], state=st)

    def stop():
        raise KeyboardInterrupt

    w.poll_once = stop
    w.run_forever()
    assert st.drain() is not None


# -- c4: park -> wake -> the first pass raises no stalled ---------------------

def _age_ledger(st, seconds):
    """Every spawn row and snapshot `seconds` older: the parked interval."""
    st._db.execute("UPDATE spawns SET spawned_at = spawned_at - ?", (seconds,))
    st._db.execute("UPDATE poll_snapshots SET ts = ts - ?", (seconds,))
    st._db.commit()


def test_park_then_wake_raises_no_stalled(config, caplog):
    """The walk: round 1 runs and its session closes it with a verdict, the
    PR leaves the search, the seat reads idle and is drained; six hours
    parked; the wake boots a fresh daemon (the redeploy), the implementer's
    re-request is the demand, and the first pass after the wake spawns
    round 2 -- with no stalled page, no stalled ping and no stall summary."""
    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), [], state=st)
    w.poll_once()
    assert len(al.enqueued) == 1

    # The session closes its round; the request is consumed.
    gh._reviews.append(review("CHANGES_REQUESTED"))
    al.verdict_count = 1
    gh.requests = []
    w.poll_once()
    idle = parking.idle_block(
        st.read_snapshots(1)[0], live=0, managed=0, queued=0, drain=None,
        now=time.time(),
    )
    assert parking.idle_refusals(idle, poll_interval=60, now=time.time()) == []
    st.set_drain(300, "park")

    # Parked: the process is gone for six hours.
    _age_ledger(st, 6 * 3600)

    # Wake: a fresh daemon on the same ledger, with the demand that woke it.
    gh.comments.clear()
    gh._pr = make_pr(sha="def456")
    gh.requests = [(OWNER, REPO, NUMBER)]
    woken = ReviewWatcher(config, github=gh, alissa=al, state=st)
    first_pass = woken.poll_once

    def one_pass_then_stop():
        first_pass()
        raise KeyboardInterrupt

    woken.poll_once = one_pass_then_stop
    with caplog.at_level(logging.INFO):
        woken.run_forever()

    assert st.drain() is None, "the boot lowered the flag"
    assert len(al.enqueued) == 2, "round 2 spawned on the first pass"
    assert operator_comments(gh) == []
    assert not any(k.startswith("stalled:") for k in
                   (p["kind"] for p in st.read_pings()))
    assert not any("STALLED" in r.message for r in caplog.records)


def test_a_stall_needs_a_live_session_so_it_can_never_span_a_park(config):
    """The only `stalled` revloop raises is the liveness deferral behind a
    LIVE session; a live session is `live > 0`, which D2 refuses to park.
    Pinned from both ends: the stall fires on the live case, and that case
    is not idle."""
    st = State(config.state_db)
    w, gh, al = watcher(config, make_pr(), [], state=st)
    w.evaluate(OWNER, REPO, NUMBER)
    session = al.enqueued[0]["session"]
    st._db.execute(
        "UPDATE spawns SET spawned_at=?",
        (int(time.time()) - STALLED_DEFER_MULTIPLE * STALE_ROUND_SECONDS - 60,),
    )
    st._db.commit()
    al.sessions = [ManagedSession(session, "busy", time.time())]

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.IN_FLIGHT and d.deferred and decision_owed(d)
    assert st.pinged(SLUG, NUMBER, stalled_kind(session))
    idle = parking.idle_block(
        {"ts": time.time(), "stages": []}, live=1, managed=1, queued=0,
        drain=None, now=time.time(),
    )
    assert "live=1" in parking.idle_refusals(idle, poll_interval=60, now=time.time())


# -- c5: the console's drain handshake ----------------------------------------

class _Client:
    def __init__(self, fail=None):
        self.posts: list[dict] = []
        self.fail = list(fail or [])

    def post_fleet_vitals(self, snapshot):
        self.posts.append(snapshot)
        if self.fail:
            raise self.fail.pop(0)
        return {"replaced": True}


def _console(tmp_path, *, roster="[]", queue="[]", pass_age=5, stages=(),
             push=True, client=None):
    config = Config.build(tmp_path, {"repos": ["acme/widgets"]}, {}, environ={})
    with State(config.state_db) as st:
        st.record_snapshot(
            duration_ms=10, candidates=len(stages), spawned=0,
            stale_reenqueued=0, in_flight=0, deferred=0, converged=0,
            capped=0, escalated=0, skipped=0, reaped=0, stages=list(stages),
        )
        st._db.execute("UPDATE poll_snapshots SET ts = ?",
                       (int(time.time()) - pass_age,))
        st._db.commit()

    def run(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return roster
        if argv[:4] == ["alissa", "tmux", "queue", "ls"]:
            return queue
        if argv[:3] == ["gh", "api", "rate_limit"]:
            return "{}"
        if argv[:2] == ["tmux", "list-panes"]:
            raise CommandError(argv, 1, "no pane")
        raise AssertionError(argv)

    sources = Sources(config=config, running_version="0.31.11", run=run,
                      http_get=lambda u, t: None)
    audit: list = []
    client = client or _Client()
    pusher = FleetVitalsPusher(config, sources, client)
    app = App(
        auth=Auth("pw", boot_nonce="n"), sources=sources, version="0.31.11",
        audit=lambda action, detail: audit.append((action, detail)),
        push_vitals=pusher.push_now if push else None,
    )
    return app, client, audit


def test_an_idle_seat_drains_and_pushes_drainedAt_before_answering(tmp_path):
    app, client, audit = _console(tmp_path)

    status, body = app.drain(300, "orcloop: idle 15 min")

    assert status == 200 and body["drained"] is True
    assert body["vitals"] == VITALS_PUSHED
    assert body["idle"]["drainedAt"] == body["drainedAt"]
    [pushed] = client.posts
    assert pushed["idle"]["drainedAt"] == body["drainedAt"], (
        "the snapshot Studio re-checks carries the drain"
    )
    assert pushed["idle"]["owed"] == 0
    with State(app.sources.config.state_db) as st:
        assert st.drain() is not None, "the flag holds"
    assert audit[-1][0] == "drain" and audit[-1][1]["ok"] is True


def test_the_push_stamps_the_daemons_last_pass_not_now(tmp_path):
    app, client, _ = _console(tmp_path, pass_age=30)
    app.drain()
    [pushed] = client.posts
    assert pushed["heartbeatAt"] == pushed["idle"]["passAt"]
    assert pushed["heartbeatAt"] != pushed["asOf"]
    assert pushed["queueDepth"] is None


@pytest.mark.parametrize("kwargs,refusal", [
    ({"roster": json.dumps([{"name": "review-widgets-pr7-r1-aaaaaa",
                             "status": "busy"}])}, "live=1"),
    ({"roster": json.dumps([{"name": "develop-acme-widgets-i7-a1",
                             "status": "busy"}])}, "live=1"),
    ({"roster": ""}, "live unknown"),
    ({"queue": json.dumps([{"items": [{"session": "x"}]}])}, "queued=1"),
    ({"queue": ""}, "queued unknown"),
    ({"stages": [{"slug": "acme/widgets#7", "stage": "spawned", "owed": True}]},
     "owed=1"),
    ({"stages": [{"slug": "acme/widgets#7", "stage": "skipped", "owed": True,
                  "timer": {"kind": "stale_window", "until": 2_000_000_000}}]},
     "1 timer(s) running"),
    ({"pass_age": 3600}, "last pass 3600s ago"),
])
def test_a_seat_that_is_not_idle_refuses_and_lowers_the_flag(tmp_path, kwargs, refusal):
    app, client, audit = _console(tmp_path, **kwargs)

    status, body = app.drain(300, "park")

    assert status == 200 and body["drained"] is False
    assert any(r.startswith(refusal) for r in body["refusals"]), body["refusals"]
    assert body["idle"]["drainedAt"] is None
    assert client.posts == [], "a refused drain pushes nothing"
    with State(app.sources.config.state_db) as st:
        assert st.drain() is None
    assert audit[-1][1]["ok"] is False


def test_a_spawn_in_progress_refuses_the_drain(tmp_path):
    app, client, _ = _console(tmp_path)
    with State(app.sources.config.state_db) as st:
        st.claim_spawn("review-widgets-pr7-r1-aaaaaa")

    status, body = app.drain()

    assert body["drained"] is False
    assert "spawn in progress: review-widgets-pr7-r1-aaaaaa" in body["refusals"]


def test_a_bad_ttl_or_reason_is_a_400(tmp_path):
    app, _, _ = _console(tmp_path)
    for ttl in (0, -5, parking.DRAIN_TTL_MAX + 1, "300", True, 1.5):
        assert app.drain(ttl, "x")[0] == 400
    assert app.drain(300, "r" * (parking.DRAIN_REASON_MAX + 1))[0] == 400
    assert app.drain(300, 7)[0] == 400
    with State(app.sources.config.state_db) as st:
        assert st.drain() is None


def test_no_state_db_is_a_503_and_creates_nothing(tmp_path):
    config = Config.build(tmp_path, {"repos": ["acme/widgets"]}, {}, environ={})
    sources = Sources(config=config, running_version="x", run=lambda a, **k: "[]",
                      http_get=lambda u, t: None)
    app = App(auth=Auth("pw", boot_nonce="n"), sources=sources, version="x",
              audit=lambda a, d: None)
    assert app.drain()[0] == 503
    assert app.undrain() == (200, {"drained": False, "cleared": False})
    assert not config.state_db.exists()


def test_undrain_lowers_the_flag_and_is_idempotent(tmp_path):
    app, _, audit = _console(tmp_path)
    assert app.drain()[1]["drained"] is True
    assert app.undrain() == (200, {"drained": False, "cleared": True})
    assert app.undrain() == (200, {"drained": False, "cleared": False})
    with State(app.sources.config.state_db) as st:
        assert st.drain() is None
    assert [a for a, _ in audit] == ["drain", "undrain", "undrain"]


def test_with_the_push_off_the_drain_says_skipped(tmp_path):
    app, client, _ = _console(tmp_path, push=False)
    status, body = app.drain()
    assert body["drained"] is True and body["vitals"] == "skipped"


def test_the_dashboard_carries_the_idle_block(tmp_path):
    app, _, _ = _console(tmp_path)
    idle = app.sources.dashboard()["idle"]
    assert idle["owed"] == 0 and idle["live"] == 0 and idle["queued"] == 0
    assert idle["drainedAt"] is None
    app.drain()
    assert app.sources.dashboard()["idle"]["drainedAt"] is not None


def test_drain_and_undrain_need_the_session_and_the_csrf_token(live):  # noqa: F811
    base, app = live
    _, cookie = _login(base)
    _, page, _ = _req(base, "/", headers={"Cookie": cookie})
    csrf = re.search(rb'csrf-token" content="([0-9a-f]+)"', page).group(1).decode()
    payload = json.dumps({"ttlS": 300, "reason": "park"}).encode()
    for path in ("/action/drain", "/action/undrain"):
        assert _req(base, path, "POST", payload)[0] == 401
        assert _req(base, path, "POST", payload, {"Cookie": cookie})[0] == 403
    status, body, _ = _req(base, "/action/drain", "POST", payload,
                           {"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200 and "drained" in json.loads(body)
    status, body, _ = _req(base, "/action/undrain", "POST", b"{}",
                           {"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200 and json.loads(body)["drained"] is False


# -- Studio's strict schema, until lane L3 lands ------------------------------

def test_a_studio_that_refuses_idle_gets_the_snapshot_without_it(tmp_path, caplog):
    refusal = AlissaError(400, {"error": "unknown key: idle"})
    app, client, _ = _console(tmp_path, client=_Client(fail=[refusal]))
    pusher = FleetVitalsPusher(app.sources.config, app.sources, client)

    with caplog.at_level(logging.WARNING):
        assert pusher.push_once(heartbeat_at=time.time()) == VITALS_PUSHED

    assert "idle" in client.posts[0] and "idle" not in client.posts[1]
    assert "predates them" in caplog.text and "`idle`" in caplog.text
    # ...and leaves it out for the reprobe window, then offers it again.
    pusher.push_once(heartbeat_at=time.time())
    assert "idle" not in client.posts[2]
    pusher._refused_until["idle"] = time.time() - 1
    pusher.push_once(heartbeat_at=time.time())
    assert "idle" in client.posts[3]
    assert IDLE_REPROBE_S == 3600.0


def test_any_other_400_is_an_ordinary_failure(tmp_path):
    app, client, _ = _console(tmp_path, client=_Client(fail=[AlissaError(400, "bad heartbeatAt")]))
    pusher = FleetVitalsPusher(app.sources.config, app.sources, client)
    assert pusher.push_once(heartbeat_at=time.time()) == "failed"
    assert len(client.posts) == 1
