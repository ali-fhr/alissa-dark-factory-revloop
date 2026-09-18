"""Hub trust: keeping Claude Code's per-directory "trust this folder?" gate
pre-accepted for every directory a reviewer session may start in (issue
#136; the reviewer-side sibling of devloop PR #124 / issue #123).

Claude Code asks, once per directory, whether the folder it was started in
is trusted -- and `--dangerously-skip-permissions` does NOT suppress that
dialog. A headless session that meets it hangs: the directive `alissa tmux
queue` types into the pane is swallowed by the prompt, the session sits in
the roster reading alive, and the stale-round probe (correctly) declines to
respawn over a live session -- `round k is stale but session … is still
active — not respawning over a live reviewer`, every poll, for as long as
the pane sits there.

The container entrypoint seeds the gate at boot for the hubs it can NAME at
boot: the `ALISSA_REVIEW_REPOS` allowlist plus every hub already on disk.
Under `repos_source: bows` `ALISSA_REVIEW_REPOS` is empty -- the allowlist
is DERIVED from the feed Bodies of Work at runtime -- so a repo hub-ified
after boot was never trusted. This module closes that gap from the daemon's
side, in three places the loop calls:

* after every successful feed refresh: the derived list is written to
  `{root}/.alissa-derived-repos` (`DERIVED_REPOS_FILENAME`) so the NEXT
  boot's entrypoint seeding can read it, and the derived hubs are trusted
  right away, hub-ified or not (trusting a path that does not exist yet is
  exactly what the entrypoint already does for the static allowlist);
* at hub-ify time and before every spawn (`ReviewWatcher._ensure_hub`): the
  hub root, `{hub}/main` (where the reviewer starts, per `hub_template`),
  the `REVIEW-<task>` checkout the review skill may create for the PR head,
  and every such checkout already on disk;
* after a first-run-dialog wedge kill: the hub the wedged session started
  in, so the re-queued round does not meet the same prompt.

WHAT IS TRUSTED IS THE REVIEWER'S SHAPE, NOT THE DEVELOPER'S. devloop's
sessions start in the hub root and create `TASK-*` worktrees; this daemon
starts a session in `{root}/{repo}/main` (the `hub_template` default, the
docker README's "Reviewers `cd` into `{root}/{repo}/main`"), and the
alissa-code-review skill tells a reviewer that must run code to make a
throwaway `REVIEW-TASK-<id>` worktree beside `main/`. So `hub_trust_paths`
lists the hub root, `main/`, the session cwd when the template puts it
elsewhere, the checkout named for the round's task, and every `REVIEW-*`
directory on disk.

The merge is the entrypoint's, transposed: load-then-update of
`~/.claude.json` and `$CLAUDE_CONFIG_DIR/.claude.json` (when set --
`CLAUDE_CONFIG_DIR` reliably relocates only the credential file, so both
targets are written and whichever claude reads carries the flag), only ever
ADDING `projects[<path>].hasTrustDialogAccepted = true`. Idempotent: a target
that already carries every entry is not rewritten at all, which is also what
keeps the daemon's writes off a file Claude Code itself rewrites -- the only
time this module touches the file is when a NEW directory has to be
trusted. That write is guarded on three sides, because Claude Code itself
rewrites the same file while sessions run: the temp-file + rename keeps a
concurrent READER from ever seeing a torn file; a compare-and-swap
immediately before the rename (the file must still hold the bytes the merge
was computed from) keeps the OTHER WRITER's update from being discarded
wholesale -- the payload is the whole file, so a lost update could drop
another session's `projects` entries or the persisted login; and a re-read
after the rename catches a writer that landed right after ours and dropped
OUR entries. Either miss restarts the merge on top of the newer file, a
bounded number of times, and giving up is one WARNING. Never raises: trust
is a convenience for the session about to spawn, and a failed write is one
WARNING, never a lost poll.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable, Sequence

log = logging.getLogger(__name__)

# The file the daemon writes the derived (bows-mode) allowlist to, under the
# workspace root, one `owner/repo` per line -- and the file the entrypoint's
# first-run seeding reads at the NEXT boot (`ALISSA_DERIVED_REPOS_FILE`
# overrides the location on both sides). The same name and override as
# devloop's, so an operator who knows one image knows the other.
DERIVED_REPOS_FILENAME = ".alissa-derived-repos"
ENV_DERIVED_REPOS_FILE = "ALISSA_DERIVED_REPOS_FILE"

# The state file Claude Code keeps its per-directory trust in, relative to
# $HOME and to $CLAUDE_CONFIG_DIR.
CLAUDE_STATE_FILENAME = ".claude.json"
ENV_CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"
TRUST_KEY = "hasTrustDialogAccepted"

# The throwaway worktree the alissa-code-review skill tells a reviewer to
# create beside `main/` when it must run code: `REVIEW-TASK-<id>`. Listed by
# prefix so a checkout already on disk is trusted whatever task it was for.
REVIEW_CHECKOUT_PREFIX = "REVIEW-"

# What a pane PARKED on one of Claude Code's first-run gates shows -- and
# what corroborates that it is parked there rather than merely mentioning
# it (devloop PR #124 review round 1). The gates' words are quoted by this
# repo's README, CHANGELOG, this module and issue #136, so a session that
# cats any of them, greps the tree or shows a diff has them on screen while
# at work -- and a match kills the session. The classifier therefore asks
# for the gate's SHAPE, not its vocabulary (see `pane_shows_first_run_dialog`):
#
# * the gate's ACCEPT option (`FIRST_RUN_DIALOG_OPTIONS`) is one of the last
#   `FIRST_RUN_DIALOG_TAIL_LINES` non-blank lines of the capture, once the
#   selection cursor, option numbering and box drawing are stripped;
# * nothing follows it but the gate's own chrome (`FIRST_RUN_DIALOG_CHROME`):
#   a session that printed ANYTHING after the text -- a spinner, a tool
#   call, the `>` input prompt an idle session sits at, a shell prompt --
#   is not on the gate;
# * the gate's QUESTION (`FIRST_RUN_DIALOG_MARKERS`) appears STRICTLY above
#   the option line -- the markers are the questions' own words, none of
#   which the accept options contain, so the option line can never stand
#   in for its question (PR #137 review round 1: with `trust this folder`
#   as the marker and the option line counted, `Yes, I trust this folder`
#   alone satisfied this leg).
#
# The trust dialog reads "Quick safety check: Is this a project you created
# or one you trust? … ❯ No, exit / Yes, I trust this folder"; the
# bypass-permissions gate reads "WARNING: Claude Code running in Bypass
# Permissions mode … No, exit / Yes, I accept" (either option order, with
# or without numbering, an optional "Enter to confirm · Esc to exit" footer
# and box drawing). Both are gates the entrypoint's seeding is meant to
# pre-answer, so a pane parked on either is a session the seeding missed,
# not a session at work. devloop's `hubs.py` checks the same first two legs
# on the same fixtures (shared verbatim); its third leg still counts the
# option line and carries `trust this folder` as the marker, which this
# module tightened -- a devloop follow-up, so the two seats agree again.
# The trust gate's older wording ("Do you trust the files in this folder?")
# is kept so a pinned Claude Code that still draws it is a gate too.
FIRST_RUN_DIALOG_MARKERS = (
    "quick safety check",
    "is this a project you created",
    "trust the files in this folder",
    "bypass permissions mode",
)
FIRST_RUN_DIALOG_OPTIONS = (
    "yes, i trust this folder",
    "yes, i accept",
)
FIRST_RUN_DIALOG_CHROME = (
    "no, exit",
    "enter to confirm",
)
FIRST_RUN_DIALOG_TAIL_LINES = 6

# What a gate draws AROUND its text: box drawing, the selection cursor and
# whitespace (stripped from both ends of a line), then "1." / "2)" option
# numbering. Nothing else is stripped -- a diff's `+`, a markdown bullet,
# a quote's backtick and the `>` of Claude Code's input prompt all stay,
# which is what keeps a line that merely quotes a gate from reading as one.
_DIALOG_DECORATION = "❯ \t│┃║╭╮╰╯┌┐└┘├┤┬┴┼─━═"
_DIALOG_NUMBERING = re.compile(r"^\d+[.)]\s*")

# How many times `seed_trust` restarts a merge that another writer moved
# the file under (see the module docstring) before it gives up with a
# WARNING. Claude Code's own rewrites are bursts around a session's start
# and end, not a stream, so a second pass is expected to land.
SEED_TRUST_ATTEMPTS = 3

# The wedge classification the loop logs and records for a stale round
# whose session's pane shows one of those gates.
WEDGE_FIRST_RUN_DIALOG = "wedged:first-run-dialog"


def claude_state_targets(
    home: "Path | str | None" = None, config_dir: "Path | str | None" = None
) -> "list[Path]":
    """The `.claude.json` files to merge trust into: `$HOME/.claude.json`,
    plus `$CLAUDE_CONFIG_DIR/.claude.json` when that variable is set and
    non-blank. Both arguments default to the environment (`~` expansion
    honours `HOME`), which is what lets a test point the merge at scratch
    directories without touching the daemon's own files."""
    home_dir = Path(home) if home is not None else Path(os.path.expanduser("~"))
    targets = [home_dir / CLAUDE_STATE_FILENAME]
    raw = (
        str(config_dir)
        if config_dir is not None
        else os.environ.get(ENV_CLAUDE_CONFIG_DIR, "")
    ).strip()
    if raw:
        extra = Path(raw) / CLAUDE_STATE_FILENAME
        if extra != targets[0]:
            targets.append(extra)
    return targets


