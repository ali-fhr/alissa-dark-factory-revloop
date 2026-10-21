"""Self-upgrade -- the revloop half of fleet auto-upgrade (MDF M6, AU-6,
issue #193; Studio's contract: studio.alissa.app docs/design/
fleet-auto-upgrade.md, AU-1). A port of devloop's module (AU-4, devloop
issue #206) and orcloop's (AU-5): the three seats behave the same way, and
only what makes a seat busy differs (here: a live reviewer session).

Every ``POST /v1/loop/fleet-vitals`` answer carries an ``upgrade`` block::

    {"mode": "managed" | "self_hosted",
     "target": {"version", "digest"} | null,
     "autoUpgrade": bool, "paused": bool, "drain": bool}

This module reads it and acts on it in four steps:

1. **Read.** `parse_upgrade` turns the answer into an `UpgradeAnswer`, or
   None when the block is absent or malformed (an older Studio). The
   `SelfUpgrader` keeps the last good answer in memory. An answer without
   the block leaves the last one in place.
2. **Drain.** `SelfUpgrader.hold` names why no new reviewer session may start:
   ``drain`` is true (either mode), or a self-hosted seat has a target to
   move to. A seat has a target to move to when `autoUpgrade` is on, the
   pause switch is off, `target.version` is strictly newer than the running
   version (numeric ``X.Y.Z``; an older or equal pin, or a version that does
   not parse, is never a target), and that version is not in the skip
   list. The loop reads the hold in its seat-drain gate
   (`ReviewWatcher._claim_spawn`), so a held round is the same QUEUED
   ``drained`` stage a parking drain gives. Running sessions are never
   touched. One INFO line per change of state, not per poll.
3. **Exit at idle.** Self-hosted only (a managed seat is swapped by the
   backend, AU-2). After two consecutive polls with no live reviewer
   session (`alissa.parse_session_name`) and no undispatched item in the
   alissa tmux queue, `after_pass` writes the
   request file (`REQUEST_FILE` under `upgrade_dir()`) and asks the daemon
   to exit with code 0. The container's restart policy brings it back, and
   the entrypoint installs the target at boot.
4. **Confirm.** The first vitals POST answered on the new version marks the
   request confirmed. The entrypoint rolls back an unconfirmed upgrade. It
   reinstalls `previous`, adds the target to the file's skip list and leaves
   an ``upgradeFailed`` report. `report` hands that report to the next
   snapshot, and `reported` marks it sent once a POST carrying it succeeds.
5. **Unserved.** A process that finds an unconfirmed request for a target
   it does not run, written by this same version (``previous``), knows its
   predecessor's exit produced no upgrade: the boot step never ran (a
   non-container or non-root start, a boot block that failed before its
   first write) or installed somewhere the daemon does not import from.
   It skips the target and reports it, so the seat never loops drain →
   exit → restart.

Never while paused, never in managed mode, never with a live session: those
three are checked here, at the moment of the exit, not only when the hold
starts.

The request file is shared with ``docker/claude/entrypoint.sh`` (the
`self_upgrade_boot` block). Both sides keep every key they do not own, so
neither can drop the other's state. Its keys:

* ``target`` -- ``{"version", "digest"}``, written by the daemon.
* ``previous`` -- the version the daemon ran when it asked.
* ``requestedAt`` -- ISO-8601 UTC.
* ``confirmed`` / ``confirmedAt`` -- written by the daemon after the first
  answered poll on the target.
* ``boots`` / ``firstBootAt`` / ``firstBootTs`` / ``lastError`` -- the
  entrypoint's attempt record for this target. ``firstBootTs`` is the epoch
  seconds its 10-minute window reads; ``firstBootAt`` the same as ISO-8601.
* ``rollbackReason`` -- why the entrypoint rolled back, kept across the
  retries of a ``rollback_failed`` reinstall.
* ``state`` -- ``requested`` (daemon), then ``installed``,
  ``install_failed``, ``rollback_failed`` (retried at the next boot),
  ``rolled_back`` or ``not_newer`` (a target not newer than the installed
  version, never installed) (entrypoint), then ``confirmed`` or
  ``unserved`` (daemon). A ``not_newer`` request this version wrote is
  unserved too: the daemon runs an older copy than the boot step sees.
* ``skip`` -- versions never to move to again. A newer target clears it.
* ``upgradeFailed`` -- ``{"version", "reason"}``, the report owed to Studio.
  ``upgradeFailedReported`` is set once a POST carried it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from .alissa import parse_session_name

log = logging.getLogger(__name__)

SEAT = "revloop"
MANAGED = "managed"
SELF_HOSTED = "self_hosted"
MODES = (MANAGED, SELF_HOSTED)

# The shared request file. The directory follows ALISSA_UPGRADE_DIR so a
# test or a non-container run can point it elsewhere. The entrypoint reads
# the same variable.
UPGRADE_DIR_ENV = "ALISSA_UPGRADE_DIR"
DEFAULT_UPGRADE_DIR = "/workspace/.alissa-upgrade"
REQUEST_FILE = f"{SEAT}.json"

# How many consecutive polls with no live reviewer session count as idle.
IDLE_POLLS = 2

# The hold reasons `hold` returns, and the poll summary quotes.
HOLD_DRAIN = "drain"
HOLD_UPGRADE = "upgrade"

# The request states in which an upgrade is still on its way: the only ones
# the daemon confirms or calls unserved. Every other state is final, or a
# failure the entrypoint owns.
PENDING_STATES = ("requested", "installed")

# The states in which a request this version wrote left the seat where it
# was: still pending, or refused at boot as not newer than the installed
# version (the daemon imports an older copy than the root pass sees). Any
# of them on a later process means the exit did not move the seat.
UNSERVED_STATES = PENDING_STATES + ("not_newer",)

# `after_pass` outcomes.
EXIT = "exit"
WAIT = "wait"
NONE = "none"


def upgrade_dir(environ: "Mapping[str, str] | None" = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get(UPGRADE_DIR_ENV) or "").strip() or DEFAULT_UPGRADE_DIR


def iso(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def version_key(version: str) -> "tuple[int, ...] | None":
    """`X.Y.Z` as a comparable tuple, or None for anything else."""
    parts = str(version).strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


@dataclass(frozen=True)
class UpgradeAnswer:
    """One vitals answer's ``upgrade`` block."""

    mode: str
    target_version: "str | None"
    target_digest: "str | None"
    auto_upgrade: bool
    paused: bool
    drain: bool

    @property
    def target(self) -> "dict | None":
        if self.target_version is None:
            return None
        return {"version": self.target_version, "digest": self.target_digest}


