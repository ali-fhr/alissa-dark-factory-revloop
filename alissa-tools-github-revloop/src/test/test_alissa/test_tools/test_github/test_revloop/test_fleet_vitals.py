"""Fleet-vitals tests (issue #126): the snapshot, the pusher, the wiring, the
client method, the config rail, the console builders it leans on.

What is pinned here, per the issue's testing contract:

* console fixtures (a seeded ledger, a faked `alissa tmux ls`, a faked
  `gh api rate_limit`, a cgroup fixture dir) build the EXACT contract body --
  required fields, the null-vs-{0,0} roster rule, the 50-entry caps, the
  round parsed out of the session grammar when no ledger row pairs it;
* the body is fitted to the 64 KB cap before it is sent;
* disabled -> no pusher and no call; dry-run -> one log line and nothing
  sent; enabled -> ONE POST per pass, AFTER the loop-events push; a failed
  push -> ONE warning and the pass completes (the snapshot row still lands);
  `403 not_activated` -> once-per-boot WARNING with the activate URL, then
  DEBUG;
* the config key / CLI flags / env var layer the way loop events do, through
  the SAME boolean reader, and the container's env var name is the one the
  library reads.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alissa.tools.github.revloop import alissa_client as client_module
from alissa.tools.github.revloop import config as config_module
from alissa.tools.github.revloop import fleet_vitals
from alissa.tools.github.revloop import state as state_module
from alissa.tools.github.revloop.__main__ import build_parser, overrides_from
from alissa.tools.github.revloop.alissa_client import (
    NOT_ACTIVATED,
    AlissaAuthError,
    AlissaClient,
    AlissaTransient,
)
from alissa.tools.github.revloop.config import (
    FLEET_VITALS_ENV,
    LOOP_EVENTS_ENV,
    Config,
    env_fleet_vitals_enabled,
    env_loop_events_enabled,
)
from alissa.tools.github.revloop.fleet_vitals import (
    BODY_MAX_BYTES,
    LIST_CAP,
    VITALS_FAILED,
    VITALS_PUSHED,
    VITALS_SKIPPED,
    FleetVitalsPusher,
    body_bytes,
    build_snapshot,
    fit_body,
    iso_utc,
)
from alissa.tools.github.revloop.loop import ReviewWatcher
from alissa.tools.github.revloop.loop_events import LoopEventsEmitter
from alissa.tools.github.revloop.proc import CommandError
from alissa.tools.github.revloop.state import State
from alissa.tools.github.revloop.webui import sysinfo
from alissa.tools.github.revloop.webui.sources import Sources

REPO = "acme/widgets"
HEAD = "a" * 40
# Every ledger stamp in these tests; the wall clock the builder reads sits
# 1000 s later, past the inbox's live grace (2 x poll_interval), so liveness
# is decided by the snapshot and not by recency.
STAMP = 1_000_000
WALL = STAMP + 1000
PANE_SESSION = "review-widgets-pr16-r2-ab12cd"


@pytest.fixture
def frozen_ledger_clock(monkeypatch):
    monkeypatch.setattr(state_module.time, "time", lambda: STAMP)


def _write_cgroup(root: Path, *, limit: str = "4294967296") -> Path:
    root.mkdir()
    (root / "memory.current").write_text("9000\n")
    (root / "memory.stat").write_text(
        "anon 1000\nfile 6000\ninactive_file 100\nshmem 0\n"
        "slab_reclaimable 500\nslab_unreclaimable 20\n"
    )
    (root / "memory.max").write_text(f"{limit}\n")
    return root


TMUX_ROSTER = [
    # This daemon's own spawn, paired with its ledger row (the authority).
    {"name": PANE_SESSION, "session": PANE_SESSION, "status": "busy",
     "live": True, "lastActivity": WALL - 100},
    # The skill's hand-driven shape: no ledger row, the NAME says PR 9 round 3.
    {"name": "review-pr-9-r3", "status": "idle", "live": True,
     "lastActivity": WALL - 50},
    # Another lane's session in the shared container: unmanaged, unpaired.
    {"name": "develop-acme-widgets-i7-a1", "status": "busy", "live": True,
     "lastActivity": WALL - 10},
    # A gone session of our grammar with no ledger row: the name resolves the
    # repo through the allowlist.
    {"name": "review-widgets-pr8-r1-ffffff", "status": "gone", "live": False},
]

RATE = {"resources": {"core": {
    "limit": 5000, "remaining": 4300, "used": 700, "reset": 1_700_000_000,
}}}


def _runner(*, roster=TMUX_ROSTER, rate=RATE, tmux_fails=False):
    def run(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            if tmux_fails:
                raise CommandError(argv, 1, "no server running")
            return json.dumps(roster)
        if argv[:2] == ["tmux", "list-panes"]:
            # No pane -> no /proc walk; cpu/rss read as unknown.
            raise CommandError(argv, 1, "no pane")
        if argv[:3] == ["gh", "api", "rate_limit"]:
            if rate is None:
                raise CommandError(argv, 1, "gh missing")
            return json.dumps(rate)
        raise AssertionError(f"unexpected run: {argv}")
    return run


def _seed(db_path):
    with State(db_path) as st:
        st.record_spawn(repo=REPO, number=16, round_=2, head_sha=HEAD,
                        session=PANE_SESSION, task_ref="TASK-9")
        st.record_escalation(REPO, 12, "deadbeefcafe")
        st.record_ping(REPO, 16, "stalled:" + PANE_SESSION)
        # Settled: PR 21 is absent from the newest pass and older than the
        # grace window, so its page is exhaust and must NOT be pushed.
        st.record_ping(REPO, 21, "stability:beefcafe1234:0ldbase99:0")
        st.record_ping(REPO, 16, "activity-deferred:" + PANE_SESSION)
        for duration in (30, 42):
            st.record_snapshot(
                duration_ms=duration, candidates=2, spawned=1, in_flight=0,
                deferred=0, converged=0, capped=1, escalated=0, skipped=0,
                reaped=0,
                stages=[
                    {"slug": f"{REPO}#16", "number": 16, "round": 2,
                     "attempt": None, "session": PANE_SESSION,
                     "stage": "in-flight", "reason": "", "task_ref": "TASK-9"},
                    {"slug": f"{REPO}#12", "number": 12, "round": None,
                     "attempt": None, "session": None, "stage": "capped",
                     "reason": "already escalated", "task_ref": None},
                ],
            )


def make_sources(tmp_path, *, runner=None, latest="0.31.0", cgroup=None,
                 running="0.30.0", repos=(REPO,), poll_interval=60):
    config = Config.build(
        tmp_path, {"repos": list(repos), "poll_interval": poll_interval}, {},
        environ={},
    )
    _seed(config.state_db)

    def http(url, timeout):
        if latest is None:
            return None
        return json.dumps({"info": {"version": latest}}).encode()

    return config, Sources(
        config=config, running_version=running,
        run=runner or _runner(), http_get=http,
        proc_root=str(tmp_path / "no-proc"),
        cgroup_root=str(cgroup if cgroup is not None else tmp_path / "no-cg"),
        clock=lambda: 0.0, wall_clock=lambda: float(WALL),
    )


# -- the snapshot: the exact contract body ---------------------------------


def test_console_fixtures_build_the_exact_contract_body(
    tmp_path, frozen_ledger_clock
):
    config, sources = make_sources(
        tmp_path, cgroup=_write_cgroup(tmp_path / "cg")
    )
    snapshot = build_snapshot(
        sources, heartbeat_at=WALL - 3, poll_interval=config.poll_interval,
        queue_depth=2, repos=config.repos, now=WALL,
    )
    assert snapshot == {
        "schemaVersion": 1,
        "seat": "revloop",
        "asOf": iso_utc(WALL),
        "heartbeatAt": iso_utc(WALL - 3),
        "pollIntervalS": 60,
        "version": "0.30.0",
        "drift": "0.31.0",
        # oldest first: the sparkline order, off the same reader
        "pollDurationsMs": [30, 42],
        "sessions": {"live": 3, "managed": 3},
        "sessionList": [
            {"name": PANE_SESSION, "edge": "review", "repo": REPO,
             "number": 16, "round": 2, "attempt": None, "ageS": 100,
             "cpu": None, "rssBytes": None, "live": True, "managed": True,
             "url": f"https://github.com/{REPO}/pull/16"},
            {"name": "review-pr-9-r3", "edge": "review", "repo": None,
             "number": 9, "round": 3, "attempt": None, "ageS": 50,
             "cpu": None, "rssBytes": None, "live": True, "managed": True,
             "url": None},
            {"name": "develop-acme-widgets-i7-a1", "edge": "review",
             "repo": None, "number": None, "round": None, "attempt": None,
             "ageS": 10, "cpu": None, "rssBytes": None, "live": True,
             "managed": False, "url": None},
            {"name": "review-widgets-pr8-r1-ffffff", "edge": "review",
             "repo": REPO, "number": 8, "round": 1, "attempt": None,
             "ageS": None, "cpu": None, "rssBytes": None, "live": False,
             "managed": True, "url": f"https://github.com/{REPO}/pull/8"},
        ],
        "rate": {"remaining": 4300, "limit": 5000,
                 "resetAt": "2023-11-14T22:13:20Z"},
        "memory": {"residentBytes": 1000, "reclaimableBytes": 6500,
                   "limitBytes": 4294967296},
        "queueDepth": 2,
        "kpis": None,
        "inbox": [
            {"kind": "cap-out", "subject": f"{REPO}#12", "repo": REPO,
             "number": 12, "isPr": True, "ageS": 1000,
             "url": f"https://github.com/{REPO}/pull/12",
             "lever": fleet_vitals.LEVER_REENTER},
            {"kind": "stalled", "subject": f"{REPO}#16", "repo": REPO,
             "number": 16, "isPr": True, "ageS": 1000,
             "url": f"https://github.com/{REPO}/pull/16",
             "lever": fleet_vitals.LEVER_STALLED},
        ],
    }
    # The wire form the strict API validates: every key the contract names,
    # and nothing else.
    assert set(snapshot) == {
        "schemaVersion", "seat", "asOf", "heartbeatAt", "pollIntervalS",
        "version", "drift", "pollDurationsMs", "sessions", "sessionList",
        "rate", "memory", "queueDepth", "kpis", "inbox",
    }


def test_iso_stamps_are_whole_second_utc_with_z():
    assert iso_utc(1_700_000_000.7) == "2023-11-14T22:13:20Z"
    assert datetime(1970, 1, 1, tzinfo=timezone.utc).timestamp() == 0
    assert iso_utc(0) == "1970-01-01T00:00:00Z"


def test_an_unlistable_roster_is_null_never_zero_zero(
    tmp_path, frozen_ledger_clock
):
    """The null-vs-{0,0} rule: a Factory card reading "0 live" over a broken
    tmux would be the reassuring-direction error."""
    _, sources = make_sources(tmp_path, runner=_runner(tmux_fails=True))
    snapshot = build_snapshot(
        sources, heartbeat_at=WALL, poll_interval=60, queue_depth=None,
    )
    assert snapshot["sessions"] is None
    assert snapshot["sessionList"] is None
    # ...while everything else still reads: one broken source, one null.
    assert snapshot["inbox"]
    assert snapshot["pollDurationsMs"] == [30, 42]
    assert snapshot["queueDepth"] is None


def test_an_empty_roster_is_zero_zero_and_an_empty_list(
    tmp_path, frozen_ledger_clock
):
    _, sources = make_sources(tmp_path, runner=_runner(roster=[]))
    snapshot = build_snapshot(sources, heartbeat_at=WALL, poll_interval=60,
                              queue_depth=0)
    assert snapshot["sessions"] == {"live": 0, "managed": 0}
    assert snapshot["sessionList"] == []


def test_every_optional_read_degrades_to_null_on_its_own(
    tmp_path, frozen_ledger_clock
):
    _, sources = make_sources(
        tmp_path, runner=_runner(rate=None), latest=None,
    )  # no cgroup dir, no rate read, no PyPI
    snapshot = build_snapshot(sources, heartbeat_at=WALL, poll_interval=60,
                              queue_depth=0)
    assert snapshot["rate"] is None
    assert snapshot["memory"] is None
    assert snapshot["drift"] is None
    assert snapshot["version"] == "0.30.0"
    assert snapshot["sessions"] == {"live": 3, "managed": 3}


def test_drift_is_null_when_current_or_ahead(tmp_path, frozen_ledger_clock):
    _, sources = make_sources(tmp_path, latest="0.30.0")
    assert build_snapshot(sources, heartbeat_at=WALL, poll_interval=60,
                          queue_depth=0)["drift"] is None
    _, sources = make_sources(tmp_path / "b", latest="0.29.0")
    assert build_snapshot(sources, heartbeat_at=WALL, poll_interval=60,
                          queue_depth=0)["drift"] is None


def test_memory_limit_max_is_null_and_resident_unknown_nulls_the_object(
    tmp_path, frozen_ledger_clock
):
    cg = _write_cgroup(tmp_path / "cg", limit="max")
    _, sources = make_sources(tmp_path, cgroup=cg)
    memory = build_snapshot(sources, heartbeat_at=WALL, poll_interval=60,
                            queue_depth=0)["memory"]
    assert memory == {"residentBytes": 1000, "reclaimableBytes": 6500,
                      "limitBytes": None}
    (cg / "memory.stat").write_text("file 6000\nshmem 0\nslab_reclaimable 5\n")
    assert build_snapshot(sources, heartbeat_at=WALL, poll_interval=60,
                          queue_depth=0)["memory"] is None


def test_session_list_caps_at_fifty_live_managed_first(
    tmp_path, frozen_ledger_clock
):
    roster = [
        {"name": f"review-widgets-pr{n}-r1-{n:06x}", "status": "gone",
         "live": False}
        for n in range(70)
    ] + [
        {"name": "develop-acme-widgets-i1-a1", "status": "busy", "live": True,
         "lastActivity": WALL},
    ] + [
        {"name": f"review-pr-{n}-r1", "status": "busy", "live": True,
         "lastActivity": WALL}
        for n in range(45)
    ]
    _, sources = make_sources(tmp_path, runner=_runner(roster=roster))
    snapshot = build_snapshot(sources, heartbeat_at=WALL, poll_interval=60,
                              queue_depth=0)
    # The counts describe the whole roster; the list is what fits.
    assert snapshot["sessions"] == {"live": 46, "managed": 115}
    rows = snapshot["sessionList"]
    assert len(rows) == LIST_CAP
    # Live own-grammar rows first, then the live foreign one, then what is
    # gone -- the cap drops the rows an operator cares least about.
    assert [(r["live"], r["managed"]) for r in rows] == (
        [(True, True)] * 45 + [(True, False)] + [(False, True)] * 4
    )
    assert rows[45]["name"] == "develop-acme-widgets-i1-a1"
    assert [r["number"] for r in rows[:3]] == [0, 1, 2]  # roster order kept


def test_inbox_caps_at_fifty_newest_first(tmp_path, monkeypatch):
    # Sixty cap-outs on sixty PRs, one second apart: the newest fifty reach
    # the snapshot, the ten oldest do not.
    now = {"t": STAMP}

    def tick():
        now["t"] += 1
        return now["t"]

    monkeypatch.setattr(state_module.time, "time", tick)
    config, sources = make_sources(tmp_path, runner=_runner(roster=[]))
    with State(config.state_db) as st:
        for n in range(100, 160):
            st.record_escalation(REPO, n, f"{n:040x}")
        st.record_snapshot(
            duration_ms=1, candidates=60,
            stages=[{"slug": f"{REPO}#{n}", "number": n, "round": None,
                     "attempt": None, "session": None, "stage": "capped",
                     "reason": "", "task_ref": None} for n in range(100, 160)],
        )
    inbox = build_snapshot(sources, heartbeat_at=WALL, poll_interval=60,
                           queue_depth=0)["inbox"]
    assert len(inbox) == LIST_CAP
    assert [i["number"] for i in inbox][:3] == [159, 158, 157]
    assert min(i["number"] for i in inbox) == 110
    assert all(i["kind"] == "cap-out" and i["isPr"] for i in inbox)


def test_round_and_number_come_from_the_session_grammar_without_a_ledger_row(
    tmp_path, frozen_ledger_clock
):
    roster = [
        {"name": "review-pr-77-r4", "status": "busy", "live": True},
        {"name": "review-pr-78", "status": "busy", "live": True},
        {"name": "review-widgets-pr79-r6-0a0a0a", "status": "busy",
         "live": True},
        # Two allowlisted repos share the slug: no guess, repo stays null.
        {"name": "review-shared-pr80-r1-0b0b0b", "status": "busy",
         "live": True},
    ]
    _, sources = make_sources(
        tmp_path, runner=_runner(roster=roster),
        repos=(REPO, "one/shared", "two/shared"),
    )
    rows = build_snapshot(sources, heartbeat_at=WALL, poll_interval=60,
                          queue_depth=0, repos=(REPO, "one/shared", "two/shared"))["sessionList"]
    by_name = {r["name"]: r for r in rows}
    assert (by_name["review-pr-77-r4"]["number"], by_name["review-pr-77-r4"]["round"]) == (77, 4)
    assert by_name["review-pr-77-r4"]["repo"] is None
    assert (by_name["review-pr-78"]["number"], by_name["review-pr-78"]["round"]) == (78, None)
    r79 = by_name["review-widgets-pr79-r6-0a0a0a"]
    assert (r79["repo"], r79["number"], r79["round"]) == (REPO, 79, 6)
    assert r79["url"] == f"https://github.com/{REPO}/pull/79"
    r80 = by_name["review-shared-pr80-r1-0b0b0b"]
    assert (r80["repo"], r80["number"], r80["round"], r80["url"]) == (None, 80, 1, None)


def test_the_ledger_row_outranks_the_name(tmp_path, frozen_ledger_clock):
    """A renamed or miscounted name never overrides the ledger: the row IS
    what the daemon's own reap sweep consults."""
    _, sources = make_sources(tmp_path)
    (row,) = [
        r for r in build_snapshot(
            sources, heartbeat_at=WALL, poll_interval=60, queue_depth=0,
            repos=(REPO,),
        )["sessionList"] if r["name"] == PANE_SESSION
    ]
    assert (row["repo"], row["number"], row["round"]) == (REPO, 16, 2)