def hub_root(cwd: "Path | str") -> Path:
    """The hub above a reviewer's session cwd. `hub_template` defaults to
    `{root}/{repo}/main`, so the cwd's parent is the hub; a template that
    starts sessions in the hub root itself names the hub directly."""
    path = Path(cwd)
    return path.parent if path.name == "main" else path


def review_checkout_name(task_ref: "str | None") -> "str | None":
    """The throwaway worktree the review skill creates for a round that must
    run code: `REVIEW-<task ref>` (`REVIEW-TASK-<id>`), or None without a
    review task to name it after."""
    ref = (task_ref or "").strip()
    return f"{REVIEW_CHECKOUT_PREFIX}{ref}" if ref else None


def hub_trust_paths(
    cwd: "Path | str", *, task_ref: "str | None" = None
) -> "list[Path]":
    """Every directory a reviewer session on this hub may START in or `cd`
    into: the hub root, `{hub}/main` (the spawn cwd under the default
    template), the session cwd itself when the template puts it elsewhere,
    the `REVIEW-<task>` checkout the skill may create for this round's PR
    head, and every `REVIEW-*` checkout already on disk (a re-queued round
    may find a surviving one). The root, `main` and the named checkout are
    listed whether or not they exist yet -- the point of seeding is to be
    there BEFORE the directory is used. De-duplicated, first mention wins."""
    start = Path(cwd)
    root = hub_root(start)
    paths = [root, root / "main", start]
    checkout = review_checkout_name(task_ref)
    if checkout:
        paths.append(root / checkout)
    try:
        paths.extend(sorted(
            p for p in root.glob(f"{REVIEW_CHECKOUT_PREFIX}*") if p.is_dir()
        ))
    except OSError:
        pass
    out: "list[Path]" = []
    for p in paths:
        if p not in out:
            out.append(p)
    return out


