"""Unchanged heads and pre-existing CI reds (issue #214).

The 4R7e deadlock (`4r7-fhr/4r7-engine-workers#102`, 2026-11-05): a PR that
only added `.github/workflows/ci.yml` got four identical `CHANGES_REQUESTED`
reviews on head f22e672. Each one blocked on old workflows that had been red
since 2026-10-11. Two rules come out of it:

1. an unchanged head is not re-reviewed on the PR author's own re-request.
   A push, or a request from anyone else (an operator included), re-arms;
2. a failing check that is ALSO red without this PR, either on the base
   head or on an earlier `pull_request` run of its workflow, and whose
   workflow the diff does not touch, is a `[minor]` pre-existing note. It
   never blocks. Any other red still blocks.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from alissa.tools.github.revloop import ci as ci_mod
from alissa.tools.github.revloop.alissa import VERDICT_APPROVE
from alissa.tools.github.revloop.ghclient import (
    CHECKS_GREEN,
    CHECKS_PENDING,
    CHECKS_RED,
    CheckContext,
    CheckRollup,
    GitHub,
    RateLimited,
    ReviewRequest,
    WorkflowJob,
    rollup_of,
)
from alissa.tools.github.revloop.loop import Action, ReviewWatcher
from alissa.tools.github.revloop.state import State
from test_loop import (  # the fakes and the PR scenario the loop tests share
    NUMBER,
    OWNER,
    REPO,
    FakeAlissa,
    FakeGitHub,
    FakeTask,
    _clock,
    _iso,
    make_pr,
    review,
)

AUTHOR = "vlissa-app"
HEAD = "f22e672"
BASE = "b45e000"
OLD_WORKFLOW = ".github/workflows/check-style.yaml"
NEW_WORKFLOW = ".github/workflows/ci.yml"


def red(name, url=None):
    return CheckContext(name, "failure", url or f"https://ci/{name}/head")


def green(name):
    return CheckContext(name, "success", f"https://ci/{name}/ok")


def job(name, *, path=OLD_WORKFLOW, event="push", workflow_id=11, conclusion="failure"):
    return WorkflowJob(
        name=name, conclusion=conclusion, url=f"https://ci/{name}/head",
        workflow_id=workflow_id, workflow_path=path, event=event,
    )


class CIGitHub(FakeGitHub):
    """FakeGitHub plus the reads ci.classify makes."""

    def __init__(self, pr, reviews):
        super().__init__(pr, reviews)
        self.files = [NEW_WORKFLOW]
        self.jobs: list[WorkflowJob] = []
        self.base_sha = BASE
        self.prior: "tuple[str, list[CheckContext]] | None" = None
        self.prior_calls: list[dict] = []
        self.first_commit_at = "2026-11-05T02:00:00Z"
        self.read_error: "BaseException | None" = None

    def pull_request_files(self, owner, repo, number):
        if self.read_error:
            raise self.read_error
        return list(self.files)

    def workflow_jobs(self, owner, repo, sha):
        return list(self.jobs)

    def branch_head_sha(self, owner, repo, ref):
        assert ref == "main"
        return self.base_sha

    def pull_request_first_commit_at(self, owner, repo, number):
        return self.first_commit_at

    def prior_pull_request_run(self, owner, repo, workflow_id, *, before, exclude_branch, base_ref):
        self.prior_calls.append(
            dict(workflow_id=workflow_id, before=before, exclude=exclude_branch, base=base_ref)
        )
        return self.prior


def ci_pr(**kw):
    pr = make_pr(author=AUTHOR, sha=HEAD, head_ref="TASK-651234738-CI", **kw)
    return dataclasses.replace(pr, base_ref="main", base_sha="0ld")


@pytest.fixture
def config(tmp_path):
    from alissa.tools.github.revloop.config import Config

    hub = tmp_path / REPO / "main"
    hub.mkdir(parents=True)
    return Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=tmp_path / "state.db",
        round_cap=10,
        operators=("ops-human",),
    )


def watcher(config, reviews, *, verdict=None, verdict_count=None, readiness=None):
    gh = CIGitHub(ci_pr(), reviews)
    al = FakeAlissa(
        FakeTask(),
        verdict=verdict,
        verdict_count=(
            sum(1 for r in reviews if r.is_substantive) if verdict_count is None else verdict_count
        ),
        readiness=readiness,
    )
    w = ReviewWatcher(config, github=gh, alissa=al, state=State(config.state_db))
    return w, gh, al


# -- 1. an unchanged head is not re-reviewed --------------------------------


def _judged_once(config, monkeypatch, *, actor, sha=HEAD):
    """Round 1 judged `sha` ten minutes ago; `actor` re-requested five
    minutes ago. The PR's head is HEAD."""
    now = _clock(monkeypatch, start=1_800_000_000.0)
    w, gh, al = watcher(
        config, [review("CHANGES_REQUESTED", sha=sha, at=_iso(now["t"] - 600))]
    )
    gh.request_events = [("alissa-app", _iso(now["t"] - 300), actor)]
    return w, gh, al