# -- fitting the body to the 64 KB cap -------------------------------------


def _big_snapshot(sessions=400, inbox=400):
    return {
        "schemaVersion": 1, "seat": "revloop", "asOf": "x", "heartbeatAt": "x",
        "pollIntervalS": 60,
        "sessions": {"live": sessions, "managed": sessions},
        "sessionList": [
            {"name": f"review-widgets-pr{n}-r1-abcdef", "edge": "review",
             "repo": REPO, "number": n, "round": 1, "attempt": None,
             "ageS": 1, "cpu": 1.0, "rssBytes": 1, "live": True,
             "managed": True, "url": f"https://github.com/{REPO}/pull/{n}"}
            for n in range(sessions)
        ],
        "inbox": [
            {"kind": "cap-out", "subject": f"{REPO}#{n}", "repo": REPO,
             "number": n, "isPr": True, "ageS": 1,
             "url": f"https://github.com/{REPO}/pull/{n}",
             "lever": fleet_vitals.LEVER_REENTER}
            for n in range(inbox)
        ],
    }


def test_fit_body_trims_the_two_lists_until_the_body_fits():
    big = _big_snapshot()
    assert body_bytes(big) > BODY_MAX_BYTES
    fitted = fit_body(big)
    assert body_bytes(fitted) <= BODY_MAX_BYTES
    assert 0 < len(fitted["sessionList"]) < 400
    assert 0 < len(fitted["inbox"]) < 400
    # Whole-roster counts are untouched: they describe the fleet, not the list.
    assert fitted["sessions"] == {"live": 400, "managed": 400}
    assert big["sessionList"] and len(big["sessionList"]) == 400  # input intact


