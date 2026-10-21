"""The workspace janitor: reclaim the disk review rounds leave behind (issue #167).

A reviewer seat is read-only (CR6) and never deletes: `_REVIEWER_RULE` in
loop.py tells every session to write its scratch files under its own checkout
and LEAVE them. Two kinds of folder accumulate under each repo hub that way,
and nothing removed either before this module:

* `<hub>/main/.review-scratch-pr<N>-r<round>/` -- a round's pr.diff, envelope,
  payload files and, often, a `wt/` git worktree (off `<hub>/.source`) with
  its own `node_modules`: 250-950 MB apiece. On 2026-10-12 the Alissa fleet's
  shared volume held 169 of them, 27 GB of a 46 GB volume.
* `<hub>/REVIEW-<task ref>/` -- the throwaway checkout the review skill lets a
  reviewer create beside `main/` when it must run code (`trust.
  review_checkout_name`). Another ~5 GB on the same day. A reviewer whose
  shell sits in its spawn cwd makes it INSIDE `main/` instead
  (`<hub>/main/REVIEW-<task ref>/`): on 2026-10-18 121 of those, 0.6-1.2 GB
  apiece with their own `node_modules`, filled the fleet's 46 GB volume to
  100% while the janitor looked only beside `main/` (issue #177). Both
  places are swept.

Modeled on devloop's janitor.py, but smaller, because what it removes is
smaller in kind: a reviewer's folders hold no unpushed work by construction
(the seat never pushes and never commits for keeps), so there is no holdout
policy and no branch to delete -- only a folder that is old, and that no live
session and no open ledger round still holds. The HOLD is the caller's
(`loop.ReviewWatcher._janitor_guard`, which owns the roster and the ledger);
this module states what a folder is, whether a `Guard` holds it, and removes
it.

Three properties everything here is written around:

* **Only the two shapes, only where they are made.** A scratch folder is a
  `.review-scratch-*` DIRECT child of `<hub>/main`, a review checkout a
  `REVIEW-*` direct child of the hub or of `<hub>/main`; symlinks are never
  candidates. Nothing else under a hub -- `main/`'s own files and its
  `node_modules`, `.source`, a task worktree, an operator's clone -- is ever
  considered, so it is never at risk. A review checkout that is a linked git
  worktree is removed with `git worktree remove --force`, so its record goes
  with it; anything else with `shutil.rmtree`.
* **Old AND unheld.** Age is the newest mtime of the folder and its direct
  children (so a round that writes a fresh file into an old folder renews it),
  and the grace is a day by default. A held folder is kept whatever its age.
  Under disk PRESSURE (issue #177) the grace drops -- to two hours by default
  -- oldest first, only while the volume is still at or above its warn line.
  A round that names no review task yet holds only its hub's checkouts made
  since it spawned (issue #185), and at or above `BACKSTOP_PCT` no such
  hub-wide hold reaches a checkout older than the stale-round window.
* **Nothing raises past its own concern.** A folder that vanishes mid-sweep,
  an unreadable hub, a failed removal: logged and skipped, because this runs
  at the end of a poll pass that must complete.

Two more bound it (issue #175). The layout is fixed: hubs are found at
`<root>/<repo>` with the session cwd at `<hub>/main`, so under any other
`hub_template` (`layout_supported` says no) the caller sweeps nothing rather
than mis-key the hold. And one sweep removes at most `MAX_REMOVALS` folders in
`MAX_SWEEP_SECONDS`; the rest wait for the next interval (`deferred`).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .alissa import session_repo_slug
from .proc import CommandError, run
from .trust import REVIEW_CHECKOUT_PREFIX

log = logging.getLogger(__name__)

# The scratch folder a reviewer writes under its spawn cwd (`<hub>/main`).
SCRATCH_PREFIX = ".review-scratch-"

# What a scratch folder's name says about its round: `pr<N>` and, usually,
# `-r<round>`. Prefix-matched after SCRATCH_PREFIX, so a suffix a session
# appended (`-2`, `.old`) still names its PR. A name that does not parse
# names no PR, and is held whenever anything in its hub is (see Guard).
_SCRATCH_PR_RE = re.compile(r"pr(?P<number>\d+)(?:-r(?P<round>\d+))?")

# The hub's bare repository, which every worktree (`main/`, a REVIEW-
# checkout, a scratch folder's `wt/`) points into. Its presence is what makes
# a workspace child a hub.
SOURCE_DIR = ".source"

KIND_SCRATCH = "scratch"
KIND_REVIEW = "review-checkout"

# One sweep's bound (issue #175): at most this many removals, and no removal
# started past this many seconds into the sweep. It runs synchronously at the
# end of a poll pass, so a large backlog is drained over several intervals
# instead of stalling one pass for minutes.
MAX_REMOVALS = 25
MAX_SWEEP_SECONDS = 120.0

# The one hub layout the janitor knows: `discover_hubs` reads the root's
# direct children and `scan_hub` reads `<hub>/main`. Named in the caller's
# refusal WARNING.
SUPPORTED_TEMPLATE = "{root}/{repo}/main"

# What `layout_supported` probes a template with: two owners that differ, so a
# template that puts the owner in the path resolves to two different places.
_PROBE_OWNERS = ("janitor-probe-owner-a", "janitor-probe-owner-b")
_PROBE_REPO = "janitor-probe-repo"

# How far before a round's spawn time a checkout may be stamped and still
# be held by that round when its review-task ref is unknown (issue #185):
# clock skew between the ledger's stamp and the filesystem's.
HOLD_SKEW_SECONDS = 5 * 60

# The volume used percentage at or above which the pressure backstop applies
# (issue #185): a hub-wide hold is void for checkouts older than the stale-
# round window, and one WARNING names what holds the most bytes.
BACKSTOP_PCT = 95.0

# How long a held folder's cached size is trusted (issue #187). The cache is
# keyed on the folder's newest mtime, which only sees the folder and its
# direct children: a write deeper down (an `npm ci` filling `node_modules`)
# leaves it unchanged. So an entry is re-walked once it is this old, mtime
# or not.
SIZE_CACHE_SECONDS = 60 * 60


@dataclass
class HeldWalk:
    """A held folder's size walk that ran out of budget (issue #189), kept
    so the next sweep resumes it instead of starting over: the folder's
    newest mtime when the walk began, the sweep `now` it began at, the bytes
    counted so far, and the directories still to read. A resumed walk's
    total may count a directory removed since it was read; it never counts a
    directory twice.

    `advanced` is the sweep `now` the walk last read a directory (issue
    #191), 0 until it has. Kept walks resume least-recently-advanced first,
    so two walks that each need several sweeps take turns: the one that just
    had the leftover budget waits behind the one that did not."""

    mtime: float
    started: float
    total: int
    stack: "list[str]"
    advanced: float = 0.0


# One held folder's cached size, by path: either a finished walk, as
# (newest mtime, bytes, the sweep `now` the walk began at), or a `HeldWalk`
# that the time budget cut short (issue #189).
SizeCache = dict[Path, "tuple[float, int, float] | HeldWalk"]

# The two reasons a hub-wide hold gives, so the sweep summary groups them.
HUB_HOLD_BOUNDED = "a live round in this hub names no review task and spawned before this checkout"
HUB_HOLD_UNBOUNDED = "a live round in this hub names no review task or spawn time"

Runner = Callable[..., str]


@dataclass(frozen=True)
class Candidate:
    """One folder the janitor may remove: its path, which hub it is under,
    what kind it is, and what its name says it belongs to -- the PR number for
    a scratch folder (None when the name does not parse), the task ref for a
    review checkout."""

    path: Path
    hub: Path
    kind: str
    number: "int | None" = None
    task_ref: "str | None" = None

    @property
    def hub_slug(self) -> str:
        """The hub's repo as a session name carries it (`session_repo_slug`),
        so a hub, a ledger row and a session name compare on one key."""
        return session_repo_slug(self.hub.name)


@dataclass
class Guard:
    """What the live roster and the open ledger rounds hold, per hub.

    Keyed by the repo SLUG (`session_repo_slug` of the repo name) -- the one
    form a hub directory, a ledger row and a session name all reduce to. Two
    repos sharing a name under different owners share a slug, which only ever
    holds MORE than needed. The key None means "every hub": a skill-shaped
    `review-pr-<n>` session names no repo, and holds its PR number anywhere.

    A round whose review-task ref is unknown cannot be mapped to a single
    REVIEW- checkout -- every fresh round starts that way, since the reviewer
    creates its review task after it spawns. Such a round holds the checkouts
    in its hub that are NEWER than its spawn time less `HOLD_SKEW_SECONDS`
    (issue #185): a checkout older than every unknown-ref round in the hub
    cannot belong to any of them. `all_checkouts` keeps, per hub, the
    earliest such bound; None there means a round whose spawn time is not
    known either, and holds every checkout in the hub, as before issue #185
    -- the hold that, with studio reviews running near-continuously, kept 78
    dead checkouts (~44 GB) and filled the fleet's volume on 2026-10-21.
    """

    numbers: "dict[str | None, set[int]]" = field(default_factory=dict)
    tasks: "dict[str | None, set[str]]" = field(default_factory=dict)
    all_checkouts: "dict[str | None, float | None]" = field(default_factory=dict)

    def hold(
        self,
        repo: "str | None",
        number: int,
        task_ref: "str | None" = None,
        since: "float | None" = None,
    ) -> None:
        """Hold PR `number` of `repo` (`owner/name`, a bare name, or a
        session-name slug; None = any hub) and the checkout its task names.
        With no task ref, `since` (the round's spawn time, epoch seconds)
        bounds the checkouts held in the hub; without it, every one is."""
        slug = None if repo is None else session_repo_slug(repo.rpartition("/")[2])
        self.numbers.setdefault(slug, set()).add(int(number))
        ref = (task_ref or "").strip()
        if ref:
            self.tasks.setdefault(slug, set()).add(ref)
            return
        bound = None if since is None else float(since) - HOLD_SKEW_SECONDS
        if slug in self.all_checkouts:
            prev = self.all_checkouts[slug]
            bound = None if prev is None or bound is None else min(prev, bound)
        self.all_checkouts[slug] = bound

    def _in(self, table: "dict[str | None, set]", slug: str, value: object) -> bool:
        return value in table.get(slug, ()) or value in table.get(None, ())

    def _any(self, slug: str) -> bool:
        return bool(self.numbers.get(slug) or self.numbers.get(None))

    def holds(
        self,
        candidate: Candidate,
        mtime: "float | None" = None,
        void_before: "float | None" = None,
    ) -> "str | None":
        """Why `candidate` must stay, or None when nothing holds it.

        `mtime` is the candidate's age stamp (`newest_mtime`); a review
        checkout with none is held by every hub-wide hold, bounded or not.
        `void_before` is the pressure backstop (issue #185): a hub-wide hold
        does not reach a checkout whose stamp is older than it. A known task
        ref's hold and a scratch folder's are never voided."""
        slug = candidate.hub_slug
        if candidate.kind == KIND_SCRATCH:
            if candidate.number is None:
                return "unnamed scratch in a hub with a live round" if self._any(slug) else None
            if self._in(self.numbers, slug, candidate.number):
                return f"PR #{candidate.number} has a live session or open round"
            return None
        if candidate.task_ref and self._in(self.tasks, slug, candidate.task_ref):
            return f"{candidate.task_ref} has a live session or open round"
        if mtime is not None and void_before is not None and mtime < void_before:
            return None
        for key in (slug, None):
            if key not in self.all_checkouts:
                continue
            bound = self.all_checkouts[key]
            if bound is None:
                return HUB_HOLD_UNBOUNDED
            if mtime is None or mtime >= bound:
                return HUB_HOLD_BOUNDED
        return None


@dataclass
class SweepResult:
    """What one sweep did. `removed` and `would_remove` carry (path, bytes);
    `held` and `failed` carry (path, reason)."""

    hubs: int = 0
    removed: "list[tuple[Path, int]]" = field(default_factory=list)
    would_remove: "list[tuple[Path, int]]" = field(default_factory=list)
    held: "list[tuple[Path, str]]" = field(default_factory=list)
    fresh: int = 0
    failed: "list[tuple[Path, str]]" = field(default_factory=list)
    pruned: int = 0
    # Unheld stale candidates the per-sweep bound left for the next interval.
    deferred: int = 0
    # Whether the volume was at or above its warn line when the sweep began,
    # so the pressure grace applied (issue #177), and how many removals (or,
    # in a dry run, would-be removals) only that shorter grace allowed.
    pressure: bool = False
    pressure_removed: int = 0
    # Issue #185: bytes held per "<hub slug>: <reason>", so held growth is
    # visible before it fills the disk; held folders not sized because the
    # sweep's time budget ran out; and, when the backstop applied, how many
    # checkouts only it released from a hub-wide hold.
    held_bytes: "dict[str, int]" = field(default_factory=dict)
    held_unsized: int = 0
    # Issue #187: held folders whose size came from the cache, not a walk
    # this sweep, so the summary can say its figure may be up to
    # SIZE_CACHE_SECONDS old.
    held_cached: int = 0
    # Issue #189: unsized held folders whose walk is kept to resume next
    # sweep (a subset of `held_unsized`).
    held_resuming: int = 0
    backstop: bool = False
    backstop_released: int = 0

    @property
    def freed(self) -> int:
        return sum(size for _, size in self.removed)

    @property
    def held_total(self) -> int:
        return sum(self.held_bytes.values())

    def top_held(self, limit: int = 5) -> "list[tuple[str, int]]":
        """The reasons holding the most bytes, largest first."""
        return sorted(self.held_bytes.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    @property
    def left(self) -> int:
        """Candidate folders still on disk after a live sweep: inside the
        grace, held, deferred, or failed."""
        return self.fresh + len(self.held) + self.deferred + len(self.failed)


def held_reasons(pairs: "Iterable[tuple[str, int]]") -> str:
    """`; <hub>: <reason> = <size>, ...` for the log lines, or '' when none."""
    parts = [f"{key} = {human(size)}" for key, size in pairs]
    return ("; " + ", ".join(parts)) if parts else ""


# How many held reasons the sweep summary names before folding the rest.
HELD_REASONS_LOGGED = 10


def held_summary(result: SweepResult) -> str:
    """The sweep summary's held-bytes tail (issue #185): every hub/reason
    holding bytes, largest first, at most HELD_REASONS_LOGGED of them, and
    how many held folders were sized from the cache or went unsized."""
    tail = held_reasons(result.top_held(HELD_REASONS_LOGGED))
    rest = len(result.held_bytes) - HELD_REASONS_LOGGED
    if rest > 0:
        tail += f", +{rest} more reason(s)"
    if result.held_cached:
        tail += (
            f"; {result.held_cached} held size(s) cached, up to "
            f"{SIZE_CACHE_SECONDS // 60} min old"
        )
    if result.held_unsized:
        tail += f"; {result.held_unsized} held folder(s) not sized this sweep"
        if result.held_resuming:
            tail += f" ({result.held_resuming} walk(s) resume next sweep)"
    return tail


def layout_supported(
    hub_for: "Callable[[str, str], Path]", workspace_root: "Path | str"
) -> bool:
    """Whether `hub_for` (`Config.hub_for`) places every hub at
    `<root>/<repo>` with the session cwd at `<hub>/main` -- the one layout
    `discover_hubs` and `scan_hub` read, and the one in which a hub's
    directory name is the repo slug the `Guard` keys on.

    Probed with two distinct owners and one repo name: both must resolve to
    `workspace_root / repo / "main"`. A template that cannot be formatted is
    unsupported. Pure: nothing is read off the disk."""
    expected = Path(os.fspath(workspace_root)).expanduser() / _PROBE_REPO / "main"
    try:
        return all(
            Path(hub_for(owner, _PROBE_REPO)) == expected for owner in _PROBE_OWNERS
        )
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        return False


def discover_hubs(workspace_root: "Path | str") -> "list[Path]":
    """Every worktree hub directly under the workspace root: a non-symlink
    directory holding a `.source` directory. Read off the disk rather than the
    allowlist, so a hub whose repo left the allowlist (or a static `repos: []`
    deployment, which names none) is still swept."""
    root = Path(workspace_root)
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        log.warning("janitor: cannot list workspace %s (%s) — no sweep", root, exc)
        return []
    hubs = []
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            source = child / SOURCE_DIR
            if source.is_dir() and not source.is_symlink():
                hubs.append(child)
        except OSError:
            continue
    return hubs


def _dirs(parent: Path, prefix: str) -> "list[Path]":
    """Non-symlink directories directly under `parent` whose name starts
    with `prefix`, sorted; empty when `parent` cannot be listed."""
    try:
        entries = sorted(parent.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    found = []
    for entry in entries:
        if not entry.name.startswith(prefix) or entry.name == prefix:
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        found.append(entry)
    return found


def scan_hub(hub: Path) -> "list[Candidate]":
    """The janitor's candidates in one hub: `main/.review-scratch-*`,
    `REVIEW-*` and `main/REVIEW-*` (issue #177), direct children only, never
    a walk."""
    found = []
    for path in _dirs(hub / "main", SCRATCH_PREFIX):
        match = _SCRATCH_PR_RE.match(path.name[len(SCRATCH_PREFIX):])
        found.append(Candidate(
            path=path, hub=hub, kind=KIND_SCRATCH,
            number=int(match["number"]) if match else None,
        ))
    for path in _dirs(hub, REVIEW_CHECKOUT_PREFIX) + _dirs(
        hub / "main", REVIEW_CHECKOUT_PREFIX
    ):
        found.append(Candidate(
            path=path, hub=hub, kind=KIND_REVIEW,
            task_ref=path.name[len(REVIEW_CHECKOUT_PREFIX):],
        ))
    return found


def newest_mtime(path: Path) -> "float | None":
    """The newest mtime of `path` and its direct children (lstat, so a
    symlink's target is never read), or None when `path` cannot be read.
    One level, not a walk: a round writing into its folder touches the
    folder or a direct child, and a `node_modules` walk every sweep is the
    cost this avoids."""
    try:
        newest = path.lstat().st_mtime
    except OSError:
        return None
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return newest


def dir_bytes(path: "Path | str") -> int:
    """Bytes allocated under `path` (st_blocks, the figure `du` reports and a
    removal frees), following no symlinks. Iterative, and every per-entry
    error is skipped -- devloop's `dir_size` walk, counting blocks."""
    return _walk_bytes(path, None) or 0


def held_bytes_of(
    path: "Path | str",
    stop: "Callable[[], bool]",
    progress: "HeldWalk | None" = None,
) -> "int | None":
    """`dir_bytes` for a held folder (issue #187): `stop` is asked before
    each directory is read, and the walk gives up -- None, never a partial
    figure -- the moment it answers True, so one large tree cannot run the
    sweep past its time budget. With `progress` (issue #189) the walk starts
    from its `total` and `stack` rather than from `path`, and an abandoned
    walk leaves its place there, so a later call carries on from it."""
    return _walk_bytes(path, stop, progress)


def _walk_bytes(
    path: "Path | str",
    stop: "Callable[[], bool] | None",
    progress: "HeldWalk | None" = None,
) -> "int | None":
    total = progress.total if progress is not None else 0
    stack = progress.stack if progress is not None else [os.fspath(path)]
    while stack:
        if stop is not None and stop():
            if progress is not None:
                progress.total = total  # `stack` is progress.stack, in place
            return None
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        st = entry.stat(follow_symlinks=False)
                        blocks = getattr(st, "st_blocks", None)
                        total += blocks * 512 if blocks is not None else st.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def human(size: "int | float") -> str:
    """Bytes as a short binary-unit string (`1.4 GiB`), as `du -h` prints."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024.0:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def disk_used_pct(path: "Path | str") -> "float | None":
    """Used percent of the volume holding `path` (one statvfs, rounded to a
    tenth), or None when it cannot be read."""
    try:
        usage = shutil.disk_usage(os.fspath(path))
    except OSError:
        return None
    if not usage.total:
        return None
    return round(100.0 * usage.used / usage.total, 1)


def _contained(candidate: Candidate) -> bool:
    """The containment rule, re-asserted at the removal site: the folder is
    a direct child of `<hub>/main` (scratch) or of its hub or `<hub>/main`
    (review checkout), all resolved, and not a symlink."""
    main = candidate.hub / "main"
    parents = (main,) if candidate.kind == KIND_SCRATCH else (candidate.hub, main)
    try:
        if candidate.path.is_symlink():
            return False
        parent = candidate.path.resolve().parent
        return any(parent == allowed.resolve() for allowed in parents)
    except OSError:
        return False


def _is_linked_worktree(path: Path) -> bool:
    """Whether `path` is a linked git worktree: its `.git` is a FILE (the
    `gitdir:` pointer `git worktree add` writes), not a directory."""
    try:
        git = path / ".git"
        return git.is_file() and not git.is_symlink()
    except OSError:
        return False


def _remove(candidate: Candidate, runner: Runner) -> None:
    """Remove one contained candidate. A review checkout that is a linked
    worktree goes through `git worktree remove --force` in its hub's
    `.source` (issue #177), so the record and the folder go together; when
    git refuses (a worktree of another repository, a locked one) or the
    folder is anything else, `shutil.rmtree` removes it and the hub's prune
    drops whatever record is left. Raises OSError when the folder stays."""
    if candidate.kind == KIND_REVIEW and _is_linked_worktree(candidate.path):
        try:
            runner(
                ["git", "-C", str(candidate.hub / SOURCE_DIR), "worktree",
                 "remove", "--force", str(candidate.path)],
                timeout=120,
            )
        except (CommandError, OSError) as exc:
            log.debug(
                "janitor: git worktree remove refused %s (%s) — removing the "
                "folder instead", candidate.path, exc,
            )
        if not os.path.lexists(candidate.path):
            return
    shutil.rmtree(candidate.path)


def prune_worktrees(hub: Path, runner: Runner = run) -> None:
    """`git worktree prune` in the hub's `.source`, as this process's user
    (the seat's own): drops the records of worktrees whose folders are gone
    -- the scratch `wt/`s and REVIEW- checkouts this sweep removed, and any
    an operator removed by hand."""
    runner(["git", "-C", str(hub / SOURCE_DIR), "worktree", "prune"], timeout=120)


def sweep(
    hubs: "Iterable[Path]",
    guard: Guard,
    *,
    grace_seconds: float,
    now: float,
    dry_run: bool = False,
    runner: Runner = run,
    max_removals: int = MAX_REMOVALS,
    max_seconds: float = MAX_SWEEP_SECONDS,
    clock: "Callable[[], float]" = time.monotonic,
    pressure_grace_seconds: "float | None" = None,
    under_pressure: "Callable[[], bool] | None" = None,
    stale_round_seconds: "float | None" = None,
    sizes: "SizeCache | None" = None,
) -> SweepResult:
    """Remove every candidate under `hubs` older than `grace_seconds` that
    `guard` does not hold, oldest first, then prune each hub's worktree
    records.

    Under pressure (issue #177): when `under_pressure` is given and answers
    True at the start of the sweep, a candidate younger than `grace_seconds`
    but older than `pressure_grace_seconds` is removable too -- but only
    while `under_pressure()`, asked again before each such removal, still
    answers True. Candidates are taken oldest first across every hub, so the
    oldest go first and the sweep stops shrinking the grace the moment the
    volume is back under its line. A held folder is never removed.

    Bounded (issue #175): before each removal, a sweep that has already
    removed `max_removals` folders, or has run `max_seconds` by `clock`,
    stops removing. Every unheld stale candidate past that point is counted
    in `deferred` and left for the next interval; every hub is still scanned
    and pruned.

    Dry-run removes nothing and prunes nothing: it logs each folder it WOULD
    remove (with its size) and reports them in `would_remove`, under the
    same bound -- so it reports what one live sweep would do (a dry run frees
    nothing, so under pressure it reports every pressure-eligible folder).

    The pressure backstop (issue #185): when `stale_round_seconds` is given
    (the caller passes STALE_ROUND_SECONDS at or above `BACKSTOP_PCT`), a
    hub-wide hold -- a round with no review-task ref -- is void for a
    checkout older than that window: no live round can own a checkout older
    than the stale window. `backstop_released` counts what only it freed.

    Every held folder is sized into `held_bytes` per hub and reason, AFTER
    the removal loop and on whatever is left of the time budget (issue
    #187): removal never pays for observability. A walk that reaches the
    budget mid-tree is abandoned, and every folder left unsized is counted in
    `held_unsized`. `sizes` (a `SizeCache`), when given, caches those walks
    across sweeps and is re-keyed to this sweep's held folders; an entry is
    reused while the folder's mtime is unchanged AND it is younger than
    SIZE_CACHE_SECONDS by `now`, and each reuse is counted in `held_cached`.
    An abandoned walk is kept there too (issue #189): the next sweep sizes
    every other held folder first and then resumes it where it stopped, so
    one large tree neither starves the folders behind it nor restarts from
    scratch every sweep."""
    result = SweepResult()
    started = clock()
    bounded = False
    hub_list = list(hubs)
    shrunk = grace_seconds
    if pressure_grace_seconds is not None and under_pressure is not None:
        result.pressure = under_pressure()
        if result.pressure:
            shrunk = min(grace_seconds, pressure_grace_seconds)
    void_before = None
    if stale_round_seconds is not None:
        result.backstop = True
        void_before = now - stale_round_seconds
    held_queue: "list[tuple[float, Candidate, str]]" = []
    aged: "list[tuple[float, Candidate]]" = []
    for hub in hub_list:
        result.hubs += 1
        for candidate in scan_hub(hub):
            mtime = newest_mtime(candidate.path)
            if mtime is None:
                continue  # vanished mid-sweep
            aged.append((mtime, candidate))
    aged.sort(key=lambda pair: (pair[0], str(pair[1].path)))
    for mtime, candidate in aged:
        age = now - mtime
        if age < shrunk:
            result.fresh += 1
            continue
        why = guard.holds(candidate, mtime)
        if why is not None and void_before is not None:
            if guard.holds(candidate, mtime, void_before) is None:
                result.backstop_released += 1
                log.info(
                    "janitor: backstop releases %s (%.1f h old) from a hub-wide "
                    "hold — %s", candidate.path, age / 3600, why,
                )
                why = None
        if why is not None:
            log.debug("janitor: keeping %s — %s", candidate.path, why)
            result.held.append((candidate.path, why))
            held_queue.append((mtime, candidate, f"{candidate.hub_slug}: {why}"))
            continue
        early = age < grace_seconds
        if early and not dry_run and not (under_pressure and under_pressure()):
            # Back under the line: the normal grace holds again.
            result.fresh += 1
            continue
        if not bounded:
            taken = len(result.would_remove if dry_run else result.removed)
            bounded = taken >= max_removals or clock() - started >= max_seconds
        if bounded:
            result.deferred += 1
            continue
        if not _contained(candidate):
            result.failed.append((candidate.path, "not contained in its hub"))
            continue
        size = dir_bytes(candidate.path)
        if dry_run:
            log.info(
                "[dry-run] janitor would remove %s (%s, %.1f h old%s)",
                candidate.path, human(size), age / 3600,
                ", under pressure" if early else "",
            )
            result.would_remove.append((candidate.path, size))
            if early:
                result.pressure_removed += 1
            continue
        try:
            _remove(candidate, runner)
        except OSError as exc:
            log.warning("janitor: could not remove %s (%s)", candidate.path, exc)
            result.failed.append((candidate.path, str(exc)))
            continue
        log.debug("janitor: removed %s (%s)", candidate.path, human(size))
        result.removed.append((candidate.path, size))
        if early:
            result.pressure_removed += 1
    _size_held(held_queue, result, sizes, now=now,
               out_of_time=lambda: clock() - started >= max_seconds)
    if dry_run:
        return result
    for hub in hub_list:
        try:
            prune_worktrees(hub, runner)
            result.pruned += 1
        except (CommandError, OSError) as exc:
            log.warning("janitor: git worktree prune failed in %s (%s)", hub, exc)
    return result


def _size_held(
    queue: "list[tuple[float, Candidate, str]]",
    result: SweepResult,
    sizes: "SizeCache | None",
    *,
    now: float,
    out_of_time: "Callable[[], bool]",
) -> None:
    """Size the sweep's held folders into `result.held_bytes`, after the
    removals (issue #187). A fresh cache entry costs nothing and is always
    used; anything else is walked only while `out_of_time()` answers False,
    and a walk is abandoned the moment it does.

    An abandoned walk is kept in `sizes` as a `HeldWalk` (issue #189). Next
    sweep its folder goes to the back of the queue, behind every folder
    whose walk was not cut short, and its walk resumes from where it stopped
    -- from scratch if the folder's mtime moved. So a tree too big for one
    sweep's leftover budget no longer leaves the folders behind it unsized
    on every sweep, and still gets its figure after a few. A finished
    resumed walk is cached as of the sweep it began in, so the hourly
    re-walk counts from the oldest directory it read.

    Kept walks go least-recently-advanced first, not oldest first (issue
    #191). Otherwise a walk that needs more than SIZE_CACHE_SECONDS of
    sweeps -- whose figure has expired by the sweep after it lands, so it
    starts over -- takes every sweep's leftover budget, and a younger kept
    walk never reads a directory.

    `sizes` is then re-keyed to this sweep's held folders: entries for gone
    folders drop out, and so do unsized folders whose walk never began."""
    previous = sizes or {}

    def turn(item: "tuple[float, Candidate, str]") -> "tuple[bool, float]":
        kept = previous.get(item[1].path)
        if isinstance(kept, HeldWalk):
            return True, kept.advanced
        return False, 0.0

    seen: SizeCache = {}
    # Stable: oldest first among the folders not cut short and among kept
    # walks that last advanced in the same sweep, as the sweep queued them.
    for mtime, candidate, key in sorted(queue, key=turn):
        cached = previous.get(candidate.path)
        if (isinstance(cached, tuple) and cached[0] == mtime
                and now - cached[2] < SIZE_CACHE_SECONDS):
            size = cached[1]
            seen[candidate.path] = cached
            result.held_cached += 1
            result.held_bytes[key] = result.held_bytes.get(key, 0) + size
            continue
        resumable = isinstance(cached, HeldWalk)
        if isinstance(cached, HeldWalk) and cached.mtime == mtime:
            progress = cached
        else:
            progress = HeldWalk(mtime, now, 0, [os.fspath(candidate.path)],
                                cached.advanced if isinstance(cached, HeldWalk) else 0.0)
        walked = None
        if not out_of_time():
            progress.advanced = now
            walked = held_bytes_of(candidate.path, out_of_time, progress)
            resumable = resumable or walked is None
        if walked is None:
            result.held_unsized += 1
            if resumable:
                seen[candidate.path] = progress
                result.held_resuming += 1
            continue
        seen[candidate.path] = (mtime, walked, progress.started)
        result.held_bytes[key] = result.held_bytes.get(key, 0) + walked
    if sizes is not None:
        sizes.clear()
        sizes.update(seen)
