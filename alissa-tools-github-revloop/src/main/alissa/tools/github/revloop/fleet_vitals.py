"""Fleet vitals: one snapshot of this daemon's live state per completed poll
pass, pushed to Studio's `POST /v1/loop/fleet-vitals` (issue #126).

The Factory's `/fleet` screen PULLS each daemon console's `/api/state` over
private networking, with passcodes held on the hosted Factory -- which is
per-deployment by construction, so a Dark Factory customer running their own
revloop sees `not_configured`. Studio (TASK-182095034) now also accepts one
PUSHED snapshot per user×seat, replaced on every POST and rendered whenever
the Factory has no console URL for the seat. This module is the revloop half
of that: opt-in (`fleet_vitals_enabled` / `ALISSA_REV_FLEET_VITALS_ENABLED`),
in the same posture as loop events -- best-effort, never fatal, honours
dry-run -- and pushed at the end of every completed pass, AFTER the
loop-events batch.

Design rules, all load-bearing:

* **Built in-process from the console's own builders.** The snapshot is the
  reviewer console's data layer (`webui.sources.Sources` -- the LIVE half of
  the inbox, the session roster, the cached rate and drift reads -- and
  `webui.sysinfo` for the cgroup memory split) mapped onto the wire
  vocabulary, so the Factory card and the console can never disagree about
  what the operator owes. The console SIDECAR is not required to be running:
  the daemon holds its own `Sources` over its own config.

* **Built to the contract EXACTLY.** The API is strict -- an unknown key is a
  400, `sessionList` and `inbox` cap at 50 entries, the whole body at 64 KB --
  so the builder emits only the contract's keys, caps every list here, and
  trims the two lists until the body fits (`fit_body`). A snapshot the API
  refuses is worth nothing, so fitting is the builder's job, not the API's.

* **`null` means "could not read", never `{0, 0}`.** A roster `alissa tmux
  ls` could not list is `sessions: null` / `sessionList: null`; a memory
  split off a host without cgroup v2 is `memory: null`. A Factory card that
  read "0 live" over a broken tmux would be the reassuring-direction error,
  and every other read here follows the same rule.

* **One push, no retry queue.** The API keeps only the latest snapshot per
  seat, so a failed push is simply replaced by the next pass's -- there is
  nothing to re-send. A failure is ONE WARNING naming the status and error;
  the pass completes. The one refinement: a `403 not_activated` (the token's
  user has not activated the loop app) warns ONCE per boot with the activate
  URL and logs at DEBUG after, because it is a fixed condition the operator
  resolves out of band, and the next pass after activation lands on its own.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from .alissa import parse_session_name, session_repo_slug
from .alissa_client import (
    NOT_ACTIVATED,
    AlissaAuthError,
    AlissaClient,
    AlissaError,
)
from .config import Config
from .proc import CommandError, run as proc_run

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .webui.sources import Sources

log = logging.getLogger(__name__)

SEAT = "revloop"
SCHEMA_VERSION = 1
# The edge this daemon's sessions run: the Factory's vocabulary for "a
# reviewer round" (devloop's is `develop`).
EDGE = "review"
# The API's per-list cap (`sessionList`, `inbox`) and its body cap. Mirrors
# Studio's FLEET_VITALS_LIST_CAP / FLEET_VITALS_BODY_MAX; both are the wire
# contract, so the builder enforces them before sending rather than learning
# them from a 400.
LIST_CAP = 50
BODY_MAX_BYTES = 64 * 1024
# How many recent pass durations ride along -- the console's sparkline
# window (`sources.SPARK_POINTS`), restated here because this module cannot
# import `sources` at module level (sources imports loop, loop imports this).
DURATION_POINTS = 60

# The three outcomes the poll summary reports.
VITALS_PUSHED = "pushed"
VITALS_SKIPPED = "skipped"
VITALS_FAILED = "failed"

# The operator's lever per inbox kind, in the Factory's `lever` slot. The
# cap-out and the stability hold are both lifted by the same re-entry ack;
# a stalled episode is the console's retry-now (or a reap).
LEVER_REENTER = "comment `alissa-review: re-enter +N` on the PR"
LEVER_STALLED = "retry-now in the reviewer console, or reap the session"
_LEVERS = {
    "cap-out": LEVER_REENTER,
    "stability-held": LEVER_REENTER,
    "stalled": LEVER_STALLED,
}


def iso_utc(seconds: "int | float") -> str:
    """An epoch stamp as the contract's ISO-8601 UTC string (whole seconds,
    `Z` suffix)."""
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _int_or_none(value: object) -> "int | None":
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _resolve_repo(slug: "str | None", repos: "tuple[str, ...]") -> "str | None":
    """The `owner/repo` a session name's repo component denotes, or None.

    A session name carries the sanitized REPO component only (no owner), so
    it resolves through the allowlist exactly the way the sweep's own name
    resolution does: the one `owner/repo` whose slug matches. Two matches --
    two owners with the same repo name -- is no answer, not a guess."""
    if not slug:
        return None
    matches = [
        full for full in repos
        if session_repo_slug(full.split("/", 1)[-1]) == slug
    ]
    return matches[0] if len(matches) == 1 else None


def _session_row(row: dict, repos: "tuple[str, ...]") -> dict:
    """One console session row -> one contract `sessionList` entry.

    The PR and round come from the spawn ledger when the console paired the
    session with a row (the authoritative record); a session with no ledger
    row -- a hand-spawned `review-pr-<n>` from the skill, or one whose ledger
    was lost -- falls back to what its NAME says (`parse_session_name`), the
    same grammar the reaper trusts. `attempt` is always null: the review
    loop is round-based and has no attempt dimension."""
    name = row.get("name")
    ref = parse_session_name(name)
    repo: "str | None" = None
    number: "int | None" = None
    round_: "int | None" = None
    pr = row.get("pr")
    if isinstance(pr, str) and "#" in pr:
        head, _, tail = pr.rpartition("#")
        if tail.isdigit():
            repo, number = head, int(tail)
    round_ = _int_or_none(row.get("round"))
    if ref is not None:
        if number is None:
            number = ref.number
        if round_ is None:
            round_ = ref.round
        if repo is None:
            repo = _resolve_repo(ref.repo, repos)
    url = (
        f"https://github.com/{repo}/pull/{number}"
        if repo and number is not None
        else None
    )
    cpu = row.get("cpu_percent")
    return {
        "name": str(name) if name is not None else "",
        "edge": EDGE,
        "repo": repo,
        "number": number,
        "round": round_,
        "attempt": None,
        "ageS": _int_or_none(row.get("age_seconds")),
        "cpu": float(cpu) if isinstance(cpu, (int, float)) else None,
        "rssBytes": _int_or_none(row.get("rss_bytes")),
        "live": bool(row.get("live")),
        "managed": bool(row.get("managed")),
        "url": url,
    }


def _inbox_row(item: dict) -> dict:
    """One console inbox item (the LIVE half) -> one contract `inbox` entry.
    Every reviewer page is a PR reference, so `isPr` is always true."""
    kind = str(item.get("kind"))
    repo = item.get("repo_slug")
    number = _int_or_none(item.get("number"))
    return {
        "kind": kind,
        "subject": (
            f"{repo}#{number}" if repo and number is not None else str(repo or "")
        ),
        "repo": str(repo) if repo else None,
        "number": number,
        "isPr": True,
        "ageS": _int_or_none(item.get("age_seconds")),
        "url": item.get("url") or None,
        "lever": _LEVERS.get(kind),
    }


def _rate(raw: "dict | None") -> "dict | None":
    """The console's cached `gh api rate_limit` read in the contract's shape,
    or None when the read failed or came back without the two numbers the
    Factory meter needs."""
    if not isinstance(raw, dict):
        return None
    remaining = _int_or_none(raw.get("remaining"))
    limit = _int_or_none(raw.get("limit"))
    if remaining is None or limit is None:
        return None
    reset = _int_or_none(raw.get("reset"))
    return {
        "remaining": remaining,
        "limit": limit,
        "resetAt": iso_utc(reset) if reset is not None else None,
    }


def _memory(raw: "dict | None") -> "dict | None":
    """The cgroup split in the contract's shape. `resident` is the one field
    the Factory cannot draw without, so its absence (no cgroup v2) makes the
    whole object null rather than a meter over nothing."""
    if not isinstance(raw, dict):
        return None
    resident = _int_or_none(raw.get("resident"))
    if resident is None:
        return None
    return {
        "residentBytes": resident,
        "reclaimableBytes": _int_or_none(raw.get("reclaimable")),
        "limitBytes": _int_or_none(raw.get("limit")),
    }


def build_snapshot(
    sources: "Sources",
    *,
    heartbeat_at: "int | float",
    poll_interval: int,
    queue_depth: "int | None",
    repos: "tuple[str, ...]" = (),
    now: "int | float | None" = None,
) -> dict:
    """The contract body for this pass, off the console's builders.

    `heartbeat_at` is the completion time of the pass that produced the
    snapshot (the Factory derives `polling` / `stalled` from its age against
    `poll_interval`); `queue_depth` is the watcher's count of owed rounds
    waiting on the spawn gate, or None when the caller cannot say. Every
    optional field degrades to null on its own -- a broken tmux does not
    blank the memory split, and vice versa -- and `kpis` is always null (an
    orcloop-only section).
    """
    stamp = time.time() if now is None else now
    snaps = sources.snapshots(DURATION_POINTS)
    durations = [
        int(s["duration_ms"]) for s in reversed(snaps)
        if _int_or_none(s.get("duration_ms")) is not None
    ]
    drift = sources.drift()
    rows = sources.session_rows()
    sessions: "dict | None"
    session_list: "list[dict] | None"
    if rows is None:
        sessions = session_list = None
    else:
        sessions = {
            "live": sum(1 for r in rows if r.get("live")),
            "managed": sum(1 for r in rows if r.get("managed")),
        }
        # Live, own-grammar sessions first, so the cap drops the rows an
        # operator cares least about (a gone session, another lane's).
        ordered = sorted(
            rows, key=lambda r: (not r.get("live"), not r.get("managed"))
        )
        session_list = [_session_row(r, repos) for r in ordered[:LIST_CAP]]
    inbox = [_inbox_row(item) for item in sources.inbox()["live"][:LIST_CAP]]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "seat": SEAT,
        "asOf": iso_utc(stamp),
        "heartbeatAt": iso_utc(heartbeat_at),
        "pollIntervalS": int(poll_interval),
        "version": str(drift.get("running") or ""),
        "drift": (
            str(drift["latest"])
            if drift.get("state") == "behind" and drift.get("latest")
            else None
        ),
        "pollDurationsMs": durations or None,
        "sessions": sessions,
        "sessionList": session_list,
        "rate": _rate(sources.rate_limit()),
        "memory": _memory(sources.memory()),
        "queueDepth": _int_or_none(queue_depth),
        "kpis": None,
        "inbox": inbox,
    }


def body_bytes(snapshot: dict) -> int:
    """The wire size of `snapshot`, encoded exactly as the client sends it."""
    return len(json.dumps(snapshot).encode("utf-8"))


def fit_body(snapshot: dict) -> dict:
    """`snapshot`, with `sessionList` and `inbox` trimmed until the encoded
    body is within BODY_MAX_BYTES. The longer list is halved first, so the
    two shrink together rather than one vanishing while the other stays
    whole; the counts in `sessions` are left as the whole roster's, because
    they describe the fleet, not the list. Everything else is bounded by
    construction and is never touched."""
    body = dict(snapshot)
    while body_bytes(body) > BODY_MAX_BYTES:
        lists = [
            key for key in ("sessionList", "inbox")
            if isinstance(body.get(key), list) and body[key]
        ]
        if not lists:
            break
        key = max(lists, key=lambda k: len(body[k]))
        body[key] = body[key][: len(body[key]) // 2]
    return body


def describe(snapshot: dict) -> str:
    """The one-line summary the log carries for a snapshot."""
    sessions = snapshot.get("sessions")
    roster = (
        f"sessions={sessions.get('live')}/{sessions.get('managed')} live/managed"
        if isinstance(sessions, dict)
        else "sessions=unlistable"
    )
    inbox = snapshot.get("inbox")
    return (
        f"{roster}, inbox={len(inbox) if isinstance(inbox, list) else 0}, "
        f"queue={snapshot.get('queueDepth')}, {body_bytes(snapshot)} bytes"
    )


class FleetVitalsPusher:
    """The once-per-pass push, best-effort.

    Owned by the watcher when `fleet_vitals_enabled` is on; `push_once` is
    called at the end of every completed pass, after the loop-events batch,
    and NEVER raises for an API or transport condition -- one WARNING, the
    pass completes, and the next pass's snapshot is the whole retry story.
    """

    def __init__(
        self,
        config: Config,
        sources: "Sources",
        client: AlissaClient,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self._sources = sources
        self._client = client
        self._clock = clock
        # Latched by the first `403 not_activated`: the operator resolves it
        # out of band, so one WARNING per boot names the activate URL and
        # every later refusal is a DEBUG line. The push itself is still
        # attempted every pass -- the pass after activation lands on its own.
        self._not_activated_warned = False

    def build(self, *, heartbeat_at: "int | float", queue_depth: "int | None") -> dict:
        """This pass's contract body, fitted to the API's caps."""
        return fit_body(build_snapshot(
            self._sources,
            heartbeat_at=heartbeat_at,
            poll_interval=self.config.poll_interval,
            queue_depth=queue_depth,
            repos=self.config.repos,
            now=self._clock(),
        ))

    def push_once(
        self, *, heartbeat_at: "int | float", queue_depth: "int | None" = None
    ) -> str:
        """Build and push this pass's snapshot. Returns one of VITALS_PUSHED,
        VITALS_SKIPPED (dry-run: logged, nothing sent) or VITALS_FAILED.

        A builder failure is a console read gone wrong -- the same
        best-effort classification the loop-events derivation has: warn and
        let the pass complete. In dry-run the snapshot is still BUILT (reads
        only: the ledger, tmux, /proc, the two cached checks) and described,
        so the operator sees what would be sent; the POST is the act dry-run
        suppresses.
        """
        try:
            snapshot = self.build(heartbeat_at=heartbeat_at, queue_depth=queue_depth)
        except Exception as exc:
            log.warning(
                "fleet-vitals: snapshot build failed (%s: %s) — skipping this "
                "pass's vitals; the loop keeps polling",
                type(exc).__name__, exc,
            )
            return VITALS_FAILED
        if self.config.dry_run:
            log.info("[dry-run] would push fleet vitals (%s)", describe(snapshot))
            return VITALS_SKIPPED
        try:
            result = self._client.post_fleet_vitals(snapshot)
        except AlissaAuthError as exc:
            if exc.code == NOT_ACTIVATED:
                self._note_not_activated(exc)
                return VITALS_FAILED
            self._warn_failed(exc)
            return VITALS_FAILED
        except AlissaError as exc:
            self._warn_failed(exc)
            return VITALS_FAILED
        log.info(
            "fleet-vitals: pushed (%s; replaced=%s)",
            describe(snapshot), result.get("replaced"),
        )
        return VITALS_PUSHED

    @staticmethod
    def _warn_failed(exc: AlissaError) -> None:
        # ONE warn per failed pass, naming the status and the error; the API
        # keeps only the latest snapshot, so the next pass's push IS the
        # retry and nothing is queued.
        log.warning(
            "fleet-vitals: push failed (status %s%s: %s) — vitals are "
            "best-effort, the pass completes; the next pass sends a fresh "
            "snapshot",
            exc.status or "transport",
            f" {exc.code}" if exc.code else "",
            exc.detail,
        )

    def _note_not_activated(self, exc: AlissaAuthError) -> None:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        activate = detail.get("activateUrl") or "the activate URL in the API's response"
        if self._not_activated_warned:
            log.debug(
                "fleet-vitals: still refused — the loop app is not activated "
                "for this token's user (403 not_activated); activate at %s",
                activate,
            )
            return
        self._not_activated_warned = True
        log.warning(
            "fleet-vitals: Studio refused the push — the loop app is not "
            "activated for this token's user (HTTP 403 not_activated). "
            "Activate it at %s and the next pass lands on its own. Reported "
            "once per boot; later refusals log at DEBUG",
            activate,
        )


def _console_runner(github: object) -> Callable[..., str]:
    """The subprocess runner the in-daemon `Sources` uses: `proc.run`, with
    every `gh` call placed under the REVIEWER identity's credential the way
    the daemon's own GitHub client does (`GitHub._env`), so the rate read is
    the reviewer's bucket and not whatever the container inherited. A
    reviewer token the client cannot resolve surfaces as a CommandError,
    which `Sources` degrades to a null rate -- honest, rather than a read
    under the wrong login."""
    env_of = getattr(github, "_env", None)

    def run(argv: "list[str]", *, timeout: int = 60, **kwargs: Any) -> str:
        env = None
        if argv and argv[0] == "gh" and callable(env_of):
            try:
                env = env_of()
            except Exception as exc:
                raise CommandError(argv, -1, str(exc)) from None
        return proc_run(argv, timeout=timeout, env=env, **kwargs)

    return run


def build_pusher(
    config: Config,
    *,
    github: object = None,
    endpoint: "str | None" = None,
) -> FleetVitalsPusher:
    """The pusher the watcher wires in when `fleet_vitals_enabled` is on.

    A seam, so tests build pushers over fake sources and clients while the
    watcher's call stays one line. The `Sources` is the console's own data
    layer over the daemon's config -- the sidecar need not be running -- and
    the token comes from the environment inside `AlissaClient` (the CLI's
    own `ALISSA_API_TOKEN`); its absence surfaces as the pusher's WARN, never
    at construction, because a daemon must boot and poll whether or not
    vitals can authenticate. The import is local because `sources` imports
    `loop`, which imports this module.
    """
    from .version import version
    from .webui.sources import Sources

    sources = Sources(
        config=config,
        running_version=version.value,
        run=_console_runner(github),
    )
    return FleetVitalsPusher(config, sources, AlissaClient(base=endpoint))
