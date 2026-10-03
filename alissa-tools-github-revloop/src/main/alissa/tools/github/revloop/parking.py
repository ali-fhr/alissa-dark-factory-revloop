"""Seat parking -- the reviewer seat's own idle verdict and the drain it
enforces (studio.alissa.app docs/design/managed-dark-factory-seat-parking.md
§2.1, §2.2, §2.4; issue #157, lane L5 -- devloop's L4 contract, for rounds).

orcloop parks this seat (removes its Railway deployment) only when the seat
itself says there is nothing to do, and wakes it on demand. This module holds
the revloop half of that contract, shared by the daemon (which records the
verdict per pass on its poll snapshot and pushes it with the fleet vitals) and
the console (which serves it on ``GET /api/state`` and runs the drain
handshake):

* **The idle block.** ``{asOf, passAt, owed, live, managed, queued, timers,
  drainedAt}``. `owed` counts the PRs whose LAST evaluation was an action or
  a clock the seat owns: an owed round (spawned, queued for a slot, held for
  the head's checks, held by the drain), a round in flight inside its stale
  window, a verdict still to be posted natively, an escalation posted this
  pass, the post-verdict cooldown, an evaluation that failed. A PR the seat
  has settled -- converged, capped, already stability-held, waiting on a
  fresh re-request, out of scope -- is not owed. Each pass records the
  verdict per PR on its poll snapshot's stage (`owed`, `timer`), so the block
  is read back from the snapshot and costs no GitHub call. `live` / `managed`
  / `queued` are read from tmux and the alissa queue by whoever builds the
  block.
* **Unknown is never idle.** No snapshot yet -> `owed` is null; a listing
  that failed -> `live` / `managed` / `queued` are null. `idle_refusals`
  reads every null as a refusal.
* **The drain.** A flag with a TTL in `state.db` (`State.set_drain`), cleared
  when the daemon boots. While it holds, the round spawn stands down before
  its first side effect (`ReviewWatcher._drain_gated`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone

# The timer kinds of the design's idle block (§2.1), the union both seats
# share. revloop emits two: `stale_window` (a round in flight inside its stale
# window, the post-verdict cooldown, a native post's grace or back-off) and
# `checks_hold` (a round, or an approve, waiting on the head's CI rollup). A
# stability hold carries no clock -- it waits on an operator's ack, a comment
# that wakes a parked seat by itself -- so `stability_hold` is never emitted.
TIMER_STALE_WINDOW = "stale_window"
TIMER_CHECKS_HOLD = "checks_hold"
TIMER_KINDS = (
    TIMER_STALE_WINDOW, "respawn", "limited_hold", "stability_hold",
    TIMER_CHECKS_HOLD,
)

# The drain's TTL: the design's handshake asks for 300 s. The ceiling bounds a
# drain nobody undrains -- a seat that refuses every spawn is an outage, so no
# caller may hold it for longer than this without asking again.
DRAIN_TTL_DEFAULT = 300
DRAIN_TTL_MAX = 900
DRAIN_REASON_MAX = 200

# A spawn claim older than this is a spawn whose daemon died between its drain
# gate and its ledger write; it stops refusing drains (the boot clears it).
SPAWN_CLAIM_TTL = 600

# D2's freshness rule: the seat's last pass is within this many poll
# intervals, or its loop is wedged -- stalled, not idle.
FRESH_PASS_INTERVALS = 2

# The stages that are owed when the stage carries no verdict of its own (a
# snapshot written before the field existed): every action but a convergence,
# a cap or a skip is the seat's own work in progress. The refined stage names
# `_stage_record` writes (`stale-re-enqueued`, `deferred`, `checks-held`,
# `prompt-held`, `drained`) are refinements of owed actions, so they are
# owed too.
OWED_STAGES = frozenset({
    "spawned", "stale-re-enqueued", "in-flight", "deferred", "queued",
    "checks-held", "prompt-held", "drained", "awaiting-post", "posted",
    "abandoned", "escalated",
})


def iso(ts: "float | int | None") -> "str | None":
    """A unix timestamp as an ISO-8601 UTC instant (whole seconds, `Z`)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def stage_owed(stage: Mapping) -> bool:
    """Whether one poll-snapshot stage is owed: its own `owed` verdict, or,
    on a stage written before the verdict existed, its stage's default."""
    owed = stage.get("owed")
    if isinstance(owed, bool):
        return owed
    return str(stage.get("stage") or "") in OWED_STAGES


