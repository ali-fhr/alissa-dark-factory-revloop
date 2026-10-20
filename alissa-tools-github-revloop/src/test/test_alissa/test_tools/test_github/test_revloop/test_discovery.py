"""The discovery skip (issue #171; build-discovery design §3.3, DS-7b) and
the light review round (issue #179; design §3.6, DS-13).

A PR in a plans repository whose body carries the discovery marker on a line
of its own and whose every changed path is under `knowledge-base/` gets NO
review round -- so no reviewer session and no review task (c1). A marked PR
that touches anything else, or any discovery PR with `discovery_review` on, is
reviewed under the CODE directive like any other PR (c2). The directives
themselves are untouched; their byte pins live in test_directives.py (c3).

Issue #179 narrows the skip to a head whose `plan-lint` run's summary ends
with `Discovery-Review: none` (c1). A `light` line -- or a missing or
unreadable one, the safe reading (c3) -- gets exactly one round under
DISCOVERY_LIGHT_DIRECTIVE, and none after its fix push (c2);
`discovery_review: true` still reviews every discovery PR under the code
directive (c4).

Issue #181 (integration-check design §3.6, IC-4) adds the same skip for an
integration check's report: the plans repository, the integration-check
marker, every changed path under `integration-check/` -- no round (c1); a
marked PR touching anything else is reviewed under the code directive (c2);
`integration_check_review: true` reviews every one (c3). No light round.
"""

from __future__ import annotations

import dataclasses

import pytest

import alissa.tools.github.revloop.loop as loop_mod
from alissa.tools.github.revloop.config import (
    CONFIG_KEYS,
    DISCOVERY_REVIEW_ENV,
    INTEGRATION_CHECK_REVIEW_ENV,
    Config,
)
from alissa.tools.github.revloop.ghclient import (
    CHECKS_UNKNOWN,
    PR_FILES_CAP,
    CheckContext,
    CheckRollup,
    GitHub,
    RateLimited,
    TruncatedListing,
    rollup_of,
)
from alissa.tools.github.revloop.loop import (
    DISCOVERY_LIGHT_DIRECTIVE,
    DISCOVERY_MARKER,
    DISCOVERY_REVIEW_LIGHT,
    DISCOVERY_REVIEW_NONE,
    INTEGRATION_CHECK_MARKER,
    Action,
    ReviewWatcher,
    discovery_marked,
    discovery_review_line,
    discovery_skip_kind,
    integration_check_marked,
    integration_check_skip_kind,
    outside_discovery_prefix,
)
from alissa.tools.github.revloop.proc import CommandError
from alissa.tools.github.revloop.state import State
from test_loop import (  # the fakes and the PR scenario the loop tests share
    NUMBER,
    OWNER,
    REPO,
    SLUG,
    FakeAlissa,
    FakeGitHub,
    FakeTask,
    activity_comments,
    make_pr,
    review,
)

DISCOVERY_BODY = (
    "# Discovery: how the loop labels drift\n"
    f"{DISCOVERY_MARKER}\n"
    "Alissa-Task: TASK-42\n"
)
KB_FILES = [
    "knowledge-base/findings/2026/10/13/loop-labels.md",
    "knowledge-base/questions/loop-labels.md",
]

# The knowledge-base report the plans template's linter publishes as the
# `plan-lint` check run's summary (lint/plan-lint.ts `reportKb`): its last
# line is the machine-read review line.
REPORT_HEAD = (
    "plan-lint: knowledge-base/2026/10/13/TASK-42 — knowledge-base mode — "
    "2 question(s) — K1–K8\n\nAll rules passed.\n\n"
)
NONE_LINE = "Discovery-Review: none"
LIGHT_LINE = "Discovery-Review: light — external cites: 2; high impact: q1"


def lint_run(summary=REPORT_HEAD + NONE_LINE, conclusion="success", name="plan-lint"):
    return CheckContext(name, conclusion, f"https://ci/{name}", summary=summary)