def test_the_authors_re_request_on_an_unchanged_head_starts_no_round(config, monkeypatch, caplog):
    w, gh, al = _judged_once(config, monkeypatch, actor=AUTHOR)

    with caplog.at_level(logging.INFO):
        first = w.evaluate(OWNER, REPO, NUMBER)
        second = w.evaluate(OWNER, REPO, NUMBER)

    assert first.action is Action.SKIPPED and second.action is Action.SKIPPED
    assert "unchanged" in first.reason
    assert al.enqueued == [], "no reviewer is started on the head round 1 judged"
    assert gh.rollup_reads == [], "skipped before any CI read"
    announced = [r for r in caplog.records if "unchanged head" in r.getMessage()]
    assert len(announced) == 1, "logged once, not every poll"


def _pages(gh):
    return [c for c in gh.comments if "A re-review is owed" in c]


def test_the_refused_re_request_pages_the_operator_once(config, monkeypatch):
    """PR #215 round 1 [major]: the refusal must not be silent. A triage-only
    CR8 round or a re-run-green red would otherwise strand with nobody told."""
    w, gh, al = _judged_once(config, monkeypatch, actor=AUTHOR)

    for _ in range(3):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED

    pages = _pages(gh)
    assert len(pages) == 1, "one page per (head, verdict), not one per poll"
    assert f"`{HEAD}`" in pages[0] and AUTHOR in pages[0]
    assert "**push**" in pages[0] and "as an operator" in pages[0]
    assert al.enqueued == []


def test_the_page_survives_a_restart_and_retries_a_failed_post(config, monkeypatch):
    w, gh, _ = _judged_once(config, monkeypatch, actor=AUTHOR)
    real = gh.comment
    gh.comment = lambda *a: (_ for _ in ()).throw(RuntimeError("502"))

    w.evaluate(OWNER, REPO, NUMBER)
    assert _pages(gh) == []

    gh.comment = real
    w.evaluate(OWNER, REPO, NUMBER)
    assert len(_pages(gh)) == 1, "the ledger row lands only after the post"

    again = ReviewWatcher(config, github=gh, alissa=w.alissa, state=State(config.state_db))
    again.evaluate(OWNER, REPO, NUMBER)
    assert len(_pages(gh)) == 1, "durable: a restarted daemon does not re-page"


def test_a_dry_run_neither_pages_nor_silences_production(config, monkeypatch):
    dry = dataclasses.replace(config, dry_run=True)
    w, gh, _ = _judged_once(dry, monkeypatch, actor=AUTHOR)

    w.evaluate(OWNER, REPO, NUMBER)
    assert _pages(gh) == []

    live = ReviewWatcher(config, github=gh, alissa=w.alissa, state=State(config.state_db))
    live.evaluate(OWNER, REPO, NUMBER)
    assert len(_pages(gh)) == 1