def stage_timer(stage: Mapping) -> "dict | None":
    """One stage's timer as the block's `{kind, until, ref}`, or None. `ref`
    is the PR's slug (`owner/repo#16`)."""
    timer = stage.get("timer")
    if not isinstance(timer, Mapping):
        return None
    kind = timer.get("kind")
    until = timer.get("until")
    if (
        kind not in TIMER_KINDS
        or isinstance(until, bool)
        or not isinstance(until, (int, float))
    ):
        return None
    return {"kind": kind, "until": iso(until), "ref": stage.get("slug")}


def queued_items(queues: object) -> "int | None":
    """How many items wait in the alissa tmux queue (`alissa tmux queue ls
    --json`, every queue) and have not been dispatched yet -- a dispatched
    item stays listed with its `lastDispatchedAt`, and its session is then
    in the tmux listing instead. None when the listing is not a list (it
    could not be read: unknown, not empty)."""
    if not isinstance(queues, list):
        return None
    count = 0
    for queue in queues:
        if not isinstance(queue, Mapping):
            continue
        for item in queue.get("items") or ():
            if isinstance(item, Mapping) and not item.get("lastDispatchedAt"):
                count += 1
    return count


def roster_counts(
    rows: "Sequence[Mapping] | None",
) -> "tuple[int | None, int | None]":
    """`(live, managed)` from the console's session rows
    (`Sources.session_rows`) -- the fleet-vitals `sessions` rule, so the idle
    block and the snapshot's `sessions` can never disagree. `(None, None)`
    when the roster could not be listed."""
    if rows is None:
        return None, None
    return (
        sum(1 for r in rows if r.get("live")),
        sum(1 for r in rows if r.get("managed")),
    )


def idle_block(
    snapshot: "Mapping | None",
    *,
    live: "int | None",
    managed: "int | None",
    queued: "int | None",
    drain: "Mapping | None",
    now: "float | int",
) -> dict:
    """The seat's idle block from its latest poll snapshot (None before the
    first pass) and the caller's live readings. `drain` is the holding drain
    row (`State.drain`) or None."""
    stages: "Iterable[Mapping]" = (snapshot or {}).get("stages") or ()
    owed: "int | None" = None
    timers: "list[dict]" = []
    if snapshot is not None:
        owed = 0
        for stage in stages:
            if stage_owed(stage):
                owed += 1
            timer = stage_timer(stage)
            if timer is not None:
                timers.append(timer)
    timers.sort(key=lambda t: (t["until"] or "", t["ref"] or ""))
    return {
        "asOf": iso(now),
        "passAt": iso(snapshot.get("ts")) if snapshot else None,
        "owed": owed,
        "live": live,
        "managed": managed,
        "queued": queued,
        "timers": timers,
        "drainedAt": iso(drain.get("drained_at")) if drain else None,
    }


def _parse_iso(value: object) -> "float | None":
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return None


def idle_refusals(
    idle: Mapping, *, poll_interval: "float | int", now: "float | int",
) -> "list[str]":
    """D2's invariant over one idle block: the reasons the seat is NOT idle,
    empty when it is. Every unknown (a null count, a missing pass) is a
    refusal -- unknown is not idle."""
    out: "list[str]" = []
    for key in ("live", "managed", "queued"):
        value = idle.get(key)
        if value is None:
            out.append(f"{key} unknown")
        elif value:
            out.append(f"{key}={value}")
    owed = idle.get("owed")
    if owed is None:
        out.append("owed unknown (no completed pass)")
    elif owed:
        out.append(f"owed={owed}")
    timers = idle.get("timers") or []
    if timers:
        out.append(f"{len(timers)} timer(s) running")
    pass_at = _parse_iso(idle.get("passAt"))
    if pass_at is None:
        out.append("no completed pass")
    elif now - pass_at > FRESH_PASS_INTERVALS * float(poll_interval):
        out.append(
            f"last pass {int(now - pass_at)}s ago (over "
            f"{FRESH_PASS_INTERVALS} × {int(poll_interval)}s)"
        )
    return out
