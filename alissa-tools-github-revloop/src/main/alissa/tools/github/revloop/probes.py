"""Boot probes: the seat's own read of its key path, reported on vitals
(issue #212; studio.alissa.app fleet-upgrade-safety design §2.2-§2.4, lane
UR-1).

A seat that boots and heartbeats can still be unable to do its job, and the
failure that matters -- the seat's OWN credential refused on the seat's OWN
read -- is invisible to Studio, which reads with different credentials. So
the seat probes its key path with its own identity and reports the answer on
its vitals. For revloop the key path is picking up a review request, check
R1 ("A review can be requested"); R0, the heartbeat, Studio reads off the
vitals rows themselves and is not probed here.

R1 is one result over these legs, every one run (a failing leg does not hide
the next):

* **search** -- the review edge's discovery read, through the SAME function
  the edge calls (`ReviewWatcher._review_requests` over the pass's own
  allowlist), never a re-implementation, so the probe cannot pass while the
  edge fails. Any answer is `ok`, an empty one included.
* **pull** -- the reviewer token reads one fed repo (`GET repos/<o>/<r>`
  under the reviewer identity's credential) and holds `pull` on it. The
  target is the first repo of the allowlist, or -- static mode with an empty
  allowlist, which watches "every repo that requests me" -- the first repo
  the search returned. No target: R1 is `not_applicable`, never `ok`.
* **D1's spawn preconditions** (devloop's check, which a reviewer spawn
  shares): the `claude` binary resolves and reports its version, the Claude
  credential answers `claude auth status`, the target's worktree hub is
  writable (a temporary file is created and removed), and tmux starts and
  lists on the socket the reviewer sessions live on. The two `claude` legs
  apply under `agent_profile: claude` only.

A 401 or 403 is `failed` with the status quoted. Every detail is one line of
at most DETAIL_MAX characters, scrubbed of every credential this process
holds before it leaves.

The set runs ONCE PER BOOT, on the first completed poll, and its block rides
on every vitals post until the next boot (`ReviewWatcher._run_boot_probes`).
Read-only: no review, comment, issue, PR, branch, label, task or session is
written. The one write -- the hub's temporary file -- is removed before the
leg answers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .alissa import tmux_socket
from .config import HUB_ADD, Config
from .fleet_vitals import SECRET_ENV, SECRET_MIN, iso_utc
from .ghclient import RateLimited
from .proc import CommandError, run as proc_run

log = logging.getLogger(__name__)

PROBE_R1 = "R1"

OK = "ok"
FAILED = "failed"
NOT_APPLICABLE = "not_applicable"

# The wire caps (design §2.2): one line, at most 300 characters.
DETAIL_MAX = 300
# Each subprocess leg's bound. A probe that hangs must not hold the pass.
LEG_TIMEOUT_S = 30

# The temporary file's prefix in the hub, so an operator who finds one left by
# a killed process knows whose it is.
HUB_PROBE_PREFIX = ".alissa-revloop-probe-"

_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
# GitHub token shapes, scrubbed even when no variable this process holds
# carries them (an error message may quote a token from another source).
_GH_TOKEN_SHAPE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_REDACTED = "[redacted]"


@dataclass(frozen=True)
class ProbeResult:
    id: str
    state: str
    detail: "str | None"

    def as_dict(self) -> dict:
        return {"id": self.id, "state": self.state, "detail": self.detail}


def probes_block(
    version: str, results: "Sequence[ProbeResult]", *, at: "int | float"
) -> dict:
    """The §2.2 `probes` block: the version that ran the set (the row's own
    `version`), when the set finished, and its results."""
    return {
        "version": version,
        "at": iso_utc(at),
        "results": [r.as_dict() for r in results],
    }


def held_secrets(
    config: Config, environ: "Mapping[str, str] | None" = None
) -> "list[str]":
    """Every credential value this process holds: the `SECRET_ENV` set plus
    the variable `reviewer_token_env` names, values of at least SECRET_MIN
    characters (the config block's own rule)."""
    env = os.environ if environ is None else environ
    names = SECRET_ENV + (
        (config.reviewer_token_env,) if config.reviewer_token_env else ()
    )
    return [
        v for k in names if len(v := (env.get(k) or "").strip()) >= SECRET_MIN
    ]


def scrub(text: str, secrets: "Sequence[str]" = ()) -> str:
    """`text` as one line of at most DETAIL_MAX characters, with every held
    secret and every GitHub-token-shaped string replaced."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED)
    text = _GH_TOKEN_SHAPE.sub(_REDACTED, text)
    text = " ".join(text.split())
    if len(text) > DETAIL_MAX:
        text = text[: DETAIL_MAX - 1] + "…"
    return text


def http_status(exc: BaseException) -> "int | None":
    """The HTTP status a `gh` failure quotes (`gh: Bad credentials (HTTP
    401)`), or None when it quotes none."""
    blob = getattr(exc, "stderr", None) or str(exc)
    match = _HTTP_STATUS.search(blob)
    return int(match.group(1)) if match else None


def _refusal(what: str, exc: BaseException) -> str:
    """One failing leg's words: the status quoted when there is one."""
    status = http_status(exc)
    message = (getattr(exc, "stderr", None) or str(exc)).strip()
    head = f"{what} refused (HTTP {status})" if status else f"{what} failed"
    return f"{head}: {message}" if message else head


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


class R1Probe:
    """R1, over seams: `search` is the edge's discovery read (called with
    the pass's allowlist), `github` the daemon's GitHub client (its `_api`,
    so the repo read runs under the reviewer identity's credential and is
    counted like every other call), `run` the subprocess runner."""

    def __init__(
        self,
        config: Config,
        *,
        search: "Callable[[tuple[str, ...] | None], list[tuple[str, str, int]]]",
        github: Any,
        run: "Callable[..., str] | None" = None,
        environ: "Mapping[str, str] | None" = None,
    ):
        self.config = config
        self._search = search
        self._github = github
        # Looked up at construction, not bound at definition, so the test
        # suite's isolation fixture can keep every probe off the host.
        self._run = run or proc_run
        self._secrets = held_secrets(config, environ)

    def run(self, allowlist: "tuple[str, ...] | None") -> ProbeResult:
        """R1's one result. Never raises: a leg that blows up is a failed
        leg, named."""
        failures: "list[str]" = []
        notes: "list[str]" = []

        requests = self._leg_search(allowlist, failures, notes)
        target = self._target(allowlist, requests)
        if target is not None:
            self._leg_pull(target, failures, notes)
        self._leg_claude(failures, notes)
        if target is not None:
            self._leg_hub(target, failures, notes)
        self._leg_tmux(failures, notes)

        if failures:
            state, words = FAILED, failures
        elif target is None:
            state = NOT_APPLICABLE
            words = ["no fed repo to probe"] + notes
        else:
            state, words = OK, notes
        return ProbeResult(PROBE_R1, state, scrub("; ".join(words), self._secrets) or None)

    # -- legs ----------------------------------------------------------------

    def _guard(self, what: str, leg: "Callable[[], Any]", failures: "list[str]") -> Any:
        try:
            return leg()
        except (CommandError, RateLimited) as exc:
            failures.append(_refusal(what, exc))
        except Exception as exc:
            failures.append(f"{what} failed: {type(exc).__name__}: {exc}")
        return None

    def _leg_search(
        self, allowlist: "tuple[str, ...] | None",
        failures: "list[str]", notes: "list[str]",
    ) -> "list[tuple[str, str, int]]":
        if allowlist is None:
            # Bows mode over an empty feed set: the edge searches nothing,
            # and so does the probe. No fed repo, so R1 is not applicable.
            notes.append("the feed set is empty, so the search is skipped")
            return []
        before = len(failures)
        requests = self._guard(
            "review-requested search", lambda: self._search(allowlist), failures
        )
        if len(failures) > before:
            return []
        requests = list(requests or [])
        notes.append(f"review-requested search answered ({len(requests)} PR(s))")
        return requests

    @staticmethod
    def _target(
        allowlist: "tuple[str, ...] | None",
        requests: "list[tuple[str, str, int]]",
    ) -> "tuple[str, str] | None":
        if allowlist:
            owner, _, repo = sorted(allowlist, key=str.casefold)[0].partition("/")
            if owner and repo:
                return owner, repo
        if allowlist is not None and requests:
            owner, repo, _ = requests[0]
            return owner, repo
        return None

    def _leg_pull(
        self, target: "tuple[str, str]", failures: "list[str]", notes: "list[str]"
    ) -> None:
        owner, repo = target
        name = f"{owner}/{repo}"
        payload = self._guard(
            f"gh read of {name}",
            lambda: self._github._api(
                f"repos/{owner}/{repo}", timeout=LEG_TIMEOUT_S,
                forbidden_is_rate_limit=False,
            ),
            failures,
        )
        if payload is None:
            if not any(f.startswith(f"gh read of {name}") for f in failures):
                failures.append(f"gh read of {name} answered nothing")
            return
        perms = payload.get("permissions") if isinstance(payload, dict) else None
        if isinstance(perms, dict):
            if not perms.get("pull"):
                failures.append(f"the reviewer token lacks pull on {name}")
                return
            notes.append(f"reviewer token has pull on {name}")
        else:
            # An answer without `permissions` (an installation token) still
            # proves the read; that read IS pull.
            notes.append(f"reviewer token reads {name}")

    def _leg_claude(self, failures: "list[str]", notes: "list[str]") -> None:
        if self.config.agent_profile != "claude":
            notes.append(f"claude legs skipped (agent_profile={self.config.agent_profile})")
            return
        out = self._guard(
            "claude --version",
            lambda: self._run(["claude", "--version"], timeout=LEG_TIMEOUT_S),
            failures,
        )
        if out is not None:
            version = _first_line(out)
            if version:
                notes.append(f"claude {version}")
            else:
                failures.append("claude --version printed no version")
        status = self._guard(
            "claude auth status",
            lambda: self._run(
                ["claude", "auth", "status", "--json"], timeout=LEG_TIMEOUT_S, check=False
            ),
            failures,
        )
        if status is None:
            return
        try:
            answer = json.loads(status)
        except ValueError:
            failures.append("claude auth status answered no JSON")
            return
        if not isinstance(answer, dict) or answer.get("loggedIn") is not True:
            failures.append("claude auth status: not logged in")
            return
        # `authMethod` only: the answer also carries the account's email and
        # org, which have no business on a vitals row.
        method = answer.get("authMethod")
        notes.append(f"claude logged in ({method})" if method else "claude logged in")

    def _leg_hub(
        self, target: "tuple[str, str]", failures: "list[str]", notes: "list[str]"
    ) -> None:
        owner, repo = target
        hub = self.config.hub_for(owner, repo)
        where: "Path | None" = hub
        if not hub.is_dir():
            # The hub is made at the first spawn when `on_missing_hub: add`;
            # until then the workspace root is the directory the spawn needs
            # writable. Under `skip` a missing hub refuses every spawn.
            if self.config.on_missing_hub == HUB_ADD:
                where = Path(self.config.workspace_root).expanduser()
            else:
                failures.append(f"worktree hub {hub} is missing (on_missing_hub: skip)")
                return
        try:
            fd, path = tempfile.mkstemp(prefix=HUB_PROBE_PREFIX, dir=str(where))
        except OSError as exc:
            failures.append(f"worktree hub {where} is not writable: {exc.strerror or exc}")
            return
        try:
            os.close(fd)
        finally:
            try:
                os.unlink(path)
            except OSError as exc:
                failures.append(f"could not remove the probe file {path}: {exc.strerror or exc}")
                return
        notes.append(f"hub {where} writable")

    def _leg_tmux(self, failures: "list[str]", notes: "list[str]") -> None:
        # `start-server ; list-sessions` on the reviewers' own socket: starts
        # the server when none runs (it exits again on its own when it holds
        # no session) and lists it. No session is created.
        out = self._guard(
            "tmux",
            lambda: self._run(
                ["tmux", "-S", tmux_socket(), "start-server", ";",
                 "list-sessions", "-F", "#{session_name}"],
                timeout=LEG_TIMEOUT_S,
            ),
            failures,
        )
        if out is not None:
            listed = sum(1 for line in out.splitlines() if line.strip())
            notes.append(f"tmux lists {listed} session(s)")


def run_r1(
    config: Config,
    *,
    allowlist: "tuple[str, ...] | None",
    search: "Callable[[tuple[str, ...] | None], list[tuple[str, str, int]]]",
    github: Any,
    version: str,
    run: "Callable[..., str] | None" = None,
    clock: "Callable[[], float]" = time.time,
    environ: "Mapping[str, str] | None" = None,
) -> dict:
    """Run the boot probe set (R1) and return its §2.2 block. Never raises."""
    try:
        result = R1Probe(
            config, search=search, github=github, run=run, environ=environ
        ).run(allowlist)
    except Exception as exc:  # a defect in the probe itself, not a leg
        result = ProbeResult(
            PROBE_R1, FAILED,
            scrub(f"probe error: {type(exc).__name__}: {exc}", held_secrets(config, environ)),
        )
    log.info("boot probes: %s %s — %s", result.id, result.state, result.detail)
    return probes_block(version, [result], at=clock())