def test_a_request_that_starts_a_round_pages_nobody(config, monkeypatch):
    w, gh, _ = _judged_once(config, monkeypatch, actor="ops-human")

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert _pages(gh) == []


def test_the_authors_re_request_matches_case_insensitively(config, monkeypatch):
    w, _, al = _judged_once(config, monkeypatch, actor=AUTHOR.upper())

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED
    assert al.enqueued == []


@pytest.mark.parametrize("actor", ["ops-human", "a-teammate", ""])
def test_a_re_request_by_someone_else_still_re_reviews_the_same_head(config, monkeypatch, actor):
    """An operator, a teammate, or an event with no actor (fail open, as on
    an unreadable timeline) is an explicit ask."""
    w, _, al = _judged_once(config, monkeypatch, actor=actor)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED and d.round == 2


def test_an_operator_who_authored_the_pr_can_re_request_it(config, monkeypatch):
    operator_author = dataclasses.replace(config, operators=(AUTHOR,))
    w, _, al = _judged_once(operator_author, monkeypatch, actor=AUTHOR)

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED


def test_a_push_re_arms_whoever_re_requests(config, monkeypatch):
    """The verdict judged an older commit, so the head is NOT unchanged."""
    w, _, al = _judged_once(config, monkeypatch, actor=AUTHOR, sha="0lderc0")

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED and d.round == 2


def test_the_timeline_read_returns_the_requests_actor(monkeypatch):
    gh = GitHub()
    page = [
        {"event": "review_requested", "created_at": "2026-11-05T03:20:00Z",
         "requested_reviewer": {"login": "alissa-app"}, "actor": {"login": "ops-human"}},
        {"event": "review_requested", "created_at": "2026-11-05T03:30:00Z",
         "requested_reviewer": {"login": "alissa-app"}, "actor": {"login": AUTHOR}},
        {"event": "review_requested", "created_at": "2026-11-05T03:40:00Z",
         "requested_reviewer": {"login": "someone-else"}, "actor": {"login": "x"}},
    ]
    monkeypatch.setattr(gh, "_timeline_page", lambda o, r, n, p: page if p == 1 else [])

    newest = gh.newest_review_request(OWNER, REPO, NUMBER, "alissa-app")

    assert newest == ReviewRequest(at="2026-11-05T03:30:00Z", actor=AUTHOR)
    assert gh.review_requested_at(OWNER, REPO, NUMBER, "alissa-app") == newest.at


# -- 2. a red that is also red on the base is a [minor] note -----------------


def base_red_world(gh, name="Check Python Style"):
    """`name` red on the head AND on the base head; its workflow untouched."""
    gh.rollups[HEAD] = rollup_of([red(name), green("Lint")])
    gh.rollups[BASE] = rollup_of([red(name, f"https://ci/{name}/base")])
    gh.jobs = [job(name), job("Lint", path=NEW_WORKFLOW, event="pull_request", conclusion="success")]


def test_classify_moves_a_base_red_untouched_check_to_pre_existing(config):
    _, gh, _ = watcher(config, [])
    base_red_world(gh)

    out = ci_mod.classify(gh, gh._pr, gh.rollups[HEAD])

    assert out.rollup.state == CHECKS_GREEN, "nothing the PR caused is red"
    assert out.rollup.failing == ()
    assert [p.context.name for p in out.preexisting] == ["Check Python Style"]
    assert out.preexisting[0].evidence_url == "https://ci/Check Python Style/base"
    assert "`main` at b45e000" in out.preexisting[0].where