def parse_upgrade(payload: object) -> "UpgradeAnswer | None":
    """The ``upgrade`` block of a vitals answer, or None.

    None when the answer is not an object or carries no ``upgrade`` (a Studio
    older than AU-1), or when the block is not one this daemon can act on
    (an unknown `mode`, a non-boolean flag). A malformed block is never read
    as a partial one: a guessed `paused: false` would let a seat upgrade
    through the operator's switch. A `target` without a string version is
    read as no target."""
    if not isinstance(payload, Mapping):
        return None
    block = payload.get("upgrade")
    if not isinstance(block, Mapping):
        return None
    mode = block.get("mode")
    flags = [block.get(k) for k in ("autoUpgrade", "paused", "drain")]
    if mode not in MODES or not all(isinstance(f, bool) for f in flags):
        return None
    version = digest = None
    target = block.get("target")
    if isinstance(target, Mapping):
        raw = target.get("version")
        if isinstance(raw, str) and raw.strip():
            version = raw.strip()
            raw_digest = target.get("digest")
            digest = raw_digest if isinstance(raw_digest, str) else None
    auto_upgrade, paused, drain = flags
    return UpgradeAnswer(
        mode=str(mode), target_version=version, target_digest=digest,
        auto_upgrade=bool(auto_upgrade), paused=bool(paused), drain=bool(drain),
    )


def live_reviewer_sessions(roster: "Sequence[object] | None") -> "list[str] | None":
    """The live reviewer sessions in the pass's roster, or None when the
    listing failed (unknown is never idle).

    The roster is the reap sweep's own (`ReviewWatcher._pass_roster`): the
    ``alissa tmux ls --live`` rows whose name parses as a reviewer session,
    minus the ones the sweep just killed. Each entry is a `ManagedSession`
    or a bare name. The reviewer grammar is re-checked here, so a session
    another daemon owns in a shared container never holds this seat."""
    if roster is None:
        return None
    names: "list[str]" = []
    for entry in roster:
        name = entry if isinstance(entry, str) else getattr(entry, "name", None)
        if isinstance(name, str) and parse_session_name(name) is not None:
            names.append(name)
    return names


