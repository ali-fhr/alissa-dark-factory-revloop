"""The BOW-derived review allowlist (`repos_source: "bows"`, issue #119).

WHY THIS EXISTS. A Dark Factory customer onboards a repo by creating a lane in
Studio, which mints the `autodev: <owner>/<repo>` feed Body of Work. orcloop
(`repos_source=bows`, >= 0.13.0) and devloop (>= 0.8.9) already derive their
allowlists from those feeds, so enrolling a repo is a no-redeploy operation
for two of the three seats. This module makes revloop the third: in `bows`
mode the review allowlist is derived from the same containers, so the
reviewer service needs no `ALISSA_REVIEW_REPOS` edit and no redeploy per lane.

WHAT IS DERIVED, AND WHAT IS NOT. The derived set is UNIONED with the static
`repos` list -- static entries always stay watched, in both modes, and this
module never removes one. `repos_source: "static"` (the default) never
constructs a `BowRepoSource` at all, so that mode is unchanged bit for bit.

THE AUTHORITY GATE IS THE SECURITY BOUNDARY. `GET /v1/bodies-of-work` is read
with `includeShared=true`, which is necessary (a feed container is the
operator's, with the daemon added as a collaborator) and which widens the
listing to every container ANY actor in the tenant has shared with this one --
unilaterally, with no acceptance step on this side. A feed-shaped TITLE is
therefore not a claim to be a feed: only an allowlisted OWNER's container is
(see `config.Config.trusts_feed_owner`, whose authority defaults to the
token's own actor, resolved at boot). Sharing makes a container readable;
ownership makes it authoritative. The allowlist bounds where reviewer
sessions spawn -- with `on_missing_hub: add`, where code is CLONED and opened
as an agent's cwd -- so a third party must not be able to widen it.

THE TWO FAIL-SAFE RULES, verbatim from devloop, because the allowlist bounds
where reviewer sessions are spawned:

* **Never shrink on failure.** A refresh that could not list keeps the last
  successfully derived set and warns. A first-boot failure has no last set to
  keep, so the daemon starts on the static list alone (empty static => watch
  nothing, exactly like bows-empty) and says so at ERROR.
* **Never drop mid-round.** A repo whose feed BOW completed, was cancelled or
  vanished is dropped on the next successful refresh -- unless a round is in
  flight for a PR in that repo (a live reviewer session of this daemon's own
  grammar, or an owed round the ledger has not closed), in which case it
  stays watched until that round finishes. Dropping a repo out from under a
  running round would strand it: the sweep, the spawn gate and the verdict
  poster all read the allowlist.

The title grammar, the status gate and the repo-shape check are the SAME as
devloop's (`bows.py` there), so the two daemons agree about which containers
are feeds -- a prefix or a regex either side could retune privately is a way
for them to disagree.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Iterable, Sequence

from .alissa_client import AlissaClient, AlissaError, BodyOfWork
from .config import Config

log = logging.getLogger(__name__)

# The feed title convention, shared verbatim with orcloop's `bow_prefix`
# default and devloop's constant: `autodev: <owner>/<repo>`. The trailing
# space is part of it. A CONSTANT rather than a config key on purpose -- the
# three daemons read the same operator containers.
FEED_PREFIX = "autodev: "

# What the repo half must look like. Mirrors orcloop's `_REPO_NAME_RE`: an
# owner of GitHub's own alphabet, a repo that is neither all dots nor a `.git`
# suffix. Shape only -- it says "this could be a repo", never "this repo
# exists", which is what the hub check and the gh token answer later.
_FEED_REPO = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"/(?!\.+$)(?!.*\.[Gg][Ii][Tt]$)[A-Za-z0-9._-]+$"
)

# The BOW status a feed must carry. An absent or unrecognised status reads as
# NOT a feed, which is the fail-closed direction: the cost is a repo that goes
# unwatched (the daemon reviews less), never one that gets enrolled on a
# payload the daemon did not understand.
STATUS_ACTIVE = "active"

# The WARN a bows-mode daemon logs when derived∪static is empty -- once per
# refresh, from the poll loop. Named here so the loop and its tests agree on
# the text the operator greps for.
EMPTY_SET_WARNING = "bows mode derived 0 repos"


def is_feed_title(title: str) -> bool:
    """Does this Body of Work's title claim to be an autodev feed?

    Prefix matching is case-insensitive (an operator typing `Autodev: ` means
    the same container), which is also why `parse_feed_repo` slices the
    ORIGINAL text rather than a casefolded copy -- casefolding can change
    length (`ß` -> `ss`) and would slice the repo half apart.
    """
    return title.lstrip().casefold().startswith(FEED_PREFIX.casefold())


def parse_feed_repo(title: str) -> "str | None":
    """The `owner/repo` an `autodev: <owner>/<repo>` title targets.

    None means the title is not a feed at all, OR is a feed whose repo half is
    malformed. The caller tells the two apart with `is_feed_title`, because a
    malformed feed title is an operator typo that must be NAMED rather than
    silently skipped -- it is the difference between "no repo was enrolled"
    and "the repo you meant to enroll is spelled wrong".
    """
    if not is_feed_title(title):
        return None
    tail = title.lstrip()[len(FEED_PREFIX):].strip()
    return tail if _FEED_REPO.match(tail) else None


def union_repos(
    static: "Sequence[str]", derived: "Sequence[str]"
) -> "tuple[str, ...]":
    """The effective allowlist: static first, then derived, de-duplicated
    case-insensitively.

    Static first because a repo named in BOTH keeps the OPERATOR's casing --
    `repos` is what an operator reads back in the startup log, and a feed
    title should not be able to restyle it. Case-insensitive because that is
    how `Config.watches` compares in bows mode (GitHub owner/repo names are),
    so two spellings of one repo must collapse to one entry rather than be
    searched twice.
    """
    out: "list[str]" = []
    seen: "set[str]" = set()
    for entry in (*static, *derived):
        key = entry.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return tuple(out)


class BowRepoSource:
    """The refresh cadence, the authority gate, and the two fail-safe rules.

    One instance per `ReviewWatcher`, constructed only in `bows` mode.
    `repos()` is the effective allowlist at any moment; `refresh()` is the
    only thing that ever changes it, and it NEVER raises -- a failing feed
    must cost the daemon a warning, not a poll pass.
    """

    def __init__(self, config: Config, client: "AlissaClient | None" = None):
        self.config = config
        self.client = client or AlissaClient(base=config.alissa_endpoint)
        # The last SUCCESSFULLY derived set. None means "no successful refresh
        # yet", which is what makes a first-boot failure fall back to the
        # static list instead of to a set nobody derived.
        self._derived: "tuple[str, ...] | None" = None
        # repo (casefolded) -> (bow id, bow title) it was derived from, for
        # the `-v` line and the console. A retained repo keeps the source it
        # last had; a repo dropped loses its entry.
        self._sources: "dict[str, tuple[str, str]]" = {}
        # Poll passes since the last refresh ATTEMPT (not the last success):
        # a failing feed must not be re-listed every pass, since the fail-safe
        # already keeps the last good answer and the next cadence tick retries.
        self._since = 0
        # Whether a refresh was ever ATTEMPTED. Distinct from `_derived is
        # None` on purpose: a first-boot failure leaves nothing derived, and
        # keying the cadence on that would re-list every single pass for as
        # long as the API stayed down -- the one case with the least to gain
        # from hammering it.
        self._attempted = False

    @property
    def derived(self) -> "tuple[str, ...]":
        """The derived half alone — `()` until a refresh succeeds."""
        return self._derived or ()

    def sources(self) -> "list[tuple[str, str, str]]":
        """`(repo, bow id, bow title)` per derived repo, in derived order --
        what the console renders beside `repos` and what `-v` logs."""
        out = []
        for repo in self.derived:
            bow_id, title = self._sources.get(repo.casefold(), ("", ""))
            out.append((repo, bow_id, title))
        return out

    def repos(self) -> "tuple[str, ...]":
        """The effective allowlist: the static list unioned with whatever the
        last successful refresh derived."""
        return union_repos(self.config.repos, self.derived)

    def due(self) -> bool:
        """Is a refresh owed? True until the first attempt, then once
        `bows_refresh_polls` passes have been counted since the last one.

        Read AFTER `tick()` (see `ReviewWatcher.refresh_repos`), which is what
        makes `bows_refresh_polls: 1` mean "every pass" rather than "every
        other pass"."""
        return not self._attempted or self._since >= self.config.bows_refresh_polls

    def tick(self) -> None:
        """Count one poll pass."""
        self._since += 1

    def refresh(
        self, mid_round: "Callable[[], frozenset[str]] | None" = None
    ) -> bool:
        """List the feeds and re-derive. Returns whether the listing worked.

        `mid_round` is called at most once, and only when a repo is actually
        a drop candidate: it answers "which repos (casefolded `owner/repo`)
        does this daemon have a round in flight on?". None means the caller
        has no way to tell — which is not the same as "none", so the drop
        half is skipped entirely rather than run on absent evidence.

        Never raises. Every failure mode ends in a log line and the previously
        derived set, because the caller is a poll pass and the allowlist is
        what bounds where the daemon spawns.
        """
        self._attempted = True
        self._since = 0

        if not self.config.bow_owners:
            # Unreachable through the CLI -- boot resolves the authority (the
            # token's own actor, or the operator's explicit list) and refuses
            # to start if it cannot. Kept because it is this mode's central
            # property and must hold for a watcher built any other way: a
            # test, an embedder, a future entry point. `trusts_feed_owner`
            # already refuses every owner on an empty list, so what this adds
            # is the LOUDNESS boot would have provided.
            log.error(
                "repos_source is 'bows' but no feed authority is set — no "
                "body of work can be authoritative, so nothing is derived "
                "this refresh. Startup normally resolves this to the token's "
                "own actor; set bow_owners explicitly if the feed containers "
                "belong to another actor"
            )
            return False

        try:
            bows = self.client.list_bodies_of_work()
        except AlissaError as exc:
            self._log_listing_failure(exc)
            return False

        feeds = self._authoritative(bows)
        fresh = self._derive(feeds)
        retained = self._retained(fresh, mid_round)
        self._derived = union_repos(fresh, retained)
        keep = {r.casefold() for r in self._derived}
        self._sources = {k: v for k, v in self._sources.items() if k in keep}
        for bow in feeds:
            repo = parse_feed_repo(bow.title)
            if repo is not None:
                self._sources.setdefault(repo.casefold(), (bow.id, bow.title))
        self._log_derived(feeds)
        return True

    # -- the pieces of one refresh ----------------------------------------

    def _log_listing_failure(self, exc: AlissaError) -> None:
        """A failed listing keeps the last good set; only the FIRST one has no
        set to keep, which is the case an operator has to know about."""
        if self._derived is None:
            log.error(
                "repos_source=bows: could not list bodies of work on the "
                "first refresh (%s) — starting on the static `repos` list "
                "alone (%s). Nothing derived from a feed is watched until a "
                "refresh succeeds",
                exc,
                ", ".join(self.config.repos) or "empty, so NO repos",
            )
        else:
            log.warning(
                "repos_source=bows: could not list bodies of work (%s) — "
                "keeping the %d repo(s) derived by the last successful "
                "refresh; the allowlist never shrinks because the API blinked",
                exc,
                len(self._derived),
            )

    def _authoritative(self, bows: "Iterable[BodyOfWork]") -> "list[BodyOfWork]":
        """The feed-shaped containers an allowlisted actor OWNS, active only.

        The foreign-owned ones get ONE aggregate warning, not one line each:
        the count is the diagnostic an operator needs (the realistic failure
        is a wrong actor id, not an attack), and a per-container warning would
        let anyone who can share a Body of Work write to this log at will.
        `-v` names them.
        """
        listed = list(bows)
        named = [b for b in listed if is_feed_title(b.title)]
        owned = [b for b in named if self.config.trusts_feed_owner(b.owner_id)]
        feeds = [b for b in owned if b.status == STATUS_ACTIVE]

        if len(owned) != len(named):
            log.warning(
                "repos_source=bows: %d body(ies) of work match the feed "
                "prefix %r but are NOT feeds — their owner is not in "
                "bow_owners (%s). A body of work SHARED with this actor does "
                "not become a feed; run with -v to see which, and add the "
                "owner id only if you meant to trust it",
                len(named) - len(owned),
                FEED_PREFIX,
                ", ".join(self.config.bow_owners),
            )
            for bow in named:
                if not self.config.trusts_feed_owner(bow.owner_id):
                    log.debug(
                        "ignoring body of work %r (%s): owner %r is not in "
                        "bow_owners",
                        bow.title, bow.id, bow.owner_id,
                    )
        for bow in owned:
            if bow.status != STATUS_ACTIVE:
                # Not a warning: an operator COMPLETING a feed is the
                # documented way to un-enroll a repo, so it is routine.
                log.debug(
                    "body of work %r is %r, not %r — its repo is not enrolled",
                    bow.title, bow.status or "(no status)", STATUS_ACTIVE,
                )
        log.debug(
            "repos_source=bows: %d body(ies) of work listed, %d feed-shaped, "
            "%d owned by an allowlisted actor, %d active",
            len(listed), len(named), len(owned), len(feeds),
        )
        return feeds

    def _derive(self, feeds: "Sequence[BodyOfWork]") -> "tuple[str, ...]":
        """`owner/repo` per active feed, malformed titles warned and skipped.

        A malformed title is NEVER fatal: one typo'd container must not stop
        the other feeds enrolling their repos, and a daemon that refused to
        start over operator text it does not control would be trivially
        deniable by anyone who can share a Body of Work.
        """
        derived: "list[str]" = []
        for bow in feeds:
            repo = parse_feed_repo(bow.title)
            if repo is None:
                log.warning(
                    "repos_source=bows: body of work %r (%s) matches the feed "
                    "prefix but its repo half is not `owner/repo` — skipped, "
                    "no repo enrolled from it",
                    bow.title, bow.id,
                )
                continue
            derived.append(repo)
        # NOT de-duplicated here: `refresh` unions this with the retained set
        # through `union_repos`, which folds duplicates across both halves at
        # once.
        return tuple(derived)

    def _retained(
        self,
        fresh: "Sequence[str]",
        mid_round: "Callable[[], frozenset[str]] | None",
    ) -> "tuple[str, ...]":
        """Previously derived repos this refresh would drop but must not.

        Drop candidates are computed first and `mid_round` is only consulted
        when there is at least one, so the steady state (nothing changed)
        costs no session listing and no ledger read. A None `mid_round` means
        the caller cannot answer, so nothing is dropped this refresh — absent
        evidence is not evidence of an idle repo, and the direction that errs
        here should be the one that keeps watching.
        """
        fresh_fold = {r.casefold() for r in fresh}
        candidates = [r for r in self.derived if r.casefold() not in fresh_fold]
        if not candidates:
            return ()
        if mid_round is None:
            log.debug(
                "repos_source=bows: %d repo(s) left the feed but in-flight "
                "rounds cannot be checked this refresh — none dropped",
                len(candidates),
            )
            return tuple(candidates)

        busy = mid_round()
        held = tuple(r for r in candidates if r.casefold() in busy)
        dropped = [r for r in candidates if r.casefold() not in busy]
        if held:
            log.info(
                "repos_source=bows: %s left the feed but still has a round in "
                "flight — kept watched until that round finishes",
                ", ".join(held),
            )
        if dropped:
            log.info(
                "repos_source=bows: %s left the feed and has no round in "
                "flight — no longer watched",
                ", ".join(dropped),
            )
        return held

    def _log_derived(self, feeds: "Sequence[BodyOfWork]") -> None:
        """One INFO line per refresh naming the derived set; `-v` names the
        source container behind each repo."""
        log.info(
            "repos_source=bows: derived %d repo(s) from %d active feed body"
            "(ies) of work: %s",
            len(self.derived), len(feeds),
            ", ".join(self.derived) or "none",
        )
        for repo, bow_id, title in self.sources():
            log.debug("  feed %r (%s) -> %s", title, bow_id, repo)
