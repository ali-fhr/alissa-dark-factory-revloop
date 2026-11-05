"""Boot probe tests (issue #212, fleet-upgrade-safety lane UR-1).

What is pinned here, per the issue's acceptance:

* an EMPTY review-requested search is `ok`, and a 401 or 403 on it is
  `failed` with the status quoted -- through the review edge's own read
  (`ReviewWatcher._review_requests` over the real `GitHub` client), not a
  re-implementation;
* no review or comment is written: every `gh` call the probe places is a
  read, and the watcher's Alissa client is never touched;
* no fed repo is `not_applicable`, never `ok`;
* each spawn precondition failing fails R1 naming it, and the hub keeps no
  file;
* the set runs ONCE per boot, on the first completed pass, and its block
  rides on every vitals post after.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from alissa.tools.github.revloop import ghclient, probes
from alissa.tools.github.revloop.alissa_client import AlissaError
from alissa.tools.github.revloop.config import HUB_ADD, REPOS_BOWS, Config
from alissa.tools.github.revloop.fleet_vitals import FleetVitalsPusher, iso_utc
from alissa.tools.github.revloop.ghclient import GitHub, RateLimited
from alissa.tools.github.revloop.loop import ReviewWatcher
from alissa.tools.github.revloop.probes import (
    DETAIL_MAX,
    FAILED,
    HUB_PROBE_PREFIX,
    NOT_APPLICABLE,
    OK,
    PROBE_R1,
    R1Probe,
    run_r1,
)
from alissa.tools.github.revloop.proc import CommandError
from alissa.tools.github.revloop.state import State
from alissa.tools.github.revloop.version import version as running_version

REPO = "acme/widgets"
AUTH_OK = json.dumps({
    "loggedIn": True, "authMethod": "oauth_token",
    "email": "reviewer@example.com", "orgName": "Example Org",
})


def _config(tmp_path, **over):
    over.setdefault("repos", (REPO,))
    return Config(workspace_root=tmp_path, state_path=tmp_path / "state.db",
                  fleet_vitals_enabled=True, **over)


def _hub(config, full=REPO):
    owner, repo = full.split("/")
    hub = config.hub_for(owner, repo)
    hub.mkdir(parents=True)
    return hub


class _Runner:
    """The subprocess seam: `claude --version`, `claude auth status`, tmux."""

    def __init__(self, *, fail=(), auth=AUTH_OK, version="2.1.316 (Claude Code)"):
        self.fail = set(fail)
        self.auth = auth
        self.version = version
        self.argvs: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.argvs.append(list(argv))
        key = argv[0] if argv[0] != "claude" else " ".join(argv[:2])
        if key in self.fail:
            raise CommandError(argv, 127, f"could not start {argv[0]}: ENOENT")
        if argv[:3] == ["claude", "auth", "status"]:
            return self.auth
        if argv[:2] == ["claude", "--version"]:
            return self.version + "\n"
        if argv[0] == "tmux":
            return "review-widgets-pr1-r1-abc123\n"
        raise AssertionError(f"unexpected argv {argv}")


class _Gh:
    """The repo read seam (`GitHub._api`)."""

    def __init__(self, payload=None, exc=None):
        self.payload = {"permissions": {"pull": True}} if payload is None else payload
        self.exc = exc
        self.calls: list[tuple] = []

    def _api(self, *args, **kw):
        self.calls.append(args)
        if self.exc is not None:
            raise self.exc
        return self.payload


def _probe(config, *, search=lambda allowlist: [], gh=None, runner=None, environ=None):
    return R1Probe(config, search=search, github=gh or _Gh(),
                   run=runner or _Runner(), environ=environ or {})


# -- the search: empty is ok, 401/403 is failed --------------------------------


def test_an_empty_review_requested_search_is_ok(tmp_path):
    config = _config(tmp_path)
    _hub(config)
    result = _probe(config).run((REPO,))
    assert result.id == PROBE_R1
    assert result.state == OK
    assert "review-requested search answered (0 PR(s))" in result.detail
    assert f"reviewer token has pull on {REPO}" in result.detail
    assert "claude 2.1.316 (Claude Code)" in result.detail
    assert "tmux lists 1 session(s)" in result.detail


@pytest.mark.parametrize("exc,status", [
    (CommandError(["gh"], 1, "gh: Bad credentials (HTTP 401)"), 401),
    # A 403 reaches the caller as RateLimited on the read path (`_api`'s
    # default), and is still a refusal with its status quoted.
    (RateLimited("gh: Resource not accessible by integration (HTTP 403)"), 403),
    (CommandError(["gh"], 1, "gh: Must have admin rights (HTTP 403)"), 403),
])
def test_a_401_or_403_on_the_search_is_failed_with_the_status(tmp_path, exc, status):
    config = _config(tmp_path)
    _hub(config)

    def search(allowlist):
        raise exc

    result = _probe(config, search=search).run((REPO,))
    assert result.state == FAILED
    assert f"review-requested search refused (HTTP {status})" in result.detail


def _real_github(monkeypatch, *, search_stderr=None, repo=None, calls=None):
    """A real `GitHub` client over a faked `gh`: every argv is recorded."""
    calls = [] if calls is None else calls

    def run_json(argv, **kw):
        calls.append(list(argv))
        if "search/issues" in argv:
            if search_stderr:
                raise CommandError(argv, 1, search_stderr)
            return {"items": []}
        if argv[2].startswith("repos/"):
            return repo if repo is not None else {"permissions": {"pull": True}}
        raise AssertionError(f"unexpected gh call {argv}")

    monkeypatch.setattr(ghclient, "run_json", run_json)
    return GitHub("alissa-app"), calls


class _NoAlissa:
    """Every attribute is a write the probe must never reach."""

    def __init__(self):
        self.touched: list[str] = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"the probe touched alissa.{name}")


@pytest.mark.parametrize("stderr,state", [
    (None, OK),
    ("gh: Bad credentials (HTTP 401)", FAILED),
    ("gh: Forbidden (HTTP 403)", FAILED),
])
def test_the_probe_runs_the_edges_own_read_and_writes_nothing(
    tmp_path, monkeypatch, stderr, state
):
    config = _config(tmp_path)
    _hub(config)
    github, calls = _real_github(monkeypatch, search_stderr=stderr)
    alissa = _NoAlissa()
    w = ReviewWatcher(config, github=github, alissa=alissa,
                      state=State(config.state_db))
    runner = _Runner()
    block = run_r1(config, allowlist=w._search_allowlist(),
                   search=w._review_requests, github=github,
                   version="9.9.9", run=runner, environ={})
    (result,) = block["results"]
    assert result["state"] == state
    if stderr:
        assert "(HTTP 40" in result["detail"]
    # The edge's own query, then the repo read -- and nothing else.
    search, *rest = calls
    assert "search/issues" in search and "q=is:open is:pr draft:false " \
        f"review-requested:@me repo:{REPO}" in search
    assert rest in ([], [["gh", "api", f"repos/{REPO}"]])
    for argv in calls:
        assert "POST" not in argv and "PATCH" not in argv and "PUT" not in argv
        assert not any(a.endswith(("/reviews", "/comments")) for a in argv)
    assert alissa.touched == []
    # No session spawned: the subprocesses are the four reads, no queue add.
    assert [a[:2] for a in runner.argvs] == [
        ["claude", "--version"], ["claude", "auth"], ["tmux", "-S"],
    ]
    assert "new-session" not in runner.argvs[-1]


# -- no fed repo -> not_applicable, never ok -----------------------------------


def test_an_empty_feed_set_is_not_applicable(tmp_path):
    config = _config(tmp_path, repos=())
    searched: list = []
    result = _probe(config, search=lambda a: searched.append(a) or []).run(None)
    assert result.state == NOT_APPLICABLE
    assert result.detail.startswith("no fed repo to probe")
    assert searched == []  # the edge searches nothing, and so does the probe


def test_static_every_repo_with_nothing_requested_is_not_applicable(tmp_path):
    config = _config(tmp_path, repos=())
    result = _probe(config).run(())
    assert result.state == NOT_APPLICABLE


def test_static_every_repo_probes_the_first_requested_repo(tmp_path):
    config = _config(tmp_path, repos=())
    _hub(config, "other/thing")
    gh = _Gh()
    result = _probe(config, gh=gh, search=lambda a: [("other", "thing", 7)]).run(())
    assert result.state == OK
    assert gh.calls == [("repos/other/thing",)]


def test_a_failure_outranks_not_applicable(tmp_path):
    config = _config(tmp_path, repos=())
    result = _probe(config, runner=_Runner(fail={"tmux"})).run(())
    assert result.state == FAILED


# -- the pull leg ----------------------------------------------------------------


def test_a_token_without_pull_is_failed(tmp_path):
    config = _config(tmp_path)
    _hub(config)
    gh = _Gh(payload={"permissions": {"pull": False, "push": False}})
    result = _probe(config, gh=gh).run((REPO,))
    assert result.state == FAILED
    assert f"the reviewer token lacks pull on {REPO}" in result.detail


def test_a_repo_read_refused_quotes_its_status(tmp_path):
    config = _config(tmp_path)
    _hub(config)
    gh = _Gh(exc=CommandError(["gh"], 1, "gh: Not Found (HTTP 404)"))
    result = _probe(config, gh=gh).run((REPO,))
    assert result.state == FAILED
    assert f"gh read of {REPO} refused (HTTP 404)" in result.detail


def test_an_answer_without_permissions_is_a_read(tmp_path):
    config = _config(tmp_path)
    _hub(config)
    result = _probe(config, gh=_Gh(payload={"full_name": REPO})).run((REPO,))
    assert result.state == OK
    assert f"reviewer token reads {REPO}" in result.detail


def test_the_target_is_the_first_fed_repo_case_folded(tmp_path):
    config = _config(tmp_path, repos=("zeta/z", "Acme/a"))
    _hub(config, "Acme/a")
    gh = _Gh()
    _probe(config, gh=gh).run(("zeta/z", "Acme/a"))
    assert gh.calls == [("repos/Acme/a",)]


# -- D1's spawn preconditions ------------------------------------------------------


@pytest.mark.parametrize("leg,named", [
    ("claude --version", "claude --version failed"),
    ("claude auth", "claude auth status failed"),
    ("tmux", "tmux failed"),
])
def test_each_spawn_precondition_failing_names_it(tmp_path, leg, named):
    config = _config(tmp_path)
    _hub(config)
    result = _probe(config, runner=_Runner(fail={leg})).run((REPO,))
    assert result.state == FAILED
    assert named in result.detail


def test_claude_logged_out_is_failed_and_no_account_detail_leaks(tmp_path):
    config = _config(tmp_path)
    _hub(config)
    out = _probe(config, runner=_Runner(auth=json.dumps({"loggedIn": False}))).run((REPO,))
    assert out.state == FAILED
    assert "claude auth status: not logged in" in out.detail
    ok = _probe(config).run((REPO,))
    assert "claude logged in (oauth_token)" in ok.detail
    assert "example.com" not in ok.detail and "Example Org" not in ok.detail


def test_claude_legs_are_skipped_for_another_agent_profile(tmp_path):
    config = _config(tmp_path, agent_profile="codex")
    _hub(config)
    runner = _Runner(fail={"claude --version", "claude auth"})
    result = _probe(config, runner=runner).run((REPO,))
    assert result.state == OK
    assert "claude legs skipped (agent_profile=codex)" in result.detail
    assert all(a[0] != "claude" for a in runner.argvs)


def test_the_hub_keeps_no_probe_file(tmp_path):
    config = _config(tmp_path)
    hub = _hub(config)
    (hub / "main").mkdir()
    result = _probe(config).run((REPO,))
    assert result.state == OK
    assert f"hub {hub} writable" in result.detail
    assert sorted(p.name for p in hub.iterdir()) == ["main"]


def test_a_missing_hub_under_skip_is_failed(tmp_path):
    config = _config(tmp_path)
    result = _probe(config).run((REPO,))
    assert result.state == FAILED
    assert "is missing (on_missing_hub: skip)" in result.detail


def test_a_missing_hub_under_add_probes_the_workspace_root(tmp_path):
    config = _config(tmp_path, on_missing_hub=HUB_ADD)
    result = _probe(config).run((REPO,))
    assert result.state == OK
    assert f"hub {tmp_path} writable" in result.detail
    assert not any(p.name.startswith(HUB_PROBE_PREFIX) for p in tmp_path.iterdir())


def test_an_unwritable_hub_is_failed(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root writes through mode bits")
    config = _config(tmp_path)
    hub = _hub(config)
    hub.chmod(0o500)
    try:
        result = _probe(config).run((REPO,))
    finally:
        hub.chmod(0o700)
    assert result.state == FAILED
    assert f"worktree hub {hub} is not writable" in result.detail


# -- the detail: one line, capped, scrubbed --------------------------------------


def test_the_detail_is_one_capped_scrubbed_line(tmp_path):
    secret = "s3cr3t-reviewer-token-value"
    config = _config(tmp_path, reviewer_token_env="REVIEWER_GH_TOKEN")
    _hub(config)
    leak = (f"gh: token {secret} and ghp_{'A' * 36} refused\n(HTTP 401)\n"
            + "x" * 400)

    def search(allowlist):
        raise CommandError(["gh"], 1, leak)

    result = _probe(config, search=search,
                    environ={"REVIEWER_GH_TOKEN": secret}).run((REPO,))
    assert result.state == FAILED
    assert secret not in result.detail
    assert "ghp_" not in result.detail
    assert "[redacted]" in result.detail
    assert "\n" not in result.detail
    assert len(result.detail) <= DETAIL_MAX


def test_a_probe_defect_is_failed_not_raised(tmp_path, monkeypatch):
    config = _config(tmp_path)

    def boom(self, allowlist):
        raise ValueError("defect")

    monkeypatch.setattr(R1Probe, "run", boom)
    block = run_r1(config, allowlist=(REPO,), search=lambda a: [], github=_Gh(),
                   version="1.2.3", clock=lambda: 1_700_000_000.0, environ={})
    assert block == {
        "version": "1.2.3",
        "at": iso_utc(1_700_000_000),
        "results": [{"id": "R1", "state": FAILED,
                     "detail": "probe error: ValueError: defect"}],
    }


# -- once per boot, on vitals ------------------------------------------------------


class _WGh:
    login = "alissa-app"

    def __init__(self):
        self.searches = 0
        self.fail_next: "Exception | None" = None

    def review_requests(self, repos=()):
        self.searches += 1
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        return []

    def _api(self, *args, **kw):
        return {"permissions": {"pull": True}}


class _Al:
    def list_review_sessions(self):
        return []

    def worker_running(self):
        return True


class _Vitals:
    def __init__(self, refuse_probes=False):
        self.snapshots: list[dict] = []
        self.refuse_probes = refuse_probes
        self.refusals = 0

    def post_fleet_vitals(self, snapshot):
        if self.refuse_probes and "probes" in snapshot:
            self.refusals += 1
            raise AlissaError(400, "Unrecognized key(s) in object: 'probes'")
        self.snapshots.append(snapshot)
        return {"replaced": True}


class _Sources:
    def snapshots(self, limit):
        return []

    def drift(self):
        return {"running": running_version.value, "latest": None, "state": "unknown"}

    def session_rows(self):
        return []

    def rate_limit(self):
        return None

    def memory(self):
        return None

    def inbox(self):
        return {"live": [], "settled": [], "settled_dropped": 0, "truncated": False}

    def queued(self):
        return 0

    def drain(self):
        return None


def _watcher(config, gh, client):
    w = ReviewWatcher(config, github=gh, alissa=_Al(), state=State(config.state_db))
    w._fleet_vitals = FleetVitalsPusher(
        config, _Sources(), client, probes_reader=lambda: w._boot_probes,
    )
    return w


def test_the_set_runs_once_per_boot_and_rides_every_post(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _hub(config)
    runs: list = []
    real = probes.run_r1

    def counting(*a, **kw):
        runs.append(kw["allowlist"])
        return real(*a, run=_Runner(), environ={}, **kw)

    monkeypatch.setattr(probes, "run_r1", counting)
    gh, client = _WGh(), _Vitals()
    w = _watcher(config, gh, client)
    w.poll_once()
    w.poll_once()
    w.poll_once()
    assert runs == [(REPO,)]
    first, *later = client.snapshots
    assert len(later) == 2
    block = first["probes"]
    assert block["version"] == first["version"] == running_version.value
    assert block["results"][0]["id"] == "R1"
    assert block["results"][0]["state"] == OK
    assert all(s["probes"] == block for s in later)


def test_a_pass_that_does_not_complete_runs_no_probe(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _hub(config)
    runs: list = []
    monkeypatch.setattr(probes, "run_r1", lambda *a, **kw: runs.append(1) or {
        "version": "x", "at": "y", "results": []})
    gh, client = _WGh(), _Vitals()
    w = _watcher(config, gh, client)
    gh.fail_next = RateLimited("API rate limit exceeded")
    with pytest.raises(RateLimited):
        w.poll_once()
    assert runs == [] and client.snapshots == []
    w.poll_once()
    assert runs == [1]
    assert "probes" in client.snapshots[0]


def test_vitals_off_runs_no_probe(tmp_path, monkeypatch):
    config = Config(workspace_root=tmp_path, state_path=tmp_path / "state.db",
                    repos=(REPO,))
    runs: list = []
    monkeypatch.setattr(probes, "run_r1", lambda *a, **kw: runs.append(1))
    w = ReviewWatcher(config, github=_WGh(), alissa=_Al(), state=State(config.state_db))
    w.poll_once()
    assert runs == [] and w._boot_probes is None


def test_a_studio_that_refuses_the_block_gets_the_rest(tmp_path, caplog):
    config = _config(tmp_path)
    _hub(config)
    client = _Vitals(refuse_probes=True)
    pusher = FleetVitalsPusher(
        config, _Sources(), client,
        probes_reader=lambda: {"version": "v", "at": "t", "results": []},
    )
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github"):
        assert pusher.push_once(heartbeat_at=1.0) == "pushed"
    assert client.refusals == 1
    (sent,) = client.snapshots
    assert "probes" not in sent and "heartbeatAt" in sent
    assert any("`probes`" in r.getMessage() for r in caplog.records)


def test_the_bows_empty_set_reports_not_applicable_on_vitals(tmp_path, monkeypatch):
    config = _config(tmp_path, repos=(), repos_source=REPOS_BOWS,
                     bow_owners=("actor1",))
    real = probes.run_r1
    monkeypatch.setattr(probes, "run_r1",
                        lambda *a, **kw: real(*a, run=_Runner(), environ={}, **kw))
    gh, client = _WGh(), _Vitals()
    w = _watcher(config, gh, client)
    monkeypatch.setattr(w, "refresh_repos", lambda *a, **kw: None)
    w.poll_once()
    (sent,) = client.snapshots
    assert sent["probes"]["results"][0]["state"] == NOT_APPLICABLE
    assert gh.searches == 0