def test_a_base_red_check_queues_with_a_minor_note_not_a_no_approve(config):
    w, gh, al = watcher(config, [])
    base_red_world(gh)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    directive = al.enqueued[0]["directive"]
    assert "Do NOT approve this round" not in directive
    assert "PRE-EXISTING REDS" in directive
    assert "Check Python Style (failure)" in directive
    assert "https://ci/Check Python Style/base" in directive, "links where it already failed"
    assert "`[minor]`" in directive and "pre-existing failure, not caused by this PR" in directive
    assert "clean except for pre-existing reds" in directive
    assert "the only failing contexts" in directive, "never told the head was green"


def test_a_base_red_check_does_not_turn_the_daemons_approve_into_request_changes(
    config, monkeypatch
):
    monkeypatch.setattr("alissa.tools.github.revloop.loop.VERDICT_POST_GRACE_SECONDS", 0)
    w, gh, _ = watcher(config, [], verdict=VERDICT_APPROVE, verdict_count=1, readiness="auto")
    base_red_world(gh)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED
    assert gh.submitted[0]["event"] == "APPROVE"
    body = gh.submitted[0]["body"]
    assert "clean except for pre-existing CI reds" in body
    assert "`[minor]` `Check Python Style`" in body
    assert "[blocker]" not in body
    last = body.strip().splitlines()[-3:]
    trailer = [line for line in last if line.startswith("Merge-Readiness:")]
    assert trailer and trailer[0].startswith("Merge-Readiness: operator — pre-existing CI reds"), (
        "an auto envelope on a head that is not fully green holds for the operator"
    )
    assert "pre-existing CI reds" in d.reason


def test_a_pull_request_only_workflow_red_on_an_earlier_pr_is_pre_existing(config):
    """The 4R7e shape: the old workflows run on pull_request only, so the
    base head has no context of that name; PR #100's runs had failed."""
    w, gh, al = watcher(config, [])
    name = "Check Unit-Tests"
    gh.rollups[HEAD] = rollup_of([red(name)])
    gh.rollups[BASE] = rollup_of([])
    gh.jobs = [job(name, event="pull_request", workflow_id=42)]
    gh.prior = ("https://ci/runs/100", [red(name, "https://ci/runs/100/job")])

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert gh.prior_calls == [dict(
        workflow_id=42, before="2026-11-05T02:00:00Z", exclude="TASK-651234738-CI", base="main",
    )]
    directive = al.enqueued[0]["directive"]
    assert "PRE-EXISTING REDS" in directive and "runs/100/job" in directive
    assert "Do NOT approve this round" not in directive


def _pr_lane_world(w, gh):
    name = "Check Unit-Tests"
    gh.rollups[HEAD] = rollup_of([red(name)])
    gh.rollups[BASE] = rollup_of([])
    gh.jobs = [job(name, event="pull_request", workflow_id=42)]
    gh.prior = ("https://ci/runs/100", [red(name, "https://ci/runs/100/job")])


@pytest.mark.parametrize("first_commit, created, cut", [
    # a rebase moved every commit date past the PR's opening: created_at wins
    ("2026-11-05T09:00:00Z", "2026-11-05T01:00:00Z", "2026-11-05T01:00:00Z"),
    # an older author date than the PR (work begun before it opened) wins
    ("2026-11-04T20:00:00Z", "2026-11-05T01:00:00Z", "2026-11-04T20:00:00Z"),
    ("", "2026-11-05T01:00:00Z", "2026-11-05T01:00:00Z"),
    ("2026-11-05T02:00:00Z", "", "2026-11-05T02:00:00Z"),
])
def test_the_earlier_pr_cut_off_is_the_earliest_unrewritable_stamp(
    config, first_commit, created, cut
):
    """PR #215 round 1 [minor]: a rebase must not move the cut-off later."""
    w, gh, _ = watcher(config, [])
    gh._pr = dataclasses.replace(gh._pr, created_at=created)
    gh.first_commit_at = first_commit
    _pr_lane_world(w, gh)

    w.evaluate(OWNER, REPO, NUMBER)

    assert [c["before"] for c in gh.prior_calls] == [cut]