def lint_rollup(*runs):
    return rollup_of(list(runs) or [lint_run()])


class FilesGitHub(FakeGitHub):
    """FakeGitHub plus the PR files listing the discovery skip reads."""

    def __init__(self, pr, reviews):
        super().__init__(pr, reviews)
        self.files: list[str] = list(KB_FILES)
        self.files_error: BaseException | None = None
        self.files_reads = 0
        # A head whose lint reads `none`: the v0.1 skip (issue #179 c1).
        self.default_rollup = lint_rollup()

    def pull_request_files(self, owner, repo, number):
        self.files_reads += 1
        if self.files_error:
            raise self.files_error
        return list(self.files)

    def move_head(self, sha):
        self._pr = dataclasses.replace(self._pr, head_sha=sha)


@pytest.fixture
def config(tmp_path):
    hub = tmp_path / REPO / "main"
    hub.mkdir(parents=True)
    return Config(
        workspace_root=tmp_path,
        hub_template="{root}/{repo}/main",
        state_path=tmp_path / "state.db",
        round_cap=3,
        plan_repos=(SLUG,),
    )


def discovery_watcher(config, pr=None, reviews=(), *, task=None, verdict=None):
    """A watcher on a marked PR in the plans repository. No review task by
    default: a discovery PR never had a round, so none was ever created."""
    pr = pr or make_pr(body=DISCOVERY_BODY)
    reviews = list(reviews)
    gh = FilesGitHub(pr, reviews)
    al = FakeAlissa(
        task, verdict=verdict, verdict_count=sum(1 for r in reviews if r.is_substantive)
    )
    w = ReviewWatcher(config, github=gh, alissa=al, state=State(config.state_db))
    return w, gh, al


def assert_code_directive(al):
    assert len(al.enqueued) == 1
    directive = al.enqueued[0]["directive"]
    assert "NEVER push commits" in directive
    assert "plan-directive.md" not in directive  # not the plan directive


# -- c1: the skip -------------------------------------------------------------


def test_a_confined_marked_plan_repo_pr_gets_no_round_and_no_review_task(config):
    w, gh, al = discovery_watcher(config)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED
    assert "discovery" in d.reason and "knowledge-base/" in d.reason
    assert al.enqueued == []          # no reviewer session ...
    assert al.task is None            # ... so no review task either
    assert gh.submitted == []
    assert w.state.get_spawn(SLUG, NUMBER, 1) is None  # no round number burned


def test_the_skip_is_logged_once_per_head_in_the_activity_comment(config):
    w, gh, al = discovery_watcher(config)

    for _ in range(3):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED

    [comment] = activity_comments(gh)
    lines = [ln for ln in comment.body.split("\n") if "skipped at" in ln]
    assert len(lines) == 1
    assert "`abc123`" in lines[0] and "discovery_review is off" in lines[0]
    assert gh.files_reads == 1  # one paged read per head, not per poll
    assert w.state.pinged(SLUG, NUMBER, discovery_skip_kind("abc123"))

    gh.move_head("def456")
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED
    [comment] = activity_comments(gh)
    assert comment.body.count("skipped at") == 2
    assert gh.files_reads == 2


def test_a_dry_run_skips_but_neither_comments_nor_records(config):
    w, gh, al = discovery_watcher(dataclasses.replace(config, dry_run=True))

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED
    assert activity_comments(gh) == []
    assert not w.state.pinged(SLUG, NUMBER, discovery_skip_kind("abc123"))
    assert al.enqueued == []


def test_a_failed_activity_append_retries_next_poll(config):
    w, gh, al = discovery_watcher(config)
    gh.comment = lambda *a, **k: (_ for _ in ()).throw(CommandError(["gh"], 1, "boom"))

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED
    assert not w.state.pinged(SLUG, NUMBER, discovery_skip_kind("abc123"))

    del gh.comment  # back to the class's working method
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED
    assert len(activity_comments(gh)) == 1