def _load_state(path: Path) -> "tuple[str | None, dict]":
    """The state file's raw text (None when absent) and its parsed object
    (`{}` when absent or not a JSON object). Only ABSENCE is folded into
    "start fresh": an unparseable file raises `ValueError` up to
    `seed_trust`, which skips that target rather than replacing it (a
    corrupt state file is never silently overwritten). The raw text is what
    `_write_atomic` compares against before its rename."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, {}
    data = json.loads(raw)
    return raw, (data if isinstance(data, dict) else {})


def _raw_text(path: Path) -> "str | None":
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, data: dict, expected: "str | None") -> bool:
    """Write `data` over `path` by temp file + rename -- unless the file no
    longer holds `expected` (the raw text the caller merged from), in which
    case nothing is replaced and False is returned so the caller merges
    again on top of whoever wrote in between. The rename keeps READERS
    safe (never a torn file); the check before it keeps the OTHER WRITER's
    update safe, down to the gap between the check and the rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    if _raw_text(path) != expected:
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, path)
    return True


def _add_trust(state: dict, wanted: "list[str]") -> "list[str]":
    """Merge `projects[<path>].hasTrustDialogAccepted = true` for every
    wanted path into `state` (in place, repairing a missing or non-object
    `projects`). Returns the paths that were NOT already trusted."""
    projects = state.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        state["projects"] = projects
    added = []
    for p in wanted:
        entry = projects.get(p)
        if not isinstance(entry, dict):
            entry = {}
            projects[p] = entry
        if entry.get(TRUST_KEY) is not True:
            entry[TRUST_KEY] = True
            added.append(p)
    return added