def test_no_cut_off_at_all_keeps_the_red_blocking(config):
    w, gh, al = watcher(config, [])
    gh.first_commit_at = ""
    _pr_lane_world(w, gh)

    classified = ci_mod.classify(gh, gh._pr, gh.rollups[HEAD])

    assert gh.prior_calls == [], "an unbounded listing could pick a later PR's run"
    assert classified.preexisting == ()


def test_the_first_commit_read_prefers_the_author_date(monkeypatch):
    gh = GitHub()
    monkeypatch.setattr(gh, "_api", lambda *a, **k: [{"commit": {
        "author": {"date": "2026-11-04T20:00:00Z"},
        "committer": {"date": "2026-11-05T09:00:00Z"},
    }}])

    assert gh.pull_request_first_commit_at(OWNER, REPO, NUMBER) == "2026-11-04T20:00:00Z"


# -- 3. a red the PR introduced still blocks ---------------------------------


def test_a_red_that_is_green_on_the_base_still_blocks(config):
    w, gh, al = watcher(config, [])
    gh.rollups[HEAD] = rollup_of([red("Lint")])
    gh.rollups[BASE] = rollup_of([green("Lint")])
    gh.jobs = [job("Lint", path=NEW_WORKFLOW, event="pull_request")]

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    directive = al.enqueued[0]["directive"]
    assert "Do NOT approve this round" in directive
    assert "PRE-EXISTING" not in directive
    assert gh.prior_calls == [], "the base ran it green: no earlier-PR lane"


def test_a_base_red_check_whose_workflow_the_pr_touches_still_blocks(config, monkeypatch):
    monkeypatch.setattr("alissa.tools.github.revloop.loop.VERDICT_POST_GRACE_SECONDS", 0)
    w, gh, _ = watcher(config, [], verdict=VERDICT_APPROVE, verdict_count=1, readiness="auto")
    base_red_world(gh)
    gh.files = [OLD_WORKFLOW]

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.POSTED
    assert gh.submitted[0]["event"] == "REQUEST_CHANGES"
    assert "pre-existing" not in gh.submitted[0]["body"]


def test_a_new_red_beside_a_pre_existing_one_still_blocks_and_names_both(config, monkeypatch):
    monkeypatch.setattr("alissa.tools.github.revloop.loop.VERDICT_POST_GRACE_SECONDS", 0)
    w, gh, _ = watcher(config, [], verdict=VERDICT_APPROVE, verdict_count=1, readiness="auto")
    gh.rollups[HEAD] = rollup_of([red("Check Python Style"), red("Lint")])
    gh.rollups[BASE] = rollup_of([red("Check Python Style"), green("Lint")])
    gh.jobs = [job("Check Python Style"), job("Lint", path=NEW_WORKFLOW)]

    w.evaluate(OWNER, REPO, NUMBER)

    assert gh.submitted[0]["event"] == "REQUEST_CHANGES"
    body = gh.submitted[0]["body"]
    lead, _, notes = body.partition("pre-existing CI reds")
    assert "`Lint`" in lead, "the real red leads"
    assert "`Check Python Style`" in notes, "the inherited one is a note"


def test_an_unknown_workflow_with_ci_config_in_the_diff_still_blocks(config):
    _, gh, _ = watcher(config, [])
    gh.rollups[HEAD] = rollup_of([red("external-ci")])
    gh.rollups[BASE] = rollup_of([red("external-ci")])
    gh.jobs = []

    out = ci_mod.classify(gh, gh._pr, gh.rollups[HEAD])
    assert out.rollup.state == CHECKS_RED and out.preexisting == ()

    gh.files = ["src/app.py"]
    out = ci_mod.classify(gh, gh._pr, gh.rollups[HEAD])
    assert out.rollup.state == CHECKS_GREEN and len(out.preexisting) == 1


