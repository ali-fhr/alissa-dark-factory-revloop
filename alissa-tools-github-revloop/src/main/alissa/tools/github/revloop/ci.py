"""Pre-existing CI reds (issue #214).

A failing check on a PR's head is not always the PR's doing. On 4R7e
(`4r7-fhr/4r7-engine-workers#102`, 2026-11-05) a PR that only added
`.github/workflows/ci.yml` collected four identical `CHANGES_REQUESTED`
reviews on one head, each with a `[blocker] CI is red`. The workflows that
were red (`check-style`, `check-types`, `check-tests`, `check-wheel`) had been
failing since 2026-10-11, long before the PR existed. The PR could not fix
them, so the loop deadlocked.

`classify` splits a red rollup's failing contexts into the ones the PR may
have caused and the ones it provably inherited. A failing context is
PRE-EXISTING when BOTH hold:

* it already failed without this PR, in either of two ways:
  - the context with the same name is failing on the base branch's CURRENT
    head; or
  - its workflow is `pull_request`-triggered and never ran on the base head,
    and the latest completed `pull_request` run of that same workflow on
    another PR, created before this PR's first commit, failed the job of
    the same name;
* the PR's diff does not touch the workflow file behind it. A context with
  no known workflow file (a legacy status, a third-party check app, a job
  name two workflows share) counts as untouched only when the diff touches
  nothing under `.github/` at all.

Everything else stays a real red and still blocks. The answer FAILS CLOSED:
any read that errors leaves every failing context where it was, so an
unreadable base never turns a red into a note. `RateLimited` propagates, as
on every read path, to `run_forever`'s backoff.

Cost: nothing on a rollup that is not red. A red one costs the PR files
listing, the head's Actions runs and jobs, the base branch head and its
rollup, plus, per `pull_request`-only workflow still unexplained, the PR's
first commit (once) and one run listing with its jobs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from .ghclient import (
    CHECKS_GREEN,
    CHECKS_PENDING,
    CHECKS_RED,
    CheckContext,
    CheckRollup,
    GitHub,
    PullRequest,
    RateLimited,
    WorkflowJob,
)

log = logging.getLogger(__name__)

# The trigger whose workflows never run on the base branch, which is why the
# base head cannot vouch for them and the earlier-PR lane exists.
PULL_REQUEST_EVENT = "pull_request"

# Where a check's definition can live. A context whose workflow file is
# unknown is untouched only if the diff stays out of this whole tree.
CI_CONFIG_PREFIX = ".github/"


@dataclass(frozen=True)
class PreExisting:
    """One failing context that was already red without this PR.

    `where` says which lane proved it, in words a review can quote ("the
    base branch `main` at 1a2b3c4d" or "an earlier pull_request run"), and
    `evidence_url` links the run where it had already failed.
    """

    context: CheckContext
    where: str
    evidence_url: str = ""


@dataclass(frozen=True)
class Classified:
    """A rollup with its pre-existing failures taken out.

    `rollup` is what the gates judge: its `failing` holds only the reds the
    PR may have caused, and its `state` is recomputed from what is left
    (red, else pending, else green). `preexisting` is what became a note.
    """

    rollup: CheckRollup
    preexisting: "tuple[PreExisting, ...]" = ()


def _workflow_file(job: "WorkflowJob | None") -> str:
    """The workflow file behind a job, without the `@ref` suffix a run's
    `path` carries for a dynamic or reusable workflow."""
    if job is None:
        return ""
    return job.workflow_path.partition("@")[0]


def untouched(files: "set[str]", job: "WorkflowJob | None") -> bool:
    """Whether the PR's diff leaves the definition of this check alone."""
    path = _workflow_file(job)
    if path:
        return path not in files
    return not any(f.startswith(CI_CONFIG_PREFIX) for f in files)


def _jobs_by_name(jobs: "list[WorkflowJob]") -> "dict[str, WorkflowJob | None]":
    """Job name -> the job. A name two workflows share maps to None: which
    workflow a check run of that name came from cannot be told apart."""
    out: "dict[str, WorkflowJob | None]" = {}
    for job in jobs:
        out[job.name] = None if job.name in out else job
    return out


def _with_failing(rollup: CheckRollup, failing: "tuple[CheckContext, ...]") -> CheckRollup:
    if failing:
        state = CHECKS_RED
    elif rollup.running:
        state = CHECKS_PENDING
    else:
        state = CHECKS_GREEN
    return replace(rollup, state=state, failing=failing)


def classify(github: GitHub, pr: PullRequest, rollup: CheckRollup) -> Classified:
    """Split `rollup`'s failures into new ones and pre-existing ones."""
    if rollup.state != CHECKS_RED or not rollup.failing:
        return Classified(rollup)
    try:
        return _classify(github, pr, rollup)
    except RateLimited:
        raise
    except Exception as exc:
        log.warning(
            "%s: could not compare the red checks at %s with the base branch "
            "(%s: %s) — every failing check stays blocking",
            pr.slug, pr.head_sha[:8], type(exc).__name__, str(exc)[:200],
        )
        return Classified(rollup)


def _classify(github: GitHub, pr: PullRequest, rollup: CheckRollup) -> Classified:
    base_ref = pr.base_ref
    if not base_ref:
        return Classified(rollup)
    files = set(github.pull_request_files(pr.owner, pr.repo, pr.number))
    jobs = _jobs_by_name(github.workflow_jobs(pr.owner, pr.repo, pr.head_sha))
    base_sha = github.branch_head_sha(pr.owner, pr.repo, base_ref)
    base = github.check_rollup(pr.owner, pr.repo, base_sha)
    base_failing = {c.name: c for c in base.failing}
    base_names = {c.name for c in base.contexts} | set(base_failing)

    first_commit_at: "str | None" = None
    new: list[CheckContext] = []
    found: list[PreExisting] = []
    for context in rollup.failing:
        job = jobs.get(context.name)
        if not untouched(files, job):
            new.append(context)
            continue
        if context.name in base_failing:
            failed = base_failing[context.name]
            found.append(
                PreExisting(
                    context=context,
                    where=f"the base branch `{base_ref}` at {base_sha[:8]}",
                    evidence_url=failed.url,
                )
            )
            continue
        if (
            job is not None
            and job.event == PULL_REQUEST_EVENT
            and job.workflow_id is not None
            and context.name not in base_names
        ):
            if first_commit_at is None:
                first_commit_at = github.pull_request_first_commit_at(
                    pr.owner, pr.repo, pr.number
                )
            prior = github.prior_pull_request_run(
                pr.owner, pr.repo, job.workflow_id,
                before=first_commit_at, exclude_branch=pr.head_ref, base_ref=base_ref,
            )
            if prior is not None:
                run_url, prior_jobs = prior
                same = [j for j in prior_jobs if j.name == context.name]
                if same and all(not j.running and not j.passing for j in same):
                    found.append(
                        PreExisting(
                            context=context,
                            where=(
                                f"an earlier `{PULL_REQUEST_EVENT}` run into "
                                f"`{base_ref}`, before this PR's first commit"
                            ),
                            evidence_url=same[0].url or run_url,
                        )
                    )
                    continue
        new.append(context)

    if found:
        log.info(
            "%s: %d of %d failing check(s) at %s were already red without this "
            "PR (%s) — reporting them as pre-existing, not blocking",
            pr.slug, len(found), len(rollup.failing), pr.head_sha[:8],
            ", ".join(p.context.name for p in found),
        )
    return Classified(_with_failing(rollup, tuple(new)), tuple(found))