def _untrusted(state: dict, wanted: "list[str]") -> "list[str]":
    projects = state.get("projects")
    if not isinstance(projects, dict):
        return list(wanted)
    return [
        p for p in wanted
        if not isinstance(projects.get(p), dict)
        or projects[p].get(TRUST_KEY) is not True
    ]


def _merge_target(target: Path, wanted: "list[str]") -> bool:
    """Merge `wanted` into ONE claude state file. True when the file was
    written. Load, merge, compare-and-swap, verify -- and start over, up to
    SEED_TRUST_ATTEMPTS times, whenever another writer moved the file
    between the load and the rename (the swap refuses) or right after the
    rename (the verify finds an entry missing). Never raises."""
    wrote = False
    for _ in range(SEED_TRUST_ATTEMPTS):
        try:
            raw, state = _load_state(target)
        except (OSError, ValueError) as exc:
            if wrote:
                # Our write landed; whoever replaced it since left something
                # this daemon will not touch. Nothing more to do here.
                log.debug("trust: %s unreadable after the merge (%s)", target, exc)
                return True
            log.warning(
                "trust: could not read %s (%s) — leaving it untouched; the "
                "directories are NOT pre-trusted there: %s",
                target, exc, ", ".join(wanted),
            )
            return False
        added = _add_trust(state, wanted)
        if not added:
            # Every entry is present: it always was, or the pass before
            # this one landed and survived.
            return wrote
        try:
            swapped = _write_atomic(target, state, raw)
        except OSError as exc:
            log.warning(
                "trust: could not write %s (%s) — the directories are NOT "
                "pre-trusted there: %s", target, exc, ", ".join(added),
            )
            return wrote
        if not swapped:
            log.info(
                "trust: %s changed underneath the merge — merging %s again "
                "on top of the newer file", target, ", ".join(added),
            )
            continue
        wrote = True
        try:
            _, after = _load_state(target)
        except (OSError, ValueError) as exc:
            log.debug("trust: %s unreadable right after the merge (%s)", target, exc)
            return True
        missing = _untrusted(after, wanted)
        if not missing:
            log.debug("trust: %s now pre-trusts %s", target, ", ".join(added))
            return True
        log.info(
            "trust: %s was rewritten right after the merge and lost %s — "
            "merging again", target, ", ".join(missing),
        )
    log.warning(
        "trust: gave up merging into %s after %d attempts — another writer "
        "kept rewriting it, so the directories may NOT be pre-trusted there: %s",
        target, SEED_TRUST_ATTEMPTS, ", ".join(wanted),
    )
    return wrote