def test_fit_body_leaves_a_fitting_body_alone():
    small = _big_snapshot(sessions=3, inbox=2)
    assert fit_body(small) == small


def test_fit_body_gives_up_once_both_lists_are_empty():
    body = _big_snapshot(sessions=0, inbox=0)
    body["version"] = "v" * (BODY_MAX_BYTES + 10)
    fitted = fit_body(body)  # must terminate
    assert fitted["sessionList"] == [] and fitted["inbox"] == []


# -- the pusher: dry-run, failure, not_activated -----------------------------


class FakeSources:
    """The six console reads the builder makes, canned."""

    def __init__(self, *, roster=None):
        self.roster = [] if roster is None else roster
        self.reads = 0

    def snapshots(self, limit):
        self.reads += 1
        return [{"duration_ms": 42}]

    def drift(self):
        return {"running": "0.30.0", "latest": "0.30.0", "state": "current"}

    def session_rows(self):
        return self.roster

    def rate_limit(self):
        return None

    def memory(self):
        return {"resident": None, "reclaimable": None, "limit": None}

    def inbox(self):
        return {"live": [], "settled": [], "settled_dropped": 0,
                "truncated": False}


class FakeVitalsClient:
    def __init__(self, fail_with=None, calls=None):
        self.snapshots: list[dict] = []
        self.fail_with = fail_with
        self.calls = calls if calls is not None else []

    def post_fleet_vitals(self, snapshot):
        self.calls.append("vitals")
        if self.fail_with is not None:
            raise self.fail_with
        self.snapshots.append(snapshot)
        return {"seat": "revloop", "receivedAt": 1, "replaced": True}