def test_a_failed_read_fails_closed(config):
    _, gh, _ = watcher(config, [])
    base_red_world(gh)
    gh.read_error = RuntimeError("files listing 502")

    out = ci_mod.classify(gh, gh._pr, gh.rollups[HEAD])

    assert out.rollup is gh.rollups[HEAD]
    assert out.preexisting == ()


def test_a_rate_limit_propagates(config):
    _, gh, _ = watcher(config, [])
    base_red_world(gh)
    gh.read_error = RateLimited("secondary rate limit")

    with pytest.raises(RateLimited):
        ci_mod.classify(gh, gh._pr, gh.rollups[HEAD])


def test_running_checks_beside_a_pre_existing_red_hold_the_spawn(config):
    """rollup_of ranks a failure over running; once the inherited red is a
    note, what is left is still running, so the round waits for it."""
    w, gh, al = watcher(config, [])
    base_red_world(gh)
    gh.rollups[HEAD] = rollup_of([red("Check Python Style"), CheckContext("Lint", "")])

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.QUEUED and d.checks_held
    assert ci_mod.classify(gh, gh._pr, gh.rollups[HEAD]).rollup.state == CHECKS_PENDING


def test_a_green_rollup_costs_no_classification_reads(config):
    _, gh, _ = watcher(config, [])
    gh.read_error = AssertionError("must not be read")

    out = ci_mod.classify(gh, gh._pr, CheckRollup(CHECKS_GREEN))

    assert out.preexisting == ()


def test_the_prior_run_read_skips_this_prs_branch_and_other_bases(monkeypatch):
    gh = GitHub()
    calls = []
    runs = {"workflow_runs": [
        {"id": 3, "head_branch": "TASK-651234738-CI", "pull_requests": []},
        {"id": 2, "head_branch": "x", "pull_requests": [{"base": {"ref": "release"}}]},
        {"id": 1, "head_branch": "y", "pull_requests": [], "html_url": "https://ci/runs/1",
         "status": "completed", "conclusion": "failure", "name": "w"},
    ]}

    def api(*args, **kw):
        calls.append(args)
        return runs

    monkeypatch.setattr(gh, "_api", api)
    monkeypatch.setattr(
        gh, "_run_contexts", lambda o, r, run_: [CheckContext("job", "failure", f"u{run_['id']}")]
    )

    url, jobs = gh.prior_pull_request_run(
        OWNER, REPO, 42, before="2026-11-05T02:00:00Z",
        exclude_branch="TASK-651234738-CI", base_ref="main",
    )

    assert url == "https://ci/runs/1" and jobs[0].url == "u1"
    assert "created=<2026-11-05T02:00:00Z" in calls[0]
    assert "event=pull_request" in calls[0]


def test_the_pr_read_carries_its_base(monkeypatch):
    gh = GitHub()
    monkeypatch.setattr(gh, "_api", lambda *a, **k: {
        "head": {"sha": HEAD, "ref": "h"}, "base": {"ref": "main", "sha": BASE},
        "created_at": "2026-11-05T01:00:00Z",
    })

    pr = gh.pull_request(OWNER, REPO, NUMBER)

    assert (pr.base_ref, pr.base_sha) == ("main", BASE)
    assert pr.created_at == "2026-11-05T01:00:00Z"


def test_workflow_jobs_carry_the_workflow_file_and_event(monkeypatch):
    gh = GitHub()
    monkeypatch.setattr(gh, "_rollup_listing", lambda *a, **k: [
        {"id": 9, "workflow_id": 42, "path": ".github/workflows/x.yml@main", "event": "pull_request",
         "run_number": 1},
    ])
    monkeypatch.setattr(gh, "_run_contexts", lambda o, r, run_: [CheckContext("t", "failure", "u")])

    jobs = gh.workflow_jobs(OWNER, REPO, HEAD)

    assert jobs == [WorkflowJob("t", "failure", "u", 42, ".github/workflows/x.yml@main", "pull_request")]
    assert ci_mod.untouched({".github/workflows/x.yml"}, jobs[0]) is False, "the @ref is dropped"