# -- c2: reviewed under the code directive ------------------------------------


@pytest.mark.parametrize(
    "files",
    [
        KB_FILES + ["README.md"],
        ["knowledge-base/x.md", "lint/rules.ts"],
        ["knowledge-base-old/x.md"],          # a sibling prefix is not the prefix
        ["plans/2026-10-13-x/plan.md"],
        [],                                   # an empty diff is not "confined"
    ],
)
def test_a_marked_pr_touching_anything_else_is_reviewed_under_the_code_directive(
    config, files
):
    w, gh, al = discovery_watcher(config)
    gh.files = files

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED and d.round == 1
    assert_code_directive(al)
    assert all("skipped at" not in c.body for c in activity_comments(gh))


def test_discovery_review_on_reviews_a_confined_discovery_pr_like_any_other(config):
    w, gh, al = discovery_watcher(dataclasses.replace(config, discovery_review=True))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert_code_directive(al)
    assert gh.files_reads == 0  # the flag short-circuits the files read


@pytest.mark.parametrize(
    "error",
    [
        CommandError(["gh", "api"], 1, "HTTP 502"),
        TruncatedListing("acme/widgets#7 reached GitHub's 3000-file listing cap"),
    ],
)
def test_unreadable_files_are_the_safe_reading_a_code_review(config, error):
    w, gh, al = discovery_watcher(config)
    gh.files_error = error

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert_code_directive(al)


def test_throttling_while_reading_files_backs_the_poll_off(config):
    w, gh, al = discovery_watcher(config)
    gh.files_error = RateLimited("API rate limit exceeded")

    with pytest.raises(RateLimited):
        w.evaluate(OWNER, REPO, NUMBER)
    assert al.enqueued == []


@pytest.mark.parametrize(
    "config_kw, body",
    [
        ({"plan_repos": ("acme/alissa-dark-factory-plans",)}, DISCOVERY_BODY),  # not a plans repo
        ({}, "# Discovery\nno marker here\n"),
        ({}, f"see {DISCOVERY_MARKER} inline\n"),       # not on a line of its own
        ({}, f"`{DISCOVERY_MARKER}`\n"),                # quoted
        ({}, "<!-- alissa-discovery v2 -->\n"),         # another version
    ],
)
def test_an_unmarked_or_out_of_repo_pr_is_reviewed_without_a_files_read(
    config, config_kw, body
):
    w, gh, al = discovery_watcher(
        dataclasses.replace(config, **config_kw), make_pr(body=body)
    )

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert_code_directive(al)
    assert gh.files_reads == 0


def test_the_marker_survives_crlf_and_surrounding_whitespace(config):
    pr = make_pr(body=f"# Discovery\r\n   {DISCOVERY_MARKER}  \r\n")
    assert discovery_marked(config, pr)


def test_a_round_already_running_still_converges(config):
    """The skip gates STARTING a round, below convergence: a discovery PR
    reviewed while the flag was on still reaches its verdict of record."""
    w, gh, al = discovery_watcher(
        config, reviews=[review("COMMENTED")], task=FakeTask(), verdict="approve"
    )

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.CONVERGED
    assert gh.files_reads == 0


def test_the_authors_filter_still_wins_over_the_skip(config):
    w, gh, al = discovery_watcher(dataclasses.replace(config, authors=("someone-else",)))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED and "authors allowlist" in d.reason
    assert gh.files_reads == 0


def test_outside_discovery_prefix():
    assert outside_discovery_prefix(KB_FILES) == []
    assert outside_discovery_prefix(KB_FILES + ["a.md"]) == ["a.md"]
    assert outside_discovery_prefix([]) == ["(no changed files)"]


# -- the files listing (ghclient) ---------------------------------------------


def _files_github(pages):
    gh = GitHub(login="alissa-app")
    calls = []

    def api(*args, **kwargs):
        calls.append(args)
        page = int([a for a in args if a.startswith("page=")][0].split("=")[1])
        return pages[page - 1] if page <= len(pages) else []

    gh._api = api
    return gh, calls