def _config(tmp_path, **over):
    return Config(workspace_root=tmp_path, state_path=tmp_path / "state.db",
                  fleet_vitals_enabled=True, **over)


def test_push_once_posts_the_fitted_snapshot_and_reports_pushed(tmp_path, caplog):
    client = FakeVitalsClient()
    pusher = FleetVitalsPusher(_config(tmp_path), FakeSources(), client,
                               clock=lambda: float(WALL))
    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        assert pusher.push_once(heartbeat_at=WALL - 1, queue_depth=3) == VITALS_PUSHED
    (sent,) = client.snapshots
    assert sent["heartbeatAt"] == iso_utc(WALL - 1)
    assert sent["asOf"] == iso_utc(WALL)
    assert sent["queueDepth"] == 3
    assert sent["sessions"] == {"live": 0, "managed": 0}
    assert any("fleet-vitals: pushed" in r.getMessage() for r in caplog.records)


def test_dry_run_builds_and_describes_but_sends_nothing(tmp_path, caplog):
    client = FakeVitalsClient()
    sources = FakeSources()
    pusher = FleetVitalsPusher(_config(tmp_path, dry_run=True), sources, client)
    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        assert pusher.push_once(heartbeat_at=WALL) == VITALS_SKIPPED
    assert client.calls == []
    assert sources.reads == 1  # the snapshot IS built: the operator sees it
    (line,) = [r for r in caplog.records if "[dry-run]" in r.getMessage()]
    assert line.levelno == logging.INFO
    assert "would push fleet vitals (" in line.getMessage()


