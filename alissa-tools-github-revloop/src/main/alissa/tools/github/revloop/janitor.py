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
  review_checkout_name`). Another ~5 GB on the same day.

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
  `REVIEW-*` direct child of the hub; symlinks are never candidates. Nothing
  else under a hub -- `main/`'s own files, `.source`, a task worktree, an
  operator's clone -- is ever considered, so it is never at risk.
* **Old AND unheld.** Age is the newest mtime of the folder and its direct
  children (so a round that writes a fresh file into an old folder renews it),
  and the grace is a day by default. A held folder is kept whatever its age.
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
    REVIEW- checkout, so it holds every checkout in its hub (`all_checkouts`).
    """

    numbers: "dict[str | None, set[int]]" = field(default_factory=dict)
    tasks: "dict[str | None, set[str]]" = field(default_factory=dict)
    all_checkouts: "set[str | None]" = field(default_factory=set)

    def hold(
        self, repo: "str | None", number: int, task_ref: "str | None" = None
    ) -> None:
        """Hold PR `number` of `repo` (`owner/name`, a bare name, or a
        session-name slug; None = any hub) and the checkout its task names."""
        slug = None if repo is None else session_repo_slug(repo.rpartition("/")[2])
        self.numbers.setdefault(slug, set()).add(int(number))
        ref = (task_ref or "").strip()
        if ref:
            self.tasks.setdefault(slug, set()).add(ref)
        else:
            self.all_checkouts.add(slug)

    def _in(self, table: "dict[str | None, set]", slug: str, value: object) -> bool:
        return value in table.get(slug, ()) or value in table.get(None, ())

    def _any(self, slug: str) -> bool:
        return bool(self.numbers.get(slug) or self.numbers.get(None))

    def holds(self, candidate: Candidate) -> "str | None":
        """Why `candidate` must stay, or None when nothing holds it."""
        slug = candidate.hub_slug
        if candidate.kind == KIND_SCRATCH:
            if candidate.number is None:
                return "unnamed scratch in a hub with a live round" if self._any(slug) else None
            if self._in(self.numbers, slug, candidate.number):
                return f"PR #{candidate.number} has a live session or open round"
            return None
        if slug in self.all_checkouts or None in self.all_checkouts:
            return "a live round in this hub names no review task"
        if candidate.task_ref and self._in(self.tasks, slug, candidate.task_ref):
            return f"{candidate.task_ref} has a live session or open round"
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

    @property
    def freed(self) -> int:
        return sum(size for _, size in self.removed)


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
    """The janitor's candidates in one hub: `main/.review-scratch-*` and
    `REVIEW-*`, direct children only, never a walk."""
    found = []
    for path in _dirs(hub / "main", SCRATCH_PREFIX):
        match = _SCRATCH_PR_RE.match(path.name[len(SCRATCH_PREFIX):])
        found.append(Candidate(
            path=path, hub=hub, kind=KIND_SCRATCH,
            number=int(match["number"]) if match else None,
        ))
    for path in _dirs(hub, REVIEW_CHECKOUT_PREFIX):
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
    total = 0
    stack = [os.fspath(path)]
    while stack:
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
    a direct child of its hub (review checkout) or of `<hub>/main` (scratch),
    both resolved, and not a symlink."""
    parent = candidate.hub / "main" if candidate.kind == KIND_SCRATCH else candidate.hub
    try:
        if candidate.path.is_symlink():
            return False
        return candidate.path.resolve().parent == parent.resolve()
    except OSError:
        return False


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
) -> SweepResult:
    """Remove every candidate under `hubs` older than `grace_seconds` that
    `guard` does not hold, then prune each hub's worktree records.

    Bounded (issue #175): before each removal, a sweep that has already
    removed `max_removals` folders, or has run `max_seconds` by `clock`,
    stops removing. Every unheld stale candidate past that point is counted
    in `deferred` and left for the next interval; every hub is still scanned
    and pruned.

    Dry-run removes nothing and prunes nothing: it logs each folder it WOULD
    remove (with its size) and reports them in `would_remove`, under the
    same bound -- so it reports what one live sweep would do."""
    result = SweepResult()
    started = clock()
    bounded = False
    for hub in hubs:
        result.hubs += 1
        for candidate in scan_hub(hub):
            mtime = newest_mtime(candidate.path)
            if mtime is None:
                continue  # vanished mid-sweep
            if now - mtime < grace_seconds:
                result.fresh += 1
                continue
            why = guard.holds(candidate)
            if why is not None:
                log.debug("janitor: keeping %s — %s", candidate.path, why)
                result.held.append((candidate.path, why))
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
                    "[dry-run] janitor would remove %s (%s, %.1f h old)",
                    candidate.path, human(size), (now - mtime) / 3600,
                )
                result.would_remove.append((candidate.path, size))
                continue
            try:
                shutil.rmtree(candidate.path)
            except OSError as exc:
                log.warning("janitor: could not remove %s (%s)", candidate.path, exc)
                result.failed.append((candidate.path, str(exc)))
                continue
            log.debug("janitor: removed %s (%s)", candidate.path, human(size))
            result.removed.append((candidate.path, size))
        if dry_run:
            continue
        try:
            prune_worktrees(hub, runner)
            result.pruned += 1
        except (CommandError, OSError) as exc:
            log.warning("janitor: git worktree prune failed in %s (%s)", hub, exc)
    return result