def test_the_files_listing_pages_to_a_short_page():
    first = [{"filename": f"knowledge-base/f{i}.md"} for i in range(100)]
    gh, calls = _files_github([first, [{"filename": "README.md"}]])

    files = gh.pull_request_files(OWNER, REPO, NUMBER)

    assert len(files) == 101 and files[-1] == "README.md"
    assert len(calls) == 2
    assert f"repos/{OWNER}/{REPO}/pulls/{NUMBER}/files" in calls[0]


def test_a_rename_contributes_both_names():
    gh, _ = _files_github([[
        {"filename": "knowledge-base/moved.md", "previous_filename": "src/moved.md"},
    ]])

    files = gh.pull_request_files(OWNER, REPO, NUMBER)

    assert files == ["knowledge-base/moved.md", "src/moved.md"]
    assert outside_discovery_prefix(files) == ["src/moved.md"]


def test_a_listing_at_githubs_file_cap_is_refused():
    page = [{"filename": "knowledge-base/x.md"}] * 100
    gh, calls = _files_github([page] * (PR_FILES_CAP // 100))

    with pytest.raises(TruncatedListing):
        gh.pull_request_files(OWNER, REPO, NUMBER)
    assert len(calls) == PR_FILES_CAP // 100


# -- the fleet flag -----------------------------------------------------------


def test_discovery_review_is_off_by_default_and_a_known_key(tmp_path):
    assert "discovery_review" in CONFIG_KEYS
    assert Config.build(tmp_path, {}, environ={}).discovery_review is False
    assert Config.build(tmp_path, {"discovery_review": True}, environ={}).discovery_review


@pytest.mark.parametrize(
    "file_value, env_value, expect",
    [
        (False, "on", True),
        (True, "0", False),
        (True, " ", True),       # blank falls through to the file
        (False, "", False),
    ],
)
def test_the_env_rail_wins_over_the_file(tmp_path, file_value, env_value, expect):
    cfg = Config.build(
        tmp_path, {"discovery_review": file_value},
        environ={DISCOVERY_REVIEW_ENV: env_value},
    )
    assert cfg.discovery_review is expect


def test_a_malformed_value_is_refused(tmp_path):
    with pytest.raises(ValueError, match=DISCOVERY_REVIEW_ENV):
        Config.build(tmp_path, {}, environ={DISCOVERY_REVIEW_ENV: "maybe"})
    with pytest.raises(ValueError, match="discovery_review must be true or false"):
        Config.build(tmp_path, {"discovery_review": "false"}, environ={})


# -- issue #179: the light review round ---------------------------------------


def assert_light_directive(al):
    assert len(al.enqueued) == 1
    directive = al.enqueued[0]["directive"]
    assert directive.startswith("You are a DISCOVERY REVIEWER")
    assert "`alissa-code-review:references/discovery-light.md`" in directive
    assert "EFFECTIVE round cap is 1" in directive
    assert "plan-directive.md" not in directive
    return directive


def test_c1_a_none_line_starts_no_round_and_says_why(config):
    w, gh, al = discovery_watcher(config)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED
    assert "`Discovery-Review: none`" in d.reason
    assert al.enqueued == [] and al.task is None
    [comment] = activity_comments(gh)
    assert "reads `Discovery-Review: none`" in comment.body


def test_c1_a_none_line_is_read_once_per_head(config):
    w, gh, al = discovery_watcher(config)

    for _ in range(3):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED

    assert gh.rollup_reads == ["abc123"]  # a settled line is cached per head


def test_c2_a_light_line_gets_round_1_under_the_light_directive(config):
    w, gh, al = discovery_watcher(config)
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED and d.round == 1
    directive = assert_light_directive(al)
    assert "Merge-Readiness: auto" in directive
    assert "THIS IS THE ONLY ROUND" in directive
    [comment] = activity_comments(gh)
    assert "round 1 of 1" in comment.body       # the light cap, not round_cap
    assert "skipped at" not in comment.body


@pytest.mark.parametrize("head", ["abc123", "def456"])  # before and after the fix push
def test_c2_no_second_round_once_the_light_round_has_a_verdict(config, head):
    w, gh, al = discovery_watcher(
        config,
        reviews=[review("CHANGES_REQUESTED", sha="abc123")],
        task=FakeTask(),
        verdict="request_changes",
    )
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE))
    gh.move_head(head)

    for _ in range(2):
        d = w.evaluate(OWNER, REPO, NUMBER)
        assert d.action is Action.SKIPPED
        assert "no second round" in d.reason and "cap is 1" in d.reason

    assert al.enqueued == []
    assert gh.rollup_reads == []            # the spent cap needs no line read
    assert w.state.get_spawn(SLUG, NUMBER, 2) is None
    [comment] = activity_comments(gh)
    assert comment.body.count("skipped at") == 1
    assert gh.issue_store == [comment]      # no cap-out escalation page


