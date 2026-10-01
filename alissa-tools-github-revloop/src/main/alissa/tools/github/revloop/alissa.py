"""Alissa CLI access: locate the review task (CR2) and enqueue the fresh
reviewer session (orchestration P1)."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .proc import CommandError, run, run_json
from .prompts import ALLOWED_KEYS

log = logging.getLogger(__name__)

# The seven canonical Alissa task statuses -- `TaskStatusSchema` in the studio
# repo (`packages/client/src/schemas/common.ts`). Mirrored here for the reason
# the studio CLI mirrors it into `TASK_STATUSES` rather than importing it: what
# this module needs is the SET of names, not the schema that validates them.
#
# It is the vocabulary the `--status` filter below is allowed to speak. CLI
# 0.2.0 validates `--status` against exactly these seven client-side and exits 1
# on anything else BEFORE issuing any request, so a single non-canonical value
# does not narrow the call slightly less -- it costs the whole narrowed call.
#
# A status Alissa adds lands HERE first: a name added to `OPEN_STATUSES` alone
# reads as non-canonical and drops the narrowing for the whole fleet. That is
# the safe direction -- wide, never a wrong task resolved -- and the canonical
# test fails on it, but it fails naming `OPEN_STATUSES`, which is the constant
# that is right; this is the one that is stale (PR #98 round 1).
CANONICAL_TASK_STATUSES = frozenset(
    {
        "draft",
        "committed",
        "in_progress",
        "blocked",
        "pending_validation",
        "validated",
        "cancelled",
    }
)

# A review task is "open" while it can still receive a verdict. Every member is
# canonical, and a test pins that: a status Alissa cannot issue is dead weight
# in `is_open` and poison in the `--status` filter derived from it.
#
# `todo` was such a member until 0.22.0 (issue #97). Alissa has never had that
# status, so `is_open` could never match it -- but it was still reaching CLI
# 0.2.0's `--status` validator, which rejects the call over it, which in turn
# drops this process back to the unnarrowed whole-corpus list for good.
OPEN_STATUSES = {"committed", "in_progress", "pending_validation"}

# -- narrowing the `alissa task list` call (issue #87) ------------------------
#
# `list_tasks` is the widest query this daemon issues -- no query string at all,
# the actor's entire non-terminal corpus, sponsor-union scoped -- and it was the
# single largest contributor to the Alissa deployment's #1 Database-I/O offender
# over 2026-08-12..16. Everything below is applied ONLY when the installed CLI
# advertises it (see Alissa.probe_task_list): an issue's claim about a flag is
# not evidence, and this daemon turns a non-zero `alissa` exit into a SKIPPED
# decision, so sending a flag the CLI does not have costs a review.

# Statuses a LIVE review task can hold. Deliberately derived from OPEN_STATUSES
# and not a hand-written list: `is_review_task_for` already rejects every other
# status client-side, so filtering server-side on exactly this set cannot change
# which task the daemon resolves -- it only stops shipping the rows over the
# wire. Any status added to `is_open` is added here by construction.
#
# The canonical guard below does not weaken that invariant, and is deliberately
# not an INTERSECTION with the canonical set, which would: an intersection makes
# the filter a strict subset of the open set the moment the two disagree, so the
# daemon would stop fetching rows `is_open` still accepts -- a skipped review,
# reported as an empty corpus. The whole derivation is kept or nothing is sent.
# Sending nothing degrades to the call the daemon has always made: wide, but
# complete, which is the direction every other failure on this path degrades in.
TASK_LIST_STATUS_FLAG = "--status"


def _status_filter(statuses: "set[str] | frozenset[str]") -> str:
    """The `--status` argument for `statuses`, or `""` when they cannot be one.

    `""` means "do not narrow by status". It is returned for a set carrying any
    non-canonical value, because such a value is not a filter the CLI serves
    less precisely -- it is one the CLI refuses outright, taking the `--view`
    and `--self` narrowing down with it and pinning this process to the
    whole-corpus list (`list_tasks`). One wide call beats every call being wide.

    Pure on purpose. The condition is a code defect and deserves saying out
    loud, but not from here: this runs at IMPORT, which is the one moment the
    daemon's logging is not configured yet (see
    `Alissa._warn_status_filter_dropped`). The tests are what pin the condition;
    the runtime warning is the second line, and it is emitted where it can both
    be formatted and be true.
    """
    if set(statuses) - CANONICAL_TASK_STATUSES:
        return ""
    return ",".join(sorted(statuses))


TASK_LIST_STATUS_FILTER = _status_filter(OPEN_STATUSES)

# Why the filter above is empty, when it is -- kept for the diagnostic, which
# cannot be emitted from module scope. Empty in the healthy case.
NON_CANONICAL_OPEN_STATUSES = tuple(sorted(OPEN_STATUSES - CANONICAL_TASK_STATUSES))

# `--self` drops the SPONSOR's corpus and keeps only the calling actor's rows.
#
# NOT enabled by default, and the reason is measured rather than cautious. On
# the live fleet corpus (2026-08-16, 932 non-terminal rows) `--self` removes 36
# rows, 4% of the payload -- and 3 of the 371 `Review PR ...` tasks in it are
# among the rows it removes: they are owned by another actor, not by the agent
# actor whose sessions write the other 368. Review tasks are therefore
# PREDOMINANTLY actor-owned but not exclusively so, and a review task this call
# cannot see is a round the daemon cannot count. 4% of the wire is not worth
# that, so the flag is opt-in per deployment (`task_list_self_scope`).
TASK_LIST_SELF_FLAG = "--self"

# A lean projection of each row. The daemon keeps only taskNumber/title/status
# (see `_task_from_row`), so a digest view is pure saving with no semantics --
# which is why it is adopted whenever it exists and has no knob. It ships in the
# studio repo separately; until then the probe simply does not find it.
TASK_LIST_VIEW_FLAG = "--view"
TASK_LIST_DIGEST_VIEW = "digest"

# `--bow` narrows the CANDIDATE SET rather than filtering the answer, and that
# is the whole reason it is worth more than the flags above (issue #100).
#
# `--status` / `--self` / `--view` all start from the operator's involvement
# index -- every non-terminal task they are party to -- and cut it down. A BOW
# scope starts from that body of work's junction rows instead, which carry a
# denormalized status, so a row that does not match never costs a task-document
# read at all. The cache story is the same shape: a BOW-scoped query is
# invalidated only by writes INSIDE that BOW, so the operator's unrelated task
# churn stops re-billing this daemon's 30-second poll.
#
# It really is a SWAP and not an intersection, and that was measured against the
# installed CLI on 2026-08-28 rather than assumed (PR #101 round 1): across five
# real BOWs, not one answer was a subset of the plain 1335-row one, and the rows
# outside it were all non-terminal. Two consequences the composition story has to
# respect, from the same probe:
#
#   * `--self` is IGNORED under `--bow`. `--bow X --self` returns exactly what
#     `--bow X` returns, and that set is not a subset of the `--self` answer. So
#     a BOW-scoped deployment gets the body of work's whole membership, actor-
#     owned or not -- WIDER than `--self`, never narrower.
#   * `--include-terminal` is ignored too (a BOW holding 52 tasks, 17 of them
#     `validated`, answers 35 either way), so the default non-terminal filter is
#     applied server-side but the flag that lifts it is not. `--status` could not
#     be probed -- this CLI has none -- and on that evidence should not be
#     assumed to compose either.
#
# None of that is a correctness risk, which is why the flags are still sent: a
# filter the server ignores makes the answer WIDER, `is_open` still runs
# client-side, and the argv is one the CLI accepts. It costs bytes, not reviews.
# Treat the list above as a dated snapshot, not a contract: this CLI has grown
# flags twice now without moving off version `0.1.0`.
#
# Opt-in per deployment for the same reason `--self` is, only sharper: a review
# task that is not attached to the configured BOW is INVISIBLE to the daemon --
# and on the default `on_missing_review_task` that is not even a missed round but
# a round spawned untethered from its task -- and review tasks are only born into
# a BOW once the sessions that create them say so. A configured BOW that nothing
# writes into lists empty; `list_tasks` retries plain and drops the BOW alone.
# That is a safety net, not a licence to guess the id.
TASK_LIST_BOW_FLAG = "--bow"


@dataclass(frozen=True)
class TaskListFlags:
    """What the installed `alissa task list` advertises in its own help.

    All-False is both the "old CLI" answer and the "the probe could not run"
    answer, and they are deliberately the same value: each means "make the call
    the daemon has always made".
    """

    status: bool = False
    self_scope: bool = False
    digest: bool = False
    bow: bool = False


# How deeply an OPTION may be indented in a commander help listing. Commander
# puts option names in a fixed left column (two spaces) and wraps each
# description onto continuation lines indented to the DESCRIPTION column, which
# is much further right -- 22 in the CLI this daemon ships against. So a small
# bounded indent is what separates an option from a wrapped description, without
# depending on the exact description-column width, which varies with the longest
# option name in the listing.
#
# Anchoring merely to line START is not enough, and that was the round-1 finding
# on PR #88: a description that wraps such that its second line BEGINS with
# `--status` reads as an offer of `--status`, and 0.1.0's own help already wraps
# `--self`'s description onto its own line, so the shape is not hypothetical.
MAX_OPTION_INDENT = 3


def _advertises(helptext: str, flag: str) -> bool:
    """Whether `flag` appears as an OPTION in a CLI help listing.

    Anchored to the option COLUMN (see MAX_OPTION_INDENT), allowing a short alias
    in front as in `-h, --help`, so a flag merely NAMED in another option's
    description is not read as an offer of that flag -- whether it is named
    mid-line ("Pair with --include-shared" is in this very help text) or at the
    start of a wrapped continuation line. The trailing guard rejects a longer
    flag that merely starts with this one (`--self-only` is not `--self`).

    The consequence of a false positive is bounded rather than silent -- the flag
    is sent, `alissa` exits non-zero, and `list_tasks` retries plain -- but it
    costs a whole-corpus fetch, which is the thing this module is here to stop
    spending.
    """
    return re.search(
        rf"(?m)^[ \t]{{0,{MAX_OPTION_INDENT}}}(?:-\w,\s+)?{re.escape(flag)}(?![\w-])",
        helptext,
    ) is not None


# The two spawn stamps of the Studio loop cost meter (studio.alissa.app
# docs/design/loop-cost-meter.md §2.3, §2.4, lane L4; issue #153, the port of
# devloop's issue #149): `--task` names the ORIGIN task the reviewer serves and
# `--repo` the canonical lowercase `owner/name`, so the session row carries
# `focusTaskId` and `repo` from its first second. The reviewer's skill then
# re-anchors `current_task` on its review task and that write wins the focus;
# the origin keeps its `code_session` evidence row and the meter resolves the
# rest at read time (§2.4).
QUEUE_ADD_TASK_FLAG = "--task"
QUEUE_ADD_REPO_FLAG = "--repo"


def queue_add_accepts_stamps(helptext: str) -> bool:
    """Whether `alissa tmux queue add --help` lists BOTH `--task` and `--repo`.

    Both or neither: the CLI this daemon shipped against (0.3.0) already lists
    `-t, --task <ref>`, but with the older meaning -- link a queue item for
    shell write-back -- and has no `--repo`; that CLI is not the stamp-aware
    one, so it gets neither flag.
    """
    text = helptext or ""
    return _advertises(text, QUEUE_ADD_TASK_FLAG) and _advertises(text, QUEUE_ADD_REPO_FLAG)


# CR6 verdict envelope outcomes.
VERDICT_APPROVE = "approve"
VERDICT_REQUEST_CHANGES = "request_changes"

# Envelope titles and bodies both read:
#   Review verdict: <org>/<repo>#<n> — request_changes (round 3, ...)
#   # Review verdict: <org>/<repo>#<n> — approve
# The separator is an em-dash in practice; en-dash and hyphen are accepted too
# so a hand-written envelope does not silently fail to parse.
# Kept to a single line on purpose: the verdict word sits on the same line as
# the "Review verdict:" lead-in, so matching across newlines could only pick up
# a later round's wording out of order.
_VERDICT_RE = re.compile(
    r"Review\s+verdict\s*:[^\n]*?[—–-]\s*(approve|request_changes)\b",
    re.IGNORECASE,
)

# The envelope's merge-readiness judgment (issue #130). The alissa-code-review
# skill writes it as one list line in the verdict envelope:
#   - **Merge-Readiness:** auto
#   - **Merge-Readiness:** operator — schema-migration: removes users.legacyId
# Tolerant of the markdown around the label -- an optional bullet, optional
# `**` bold around the label (with the colon inside or outside it), optional
# bold around the value -- but NOT of the value's case: `Auto` is not a
# judgment this daemon will carry, so it reads as missing and fails closed to
# `operator` downstream. Line-anchored, first match wins, like the consumer's
# own grammar; the reason runs to the end of the line and no further.
READINESS_AUTO = "auto"
READINESS_OPERATOR = "operator"


def _envelope_line_re(key: str) -> "re.Pattern[str]":
    """The tolerant envelope-line grammar for one readiness KEY. Two keys
    share it (issue #148): `Merge-Readiness` on a code PR, `Commit-Readiness`
    on a plan PR -- the same markdown tolerance, the same case-sensitive
    value, the same first-match-wins."""
    return re.compile(
        r"^[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?[ \t]*" + re.escape(key)
        + r"[ \t]*:?[ \t]*(?:\*\*)?"
        r"[ \t]*:?[ \t]*(?:\*\*)?(auto|operator)\b(?:\*\*)?"
        r"(?:[ \t]*[—–-][ \t]*(.*?))?[ \t]*$",
        re.MULTILINE,
    )


_READINESS_RE = _envelope_line_re("Merge-Readiness")

# The operator reason's CLASS (issue #142). An `operator` judgment names WHY
# the merge is a human's, and until now it did so in prose the merge edge
# could not act on: of 14 operator verdicts on ~40 studio PRs, 3 named real
# merge risk and 11 were "the PR body lists an unverified item" -- validation
# work the human does at gate 2 anyway, not a reason to withhold the merge.
# So the reason now LEADS with one token from this closed enum, in the
# consumer's grammar:
#
#   Merge-Readiness: auto
#   Merge-Readiness: operator — <class>: <one-line reason>
#
# The order below is the SEVERITY order: when more than one row applies, the
# reviewer names the one nearest the top (the most severe wins), and exactly
# one. `auto` never carries a class. An operator line whose reason does not
# lead with a recognised token is still valid grammar -- it parses, value and
# reason intact -- and reads as UNCLASSED (`klass=None`); the consumer treats
# unclassed as a hard hold (fail closed), so a reviewer that forgets the
# class loses throughput, never safety. The token is matched exactly as
# written here (lowercase, hyphenated): `Schema-Migration:` is not a class
# this daemon reads, for the same reason `Auto` is not a value it carries.
READINESS_CLASSES: "tuple[str, ...]" = (
    "schema-migration",   # schema shape changes needing a migration / removal / rename on live data
    "data-backfill",      # one-off writes over existing rows (backfills, stored-shape version bumps)
    "secrets-env",        # new / changed env vars, secrets, credentials the deploy must carry
    "infra-deploy",       # Dockerfiles, workflows, Railway config, base-image pins -- what runs
    "billing",            # credit charging, pricing, tiers, quotas
    "security",           # auth, permission gates, redaction, sandboxing flagged operator-worthy
    "unverified-ux",      # the PR body's operator gate is a human look at a screen / copy / mockup
    "unverified-runtime",  # the PR body's operator gate is a live smoke / real-model run / replay
    "release-act",        # the merge itself is a release (VERSION bump -> publish, tag, npm)
)
_READINESS_CLASS_RE = re.compile(
    r"^(" + "|".join(re.escape(klass) for klass in READINESS_CLASSES) + r")[ \t]*:"
)

# The reason lands in a GitHub review body (as a line-anchored trailer) and in
# a log line, so it is bounded and flattened at the parser: one line, no
# backticks (a fence would swallow the trailer and everything after it), and
# no more than this many characters. The class prefix is PART of the reason
# and survives the flattening untouched: the native trailer must stay
# byte-equal to the envelope's line, class included.
MAX_READINESS_REASON_CHARS = 200


def clean_readiness_reason(text: object) -> str:
    """Flatten a Merge-Readiness reason to one bounded, backtick-free line.

    The `<class>:` prefix, when present, is kept intact at the head of the
    line -- classify_readiness_reason reads it off the cleaned reason, and the
    emitter copies the cleaned reason whole.
    """
    if not isinstance(text, str):
        return ""
    flat = " ".join(text.replace("`", "").split())
    return flat[:MAX_READINESS_REASON_CHARS].rstrip()


def classify_readiness_reason(value: object, reason: object) -> "str | None":
    """The READINESS_CLASSES token an `operator` reason leads with, or None.

    None for `auto` (never classed), for an operator reason that leads with
    no recognised token (UNCLASSED -- the consumer's hard hold), and for a
    token in the wrong case or spelling. The reason itself is never altered:
    the class is read off it, not cut out of it.
    """
    if value != READINESS_OPERATOR or not isinstance(reason, str):
        return None
    match = _READINESS_CLASS_RE.match(reason)
    return match.group(1) if match else None


def _classed(value: "str | None", raw_reason: object) -> "tuple[str | None, str, str | None]":
    """The `(value, reason, klass)` triple both parsers hand back."""
    reason = clean_readiness_reason(raw_reason)
    return (value, reason, classify_readiness_reason(value, reason))


# The trailer's own grammar (issue #134) -- the bare-line sibling of
# _READINESS_RE. This is the line a native review ENDS with, whoever wrote
# it: the daemon's emitter (loop.readiness_trailer) when it posts the verdict
# itself, or the reviewer session's own `gh pr review` on the normal path.
# It is the consumer's grammar verbatim (README, "The `Merge-Readiness`
# trailer"): line-anchored, no bullet, no bold, no backticks, first match
# wins, value case-sensitive. The label is the ONE constant the emitter
# builds from, so the two cannot drift: a line the emitter writes is, by
# construction, a line this regex reads.
READINESS_TRAILER_LABEL = "Merge-Readiness:"
READINESS_MISSING = "missing"


def _trailer_line_re(label: str) -> "re.Pattern[str]":
    """The strict bare-line grammar for one trailer LABEL (see _TRAILER_RE);
    shared by the Merge- and the Commit-Readiness trailers (issue #148)."""
    return re.compile(
        r"^" + re.escape(label)
        + r"[ \t]*(auto|operator)(?:[ \t]*[—-][ \t]*(.+))?[ \t]*$",
        re.MULTILINE,
    )


_TRAILER_RE = _trailer_line_re(READINESS_TRAILER_LABEL)


def parse_trailer(body: object) -> "tuple[str | None, str, str | None]":
    """`(value, reason, klass)` from the first bare `Merge-Readiness:` line in
    a review body, or `(None, "", None)` when no line matches the trailer
    grammar.

    Strict where parse_readiness is tolerant: the line inside backticks
    mid-sentence (studio #1258) and the envelope's `- **Merge-Readiness:**`
    bullet both read as MISSING here, because the consumer's regex is what
    decides whether the merge edge sees the judgment at all. CRLF bodies (a
    review typed into the web form) are normalised first; the grammar itself
    is unchanged. `klass` is the READINESS_CLASSES token an operator reason
    leads with, or None (auto, or an unclassed operator -- issue #142).
    """
    if not isinstance(body, str):
        return (None, "", None)
    match = _TRAILER_RE.search(body.replace("\r\n", "\n"))
    if match is None:
        return (None, "", None)
    return _classed(match.group(1), match.group(2))


def parse_readiness(blob: object) -> "tuple[str | None, str, str | None]":
    """`(value, reason, klass)` from the first Merge-Readiness line in `blob`.

    `value` is READINESS_AUTO / READINESS_OPERATOR, or None when no line
    parses; `reason` is the cleaned trailing text (empty when there is none),
    class prefix included; `klass` is the READINESS_CLASSES token that reason
    leads with, or None when the value is `auto`, the reason is unclassed, or
    nothing parsed (issue #142).
    """
    if not isinstance(blob, str):
        return (None, "", None)
    match = _READINESS_RE.search(blob)
    if match is None:
        return (None, "", None)
    return _classed(match.group(1), match.group(2))


# The Commit-Readiness judgment on a PLAN PR (issue #148; genloop design D8,
# §6.3, §7.1). A plan PR is never a code merge candidate: what its approve
# decides is whether merging the plan COMMITS its tasks, and orcloop's plan
# close edge reads that off this trailer the way its merge edge reads
# Merge-Readiness -- the same grammar with the other key:
#
#   Commit-Readiness: auto
#   Commit-Readiness: operator — <class>: <one-line reason>
#
# Eleven classes in SEVERITY order, the enum orcloop copies verbatim: the
# first six never auto-commit under any policy, the last five may. A task
# file's `commitClass` draws on the last ten -- `unanchored` is the
# refuter's alone. Same rules as the merge enum: exactly one class, the most
# severe applicable; `auto` never carries one; an operator reason that leads
# with no recognised token parses and reads as UNCLASSED (the hard hold).
#
# One rule is STRICTER than the merge grammar, because the acceptance names
# it (c4): a class on an `auto` line is REFUSED WHOLE -- the line reads as
# missing and the fallback holds -- rather than read as a bare `auto` with a
# stray reason. An `auto` whose trailing text is class-shaped is a reviewer
# that could not decide between the two forms, and the commit policy must
# never resolve that doubt toward committing.
COMMIT_READINESS_KEY = "Commit-Readiness"
COMMIT_READINESS_CLASSES: "tuple[str, ...]" = (
    "unanchored",         # the anchor does not resolve, is closed, or the sources are not its class
    "schema-migration",   # a leaf's scope names **/schema.ts, **/migrations/**, a backfill or a migration
    "auth-secrets",       # auth, permission gates, tokens, credentials, secrets, deploy env vars
    "pricing-billing",    # pricing, credits, tiers, quotas, invoices, refunds
    "customer-promise",   # copy or behaviour a customer is told about
    "product-surface",    # a new screen, route, tool, command or exported type
    "gate-verification",  # leaves that verify an unverified operator's-gate item
    "drift-fix",          # leaves that make a lagging repo follow a fleet-wide contract change
    "flaky-test",         # leaves that fix, quarantine or pin a test the loop capped out on
    "exhaust-followup",   # leaves that carry [triage:later] replies and unpursued [minor]/[nit]
    "doc-correction",     # leaves confined to docs, READMEs, API.md prose, design-doc pins
)
# The six that stay operator's under every commit policy (design §7.1).
COMMIT_READINESS_OPERATOR_ONLY: "tuple[str, ...]" = COMMIT_READINESS_CLASSES[:6]
COMMIT_READINESS_TRAILER_LABEL = COMMIT_READINESS_KEY + ":"
_COMMIT_READINESS_RE = _envelope_line_re(COMMIT_READINESS_KEY)
_COMMIT_TRAILER_RE = _trailer_line_re(COMMIT_READINESS_TRAILER_LABEL)
_COMMIT_READINESS_CLASS_RE = re.compile(
    r"^(" + "|".join(re.escape(klass) for klass in COMMIT_READINESS_CLASSES)
    + r")[ \t]*:"
)
# What "a class on an auto line" looks like: the reason leads with a
# `<token>:` -- a recognised class, a misspelt one or a merge-enum token
# alike. Deliberately wider than the enum, for the reason above.
_CLASS_SHAPED_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*[ \t]*:")


def classify_commit_readiness_reason(value: object, reason: object) -> "str | None":
    """The COMMIT_READINESS_CLASSES token an `operator` reason leads with, or
    None (auto, unclassed, misspelt, wrong case) -- classify_readiness_reason
    over the commit enum."""
    if value != READINESS_OPERATOR or not isinstance(reason, str):
        return None
    match = _COMMIT_READINESS_CLASS_RE.match(reason)
    return match.group(1) if match else None


def _commit_classed(
    value: "str | None", raw_reason: object
) -> "tuple[str | None, str, str | None]":
    """The `(value, reason, klass)` triple both commit parsers hand back, with
    the class-on-auto refusal applied."""
    reason = clean_readiness_reason(raw_reason)
    if value == READINESS_AUTO and _CLASS_SHAPED_RE.match(reason):
        return (None, "", None)
    return (value, reason, classify_commit_readiness_reason(value, reason))


def parse_commit_trailer(body: object) -> "tuple[str | None, str, str | None]":
    """`(value, reason, klass)` from the first bare `Commit-Readiness:` line of
    a review body, or `(None, "", None)` -- parse_trailer with the other key,
    plus the class-on-auto refusal."""
    if not isinstance(body, str):
        return (None, "", None)
    match = _COMMIT_TRAILER_RE.search(body.replace("\r\n", "\n"))
    if match is None:
        return (None, "", None)
    return _commit_classed(match.group(1), match.group(2))


def parse_commit_readiness(blob: object) -> "tuple[str | None, str, str | None]":
    """`(value, reason, klass)` from the first Commit-Readiness line of a
    verdict envelope -- parse_readiness with the other key, plus the
    class-on-auto refusal."""
    if not isinstance(blob, str):
        return (None, "", None)
    match = _COMMIT_READINESS_RE.search(blob)
    if match is None:
        return (None, "", None)
    return _commit_classed(match.group(1), match.group(2))


# The contract version the reviewer judged against (issue #155; Studio
# design `loop-operator-surface.md` §6.4, lanes L10/L11): the reviewer reads
# the task at round start and records `**Contract:** v<n>` under the reviewed
# head. The same markdown tolerance as the readiness lines -- an optional
# bullet, optional bold around the label with the colon inside or outside --
# but the `v` is REQUIRED: L11 has not shipped the line yet, and without it any
# prose that opens with `Contract` and a number (`Contract 3 criteria
# re-checked`) would set the version (PR #156 round 1). Absent (every
# envelope written before L11 ships) reads as None, never as v1: the event's
# key is then null, the design's "absent" case, rather than a version nobody
# recorded.
_CONTRACT_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?[ \t]*Contract[ \t]*:?[ \t]*(?:\*\*)?"
    r"[ \t]*:?[ \t]*(?:\*\*)?[ \t]*v(\d{1,9})\b",
    re.MULTILINE,
)


def parse_contract_version(blob: object) -> "int | None":
    """The `Contract: v<n>` an envelope records, as an int, or None."""
    if not isinstance(blob, str):
        return None
    match = _CONTRACT_RE.search(blob)
    return None if match is None else int(match.group(1))


@dataclass(frozen=True)
class Task:
    ref: str  # TASK-<taskNumber>
    title: str
    status: str

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


# The namespace loop.session_name() spawns reviewers into. Kept as the
# display-level answer to "is this session in the reviewer namespace?" (the
# console marks unmanaged sessions with it); the REAP path uses the stricter
# grammar below, because a prefix is not a strong enough claim of ownership to
# kill on.
REVIEW_SESSION_PREFIX = "review-"

# `alissa tmux new` names the REAL tmux session `ali-<name>`; the roster's
# `name` is the managed name without it. The raw tmux surface below addresses
# the real one, and the waiting marker (docker/claude/hooks/note-waiting.py)
# is named after it too.
MANAGED_PREFIX = "ali-"

# The console's session-name rule (webui.server), stated once so the raw
# tmux argv builders and the HTTP surface can never drift: a managed name
# starts with a letter or digit and carries only these characters, which is
# what keeps a `-flag`-shaped or shell-metacharacter name out of any argv.
SAFE_SESSION = re.compile(r"\A[A-Za-z0-9._][A-Za-z0-9._-]{0,199}\Z")


def tmux_socket() -> str:
    """The path of the tmux server socket the reviewer sessions live on:
    `$TMUX_TMPDIR/tmux-<uid>/default` (tmux's own default). Spelled out
    rather than left to tmux because the daemon may run without `TMUX` in
    its environment and must still reach the SAME server `alissa tmux new`
    created for the reviewers -- same container, same user, same socket."""
    base = os.environ.get("TMUX_TMPDIR", "").strip() or "/tmp"
    return os.path.join(base, f"tmux-{os.getuid()}", "default")


def tmux_target(name: str) -> str:
    """The `-t` target for a managed session: the real tmux name, prefixed
    with `=` so tmux matches it EXACTLY (its default is prefix matching, and
    `review-widgets-pr1-r1-abcdef` is a prefix of `…-r1-abcdef0`) and
    suffixed with `:` so the target is the session's CURRENT WINDOW (and its
    active pane) -- a bare `=name` is a session target, which `capture-pane`
    and `send-keys` refuse with "can't find pane" (tmux 3.3a)."""
    real = name if name.startswith(MANAGED_PREFIX) else MANAGED_PREFIX + name
    return "=" + real + ":"


def check_session_name(name: "str | None") -> str:
    """`name` when it passes SAFE_SESSION, else ValueError. Every raw tmux
    argv builder calls this first."""
    if not name or not SAFE_SESSION.match(name):
        raise ValueError(f"invalid session name {name!r}")
    return name


def capture_pane_argv(name: str, lines: int) -> "list[str]":
    """`tmux -S <socket> capture-pane -p -t =<real> -S -<lines>`: the last
    `lines` lines of the session's active pane, printed to stdout, with
    escape sequences stripped (`-p` without `-e`)."""
    check_session_name(name)
    return [
        "tmux", "-S", tmux_socket(), "capture-pane", "-p",
        "-t", tmux_target(name), "-S", f"-{max(1, int(lines))}",
    ]


def pane_path_argv(name: str) -> "list[str]":
    check_session_name(name)
    return [
        "tmux", "-S", tmux_socket(), "display-message", "-p",
        "-t", tmux_target(name), "#{pane_current_path}",
    ]


# What `#{pane_current_command}` reads on a reviewer pane whose `claude` has
# EXITED (issue #146, devloop #140's set): the shell `alissa tmux new`
# launched it from, back in the foreground. A live reviewer reads `node`
# (Claude Code's runtime); a name here means the process the session was
# spawned for is gone, whatever the session listing says about liveness.
SHELL_COMMANDS = frozenset((
    "bash", "sh", "zsh", "fish", "dash", "ksh", "ash", "tcsh", "csh",
))


def pane_command_argv(name: str) -> "list[str]":
    """`tmux -S <socket> display-message -p -t =<real>: #{pane_current_command}`:
    the foreground process of the session's active pane, by name."""
    check_session_name(name)
    return [
        "tmux", "-S", tmux_socket(), "display-message", "-p",
        "-t", tmux_target(name), "#{pane_current_command}",
    ]


def send_keys_argv(name: str, keys: "tuple[str, ...]") -> "list[str]":
    """`tmux -S <socket> send-keys -t =<real> <key>…` with every key drawn
    from `prompts.ALLOWED_KEYS`. Anything else -- a letter, a word, a path --
    is a ValueError before any process is spawned: the responder presses a
    dialog's buttons and can never type into a reviewer."""
    check_session_name(name)
    if not keys:
        raise ValueError("send_keys needs at least one key")
    bad = [k for k in keys if k not in ALLOWED_KEYS]
    if bad:
        raise ValueError(
            f"key(s) not in the allowlist {sorted(ALLOWED_KEYS)}: {bad!r}"
        )
    return ["tmux", "-S", tmux_socket(), "send-keys", "-t", tmux_target(name), *keys]


# The reviewer-session GRAMMAR. The sweep kills sessions and the worker
# container is shared with other lanes (`develop-*`, `fix-*`, `maintain-*`,
# ...), so only a name that parses as one of the two shapes the review loop
# itself produces is ever a reap candidate; anything else is another daemon's
# (or a human's) and is never touched. Two shapes, both ours:
#
#   review-<repo>-pr<n>-r<k>-<nonce>   this daemon's spawns (loop.session_name)
#   review-pr-<n>[-r<k>]               the alissa-code-review skill's own
#                                      procedures (spawn-a-reviewer-session.md,
#                                      run-the-review-loop.md) -- hand-driven
#                                      rounds of the SAME loop. No spawn ledger
#                                      knows about those and nothing reaped
#                                      them: every session in the 2026-07-28
#                                      memory incident was of this shape.
#
# The daemon shape is matched with a non-greedy repo so a hyphenated repo name
# (`alissa-github-review-daemon`) still lands in the repo group, and it is
# anchored on the `-pr<n>-r<k>-<nonce>` tail that loop.session_name always
# emits. The skill shape carries no repo and no nonce -- that is the whole
# reason a bare `review-pr-<n>` needs the repos allowlist to be resolvable at
# all (see loop.ReviewWatcher._resolve_pr).
_DAEMON_SESSION_RE = re.compile(
    r"^review-(?P<repo>[a-z0-9-]+?)-pr(?P<number>\d+)-r(?P<round>\d+)-[0-9a-f]{4,16}$"
)
_SKILL_SESSION_RE = re.compile(
    r"^review-pr-(?P<number>\d+)(?:-r(?P<round>\d+))?$"
)


@dataclass(frozen=True)
class SessionRef:
    """What a reviewer session's NAME says about the round it is running.

    `repo` is the sanitized repo slug (`session_repo_slug`), not an
    `owner/repo`: session names never carry the owner. `repo` and `round` are
    None for the skill shape, which encodes neither.
    """

    number: int
    repo: "str | None" = None
    round: "int | None" = None


def session_repo_slug(repo: str) -> str:
    """The repo component of a session name: tmux-safe, lowercase, no dots.

    Shared by the producer (loop.session_name) and the parser below so the two
    can never drift -- a rename here that the matcher did not follow would make
    the daemon stop recognizing its own sessions.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", repo).strip("-").lower()


def parse_session_name(name: "str | None") -> "SessionRef | None":
    """Parse a reviewer session name, or None when it is not one of ours.

    None is the security-relevant answer: it is what keeps the sweep off every
    other lane's sessions in a shared container.
    """
    if not isinstance(name, str):
        return None
    for pattern in (_SKILL_SESSION_RE, _DAEMON_SESSION_RE):
        match = pattern.match(name)
        if match is None:
            continue
        parts = match.groupdict()
        round_ = parts.get("round")
        return SessionRef(
            number=int(parts["number"]),
            repo=parts.get("repo"),
            round=int(round_) if round_ else None,
        )
    return None


@dataclass(frozen=True)
class ManagedSession:
    name: str
    status: str  # the worker's view: "idle", "busy", ...
    # Epoch seconds of the session's last tmux activity. 0 when the CLI did
    # not report one -- treated as "long quiet", so a missing field can never
    # indefinitely immunize a session against the sweep.
    last_activity: float = 0.0
    # The REAL tmux session name (`ali-<name>`, the roster's `session`
    # field), or None when the CLI did not report one. The prompt responder
    # names the waiting marker by it; absent, `MANAGED_PREFIX + name`.
    session: "str | None" = None

    @property
    def is_idle(self) -> bool:
        return self.status == "idle"

    @property
    def tmux_name(self) -> str:
        """The real tmux session name -- the roster's, else derived."""
        return self.session or MANAGED_PREFIX + self.name

    @property
    def quiet_for(self) -> "float | None":
        """Seconds since the last tmux activity, or None when the CLI
        reported no timestamp (the sweep reads that 0 as "long quiet"; the
        prompt responder reads it as "unknown" and waits for a marker)."""
        if not self.last_activity:
            return None
        return max(0.0, time.time() - self.last_activity)

    @property
    def ref(self) -> "SessionRef | None":
        """What the name says about the round -- see parse_session_name."""
        return parse_session_name(self.name)


# The CR2 title's lead-in. A code PR's review task reads `Review PR …`; a PLAN
# PR's reads `Review plan <org>/<repo>#<n> (<plan id>)` (issue #148, genloop
# design §6.1) -- created downstream of nothing, since a plan has no origin
# task. Both leads resolve to the same PR, so one pattern serves the daemon's
# search, its cache check and `alissa-pr-review` alike.
REVIEW_TASK_TITLE_LEAD = r"^Review\s+(?:PR|plan)\s+"


def review_task_title_pattern(owner: str, repo: str, number: int) -> re.Pattern[str]:
    """CR2 title convention: `Review PR <org>/<repo>#<n> (TASK-<origin>)`, or
    `Review plan <org>/<repo>#<n> (<plan id>)` for a plan PR."""
    return re.compile(
        rf"{REVIEW_TASK_TITLE_LEAD}{re.escape(owner)}/{re.escape(repo)}#{number}\b",
        re.IGNORECASE,
    )


def is_review_task_for(owner: str, repo: str, number: int, task: "Task") -> bool:
    """Whether `task` is THE open CR2 review task for this PR.

    The one predicate, shared by the search (`find_review_task`, over a whole
    task list) and by the cache check (loop._review_task, over a single task
    read back by ref). They must agree: a cached ref that the search would not
    have returned is a mapping the daemon has to drop, and a divergence here
    would either pin a wrong task forever or re-fetch the corpus every pass
    while disagreeing with itself.
    """
    return bool(review_task_title_pattern(owner, repo, number).match(task.title)) and task.is_open


def _task_from_row(row: object) -> "Task | None":
    """One task out of a CLI payload row, or None when it carries no usable ref.

    Shared by the list reader and the single-task reader so both agree on which
    field is the resolvable ref.
    """
    if not isinstance(row, dict):
        return None
    # `taskNumber` is the ref the API resolves; `taskSeq` is a display
    # ordinal and 404s as `TASK-<seq>`.
    number = row.get("taskNumber")
    if number is None:
        return None
    title = row.get("title")
    status = row.get("status")
    return Task(
        ref=f"TASK-{number}",
        title=title if isinstance(title, str) else "",
        status=status if isinstance(status, str) else "",
    )


@dataclass(frozen=True)
class VerdictEnvelope:
    """One parsed CR6 verdict envelope: the verdict, plus the merge-readiness
    judgment the same envelope carries (issue #130).

    `readiness` is READINESS_AUTO / READINESS_OPERATOR, or None when the
    envelope has no parseable `Merge-Readiness` line -- the case the native
    post fails closed on. `readiness_reason` is already one bounded,
    backtick-free line (see clean_readiness_reason); empty when the envelope
    gave none, and it keeps the class prefix an operator reason leads with
    (issue #142) -- so the class is never lost between here and the native
    trailer.

    The class is deliberately NOT a field. It is a function of the reason
    (classify_readiness_reason), the emitter copies the reason whole, and
    the narration reads the class back off the emitted line -- so the fact
    has one source, and a second copy here would be a reader-less field
    that could only ever drift from it (PR #143 round 1).

    `commit_readiness` / `commit_readiness_reason` are the same pair for the
    envelope's `Commit-Readiness` line (issue #148), read off the same
    evidence item; only a PLAN PR's native post reads them, and a code PR's
    envelope simply has none.

    `contract_version` is the envelope's `Contract: v<n>` (issue #155), or
    None when it records none -- read for the `round.verdict` event only.
    """

    verdict: str
    readiness: "str | None" = None
    readiness_reason: str = ""
    commit_readiness: "str | None" = None
    commit_readiness_reason: str = ""
    contract_version: "int | None" = None


@dataclass(frozen=True)
class TaskDetail:
    """One task as `alissa task get` sees it: the task itself, plus everything
    the decide path reads off its CR6 verdict evidence.

    All three travel together because they come out of ONE payload. The decide
    path needs every one of them (is this still the PR's open review task? how
    many rounds has it recorded? what did the newest round decide?), and
    `_count_verdicts` and `_newest_verdict` are deliberately written as mirrors
    over the same evidence array -- so reading them apart would mean fetching
    and re-parsing the same task two and three times per PR per poll, which is
    the exact cost this whole path exists to stop paying.

    `verdict` is None when no envelope on the task parses -- the normal round-1
    case, indistinguishable here from "no verdict of record yet", which is what
    the caller wants it to mean anyway.

    `envelope` is that same newest envelope whole (issue #155): the
    `round.verdict` event of a session-closed round reads its readiness and
    contract version off the read that already knew the verdict word.
    """

    task: Task
    verdicts: int
    verdict: "str | None"
    envelope: "VerdictEnvelope | None" = None


class Alissa:
    def __init__(
        self,
        *,
        task_list_self_scope: bool = False,
        task_list_bow_id: "str | None" = None,
    ) -> None:
        """`task_list_self_scope` opts the list call into `--self`.

        Default OFF, and that default is evidence, not caution -- see
        TASK_LIST_SELF_FLAG. It is still a knob because ownership is a property
        of a DEPLOYMENT (who creates its review tasks), not of this code, and an
        operator who knows their review tasks are all actor-owned should be able
        to say so.

        `task_list_bow_id` opts the list call into `--bow <id>` (issue #100).
        Unset -- the default -- is today's call shape, unconditionally. It is
        the id ALREADY RESOLVED through the config layers (file < CLI < env);
        this class does not read the environment, so the one place that decides
        what the id is stays `config.env_task_list_bow_id`.
        """
        self._task_list_self_scope = bool(task_list_self_scope)
        self._task_list_bow_id = (task_list_bow_id or "").strip() or None
        # The probe's answer, memoized for the process; None = not probed yet.
        # A probe that FAILS is deliberately not memoized (see probe_task_list).
        self._task_list_flags: "TaskListFlags | None" = None
        # Set when a narrowed call has been disproved at RUNTIME -- the CLI
        # advertised a flag whose call then failed or came back empty. From then
        # on this process makes the plain call, because a list that answers
        # wrongly is worse than a list that is large: `find_review_task` reads an
        # empty corpus as "this PR has no review task".
        self._task_list_narrowing_disabled = False
        # Set when the BOW SPECIFICALLY has been disproved at runtime -- a
        # BOW-scoped call that answered empty. Separate from the flag above
        # because the evidence is different in kind: `--status`/`--self`/
        # `--view` filter the same corpus, so an empty answer from them is
        # anomalous, while `--bow` REPLACES the corpus and "this body of work
        # holds nothing" is a legitimate answer (see `list_tasks`).
        self._task_list_bow_disabled = False
        # One diagnostic per client for a status filter that could not be sent
        # (see `_warn_status_filter_dropped`); this path runs every poll pass.
        self._status_filter_warned = False
        # The spawn-stamp probe's answer, memoized like `_task_list_flags`;
        # None = not probed yet (or the last probe could not run).
        self._spawn_stamps_accepted: "bool | None" = None

    @property
    def task_list_bow_id(self) -> "str | None":
        """The BOW this client scopes its task list to, or None.

        Read by the boot preflight, which must warn on the id this CLIENT holds
        rather than on the one the config file happens to carry -- the env layer
        wins over both file and CLI, so those two can disagree.
        """
        return self._task_list_bow_id

    # -- the `alissa task list` narrowing probe -----------------------------

    def probe_task_list(self) -> "TaskListFlags":
        """Which narrowing flags the INSTALLED `alissa task list` advertises.

        Read off the CLI's own `--help`, which is local, tokenless and free.
        The alternative -- send the flag and fall back when the call fails --
        cannot tell an unknown flag from an auth hiccup, and this daemon turns a
        non-zero `alissa` exit into a SKIPPED decision, so a mis-sent flag does
        not cost a slower call, it costs a REVIEW.

        Probed off the help OUTPUT rather than the exit status on purpose: this
        CLI is commander-based and answers an unknown *subcommand* by printing
        the parent help and exiting 0, so "it exited 0" reports every old CLI as
        capable. (Flags are stricter than subcommands here, but the rule is the
        same one and there is no reason to keep two.)

        A probe that ANSWERS is memoized for the process -- the CLI cannot
        change under a running daemon. A probe that FAILS is not: a transient
        `alissa` failure then degrades one pass instead of pinning the daemon to
        the widest call until someone restarts it.
        """
        if self._task_list_flags is not None:
            return self._task_list_flags
        try:
            helptext = run(["alissa", "task", "list", "--help"], timeout=20)
        except CommandError as exc:
            log.warning(
                "could not probe `alissa task list --help` (%s) — this pass "
                "lists tasks unnarrowed, as the daemon always did", exc,
            )
            return TaskListFlags()
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure probing `alissa task list --help`")
            return TaskListFlags()

        flags = TaskListFlags(
            status=_advertises(helptext, TASK_LIST_STATUS_FLAG),
            self_scope=_advertises(helptext, TASK_LIST_SELF_FLAG),
            digest=_advertises(helptext, TASK_LIST_VIEW_FLAG),
            bow=_advertises(helptext, TASK_LIST_BOW_FLAG),
        )
        self._task_list_flags = flags
        return flags

    def _warn_status_filter_dropped(self) -> None:
        """Say, once, that a status narrowing this CLI offers is going unused.

        Not emitted where the filter is COMPUTED, which is import time, and that
        is the whole point of it living here (PR #98 round 1): `__main__` pulls
        this module in through `.loop` long before it calls
        `logging.basicConfig`, so a warning at module scope falls through to
        `logging.lastResort` -- bare on stderr, no timestamp or logger name, and
        outside whatever handler the deployment configured.

        It would also be premature there. At import nothing yet knows whether
        this CLI serves `--status` at all, and on a CLI that does not, the
        filter costs nothing and there is nothing to warn about. Here the
        message is only emitted when the flag was on offer and could not be
        used, which is the only case where the defect actually costs reads.
        """
        if self._status_filter_warned:
            return
        self._status_filter_warned = True
        reason = (
            "non-canonical status(es) in OPEN_STATUSES: "
            + ", ".join(NON_CANONICAL_OPEN_STATUSES)
            if NON_CANONICAL_OPEN_STATUSES
            else "OPEN_STATUSES is empty"
        )
        log.warning(
            "`alissa task list` offers --status and this daemon is not using "
            "it (%s) -- the CLI rejects the whole call over one unknown value, "
            "so the corpus is listed unnarrowed",
            reason,
        )

    def task_list_argv(self, *, narrow_status: bool = True) -> list[str]:
        """The narrowest `alissa task list` this CLI actually supports.

        Every addition is probe-gated, so a CLI that offers none of them
        produces exactly the call the daemon has always made. (The CLI installed
        on 2026-08-28 offers `--self` and `--bow`, so that is no longer the
        no-op it once was -- and it still reports version `0.1.0`, which is why
        the gate reads the help rather than the version. See
        TASK_LIST_BOW_FLAG.)
        """
        argv = ["alissa", "task", "list", "--json"]
        if self._task_list_narrowing_disabled:
            return argv
        flags = self.probe_task_list()
        # The BOW goes FIRST because it is the only one of these that chooses a
        # candidate set rather than filtering one, and reading the argv left to
        # right is reading them in that order. The others are still sent: the
        # projection (`--view digest`) genuinely applies within a BOW, and the
        # ones the server ignores there cost bytes rather than reviews -- see
        # TASK_LIST_BOW_FLAG for which is which, and for why that list is a
        # dated snapshot rather than a contract.
        if flags.bow and self._task_list_bow_id and not self._task_list_bow_disabled:
            argv += [TASK_LIST_BOW_FLAG, self._task_list_bow_id]
        # An empty filter is not a filter: `_status_filter` answers `""` when
        # the open set carries a status the CLI would reject, and sending
        # `--status ""` would be that same rejected call with an extra step.
        if flags.status and narrow_status:
            if TASK_LIST_STATUS_FILTER:
                argv += [TASK_LIST_STATUS_FLAG, TASK_LIST_STATUS_FILTER]
            else:
                self._warn_status_filter_dropped()
        if flags.self_scope and self._task_list_self_scope:
            argv.append(TASK_LIST_SELF_FLAG)
        if flags.digest:
            argv += [TASK_LIST_VIEW_FLAG, TASK_LIST_DIGEST_VIEW]
        return argv

    def list_tasks(self, *, narrow_status: bool = True) -> list[Task]:
        """This actor's live task corpus -- the expensive call.

        `alissa task list` (CLI 0.1.0) exposed no server-side narrowing at all:
        its only flags were `--json` and `--include-terminal`, and omitting the
        latter -- already the default -- was the whole of the available
        filtering. Newer CLIs offer more, so the call is now assembled from a
        boot-time probe of the installed CLI's help (`task_list_argv`): a status
        filter covering exactly the statuses a live review task can hold, a lean
        `--view digest`, `--self` when the deployment says its review tasks are
        actor-owned, and `--bow <id>` when it has named the body of work they
        are created into. None of it is required; an absent flag is simply not
        sent.

        Narrowing is still the SECOND line of defence, not the first. Even a
        perfectly narrowed call is the actor's whole review-task corpus, so the
        daemon's job remains to call this RARELY: see loop._review_task (the
        persisted PR -> task mapping), loop._pass_task_list (at most one fetch
        per poll pass) and the negative cache behind them (state's
        `review_task_misses`, which bounds the ONE case where none of those
        help -- a PR that has no review task at all).

        `narrow_status=False` is for callers that must see review tasks the
        daemon's own `is_open` predicate would reject (prreview reads a task's
        verdict envelope after the round is over). It suppresses only the status
        filter; every other narrowing still applies.

        A narrowed call that FAILS, or that answers with an empty corpus, is
        retried once unnarrowed. Both are how a CLI that advertises a flag its
        API does not serve would present, and either would otherwise read as
        "this actor has no review tasks" -- which is a skipped review, not a
        slower one.

        What the retry then turns OFF differs by which signal it was, because
        the two carry different evidence. A FAILURE turns off all narrowing for
        the process. An EMPTY answer does too -- unless the call was BOW-scoped,
        in which case only the BOW is dropped and the other flags stand: `--bow`
        replaces the corpus instead of filtering it, so an empty answer from it
        is a legitimate result and says nothing about the rest.
        """
        argv = self.task_list_argv(narrow_status=narrow_status)
        plain = ["alissa", "task", "list", "--json"]
        try:
            data = run_json(argv, timeout=90) or []
        except CommandError:
            if argv == plain:
                raise
            # Deliberately blunter than the empty-answer path below, which
            # attributes the disproof to `--bow` alone when it can. A hard
            # failure cannot be attributed: it does not say which flag the
            # server choked on, and a transient timeout or 5xx says nothing
            # about the flags at all. So all narrowing goes -- wide but
            # complete, which costs the optimization and never a review. The
            # asymmetry is a decision, not a corner the BOW split missed.
            log.warning(
                "`%s` failed — retrying the plain task list and dropping the "
                "narrowing for this process", " ".join(argv),
            )
            self._task_list_narrowing_disabled = True
            data = run_json(plain, timeout=90) or []

        tasks = self._tasks_from(data)
        if tasks or argv == plain or self._task_list_narrowing_disabled:
            # The third clause is the `except` branch above having already
            # retried `plain`: without it, a retry that legitimately answers an
            # EMPTY corpus falls into the cross-check below and calls `plain` a
            # second time -- three subprocess calls where two happened, inside
            # the change whose purpose is removing whole-corpus fetches -- and
            # warns about a narrowed call that in fact errored and never
            # answered (PR #88 round 1).
            return tasks

        # An empty answer from a narrowed call. A genuinely empty corpus is
        # possible and costs one extra list; a filter the API does not serve
        # would cost every review this actor owns.
        log.warning(
            "`%s` returned no tasks — retrying the plain task list to tell an "
            "empty corpus from a filter this API does not serve", " ".join(argv),
        )
        tasks = self._tasks_from(run_json(plain, timeout=90) or [])
        if not tasks:
            return tasks
        if TASK_LIST_BOW_FLAG in argv:
            # "The plain list found rows" proves nothing about a BOW-scoped
            # call, because the two answers are not comparable: `--bow` swaps
            # the candidate set rather than filtering the actor's, so its answer
            # is not a subset of the plain one (measured 2026-08-28 -- see
            # TASK_LIST_BOW_FLAG). Condemning the other three flags on this
            # evidence would drop the whole optimization on the state the
            # rollout STARTS in: a review BOW nothing has been created into yet.
            #
            # The BOW itself is still dropped for the process, and that is not a
            # third option so much as the only tractable one: with a swap there
            # is no way to tell a mistyped id from an empty-but-correct one, and
            # holding the BOW would mean paying two list calls every pass, for
            # ever, on a typo. Wide-but-complete, the direction everything on
            # this path degrades in.
            self._task_list_bow_disabled = True
            log.warning(
                "the BOW-scoped task list answered empty while the plain list "
                "returned %d task(s) — that is not evidence the call is broken "
                "(a BOW-scoped list REPLACES the actor's corpus), so only the "
                "BOW is dropped for this process and the other narrowing "
                "stands. Either the configured id is wrong, or nothing has been "
                "created into that body of work yet; once it is populated, "
                "restart the daemon to scope to it again", len(tasks),
            )
        else:
            self._task_list_narrowing_disabled = True
            log.warning(
                "the plain task list returned %d task(s) — the narrowed call is "
                "dropping rows, so this process stops narrowing", len(tasks),
            )
        return tasks

    @staticmethod
    def _tasks_from(data: object) -> list[Task]:
        tasks = []
        for row in data if isinstance(data, list) else []:
            task = _task_from_row(row)
            if task is not None:
                tasks.append(task)
        return tasks

    def get_task(self, ref: str) -> "TaskDetail | None":
        """Read ONE task by ref: title, status and verdict count in one call.

        This is what makes a cached PR -> review-task mapping usable. Resolving
        the task by ref costs a single-task fetch; resolving it by searching
        titles costs the actor's entire corpus, which is the read this whole
        path exists to stop paying every poll.

        None means "could not be read" and NOTHING more -- a deleted task and a
        transient CLI failure are indistinguishable from here, so a caller must
        not treat None as proof that a cached mapping is wrong (see
        loop._review_task, which keeps the row and falls back to the search).
        Never raises: the daemon polls forever and this runs inside every pass.
        """
        try:
            data = run_json(["alissa", "task", "get", ref, "--json"], timeout=90)
        except CommandError as exc:
            log.warning("could not read task %s: %s", ref, exc)
            return None
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure reading task %s", ref)
            return None

        try:
            task = _task_from_row(data)
            if task is None:
                return None
            envelope = self._newest_envelope(data)
            return TaskDetail(
                task=task,
                verdicts=self._count_verdicts(data),
                verdict=None if envelope is None else envelope.verdict,
                envelope=envelope,
            )
        except Exception:  # pragma: no cover - defence in depth
            log.exception("could not parse task payload for %s", ref)
            return None

    def find_review_task(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        tasks: "list[Task] | None" = None,
    ) -> Task | None:
        """CR2: exactly one review task per PR. Reuse it across rounds (CR7).

        `tasks` supplies a corpus the caller already fetched, so several PRs
        missing the cache in the SAME poll pass share one list call instead of
        issuing an identical one each (the observed 2-4 same-second bursts).
        Omitted -- the console-script path, and any caller with no pass to
        scope to -- fetches its own.
        """
        pool = self.list_tasks() if tasks is None else tasks
        matches = [t for t in pool if is_review_task_for(owner, repo, number, t)]

        if not matches:
            return None
        if len(matches) > 1:
            # Several verdicts on one task are fine; several tasks per PR are not.
            log.warning(
                "CR2 violation: %d open review tasks for %s/%s#%d (%s) -- using %s",
                len(matches),
                owner,
                repo,
                number,
                ", ".join(t.ref for t in matches),
                matches[0].ref,
            )
        return matches[0]

    def latest_verdict(self, task_ref: str) -> str | None:
        """The newest CR6 verdict envelope's verdict on a review task, or None.

        Returns VERDICT_APPROVE / VERDICT_REQUEST_CHANGES. This is the verdict
        of record: reviewers post comment-mode reviews, so the GitHub review
        state is always COMMENTED and cannot express approval at all.

        The word alone; `latest_envelope` returns the whole record (verdict
        plus merge-readiness) off the same read for the caller that posts it.

        Never raises. The daemon polls forever and this runs inside every pass,
        so absent, empty or malformed evidence degrades to "no verdict" rather
        than taking the loop down.
        """
        envelope = self.latest_envelope(task_ref)
        return None if envelope is None else envelope.verdict

    def latest_envelope(self, task_ref: str) -> "VerdictEnvelope | None":
        """The newest CR6 verdict envelope on a review task, parsed, or None.

        Same read and same tolerance as `latest_verdict`; this is the record
        the native post carries onto GitHub (issue #130), so it needs the
        envelope's Merge-Readiness line alongside the verdict word.
        """
        try:
            data = run_json(["alissa", "task", "get", task_ref, "--json"], timeout=90)
        except CommandError as exc:
            log.warning("could not read verdict evidence for %s: %s", task_ref, exc)
            return None
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure reading verdict evidence for %s", task_ref)
            return None

        try:
            return self._newest_envelope(data)
        except Exception:  # pragma: no cover - defence in depth
            log.exception("could not parse verdict evidence for %s", task_ref)
            return None

    @staticmethod
    def _created_key(value: object) -> tuple[int, float]:
        """One evidence item's `createdAt`, as a sortable stamp.

        `alissa task get --json` dates evidence with epoch MILLISECONDS as an
        int; the API's other surfaces (and hand-written fixtures) use an ISO-8601
        string. Both are normalised here to one float, because the previous key
        -- `created if isinstance(created, str) else ""` -- collapsed every real
        item to the empty string, leaving `max` to keep the FIRST element of an
        all-equal set. Evidence comes back oldest-first, so on live data the
        OLDEST verdict won: a PR whose round 1 was request_changes and round 2
        approve never converged through the envelope branch (TASK-194837655).

        Normalising rather than widening the isinstance is deliberate: a task
        carrying both shapes would produce `(str, ...)` and `(int, ...)` keys
        that raise TypeError the moment sorting compared them.

        Returns `(has_stamp, seconds)`. An absent or unparseable stamp is
        `(0, 0.0)` and sorts FIRST, preserving the old rule that a dated
        envelope always beats one that lost its timestamp.
        """
        if isinstance(value, bool):  # bool is an int; never a timestamp
            return (0, 0.0)
        if isinstance(value, (int, float)):
            seconds = float(value)
            # Milliseconds, by magnitude: 1e11 seconds is the year 5138, while
            # 1e11 milliseconds is 1973 -- so anything above it is ms, and a
            # task holding both units still orders correctly.
            if abs(seconds) > 1e11:
                seconds /= 1000.0
            return (1, seconds)
        if isinstance(value, str) and value:
            try:
                return (1, datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except ValueError:
                return (0, 0.0)
        return (0, 0.0)

    @staticmethod
    def _newest_verdict(payload: object) -> str | None:
        """The newest parseable verdict WORD out of a task's evidence array;
        `_newest_envelope` with the readiness dropped, for the callers that
        only decide on the word."""
        envelope = Alissa._newest_envelope(payload)
        return None if envelope is None else envelope.verdict

    @staticmethod
    def _newest_envelope(payload: object) -> "VerdictEnvelope | None":
        """Pick the newest parseable verdict envelope out of a task's evidence
        array, with the Merge-Readiness judgment it carries (issue #130).

        Every layer is optional by design -- the payload shape is whatever the
        CLI printed, and a task with no evidence is the normal round-1 case.

        The readiness is read off the SAME evidence item as the verdict (its
        body first, then its title), never off a neighbour: a stray line on an
        older envelope must not decorate a newer round's verdict.
        """
        if not isinstance(payload, dict):
            return None
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            return None

        found: list[tuple[tuple[int, float], int, VerdictEnvelope]] = []
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            content = item.get("markdownContent")
            for blob in (title, content):
                if not isinstance(blob, str):
                    continue
                match = _VERDICT_RE.search(blob)
                if match:
                    # The class the triple carries is not stored: the
                    # reason keeps its prefix, and the envelope's readers
                    # re-derive the class from the line they emit.
                    readiness, reason, _ = parse_readiness(content)
                    if readiness is None:
                        readiness, reason, _ = parse_readiness(title)
                    commit, commit_reason, _ = parse_commit_readiness(content)
                    if commit is None:
                        commit, commit_reason, _ = parse_commit_readiness(title)
                    contract = parse_contract_version(content)
                    if contract is None:
                        contract = parse_contract_version(title)
                    found.append(
                        (Alissa._created_key(item.get("createdAt")),
                         index,
                         VerdictEnvelope(
                             verdict=match.group(1).lower(),
                             readiness=readiness,
                             readiness_reason=reason,
                             commit_readiness=commit,
                             commit_readiness_reason=commit_reason,
                             contract_version=contract,
                         ))
                    )
                    break

        if not found:
            return None
        # Newest stamp wins; undated evidence sorts first, so a dated envelope
        # always beats one that lost its timestamp (see _created_key). The
        # append INDEX breaks ties: evidence comes back oldest-first, so two
        # envelopes sharing a stamp resolve to the later-recorded one rather
        # than to whichever `max` happened to reach first.
        return max(found, key=lambda item: (item[0], item[1]))[2]

    def count_verdicts(self, task_ref: str) -> int:
        """How many CR6 verdict envelopes are on the review task.

        One envelope per completed round (CR7, append-only), so this is the
        authoritative round count -- unlike the GitHub review count it cannot be
        thrown off by an empty-bodied review or two reviews in one cycle. Never
        raises: absent, empty, or malformed evidence degrades to 0 (round 1)
        rather than taking the loop down.
        """
        try:
            data = run_json(["alissa", "task", "get", task_ref, "--json"], timeout=90)
        except CommandError as exc:
            log.warning("could not read verdict evidence for %s: %s", task_ref, exc)
            return 0
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure reading verdict evidence for %s", task_ref)
            return 0
        try:
            return self._count_verdicts(data)
        except Exception:  # pragma: no cover - defence in depth
            log.exception("could not count verdict evidence for %s", task_ref)
            return 0

    @staticmethod
    def _count_verdicts(payload: object) -> int:
        """Count evidence items carrying a verdict envelope. Mirrors
        `_newest_verdict`'s tolerant parsing -- one match per item at most."""
        if not isinstance(payload, dict):
            return 0
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            return 0
        count = 0
        for item in evidence:
            if not isinstance(item, dict):
                continue
            for blob in (item.get("title"), item.get("markdownContent")):
                if isinstance(blob, str) and _VERDICT_RE.search(blob):
                    count += 1
                    break
        return count

    # -- the `alissa tmux queue add` spawn-stamp probe -----------------------

    def accepts_spawn_stamps(self) -> bool:
        """Whether the installed `queue add` takes `--task` AND `--repo`.

        Read off the CLI's own `--help`, for the reasons `probe_task_list`
        gives. An answer, yes or no, is memoized for the process; a probe that
        cannot run at all reads as "no" for THIS spawn and is not memoized, so
        a transient failure never switches the stamps off until a restart.
        """
        if self._spawn_stamps_accepted is not None:
            return self._spawn_stamps_accepted
        try:
            helptext = run(["alissa", "tmux", "queue", "add", "--help"], timeout=20)
        except CommandError as exc:
            log.warning(
                "could not probe `alissa tmux queue add --help` (%s) — this "
                "reviewer is enqueued without --task/--repo", exc,
            )
            return False
        except Exception:  # pragma: no cover - defence in depth
            log.exception("unexpected failure probing `alissa tmux queue add --help`")
            return False
        self._spawn_stamps_accepted = queue_add_accepts_stamps(helptext)
        if not self._spawn_stamps_accepted:
            log.info(
                "the installed alissa CLI does not accept `queue add --task/--repo`; "
                "reviewer spawns go unstamped"
            )
        return self._spawn_stamps_accepted

    def enqueue_reviewer(
        self,
        *,
        session: str,
        directive: str,
        cwd: Path,
        agent: str,
        task_ref: str | None,
        repo: str | None = None,
        dry_run: bool = False,
    ) -> None:
        """Enqueue one reviewer session.

        `task_ref` is the ORIGIN task (`TASK-<n>`) the round serves -- not the
        review task, which the directive names and the ledger records -- and
        `repo` the PR's `owner/name`, lowercased here. They are passed as
        `--task` / `--repo` only when the installed CLI accepts both (see
        `accepts_spawn_stamps`); an older CLI gets neither, so the enqueue
        never fails for want of a stamp.
        """
        argv = [
            "alissa",
            "tmux",
            "queue",
            "add",
            session,
            "--agent",
            agent,
            "--cwd",
            str(cwd),
        ]
        stamps: "list[str]" = []
        if task_ref:
            stamps += [QUEUE_ADD_TASK_FLAG, task_ref]
        if repo:
            stamps += [QUEUE_ADD_REPO_FLAG, repo.lower()]

        if dry_run:
            # Runs nothing, the probe included: the line shows the stamps a
            # stamp-aware CLI would receive.
            log.info("[dry-run] would enqueue: %s", " ".join(argv + stamps) + " <directive>")
            return

        if stamps and self.accepts_spawn_stamps():
            argv += stamps
        argv.append(directive)
        run(argv, timeout=60)

        # Reviewers are one-shot per round (CR3): once the session finishes and is
        # reaped, it must never be respawned. Make that explicit so a self-kill or
        # a daemon reap can't trigger a respawn loop. Best-effort — an older CLI
        # without `queue set` should not fail the enqueue.
        try:
            run(["alissa", "tmux", "queue", "set", session, "respawn", "off"],
                timeout=30, check=False)
        except CommandError:  # pragma: no cover - defence in depth
            log.warning("could not set respawn off for %s", session)

    def list_review_sessions(self) -> list[ManagedSession]:
        """The live reviewer-grammar managed sessions, from `alissa tmux ls`.

        The reap sweep's starting point. Unlike the review-requested search,
        the live session list cannot lose a finished session, so every reap
        candidate is reachable from here. `--live` because a session that is
        already gone (self-killed, or killed by an operator) holds no worker
        slot and needs no reap. Raises CommandError upward -- the sweep skips
        this pass and tries again next poll.

        The filter is `parse_session_name`, not the bare `review-` prefix: this
        list is what the sweep kills from, the container is shared with other
        daemons, and a name that does not parse as a reviewer session is never
        enumerated here at all -- so no later bug in the sweep can reach one.
        """
        data = run_json(["alissa", "tmux", "ls", "--json", "--live"], timeout=60) or []
        sessions = []
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if isinstance(name, str) and parse_session_name(name) is not None:
                last = row.get("lastActivity")
                real = row.get("session")
                sessions.append(
                    ManagedSession(
                        name=name,
                        status=str(row.get("status") or ""),
                        last_activity=float(last) if isinstance(last, (int, float)) else 0.0,
                        session=real if isinstance(real, str) and real else None,
                    )
                )
        return sessions

    def kill_session(self, session: str) -> None:
        """Kill ONE finished reviewer's managed session to free its worker slot.

        Per-session, always: `alissa tmux kill <name>` and never a server-wide
        kill. The worker container is shared with every other lane, so a
        `kill-server` here would take down unrelated daemons' sessions along
        with the one that is actually finished. A test pins the absence of that
        verb across the whole package.

        Best-effort and idempotent-friendly: the session may already be gone (the
        reviewer self-killed), so a non-zero exit is not an error here. Dry-run
        is the caller's job (the sweep decides and logs before calling).
        """
        run(["alissa", "tmux", "kill", session], timeout=30, check=False)

    def tail_session(self, session: str, lines: int) -> str:
        """The last `lines` of ONE session's terminal, via `alissa tmux tail`.

        The stale-round probe's evidence seam (issue #136): it asks whether a
        session that reads alive is parked on Claude Code's first-run dialog.
        Read-only, and best-effort by contract -- a CLI that cannot capture
        the pane (the session just died, no tmux server, a timeout) answers
        the EMPTY string, which the classifier treats as "no evidence", never
        as the dialog: absence of a capture keeps the existing defer.
        """
        try:
            return run(
                ["alissa", "tmux", "tail", "-n", str(lines), session],
                timeout=30,
                check=False,
            )
        except CommandError as exc:
            log.debug("could not tail %s: %s", session, exc)
            return ""

    def capture_pane(self, name: str, lines: int = 40) -> str:
        """The last `lines` of ONE managed session's pane over raw tmux
        (`capture-pane -p`), or "" when tmux cannot answer (no such session,
        the socket gone, a timeout).

        The responder's evidence seam (issue #138). Like `tail_session` it
        returns UNTRUSTED, possibly sensitive terminal content: the caller
        classifies it and lets only the classification -- plus a scrubbed
        signature line -- reach an operator-facing level. A READ, so it runs
        under dry-run too: a dry pass has to classify what a live one would.
        Raw tmux rather than the `alissa` CLI because the CLI's `tail` is a
        convenience without `send-keys` beside it, and the answer has to land
        on the very pane the capture read."""
        try:
            argv = capture_pane_argv(name, lines)
        except ValueError as exc:
            log.warning("capture_pane refused: %s", exc)
            return ""
        try:
            out = run(argv, timeout=15)
        except CommandError as exc:
            log.debug("could not capture the pane of %s (%s)", name, exc)
            return ""
        return out.rstrip("\n")

    def pane_path(self, name: str) -> str:
        """The session's pane current path (`#{pane_current_path}`), or ""
        when tmux cannot answer. One leg of the review-checkout resolution:
        a reviewer that `cd`ed into its `REVIEW-*` checkout names it here."""
        try:
            argv = pane_path_argv(name)
        except ValueError as exc:
            log.warning("pane_path refused: %s", exc)
            return ""
        try:
            return run(argv, timeout=15).strip()
        except CommandError as exc:
            log.debug("could not read the pane path of %s (%s)", name, exc)
            return ""

    def pane_command(self, name: str) -> str:
        """The session's pane current command (`#{pane_current_command}`),
        or "" when tmux cannot answer. The first-turn death check's one
        liveness probe (issue #146): a reviewer whose pane reads a shell
        (`SHELL_COMMANDS`) has lost its `claude`, however alive the tmux
        session itself is. A READ: runs under dry-run too."""
        try:
            argv = pane_command_argv(name)
        except ValueError as exc:
            log.warning("pane_command refused: %s", exc)
            return ""
        try:
            return run(argv, timeout=15).strip()
        except CommandError as exc:
            log.debug("could not read the pane command of %s (%s)", name, exc)
            return ""

    def send_keys(self, name: str, *keys: str, dry_run: bool = False) -> bool:
        """Press `keys` -- each one of `prompts.ALLOWED_KEYS`, nothing else
        -- on ONE managed session's pane. True when tmux accepted them.

        The ONLY key-pressing surface in this daemon, and deliberately
        narrow: an allowlisted enum, a validated session name, an exact-match
        target, one `send-keys` per call. A key outside the allowlist is
        refused before any process is spawned and logged at WARNING (it is a
        programming error, never a pane's doing -- keys come from the
        policy table, not from the screen). Dry-run logs the intent and
        sends nothing."""
        try:
            argv = send_keys_argv(name, tuple(keys))
        except ValueError as exc:
            log.warning("send_keys refused for %s: %s", name, exc)
            return False
        if dry_run:
            log.info("[dry-run] would send keys %s to %s", " ".join(keys), name)
            return False
        try:
            run(argv, timeout=15)
        except CommandError as exc:
            log.warning("could not send keys %s to %s (%s)", " ".join(keys), name, exc)
            return False
        return True

    def add_repo_to_workspace(
        self, owner: str, repo: str, workspace_root: Path, *, dry_run: bool = False
    ) -> None:
        """Hub-ify a repo into the workspace (bare clone + main/ worktree) and
        record it in alissa-workspace.yaml. Idempotent per the CLI's contract."""
        argv = ["alissa", "code", "workspace", "add", f"{owner}/{repo}"]
        if dry_run:
            log.info("[dry-run] would run: %s (cwd=%s)", " ".join(argv), workspace_root)
            return

        log.info("hub-ifying %s/%s into %s", owner, repo, workspace_root)
        # Cloning a repo can be slow; the poll loop tolerates a long pass.
        run(argv, timeout=600, cwd=workspace_root)

    def worker_running(self) -> bool:
        """The queue only drains while `alissa worker` reconciles it."""
        try:
            out = run(["alissa", "worker", "status"], timeout=30, check=False)
        except CommandError:
            return False
        return "not running" not in out.lower() and "no worker" not in out.lower()