def test_a_failed_push_is_one_warning_naming_status_and_error(tmp_path, caplog):
    client = FakeVitalsClient(
        fail_with=AlissaTransient(503, {"error": "UNAVAILABLE", "message": "m"})
    )
    pusher = FleetVitalsPusher(_config(tmp_path), FakeSources(), client)
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert pusher.push_once(heartbeat_at=WALL) == VITALS_FAILED
        # A second pass fails again: one WARNING PER PASS, no latch -- an
        # outage is transient and the next snapshot is the retry.
        assert pusher.push_once(heartbeat_at=WALL) == VITALS_FAILED
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "status 503" in warnings[0].getMessage()
    assert "UNAVAILABLE" in warnings[0].getMessage()
    assert "the pass completes" in warnings[0].getMessage()


def test_a_transport_failure_warns_without_a_status(tmp_path, caplog):
    client = FakeVitalsClient(fail_with=AlissaTransient(0, "dns says no"))
    pusher = FleetVitalsPusher(_config(tmp_path), FakeSources(), client)
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert pusher.push_once(heartbeat_at=WALL) == VITALS_FAILED
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "status transport" in warning.getMessage()
    assert "dns says no" in warning.getMessage()


def test_not_activated_warns_once_per_boot_with_the_url_then_debug(
    tmp_path, caplog
):
    client = FakeVitalsClient(fail_with=AlissaAuthError(
        403,
        {"error": NOT_ACTIVATED, "appId": "loop",
         "activateUrl": "https://studio.example/activate/loop"},
        NOT_ACTIVATED,
    ))
    pusher = FleetVitalsPusher(_config(tmp_path), FakeSources(), client)
    with caplog.at_level(logging.DEBUG, logger="alissa.tools.github"):
        for _ in range(3):
            assert pusher.push_once(heartbeat_at=WALL) == VITALS_FAILED
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "https://studio.example/activate/loop" in warnings[0].getMessage()
    assert "not_activated" in warnings[0].getMessage()
    debugs = [r for r in caplog.records
              if r.levelno == logging.DEBUG and "still refused" in r.getMessage()]
    assert len(debugs) == 2
    # The push is still attempted every pass: activation lands on its own.
    assert client.calls == ["vitals"] * 3