def test_c2_an_in_flight_light_round_is_not_respawned_or_re_read(config):
    w, gh, al = discovery_watcher(config)
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE))
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    reads = len(gh.rollup_reads)

    gh.move_head("def456")  # the worker pushed while the round was running
    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.IN_FLIGHT and d.round == 1
    assert len(al.enqueued) == 1
    assert len(gh.rollup_reads) == reads    # round 1 started: nothing re-read


@pytest.mark.parametrize(
    "rollup",
    [
        CheckRollup(CHECKS_UNKNOWN, unreadable="HTTP 502"),           # unreadable
        rollup_of([]),                                                # no plan-lint run
        rollup_of([CheckContext("test", "success")]),                 # ... other checks only
        lint_rollup(lint_run("")),                                    # a run with no summary
        lint_rollup(lint_run(REPORT_HEAD + "All rules passed.")),     # no line at all
        lint_rollup(lint_run(NONE_LINE + "\ntrailing text")),        # not the LAST line
        lint_rollup(lint_run(REPORT_HEAD + "Discovery-Review: maybe")),
        lint_rollup(lint_run(REPORT_HEAD + "`Discovery-Review: none`")),
        lint_rollup(lint_run(REPORT_HEAD + "Discovery-Review: none — but")),
        lint_rollup(lint_run(), lint_run(REPORT_HEAD + LIGHT_LINE)),  # one light run of two
    ],
)
def test_c3_a_missing_or_unreadable_line_is_light(config, rollup):
    w, gh, al = discovery_watcher(config)
    gh.default_rollup = rollup

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED and d.round == 1
    assert_light_directive(al)


def test_c3_a_line_still_being_linted_holds_then_reads_as_light(config, monkeypatch):
    cfg = dataclasses.replace(config, checks_spawn_wait_seconds=600)
    w, gh, al = discovery_watcher(cfg)
    gh.default_rollup = rollup_of([CheckContext("plan-lint (trusted, the base branch's code)")])

    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.QUEUED and d.checks_held and d.round == 1
    assert "light round waits for plan-lint" in d.reason
    assert al.enqueued == []

    now = loop_mod.time.time()
    monkeypatch.setattr(loop_mod.time, "time", lambda: now + 601)
    d = w.evaluate(OWNER, REPO, NUMBER)
    assert d.action is Action.SPAWNED
    assert_light_directive(al)


def test_c3_a_running_plan_lint_run_holds_and_its_answer_then_decides(config):
    cfg = dataclasses.replace(config, checks_spawn_wait_seconds=600)
    w, gh, al = discovery_watcher(cfg)
    gh.default_rollup = lint_rollup(lint_run("", conclusion=""))

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.QUEUED

    gh.default_rollup = lint_rollup()  # it reported `none`
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED
    assert al.enqueued == []