# -- the request file ---------------------------------------------------------


def read_request(path: str) -> "dict | None":
    """The request file, or None when it is absent. An unreadable or
    non-object file is None too, with a WARNING: the daemon must not crash
    on a file the entrypoint half-wrote."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("self-upgrade: could not read %s (%s) — ignoring it", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("self-upgrade: %s is not a JSON object — ignoring it", path)
        return None
    return data


def write_request(path: str, data: "Mapping") -> None:
    """Write the request file atomically (a temp file in the same directory,
    then a rename), so the entrypoint never reads half a file. Raises
    OSError."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".revloop-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(data), fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _target_version(data: "Mapping") -> "str | None":
    target = data.get("target")
    version = target.get("version") if isinstance(target, Mapping) else None
    return version if isinstance(version, str) else None


def _count(queued: "Callable[[], int | None]") -> "int | None":
    """`queued()`, with a failure read as unknown (never idle)."""
    try:
        return queued()
    except Exception as exc:
        log.warning("self-upgrade: could not count the alissa queue (%s)", exc)
        return None


def skip_list(data: "Mapping | None") -> "list[str]":
    if not data:
        return []
    skip = data.get("skip")
    if not isinstance(skip, list):
        return []
    return [str(v) for v in skip if isinstance(v, str)]


# -- the upgrader -------------------------------------------------------------