def test_other_auth_failures_are_a_warning_per_pass_not_the_activation_path(
    tmp_path, caplog
):
    client = FakeVitalsClient(fail_with=AlissaAuthError(0, "ALISSA_API_TOKEN is not set"))
    pusher = FleetVitalsPusher(_config(tmp_path), FakeSources(), client)
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert pusher.push_once(heartbeat_at=WALL) == VITALS_FAILED
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "ALISSA_API_TOKEN is not set" in warning.getMessage()
    assert "activate" not in warning.getMessage()


def test_a_builder_failure_is_a_warning_and_no_post(tmp_path, caplog):
    class Broken(FakeSources):
        def session_rows(self):
            raise RuntimeError("boom")

    client = FakeVitalsClient()
    pusher = FleetVitalsPusher(_config(tmp_path), Broken(), client)
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert pusher.push_once(heartbeat_at=WALL) == VITALS_FAILED
    assert client.calls == []
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "snapshot build failed" in warning.getMessage()


# -- wiring into the watcher: disabled, enabled, order, ledger ----------------


class _Gh:
    login = "alissa-app"

    def review_requests(self, repos=()):
        return []


class _Al:
    def list_review_sessions(self):
        return []

    def worker_running(self):
        return True


class FakeEventsClient:
    def __init__(self, calls):
        self.calls = calls

    def post_loop_events(self, events):
        self.calls.append("events")
        return {"accepted": len(events), "duplicates": 0}


def _watcher(config):
    state = State(config.state_db)
    return ReviewWatcher(config, github=_Gh(), alissa=_Al(), state=state)


def test_disabled_by_default_builds_no_pusher_and_makes_no_call(tmp_path, caplog):
    config = Config(workspace_root=tmp_path, state_path=tmp_path / "state.db")
    assert config.fleet_vitals_enabled is False
    w = _watcher(config)
    assert w._fleet_vitals is None
    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        w.poll_once()
    assert w._push_fleet_vitals(completed_at=1.0) == VITALS_SKIPPED
    (summary,) = [r for r in caplog.records if "poll summary" in r.getMessage()]
    assert summary.getMessage().endswith("vitals: skipped")


def test_enabled_config_wires_a_pusher_over_the_endpoint(tmp_path):
    config = _config(tmp_path, alissa_endpoint="https://staging.example")
    w = _watcher(config)
    assert isinstance(w._fleet_vitals, FleetVitalsPusher)
    assert w._fleet_vitals._client.base == "https://staging.example"
    assert isinstance(w._fleet_vitals._sources, Sources)
    assert w._fleet_vitals._sources.config is config


def test_the_in_daemon_sources_run_gh_under_the_reviewer_credential(
    tmp_path, monkeypatch
):
    """The rate read is the REVIEWER identity's bucket: `gh` runs under the
    env the daemon's own GitHub client builds, everything else inherits."""
    seen = []

    def fake_run(argv, *, timeout=60, env=None, **kw):
        seen.append((argv, env))
        return "{}"

    monkeypatch.setattr(fleet_vitals, "proc_run", fake_run)

    class Gh(_Gh):
        def _env(self):
            return {"GH_TOKEN": "reviewer-token"}

    run = fleet_vitals._console_runner(Gh())
    run(["gh", "api", "rate_limit"])
    run(["alissa", "tmux", "ls", "--json"])
    assert seen == [
        (["gh", "api", "rate_limit"], {"GH_TOKEN": "reviewer-token"}),
        (["alissa", "tmux", "ls", "--json"], None),
    ]

    class Unset(_Gh):
        def _env(self):
            raise RuntimeError("reviewer_token_env is unset")

    with pytest.raises(CommandError):
        fleet_vitals._console_runner(Unset())(["gh", "api", "rate_limit"])
    assert fleet_vitals._console_runner(object())(["gh", "x"]) == "{}"


def test_one_post_per_pass_after_the_loop_events_push(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module.time, "time", lambda: STAMP)
    config = _config(tmp_path, loop_events_enabled=True)
    w = _watcher(config)
    w.state.record_reap("s1")  # something for loop events to send
    calls: list[str] = []
    w._loop_events = LoopEventsEmitter(w.state, FakeEventsClient(calls))
    vitals = FakeVitalsClient(calls=calls)
    w._fleet_vitals = FleetVitalsPusher(config, FakeSources(), vitals)

    w.poll_once()
    assert calls == ["events", "vitals"]
    w.poll_once()
    # Loop events had nothing new; vitals go every pass regardless.
    assert calls == ["events", "vitals", "vitals"]
    assert len(vitals.snapshots) == 2
    assert vitals.snapshots[-1]["pollIntervalS"] == config.poll_interval


def test_a_failed_push_does_not_stop_the_pass_or_the_ledger(tmp_path, caplog):
    config = _config(tmp_path)
    w = _watcher(config)
    w._fleet_vitals = FleetVitalsPusher(
        config, FakeSources(),
        FakeVitalsClient(fail_with=AlissaTransient(502, "bad gateway")),
    )
    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        results = w.poll_once()
    assert results == []
    assert len(w.state.read_snapshots()) == 1  # the pass's exhaust landed
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "fleet-vitals: push failed" in warnings[0].getMessage()
    (summary,) = [r for r in caplog.records if "poll summary" in r.getMessage()]
    assert summary.getMessage().endswith("vitals: failed")