def seed_trust(
    paths: "Iterable[Path | str]",
    *,
    home: "Path | str | None" = None,
    config_dir: "Path | str | None" = None,
) -> "list[Path]":
    """Merge `projects[<path>].hasTrustDialogAccepted = true` for every path
    into every claude state target. Returns the targets that were WRITTEN
    (a target already carrying every entry is left untouched -- byte for
    byte, no rewrite). Never raises and never removes anything: an
    unparseable target is skipped with a WARNING rather than replaced, a
    failed write is a WARNING too, and a target another writer keeps
    moving under the merge is retried a bounded number of times (see
    `_merge_target` and the module docstring) before that, too, is one
    WARNING.

    Paths are recorded as absolute strings, which is how Claude Code keys
    them; a relative path is resolved against the cwd, which is never what a
    caller means, so callers pass absolute hubs."""
    wanted: "list[str]" = []
    for p in paths:
        s = os.fspath(Path(p))
        if s not in wanted:
            wanted.append(s)
    changed: "list[Path]" = []
    if not wanted:
        return changed
    for target in claude_state_targets(home, config_dir):
        if _merge_target(target, wanted):
            changed.append(target)
    return changed


def derived_repos_path(root: "Path | str") -> Path:
    """Where the derived allowlist is written under this workspace root, or
    wherever `ALISSA_DERIVED_REPOS_FILE` points (the same override the
    entrypoint honours, so an operator relocating one relocates both)."""
    override = os.environ.get(ENV_DERIVED_REPOS_FILE, "").strip()
    if override:
        return Path(override)
    return Path(root) / DERIVED_REPOS_FILENAME


def write_derived_repos(root: "Path | str", repos: "Sequence[str]") -> "Path | None":
    """Record the derived allowlist, one `owner/repo` per line, for the next
    boot's entrypoint seeding (the entrypoint's 3a block is the only reader:
    it skips blank lines, `#` comments and lines without a `/`). Rewritten
    only when the content differs.
    Returns the path written, or None when nothing changed or the write
    failed (logged; never raised -- the daemon has already trusted the hubs
    itself, so this file only matters to a future boot)."""
    path = derived_repos_path(root)
    body = "".join(f"{r}\n" for r in repos)
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == body:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning(
            "could not record the derived allowlist at %s (%s) — the next "
            "boot's entrypoint seeding will not pre-trust these hubs until a "
            "refresh succeeds: %s", path, exc, ", ".join(repos),
        )
        return None
    return path


def _dialog_line(line: str) -> str:
    """A pane line reduced to the text a gate would render there (see
    _DIALOG_DECORATION), casefolded."""
    text = line.strip(_DIALOG_DECORATION)
    text = _DIALOG_NUMBERING.sub("", text).strip(_DIALOG_DECORATION)
    return text.casefold()


def _is_dialog_chrome(text: str) -> bool:
    return any(text.startswith(chrome) for chrome in FIRST_RUN_DIALOG_CHROME)


def pane_shows_first_run_dialog(pane: str) -> bool:
    """Whether a session's terminal tail is PARKED on one of Claude Code's
    first-run gates: the gate's accept option among the last
    FIRST_RUN_DIALOG_TAIL_LINES non-blank lines, nothing but the gate's own
    chrome below it, and the gate's question strictly above it (the notes on
    FIRST_RUN_DIALOG_MARKERS say why each leg is there). An empty capture
    (the CLI could not tail) is never a dialog -- absence of evidence keeps
    the defer -- and neither is a pane that merely MENTIONS a gate: a
    session at work or idle at its prompt has printed something below the
    words, and that something is not the gate's chrome."""
    if not pane:
        return False
    lines = [t for t in (_dialog_line(ln) for ln in pane.splitlines()) if t]
    tail = lines[-FIRST_RUN_DIALOG_TAIL_LINES:]
    for offset in range(len(tail) - 1, -1, -1):
        if any(tail[offset].startswith(o) for o in FIRST_RUN_DIALOG_OPTIONS):
            break
    else:
        return False
    if not all(_is_dialog_chrome(t) for t in tail[offset + 1:]):
        return False
    above = lines[:len(lines) - len(tail) + offset]
    return any(m in t for t in above for m in FIRST_RUN_DIALOG_MARKERS)
