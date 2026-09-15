"""Alissa REST access — loop telemetry's write (issue #112), the fleet-vitals
write beside it (issue #126) and the two reads `repos_source: bows` needs
(issue #119).

This is the SECOND Alissa adapter in the package, and the split is deliberate.
`alissa.py` shells out to the `alissa` CLI, which is the daemon's established
Alissa idiom for everything it does today (the review-task search, the tmux
queue, the CR6 envelope reads). Loop telemetry needs a thing that idiom cannot
supply: the CLI has no loop-events command, and it cannot attach the actor
identity `POST /v1/loop-events` keys its rows by — the API stores events under
the token's principal user, which is exactly the credential this daemon's
`ALISSA_API_TOKEN` already carries.

So the one write goes over the REST API directly, in the shape devloop's
`alissa_client.py` adopted (PR #91 there): stdlib `urllib` only — the
distribution ships no third-party runtime dependency and this must not be the
change that adds one — a bearer token from the environment, bounded timeouts,
and errors classified into a small taxonomy instead of leaking raw urllib
exceptions. The CLI adapter stays untouched for everything else.

The caller here is a BEST-EFFORT emitter (`loop_events`): its answer to every
bucket is the same — warn once and let the pass complete — so the taxonomy
exists for the log line, which should say "your token is wrong" (permanent,
operator-fixable) differently from "the API blinked".

The two READS (issue #119) ride the same transport and the same taxonomy, and
they are the shape devloop's client already gives them, so the two daemons
agree about the wire:

``GET /v1/ping``
    ``{"pong", "timestamp", "userId", "actorId", "displayName"}`` — the
    identity this token acts as. `actorId` is the same opaque id that appears
    as `ownerActorId` on a Body of Work, which is what makes "trust my own
    feeds" resolvable at boot without an operator hand-copying an id.

``GET /v1/bodies-of-work?includeShared=true``
    ``{"bodiesOfWork": [{"_id", "title", "status", "ownerActorId", ...}]}``.
    `includeShared=true` is load-bearing: the endpoint defaults to OWNED-only,
    and a feed container is the operator's with the daemon merely a
    collaborator. It also widens the listing to whatever anyone shared, which
    is why `bows.py` gates on the owner and never on the title alone.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://api.alissa.app"

# The env var the `alissa` CLI itself reads, so a daemon whose CLI is already
# authenticated needs no second secret.
ENV_TOKEN = "ALISSA_API_TOKEN"

# The ingest cap: POST /v1/loop-events takes 1-200 events per call. The
# EMITTER splits batches at this bound; the client refuses an oversized one
# rather than silently posting a request the API will 400.
MAX_EVENTS_PER_POST = 200

# The `error` code a 403 carries when the token's user has not activated the
# stacked app that owns the loop ingest. Advisory (classification keys on the
# status), but the vitals pusher reads it to warn ONCE with the activate URL
# instead of once per pass.
NOT_ACTIVATED = "not_activated"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it (PR #113 round 1).

    The default handler copies the request's headers — `Authorization`
    included — onto the redirected request, ACROSS HOSTS, so a 30x from
    whatever `alissa_endpoint` names would hand the bearer token to the
    redirect target (and 301/302/303 would downgrade the POST to a GET that
    ingests nothing while reading as success). A redirected ingest cannot
    succeed anyway, so refusal beats strip-and-follow: returning None makes
    urllib raise the 30x as an HTTPError, which the taxonomy reports like any
    other unexpected status.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# One opener for the module: default handlers with only the redirect
# behaviour replaced, built once because handler construction is not free
# and every client shares the same policy.
_opener = urllib.request.build_opener(_RefuseRedirects())


class AlissaError(Exception):
    """Base of the taxonomy. `status` is 0 for a transport failure (there was
    no HTTP response to carry one)."""

    def __init__(self, status: int, detail: object, code: "str | None" = None):
        super().__init__(f"HTTP {status}: {detail}" if status else str(detail))
        self.status = status
        self.detail = detail
        self.code = code


class AlissaAuthError(AlissaError):
    """401/403, or no token at all. Permanent and operator-fixable — retrying
    it every pass only writes the same warning again."""


class AlissaTransient(AlissaError):
    """408/429/5xx and every transport failure (DNS, refused, timeout). The
    'the API blinked' bucket — re-emission next pass is the retry, and the
    deterministic dedupe keys are what make it harmless."""


@dataclass(frozen=True)
class Identity:
    """Who this token acts as (`GET /v1/ping`).

    `actor_id` is the field the feed-authority gate compares against a Body
    of Work's `ownerActorId`. The other two are context for the log line —
    a display name is renameable, so nothing is ever compared against it."""

    actor_id: str
    user_id: str = ""
    display_name: str = ""


@dataclass(frozen=True)
class BodyOfWork:
    """A Body of Work as the LIST endpoint reports it — only the four fields
    the feed reads.

    `owner_id` is the container's provenance and the only field here that is
    not operator-authored text: the listing includes containers merely SHARED
    with this actor, so the title alone cannot say whether a feed is one the
    operator set up. Empty when the payload omits it, which the authority gate
    treats as untrusted rather than unknown."""

    id: str
    title: str
    status: str = ""
    owner_id: str = ""


class AlissaClient:
    """One write and two reads, with the transport hidden behind the taxonomy.

    Reads `ALISSA_API_TOKEN` from the environment; `base` defaults to the
    public API. Both are constructor arguments so a test never needs the
    network and an operator can point a daemon at another deployment
    (`alissa_endpoint` in the config)."""

    def __init__(
        self,
        token: "str | None" = None,
        base: "str | None" = None,
        *,
        timeout: int = 30,
    ):
        self.base = (base or DEFAULT_ENDPOINT).rstrip("/")
        self._token = token if token is not None else os.environ.get(ENV_TOKEN)
        self._timeout = timeout

    def _request(self, path: str, payload: "dict | None" = None) -> object:
        """One call: a POST when `payload` is given, a GET otherwise. Every
        failure leaves as a taxonomy exception — the caller never sees a raw
        urllib error or an HTTP status. Both verbs share the redirect-refusing
        opener: a 30x on a GET would carry the bearer token across hosts just
        as it would on the POST."""
        if not self._token:
            # No token at all is an auth condition, not a transport one: the
            # operator must set the env var. Fail the way a 401 would, so the
            # emitter's warning reads as permanent rather than transient.
            raise AlissaAuthError(0, f"{ENV_TOKEN} is not set")

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}",
            method="POST" if payload is not None else "GET",
            data=data,
            headers=headers,
        )
        try:
            with _opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise self._classify(exc) from None
        except urllib.error.URLError as exc:
            raise AlissaTransient(0, str(exc.reason)) from None
        except (TimeoutError, OSError) as exc:  # pragma: no cover - defence
            # A socket timeout on the READ does not arrive as URLError.
            raise AlissaTransient(0, str(exc)) from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            # A 2xx that is not JSON is a contract violation, not a retry
            # signal -- but it must not escape as a bare ValueError either,
            # because the emitter catches AlissaError and nothing else.
            raise AlissaError(200, f"response was not JSON ({exc})") from None

    @staticmethod
    def _classify(exc: "urllib.error.HTTPError") -> AlissaError:
        """Map an HTTP error onto the taxonomy. The API sends JSON error
        bodies (`{"error": CODE, "message": ...}`); the code rides along when
        present, but classification keys on the STATUS — codes are advisory,
        statuses are the contract."""
        detail: object = exc.read().decode("utf-8", "replace")
        code: "str | None" = None
        try:
            parsed = json.loads(detail)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            detail = parsed
            raw_code = parsed.get("error")
            code = raw_code if isinstance(raw_code, str) else None

        status = exc.code
        if status in (401, 403):
            return AlissaAuthError(status, detail, code)
        if status in (408, 429) or 500 <= status <= 599:
            return AlissaTransient(status, detail, code)
        return AlissaError(status, detail, code)

    def post_loop_events(self, events: "list[dict]") -> dict:
        """Ingest one batch of loop events (`POST /v1/loop-events`).

        The API is idempotent on `(user, dedupeKey)`, so re-posting a batch —
        which is exactly what the emitter does after a failed pass — lands as
        silent duplicates, never as errors or overwrites. Returns the API's
        `{"accepted": N, "duplicates": M}` payload (empty dict when the body
        was empty), for the caller's debug line.

        An oversized batch is refused HERE, loudly: the API fails the whole
        call at >200 events, and the emitter owns the splitting, so reaching
        this guard is a code defect rather than an operational condition.
        """
        if len(events) > MAX_EVENTS_PER_POST:
            raise ValueError(
                f"post_loop_events takes at most {MAX_EVENTS_PER_POST} events "
                f"per call, got {len(events)} — the emitter must split"
            )
        payload = self._request("/v1/loop-events", {"events": events})
        return payload if isinstance(payload, dict) else {}

    def post_fleet_vitals(self, snapshot: dict) -> dict:
        """Replace this seat's fleet-vitals snapshot (`POST
        /v1/loop/fleet-vitals`, issue #126).

        One body, one seat, replaced on every call — there is no batching
        and no dedupe key, because the API keeps only the LATEST snapshot per
        user×seat (loop events are the history). Returns the API's
        `{"seat", "receivedAt", "replaced"}` payload (empty dict when the
        body was empty), for the caller's debug line. The API is strict —
        unknown keys 400, lists cap at 50, the body at 64 KB — and the
        BUILDER (`fleet_vitals`) owns fitting the snapshot to that, so this
        method sends exactly what it is handed.
        """
        payload = self._request("/v1/loop/fleet-vitals", snapshot)
        return payload if isinstance(payload, dict) else {}

    def ping(self) -> Identity:
        """The identity this token acts as (`GET /v1/ping`).

        On the BOOT path of `repos_source: bows`, and fatal there — see
        `__main__.resolve_feed_authority` for why an unanswered whoami must
        stop the daemon rather than soften into an empty authority."""
        payload = self._request("/v1/ping")
        row = payload if isinstance(payload, dict) else {}
        actor_id = row.get("actorId")
        if not isinstance(actor_id, str) or not actor_id.strip():
            # A 2xx with no actorId is a contract violation, not a missing
            # resource: name it as such rather than letting an empty authority
            # travel onwards looking like a configuration choice.
            raise AlissaError(
                200,
                f"GET /v1/ping returned no actorId (got {payload!r}) — the "
                f"token's acting identity could not be determined",
            )
        return Identity(
            actor_id=actor_id.strip(),
            user_id=str(row.get("userId") or ""),
            display_name=str(row.get("displayName") or ""),
        )

    def list_bodies_of_work(self) -> "list[BodyOfWork]":
        """Every Body of Work this actor owns OR collaborates on — ONE call.

        There is no server-side title or status filter, so the caller narrows.
        Rows without an `_id` are dropped: a container that cannot be
        addressed cannot be reported on either.

        A payload with no `bodiesOfWork` LIST is a contract violation, not an
        empty listing, and is raised as such -- `ping`'s missing-`actorId`
        reasoning applied to the other read. Degrading it to `[]` would report
        SUCCESS to the refresh, which would then derive nothing and drop every
        feed-enrolled repo on a routine INFO line: a response-shape change or
        an `alissa_endpoint` answering 200 with something else would silently
        un-enroll everything, which is precisely what the never-shrink rule
        promises cannot happen. Raising routes it to the refresh's failure
        path instead, which keeps the last good set. `{"bodiesOfWork": []}`
        is untouched -- present-and-empty is a legitimate empty listing, and
        the documented way an operator un-enrolls the last repo."""
        payload = self._request("/v1/bodies-of-work?includeShared=true")
        rows = payload.get("bodiesOfWork") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise AlissaError(
                200,
                f"GET /v1/bodies-of-work returned no bodiesOfWork list "
                f"(got {payload!r})",
            )
        out: "list[BodyOfWork]" = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bow_id = row.get("_id")
            if not isinstance(bow_id, str) or not bow_id:
                continue
            out.append(
                BodyOfWork(
                    id=bow_id,
                    title=str(row.get("title") or ""),
                    status=str(row.get("status") or ""),
                    owner_id=str(row.get("ownerActorId") or ""),
                )
            )
        return out