class SelfUpgrader:
    """The daemon's self-upgrade state: the last answer, the hold, the idle
    streak and the request file.

    Every method is best-effort and never raises. The daemon's job is
    spawning reviewer sessions, and an upgrade that cannot be done must
    leave the seat running."""

    def __init__(
        self,
        running: str,
        *,
        directory: "str | None" = None,
        clock: "Callable[[], float]" = time.time,
        dry_run: bool = False,
    ):
        self.running = running
        self.path = os.path.join(directory or upgrade_dir(), REQUEST_FILE)
        self._clock = clock
        self.dry_run = dry_run
        self.answer: "UpgradeAnswer | None" = None
        self._hold: "str | None" = None
        self._idle_polls = 0
        # Targets this process could not request (the file would not
        # write): never held for again in this process, or the seat would
        # drain for ever without ever exiting.
        self._unwritable: "set[str]" = set()
        self.exit_requested = False

    # -- reading the answer --

    def observe(self, payload: object) -> "UpgradeAnswer | None":
        """Read one vitals answer. Keeps the last good block (an answer
        without one changes nothing), confirms a pending upgrade the first
        time an answer arrives on the target version, and clears the skip
        list when a newer target arrives. Returns the block read, or None."""
        answer = parse_upgrade(payload)
        if answer is None:
            if payload is not None:
                log.debug("self-upgrade: the vitals answer carries no upgrade block")
            return None
        self.answer = answer
        try:
            self._confirm()
            self._unserved()
            self._clear_skip(answer)
        except OSError as exc:
            log.warning("self-upgrade: could not update %s (%s)", self.path, exc)
        self._update_hold()
        return answer

    def _confirm(self) -> None:
        data = read_request(self.path)
        if not data or data.get("confirmed"):
            return
        if data.get("state") not in PENDING_STATES:
            # A failed or rolled-back target is never confirmed, even when
            # a rollback that could not reinstall left the daemon on it.
            return
        version = _target_version(data)
        if version != self.running or version in skip_list(data):
            return
        data["confirmed"] = True
        data["confirmedAt"] = iso(self._clock())
        data["state"] = "confirmed"
        write_request(self.path, data)
        log.info(
            "self-upgrade: confirmed — this daemon runs %s and Studio answered "
            "its first poll (upgraded from %s)", self.running,
            data.get("previous") or "unknown",
        )

    def _unserved(self) -> None:
        """Skip and report a target whose last request never moved this
        seat: the file still holds an unconfirmed, pending request that
        THIS version wrote (`previous == running`) for another version,
        or a request of this version's that the boot step refused as
        `not_newer`. Asking again would only repeat the drain, the exit and
        the restart for ever. A file that cannot be written puts the target out of
        reach for this process instead."""
        data = read_request(self.path)
        if not data or data.get("confirmed"):
            return
        state = data.get("state")
        version = _target_version(data)
        if state not in UNSERVED_STATES or not version or version == self.running:
            return
        if data.get("previous") != self.running:
            return
        skip = skip_list(data)
        if version in skip:
            return
        if state == "requested":
            reason = "the boot step never acted on the request (no install was tried)"
        elif state == "not_newer":
            reason = (
                "the boot step refused it: not newer than the installed "
                f"version, but the daemon still runs {self.running}"
            )
        else:
            reason = f"installed at boot, but the daemon still runs {self.running}"
        data.update({
            "state": "unserved", "skip": skip + [version], "lastError": reason,
            "upgradeFailed": {"version": version, "reason": reason},
            "upgradeFailedReported": False,
        })
        try:
            write_request(self.path, data)
        except OSError as exc:
            self._unwritable.add(version)
            log.warning(
                "self-upgrade: the request for %s was never served (%s) and "
                "%s could not be updated (%s) — this process will not try %s "
                "again", version, reason, self.path, exc, version,
            )
            return
        log.warning(
            "self-upgrade: the request for %s → %s was never served: %s — "
            "%s is now on the skip list and reported to Studio; this seat "
            "keeps running %s", self.running, version, reason, version,
            self.running,
        )

    def _clear_skip(self, answer: UpgradeAnswer) -> None:
        version = answer.target_version
        new = version_key(version or "")
        if new is None:
            return
        data = read_request(self.path)
        skip = skip_list(data)
        if not skip or version in skip:
            return
        keys = [k for k in (version_key(v) for v in skip) if k is not None]
        if keys and new <= max(keys):
            return
        assert data is not None
        data["skip"] = []
        write_request(self.path, data)
        log.info(
            "self-upgrade: target %s is newer than every skipped version "
            "(%s) — skip list cleared", version, ", ".join(skip),
        )

    # -- the hold --

    def _skipped(self, version: str) -> bool:
        return version in skip_list(read_request(self.path))

    def wanted(self) -> "str | None":
        """The version a self-hosted seat should move to now, or None."""
        a = self.answer
        if a is None or a.mode != SELF_HOSTED:
            return None
        if not a.auto_upgrade or a.paused:
            return None
        version = a.target_version
        if not version:
            return None
        # Only a strictly newer target. A pin briefly older than what runs
        # (a merge rolls the seat before the platform pin moves) must never
        # drain and downgrade the seat. Unparseable on either side: never.
        new, cur = version_key(version), version_key(self.running)
        if new is None or cur is None or new <= cur:
            return None
        if version in self._unwritable or self._skipped(version):
            return None
        return version

    def hold(self) -> "str | None":
        """Why no new reviewer session may start now (`HOLD_DRAIN`,
        `HOLD_UPGRADE`), or None. Read by every spawn path."""
        return self._hold

    def hold_detail(self) -> str:
        """The hold in words, for a deferral's reason."""
        if self._hold == HOLD_DRAIN:
            return "Studio asked this seat to drain for a managed upgrade"
        if self._hold == HOLD_UPGRADE:
            return (
                f"draining to self-upgrade {self.running} → "
                f"{self.answer.target_version if self.answer else '?'}"
            )
        return ""

    def _update_hold(self) -> None:
        if self.answer is not None and self.answer.drain:
            new = HOLD_DRAIN
        elif self.wanted() is not None:
            new = HOLD_UPGRADE
        else:
            new = None
        if new == self._hold:
            return
        old, self._hold = self._hold, new
        if new is None:
            self._idle_polls = 0
            log.info(
                "self-upgrade: drain lifted (was: %s) — new reviewer sessions may "
                "start again", old,
            )
        else:
            log.info(
                "self-upgrade: draining — %s; no new reviewer session starts, "
                "running ones finish normally", self.hold_detail(),
            )

    # -- the exit --

    def after_pass(
        self,
        roster: "Sequence[object] | None",
        queued: "Callable[[], int | None] | None" = None,
    ) -> str:
        """Called once per completed pass, after the vitals push. Returns
        EXIT when the daemon should exit now for the upgrade (the request is
        written), WAIT while it is draining towards one, NONE otherwise.

        `roster` is the pass's live reviewer sessions (the reap sweep's
        ``alissa tmux ls --live`` listing, None when it failed; see
        `live_reviewer_sessions`). `queued` counts
        the undispatched items in the alissa tmux queue (None: unknown). It
        is read only when the roster is idle: an item queued before the
        hold came on is in neither listing yet, and a restart would kill the
        session it dispatches to."""
        # Re-checked here, not only at the hold's start: a skip list the
        # entrypoint just wrote, or a target that moved, must stop the exit.
        self._update_hold()
        version = self.wanted()
        if version is None or self.answer is None:
            self._idle_polls = 0
            return NONE
        live = live_reviewer_sessions(roster)
        if live is None or live:
            if self._idle_polls:
                log.info(
                    "self-upgrade: not idle any more (%s) — idle count reset",
                    "the session listing failed" if live is None
                    else ", ".join(live),
                )
            self._idle_polls = 0
            return WAIT
        waiting = 0 if queued is None else _count(queued)
        if waiting is None or waiting:
            if self._idle_polls:
                log.info(
                    "self-upgrade: not idle any more (%s) — idle count reset",
                    "the alissa queue listing failed" if waiting is None
                    else f"{waiting} undispatched item(s) in the alissa queue",
                )
            self._idle_polls = 0
            return WAIT
        self._idle_polls += 1
        if self._idle_polls < IDLE_POLLS:
            log.info(
                "self-upgrade: idle on poll %d of %d before the exit to %s",
                self._idle_polls, IDLE_POLLS, version,
            )
            return WAIT
        if self.dry_run:
            log.info(
                "[dry-run] self-upgrade: idle — would write %s and exit to "
                "upgrade %s → %s", self.path, self.running, version,
            )
            return WAIT
        try:
            self._request(self.answer)
        except OSError as exc:
            self._unwritable.add(version)
            log.warning(
                "self-upgrade: could not write %s (%s) — NOT upgrading to %s; "
                "the drain is lifted and this process will not try %s again",
                self.path, exc, version, version,
            )
            self._update_hold()
            return NONE
        self.exit_requested = True
        log.info(
            "self-upgrade: idle on %d consecutive polls — wrote %s; exiting "
            "with code 0 so the container restarts and installs %s (from %s)",
            IDLE_POLLS, self.path, version, self.running,
        )
        return EXIT

    def _request(self, answer: UpgradeAnswer) -> None:
        old = read_request(self.path) or {}
        data: dict = {
            "target": answer.target,
            "previous": self.running,
            "requestedAt": iso(self._clock()),
            "state": "requested",
            "confirmed": False,
            "boots": 0,
            "skip": skip_list(old),
        }
        # A report not yet delivered survives a new request.
        if old.get("upgradeFailed") and not old.get("upgradeFailedReported"):
            data["upgradeFailed"] = old["upgradeFailed"]
            data["upgradeFailedReported"] = False
        write_request(self.path, data)

    # -- the failure report --

    def report(self) -> "dict | None":
        """The ``upgradeFailed`` report the next snapshot owes Studio, or
        None. Only a well-formed `{version, reason}` is offered: the request
        schema is strict."""
        data = read_request(self.path)
        if not data or data.get("upgradeFailedReported"):
            return None
        failed = data.get("upgradeFailed")
        if not isinstance(failed, Mapping):
            return None
        version, reason = failed.get("version"), failed.get("reason")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", version):
            return None
        reason = " ".join(str(reason or "rolled back").split())[:300] or "rolled back"
        return {"version": version, "reason": reason}

    def reported(self, sent: "Mapping") -> None:
        """Mark the report delivered, after a POST carrying `sent`
        succeeded."""
        data = read_request(self.path)
        if not data:
            return
        failed = data.get("upgradeFailed")
        if not isinstance(failed, Mapping) or failed.get("version") != sent.get("version"):
            return
        data["upgradeFailedReported"] = True
        try:
            write_request(self.path, data)
        except OSError as exc:
            log.warning("self-upgrade: could not update %s (%s)", self.path, exc)
            return
        log.info(
            "self-upgrade: reported to Studio that %s would not start "
            "(rolled back to %s)", sent.get("version"), self.running,
        )