def test_dry_run_pass_logs_and_sends_nothing(tmp_path, caplog):
    config = _config(tmp_path, dry_run=True)
    w = _watcher(config)
    client = FakeVitalsClient()
    w._fleet_vitals = FleetVitalsPusher(config, FakeSources(), client)
    with caplog.at_level(logging.INFO, logger="alissa.tools.github"):
        w.poll_once()
    assert client.calls == []
    assert any("[dry-run] would push fleet vitals" in r.getMessage()
               for r in caplog.records)
    (summary,) = [r for r in caplog.records if "poll summary" in r.getMessage()]
    assert summary.getMessage().endswith("vitals: skipped")


def test_the_queue_depth_is_the_spawn_gates_waiting_set(tmp_path):
    from alissa.tools.github.revloop.loop import Waiting

    config = _config(tmp_path)
    w = _watcher(config)
    client = FakeVitalsClient()
    w._fleet_vitals = FleetVitalsPusher(config, FakeSources(), client)
    w._waiting = {(REPO, 1): Waiting(seq=1, since=0.0),
                  (REPO, 2): Waiting(seq=2, since=0.0)}
    assert w._push_fleet_vitals(completed_at=float(WALL)) == VITALS_PUSHED
    (sent,) = client.snapshots
    assert sent["queueDepth"] == 2
    assert sent["heartbeatAt"] == iso_utc(WALL)


def test_a_pusher_defect_cannot_take_down_the_pass(tmp_path, caplog):
    config = _config(tmp_path)
    w = _watcher(config)

    class Broken:
        def push_once(self, **kw):
            raise RuntimeError("defect")

    w._fleet_vitals = Broken()
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert w._push_fleet_vitals(completed_at=1.0) == VITALS_FAILED
    assert any("best-effort" in r.getMessage() for r in caplog.records)


# -- the client method --------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, fn):
        self._fn = fn

    def open(self, req, timeout=None):
        return self._fn(req, timeout=timeout)


def test_post_fleet_vitals_sends_the_body_verbatim(monkeypatch):
    seen = {}

    def fake_open(req, timeout=None):
        seen["req"] = req
        return FakeResponse(b'{"seat": "revloop", "receivedAt": 1, "replaced": false}')

    monkeypatch.setattr(client_module, "_opener", FakeOpener(fake_open))
    client = AlissaClient(token="tok", base="https://api.example/")
    body = {"schemaVersion": 1, "seat": "revloop", "kpis": None}
    assert client.post_fleet_vitals(body) == {
        "seat": "revloop", "receivedAt": 1, "replaced": False,
    }
    req = seen["req"]
    assert req.full_url == "https://api.example/v1/loop/fleet-vitals"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tok"
    assert json.loads(req.data.decode()) == body


def test_post_fleet_vitals_classifies_a_not_activated_403(monkeypatch):
    import io
    import urllib.error

    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", None,
            io.BytesIO(b'{"error": "not_activated", "appId": "loop", '
                       b'"activateUrl": "https://s/activate"}'),
        )

    monkeypatch.setattr(client_module, "_opener", FakeOpener(fake_open))
    with pytest.raises(AlissaAuthError) as info:
        AlissaClient(token="t").post_fleet_vitals({})
    assert info.value.code == NOT_ACTIVATED
    assert info.value.detail["activateUrl"] == "https://s/activate"


# -- config key, CLI flags, env var: the shared boolean rail ------------------


def test_config_default_is_off(tmp_path):
    config = Config.build(tmp_path, environ={})
    assert config.fleet_vitals_enabled is False
    assert "fleet_vitals_enabled" in config_module.CONFIG_KEYS


def test_the_file_enables_and_the_cli_overrides_both_ways(tmp_path):
    args = build_parser().parse_args(["--no-fleet-vitals"])
    config = Config.build(
        tmp_path, {"fleet_vitals_enabled": True}, overrides_from(args),
        environ={},
    )
    assert config.fleet_vitals_enabled is False
    args = build_parser().parse_args(["--fleet-vitals"])
    config = Config.build(tmp_path, {}, overrides_from(args), environ={})
    assert config.fleet_vitals_enabled is True


def test_the_env_var_outranks_the_file_and_the_cli(tmp_path):
    args = build_parser().parse_args(["--no-fleet-vitals"])
    config = Config.build(
        tmp_path, {"fleet_vitals_enabled": False}, overrides_from(args),
        environ={FLEET_VITALS_ENV: "1"},
    )
    assert config.fleet_vitals_enabled is True
    config = Config.build(
        tmp_path, {"fleet_vitals_enabled": True}, None,
        environ={FLEET_VITALS_ENV: "off"},
    )
    assert config.fleet_vitals_enabled is False