def test_c3_with_the_spawn_wait_off_a_pending_line_reads_light_at_once(config):
    w, gh, al = discovery_watcher(dataclasses.replace(config, checks_spawn_wait_seconds=0))
    gh.default_rollup = lint_rollup(lint_run("", conclusion=""))

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert_light_directive(al)


def test_a_light_head_the_lint_refused_holds_the_one_round(config):
    w, gh, al = discovery_watcher(config)
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE, conclusion="failure"))

    for _ in range(2):
        d = w.evaluate(OWNER, REPO, NUMBER)
        assert d.action is Action.QUEUED and d.checks_held
        assert "concluded failure" in d.reason
    assert al.enqueued == []

    gh.move_head("def456")  # devloop's fix edge pushed a green head
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE))
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert_light_directive(al)


def test_a_none_head_the_lint_refused_is_still_skipped(config):
    w, gh, al = discovery_watcher(config)
    gh.default_rollup = lint_rollup(lint_run(conclusion="failure"))

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED
    assert al.enqueued == []


def test_c4_discovery_review_on_reviews_a_light_pr_under_the_code_directive(config):
    w, gh, al = discovery_watcher(dataclasses.replace(config, discovery_review=True))
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert_code_directive(al)
    assert "DISCOVERY REVIEWER" not in al.enqueued[0]["directive"]


def test_an_unconfined_marked_pr_with_a_light_line_is_still_a_code_review(config):
    w, gh, al = discovery_watcher(config)
    gh.files = KB_FILES + ["README.md"]
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE))

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert_code_directive(al)
    assert "DISCOVERY REVIEWER" not in al.enqueued[0]["directive"]


def test_a_code_pr_is_not_given_the_light_directive_by_a_light_lint_line(config):
    w, gh, al = discovery_watcher(config, make_pr(body="# Fix\n"))
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE))

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert_code_directive(al)


# -- the review line reader ----------------------------------------------------


@pytest.mark.parametrize(
    "summary, level, why",
    [
        (REPORT_HEAD + NONE_LINE, DISCOVERY_REVIEW_NONE, NONE_LINE),
        (REPORT_HEAD + NONE_LINE + "\n\n  \n", DISCOVERY_REVIEW_NONE, NONE_LINE),
        ((REPORT_HEAD + NONE_LINE).replace("\n", "\r\n"), DISCOVERY_REVIEW_NONE, NONE_LINE),
        (REPORT_HEAD + LIGHT_LINE, DISCOVERY_REVIEW_LIGHT, "external cites: 2; high impact: q1"),
        (REPORT_HEAD + "Discovery-Review: light", DISCOVERY_REVIEW_LIGHT, "no reasons given"),
        ("", DISCOVERY_REVIEW_LIGHT, "no readable `Discovery-Review:` line"),
    ],
)
def test_discovery_review_line(summary, level, why):
    line = discovery_review_line(lint_rollup(lint_run(summary)))
    assert line.level == level and why in line.why
    assert line.settled and not line.lint_failed


def test_the_reader_ignores_runs_with_other_names():
    rollup = rollup_of([
        lint_run(REPORT_HEAD + LIGHT_LINE, name="plan-lint (offline, the PR's code)"),
        lint_run(),
    ])
    assert discovery_review_line(rollup).level == DISCOVERY_REVIEW_NONE


def test_only_a_completed_run_settles_the_line():
    assert not discovery_review_line(lint_rollup(lint_run("", conclusion=""))).level
    assert not discovery_review_line(CheckRollup(CHECKS_UNKNOWN)).settled
    assert not discovery_review_line(rollup_of([])).settled


# -- the check run's output summary (ghclient) ---------------------------------


