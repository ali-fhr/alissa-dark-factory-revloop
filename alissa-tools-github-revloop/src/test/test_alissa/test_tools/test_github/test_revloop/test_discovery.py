"""The discovery skip (issue #171; build-discovery design §3.3, DS-7b).

A PR in a plans repository whose body carries the discovery marker on a line
of its own and whose every changed path is under `knowledge-base/` gets NO
review round -- so no reviewer session and no review task (c1). A marked PR
that touches anything else, or any discovery PR with `discovery_review` on, is
reviewed under the CODE directive like any other PR (c2). The directives
themselves are untouched; their byte pins live in test_directives.py (c3).
"""

from __future__ import annotations

import dataclasses

import pytest

from alissa.tools.github.revloop.config import (
    CONFIG_KEYS,
    DISCOVERY_REVIEW_ENV,
    Config,
)
from alissa.tools.github.revloop.ghclient import (
    PR_FILES_CAP,
    GitHub,
    RateLimited,
    TruncatedListing,
)
from alissa.tools.github.revloop.loop import (
    DISCOVERY_MARKER,
    Action,
    ReviewWatcher,
    discovery_marked,
    discovery_skip_kind,
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


class FilesGitHub(FakeGitHub):
    """FakeGitHub plus the PR files listing the discovery skip reads."""

    def __init__(self, pr, reviews):
        super().__init__(pr, reviews)
        self.files: list[str] = list(KB_FILES)
        self.files_error: BaseException | None = None
        self.files_reads = 0

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