def test_the_two_rails_are_independent(tmp_path):
    config = Config.build(
        tmp_path, environ={FLEET_VITALS_ENV: "1", LOOP_EVENTS_ENV: "0"},
    )
    assert (config.fleet_vitals_enabled, config.loop_events_enabled) == (True, False)


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("No", False), ("OFF", False),
])
def test_env_var_boolean_spellings_match_loop_events(tmp_path, raw, expected):
    """One reader, two rails: every spelling the loop-events variable takes,
    the fleet-vitals one takes, and reads the same."""
    assert env_fleet_vitals_enabled({FLEET_VITALS_ENV: raw}) is expected
    assert env_loop_events_enabled({LOOP_EVENTS_ENV: raw}) is expected
    config = Config.build(tmp_path, environ={FLEET_VITALS_ENV: raw})
    assert config.fleet_vitals_enabled is expected


def test_blank_and_unset_fall_through(tmp_path):
    assert env_fleet_vitals_enabled({}) is None
    assert env_fleet_vitals_enabled({FLEET_VITALS_ENV: "  "}) is None
    config = Config.build(
        tmp_path, {"fleet_vitals_enabled": True},
        environ={FLEET_VITALS_ENV: ""},
    )
    assert config.fleet_vitals_enabled is True


def test_a_non_boolean_env_var_is_refused_by_name(tmp_path):
    with pytest.raises(ValueError, match=FLEET_VITALS_ENV):
        Config.build(tmp_path, environ={FLEET_VITALS_ENV: "enable"})
    # ...and the sibling rail names ITSELF, not this one: the shared reader
    # is parameterised by the variable, never a copy with a pinned name.
    with pytest.raises(ValueError, match=LOOP_EVENTS_ENV):
        env_loop_events_enabled({LOOP_EVENTS_ENV: "enable"})


def test_both_readers_share_one_implementation(monkeypatch):
    seen = []

    def spy(name, environ=None):
        seen.append(name)
        return None

    monkeypatch.setattr(config_module, "_env_bool", spy)
    env_loop_events_enabled({})
    env_fleet_vitals_enabled({})
    assert seen == [LOOP_EVENTS_ENV, FLEET_VITALS_ENV]


def test_the_container_reads_the_same_variable_name():
    """The env var the Dockerfile bakes and the renderer validates is the one
    the library reads -- a rename on either side fails here."""
    assert FLEET_VITALS_ENV == "ALISSA_REV_FLEET_VITALS_ENABLED"
    docker = Path(__file__).resolve().parents[7] / "docker" / "claude"
    if not docker.is_dir():  # tested from a wheel/sdist: nothing to pin
        pytest.skip("docker/claude not present in this checkout")
    dockerfile = (docker / "Dockerfile").read_text()
    assert f'ARG {FLEET_VITALS_ENV}=""' in dockerfile
    assert f"{FLEET_VITALS_ENV}=${{{FLEET_VITALS_ENV}}}" in dockerfile
    renderer = (docker / "revloop-config.sh").read_text()
    assert FLEET_VITALS_ENV in renderer and "fleet_vitals_enabled" in renderer


# -- the console builders the snapshot leans on --------------------------------


def test_cgroup_memory_limit_reads_memory_max(tmp_path):
    cg = _write_cgroup(tmp_path / "cg")
    assert sysinfo.cgroup_memory_limit(cg) == 4294967296
    (cg / "memory.max").write_text("max\n")
    assert sysinfo.cgroup_memory_limit(cg) is None
    assert sysinfo.cgroup_memory_limit(tmp_path / "nope") is None
    # The dashboard tile's payload is untouched: no new key.
    assert "limit" not in sysinfo.cgroup_memory(cg)


def test_sources_memory_carries_the_limit(tmp_path, frozen_ledger_clock):
    _, sources = make_sources(tmp_path, cgroup=_write_cgroup(tmp_path / "cg"))
    memory = sources.memory()
    assert memory["limit"] == 4294967296
    assert memory["resident"] == 1000 and memory["reclaimable"] == 6500


def test_session_rows_is_none_when_tmux_cannot_list_but_sessions_is_empty(
    tmp_path, frozen_ledger_clock
):
    _, sources = make_sources(tmp_path, runner=_runner(tmux_fails=True))
    assert sources.session_rows() is None
    assert sources.sessions() == []

    def not_a_list(argv, **kw):
        if argv[:3] == ["alissa", "tmux", "ls"]:
            return '{"oops": 1}'
        raise CommandError(argv, 1, "x")

    _, sources = make_sources(tmp_path / "b", runner=not_a_list)
    assert sources.session_rows() is None


def test_sources_inbox_matches_the_dashboards_inbox(tmp_path, frozen_ledger_clock):
    _, sources = make_sources(tmp_path)
    inbox = sources.inbox()
    dashboard = sources.dashboard()
    assert inbox["live"] == dashboard["inbox"]
    assert inbox["settled"] == dashboard["inbox_settled"]
    assert inbox["truncated"] == dashboard["inbox_truncated"]
    assert [(i["kind"], i["number"]) for i in inbox["live"]] == [
        ("cap-out", 12), ("stalled", 16),
    ]
    assert [(i["kind"], i["number"]) for i in inbox["settled"]] == [
        ("stability-held", 21),
    ]