def test_check_runs_carry_their_output_summary():
    gh = GitHub(login="alissa-app")
    runs = [
        {"name": "plan-lint", "status": "completed", "conclusion": "success",
         "output": {"title": "knowledge-base", "summary": REPORT_HEAD + NONE_LINE}},
        {"name": "test", "status": "completed", "conclusion": "success", "output": None},
        {"name": "odd", "status": "completed", "conclusion": "success", "output": {"summary": 7}},
        {"name": "bare", "status": "in_progress"},
    ]
    gh._rollup_listing = lambda path, key, what, params=(): runs

    contexts = gh._check_runs(OWNER, REPO, "abc123")

    assert [c.summary for c in contexts] == [REPORT_HEAD + NONE_LINE, "", "", ""]
    assert discovery_review_line(rollup_of(contexts)).level == DISCOVERY_REVIEW_NONE


def test_the_light_directive_is_the_one_rendered(config):
    """The template rendered is DISCOVERY_LIGHT_DIRECTIVE itself, slot for slot."""
    w, gh, al = discovery_watcher(config)
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE))
    w.evaluate(OWNER, REPO, NUMBER)
    head = DISCOVERY_LIGHT_DIRECTIVE.split("{assignment}")[0]
    assert al.enqueued[0]["directive"].startswith(head)


# -- issue #181: the integration-check skip -----------------------------------

IC_BODY = (
    "# Integration check: shop-m1, run 1\n"
    f"{INTEGRATION_CHECK_MARKER}\n"
    "Alissa-Task: TASK-77\n"
)
IC_FILES = [
    "integration-check/2026/10/19/shop-m1/report.md",
    "integration-check/2026/10/19/shop-m1/evidence/build.log",
    "integration-check/2026/10/19/shop-m1/evidence/c1-checkout-1440.png",
]


def ic_watcher(config, pr=None, **kw):
    w, gh, al = discovery_watcher(config, pr or make_pr(body=IC_BODY), **kw)
    gh.files = list(IC_FILES)
    return w, gh, al


def test_ic_c1_a_confined_marked_pr_gets_no_round_and_no_review_task(config):
    w, gh, al = ic_watcher(config)

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SKIPPED
    assert "integration-check PR" in d.reason and "integration-check/" in d.reason
    assert "integration_check_review is off" in d.reason
    assert al.enqueued == [] and al.task is None and gh.submitted == []
    assert w.state.get_spawn(SLUG, NUMBER, 1) is None


def test_ic_c1_the_skip_is_logged_once_per_head(config):
    w, gh, al = ic_watcher(config)

    for _ in range(3):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED

    [comment] = activity_comments(gh)
    assert comment.body.count("skipped at `abc123`") == 1
    assert gh.files_reads == 1
    assert w.state.pinged(SLUG, NUMBER, integration_check_skip_kind("abc123"))
    assert not w.state.pinged(SLUG, NUMBER, discovery_skip_kind("abc123"))

    gh.move_head("def456")
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED
    [comment] = activity_comments(gh)
    assert comment.body.count("skipped at") == 2


def test_ic_c1_no_light_round_a_recorded_round_still_skips(config):
    """Unlike discovery there is no light round and no lint read: the skip
    holds whatever `completed` says and never reads the check rollup."""
    w, gh, al = ic_watcher(config, reviews=[review("COMMENTED")])
    gh.default_rollup = lint_rollup(lint_run(REPORT_HEAD + LIGHT_LINE))
    gh.check_rollup = lambda *a: pytest.fail("the IC skip reads no rollup")

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED
    assert al.enqueued == []


@pytest.mark.parametrize(
    "files",
    [
        IC_FILES + ["README.md"],
        IC_FILES + ["knowledge-base/2026/10/19/TASK-1/findings.md"],
        ["integration-check/x/report.md", "plans/2026-10-19-x/plan.md"],
        ["integration-checks/x/report.md"],   # a sibling prefix is not the prefix
        [],                                   # an empty diff is not "confined"
    ],
)
def test_ic_c2_a_marked_pr_touching_anything_else_gets_the_code_directive(config, files):
    w, gh, al = ic_watcher(config)
    gh.files = files

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED and d.round == 1
    assert_code_directive(al)
    assert all("skipped at" not in c.body for c in activity_comments(gh))


@pytest.mark.parametrize(
    "error",
    [
        CommandError(["gh", "api"], 1, "HTTP 502"),
        TruncatedListing("acme/widgets#7 reached GitHub's 3000-file listing cap"),
    ],
)
def test_ic_c2_unreadable_files_are_a_code_review(config, error):
    w, gh, al = ic_watcher(config)
    gh.files_error = error

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert_code_directive(al)


def test_ic_c2_throttling_while_reading_files_backs_the_poll_off(config):
    w, gh, al = ic_watcher(config)
    gh.files_error = RateLimited("API rate limit exceeded")

    with pytest.raises(RateLimited):
        w.evaluate(OWNER, REPO, NUMBER)
    assert al.enqueued == []


@pytest.mark.parametrize(
    "config_kw, body",
    [
        ({"plan_repos": ("acme/alissa-dark-factory-plans",)}, IC_BODY),  # not a plans repo
        ({}, "# Integration check\nno marker here\n"),
        ({}, f"see {INTEGRATION_CHECK_MARKER} inline\n"),
        ({}, "<!-- alissa-integration-check v2 -->\n"),
        ({}, DISCOVERY_BODY),                  # the discovery marker is not this one
    ],
)
def test_ic_c2_an_unmarked_or_out_of_repo_pr_is_reviewed(config, config_kw, body):
    w, gh, al = ic_watcher(dataclasses.replace(config, **config_kw), make_pr(body=body))

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SPAWNED
    assert_code_directive(al)


def test_ic_the_marker_survives_crlf_and_surrounding_whitespace(config):
    pr = make_pr(body=f"# IC\r\n   {INTEGRATION_CHECK_MARKER}  \r\n")
    assert integration_check_marked(config, pr)


def test_ic_c3_the_flag_reviews_every_one_under_the_code_directive(config):
    w, gh, al = ic_watcher(dataclasses.replace(config, integration_check_review=True))

    d = w.evaluate(OWNER, REPO, NUMBER)

    assert d.action is Action.SPAWNED
    assert_code_directive(al)
    assert gh.files_reads == 0  # the flag short-circuits the files read


def test_ic_c3_the_flags_are_independent(config):
    w, gh, al = ic_watcher(dataclasses.replace(config, discovery_review=True))
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED

    w, gh, al = discovery_watcher(dataclasses.replace(config, integration_check_review=True))
    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.SKIPPED


def test_ic_a_round_already_running_still_converges(config):
    w, gh, al = ic_watcher(
        config, reviews=[review("COMMENTED")], task=FakeTask(), verdict="approve"
    )

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.CONVERGED
    assert gh.files_reads == 0


def test_integration_check_review_is_off_by_default_and_a_known_key(tmp_path):
    assert "integration_check_review" in CONFIG_KEYS
    assert Config.build(tmp_path, {}, environ={}).integration_check_review is False
    assert Config.build(
        tmp_path, {"integration_check_review": True}, environ={}
    ).integration_check_review


@pytest.mark.parametrize(
    "file_value, env_value, expect",
    [(False, "on", True), (True, "0", False), (True, " ", True), (False, "", False)],
)
def test_the_ic_env_rail_wins_over_the_file(tmp_path, file_value, env_value, expect):
    cfg = Config.build(
        tmp_path, {"integration_check_review": file_value},
        environ={INTEGRATION_CHECK_REVIEW_ENV: env_value},
    )
    assert cfg.integration_check_review is expect


def test_a_malformed_ic_value_is_refused(tmp_path):
    with pytest.raises(ValueError, match=INTEGRATION_CHECK_REVIEW_ENV):
        Config.build(tmp_path, {}, environ={INTEGRATION_CHECK_REVIEW_ENV: "maybe"})
    with pytest.raises(ValueError, match="integration_check_review must be true or false"):
        Config.build(tmp_path, {"integration_check_review": "false"}, environ={})
