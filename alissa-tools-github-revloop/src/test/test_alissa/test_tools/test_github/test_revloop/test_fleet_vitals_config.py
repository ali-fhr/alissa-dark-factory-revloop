"""Fleet vitals' `config` block (issue #159, studio.alissa.app provisioner
design §2.7, lane L9): the reviewer seat's own word on what it derived --
feed owner, effective allowlist, reviewers requested (none), own GitHub
login -- with no secret in it, built off the config the watcher RUNS on,
left out by the console's push, and dropped (then re-offered) by a Studio
whose strict schema predates it."""

from __future__ import annotations

import json
import logging

from alissa.tools.github.revloop import fleet_vitals as mod
from alissa.tools.github.revloop.alissa_client import AlissaError
from alissa.tools.github.revloop.config import REPOS_BOWS, Config
from alissa.tools.github.revloop.fleet_vitals import (
    BLOCK_REPROBE_S,
    CONFIG_KEYS,
    IDLE_REPROBE_S,
    SECRET_ENV,
    VITALS_FAILED,
    VITALS_PUSHED,
    VITALS_SKIPPED,
    FleetVitalsPusher,
    build_snapshot,
    config_block,
    fit_body,
)
from alissa.tools.github.revloop.loop import ReviewWatcher
from alissa.tools.github.revloop.state import State

from test_fleet_vitals import FakeSources, FakeVitalsClient, WALL

OWNER = "j5706fv7xe5jy1k5wdwzacab9s8axcd2"
OTHER = "k17bakct137qzb8k334cg18f158ex6r1"
LOGIN = "dark-rev-acme"
SLUG = "acme/widgets"
REVIEWER_TOKEN_ENV = "REVLOOP_REVIEWER_GH_TOKEN"

# One distinctive value per credential the container holds. The scan below
# asserts none of them -- nor any 12-character run of one -- reaches the
# wire, whichever field an operator mistakenly put it in.
SECRETS = {
    "GH_TOKEN": "ghp_SENTINELghTOKEN0123456789abcdefABCD",
    "GITHUB_TOKEN": "github_pat_SENTINEL_gh_fallback_0123456789",
    "ALISSA_API_TOKEN": "alissa_pat_SENTINEL_api_token_0123456789",
    "ALISSA_UI_PASSCODE": "SENTINEL-console-passcode-0123456789",
    "ANTHROPIC_API_KEY": "sk-ant-api03-SENTINEL-anthropic-0123456789",
    "ANTHROPIC_AUTH_TOKEN": "SENTINEL-anthropic-auth-token-0123456789",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-SENTINEL-oauth-0123456789",
}
# The reviewer identity's token, under the name `reviewer_token_env` gives it.
REVIEWER_SECRET = "ghp_SENTINELreviewerTOKEN0123456789abcd"
ALL_SECRETS = {**SECRETS, REVIEWER_TOKEN_ENV: REVIEWER_SECRET}


def bows_config(tmp_path, **kw):
    """A managed reviewer seat's config after boot: bows source, feed
    authority resolved to self, the reviewer token under its own name."""
    fields = dict(
        workspace_root=tmp_path, state_path=tmp_path / "state.db",
        repos_source=REPOS_BOWS, bow_owners=(OWNER,),
        repos=("acme/Widgets", "acme/api"),
        reviewer_token_env=REVIEWER_TOKEN_ENV,
    )
    fields.update(kw)
    return Config(**fields)


def scan(body, secrets=ALL_SECRETS):
    """Every secret, and every 12-character slice of one, absent from the
    serialised body: a truncated or embedded token is still a leak."""
    wire = json.dumps(body)
    for name, secret in secrets.items():
        for i in range(len(secret) - 11):
            assert secret[i:i + 12] not in wire, f"{name} leaked into the body"


# -- c1: the block ------------------------------------------------------------

def test_a_managed_seat_reports_what_it_derived(tmp_path):
    block = config_block(bows_config(tmp_path), gh_login=LOGIN, environ={})
    assert tuple(block) == CONFIG_KEYS
    assert block == {
        "feedOwnerActorId": OWNER,
        "repos": ["acme/api", "acme/Widgets"],
        "reviewersRequested": [],
        "ghLogin": LOGIN,
    }


def test_static_mode_has_no_feed_owner(tmp_path):
    """No body of work is read in static mode, so a configured `bow_owners`
    is not an authority the seat exercises: null, not the inert value."""
    config = bows_config(tmp_path, repos_source="static", repos=(SLUG,))
    block = config_block(config, gh_login=LOGIN, environ={})
    assert block["feedOwnerActorId"] is None
    assert block["repos"] == [SLUG]


def test_several_trusted_owners_report_no_single_owner(tmp_path):
    config = bows_config(tmp_path, bow_owners=(OWNER, OTHER))
    assert config_block(config, gh_login=LOGIN, environ={})["feedOwnerActorId"] is None


def test_unknowns_are_nulls_and_empty_lists_not_missing_keys(tmp_path):
    config = Config(workspace_root=tmp_path)
    block = config_block(config, gh_login=None, environ={})
    assert block == {"feedOwnerActorId": None, "repos": [],
                     "reviewersRequested": [], "ghLogin": None}
    assert config_block(config, gh_login="", environ={})["ghLogin"] is None


# -- c2: no token or passcode in the block --------------------------------------

def test_no_secret_reaches_the_block_even_from_a_misconfigured_field(
        tmp_path, caplog):
    """Every credential the container holds, planted in every field the
    block reads (a token in the allowlist, the login, the owner): each
    carrying value is blanked, the clean ones survive, and the WARNING names
    the field, never the value."""
    config = bows_config(
        tmp_path,
        bow_owners=(SECRETS["ALISSA_API_TOKEN"],),
        repos=(SLUG, f"acme/{SECRETS['GH_TOKEN']}",
               f"x{SECRETS['CLAUDE_CODE_OAUTH_TOKEN']}x",
               f"acme/{REVIEWER_SECRET}"),
    )
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        block = config_block(config, gh_login=SECRETS["ALISSA_UI_PASSCODE"],
                             environ=ALL_SECRETS)
    assert block == {"feedOwnerActorId": None, "repos": [SLUG],
                     "reviewersRequested": [], "ghLogin": None}
    scan(block)
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 3
    assert all("credential" in w for w in warned)
    scan({"log": warned})


def test_the_reviewer_token_is_scanned_under_the_name_the_config_gives_it(
        tmp_path):
    """revloop's reviewer credential may live in a variable of any name
    (`reviewer_token_env`); the scan follows the name, not a fixed list."""
    config = bows_config(tmp_path)
    environ = {REVIEWER_TOKEN_ENV: REVIEWER_SECRET}
    assert config_block(config, gh_login=REVIEWER_SECRET,
                        environ=environ)["ghLogin"] is None
    unnamed = bows_config(tmp_path, reviewer_token_env=None)
    assert config_block(unnamed, gh_login=REVIEWER_SECRET,
                        environ=environ)["ghLogin"] == REVIEWER_SECRET


def test_the_scan_list_is_every_credential_the_seat_holds():
    """The provisioner's variable collection (design §2.6) gives revloop
    these secrets; gh also honours GITHUB_TOKEN."""
    assert set(SECRET_ENV) == set(SECRETS)


def test_a_short_value_is_not_a_credential(tmp_path):
    """A passcode of a few characters would otherwise blank every login
    that happens to contain it."""
    block = config_block(bows_config(tmp_path), gh_login="dark-1-rev",
                         environ={"ALISSA_UI_PASSCODE": "1"})
    assert block["ghLogin"] == "dark-1-rev"


def test_the_process_env_is_the_default_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", SECRETS["GH_TOKEN"])
    block = config_block(bows_config(tmp_path), gh_login=SECRETS["GH_TOKEN"])
    assert block["ghLogin"] is None


# -- the snapshot ------------------------------------------------------------------

def test_the_snapshot_carries_the_block_last(tmp_path):
    block = config_block(bows_config(tmp_path), gh_login=LOGIN, environ={})
    body = build_snapshot(FakeSources(), heartbeat_at=WALL, poll_interval=60,
                          queue_depth=0, now=WALL, config=block)
    assert list(body)[-2:] == ["idle", "config"]
    assert body["config"] == block


def test_without_a_block_the_key_is_absent(tmp_path):
    """Absent, not null: a Studio that predates the field must not be sent
    the key at all."""
    body = build_snapshot(FakeSources(), heartbeat_at=WALL, poll_interval=60,
                          queue_depth=0, now=WALL)
    assert "config" not in body


def test_the_block_survives_the_body_cap(tmp_path):
    """The cap trims lists, never a block: an oversized body loses session
    rows first and keeps the seat's word."""
    block = config_block(bows_config(tmp_path), gh_login=LOGIN, environ={})
    body = build_snapshot(FakeSources(), heartbeat_at=WALL, poll_interval=60,
                          queue_depth=0, now=WALL, config=block)
    body["sessionList"] = [{"name": "x" * 2000}] * 50
    assert fit_body(body)["config"] == block


# -- the daemon's pusher -------------------------------------------------------------

class LoginGitHub:
    def __init__(self, login=LOGIN):
        self._value = login

    @property
    def login(self):
        return self._value

    def review_requests(self, repos=()):
        return []


class BrokenLogin(LoginGitHub):
    @property
    def login(self):
        raise RuntimeError("gh api user: HTTP 502")


class _Al:
    def list_review_sessions(self):
        return []

    def worker_running(self):
        return True


def make_watcher(tmp_path, *, github=None, **kw):
    config = bows_config(tmp_path, fleet_vitals_enabled=True, **kw)
    return ReviewWatcher(config, github=github or LoginGitHub(), alissa=_Al(),
                         state=State(config.state_db))


def test_the_daemon_wires_its_own_config_into_the_pusher(tmp_path):
    watcher = make_watcher(tmp_path)
    assert watcher._fleet_vitals._config_reader == watcher._vitals_config


def test_the_block_follows_the_allowlist_the_watcher_runs_on(tmp_path):
    """The pusher was built with the BOOT config; a feed refresh re-binds
    `self.config`, and the block reports the refreshed set."""
    watcher = make_watcher(tmp_path)
    assert watcher._vitals_config()["repos"] == ["acme/api", "acme/Widgets"]
    watcher._apply_repos(("acme/api", "acme/new"))
    assert watcher._vitals_config()["repos"] == ["acme/api", "acme/new"]
    assert watcher._fleet_vitals.config.repos == ("acme/Widgets", "acme/api")


def daemon_pusher(tmp_path, watcher, client, **config_over):
    return FleetVitalsPusher(
        bows_config(tmp_path, **config_over), FakeSources(), client,
        clock=lambda: float(WALL), config_reader=watcher._vitals_config,
    )


def test_a_pass_pushes_the_block_with_no_secret_in_the_body(
        tmp_path, monkeypatch):
    """c1 + c2 end to end: the container's credentials in the env, a
    pass's push, and a scan of the whole body on the wire."""
    for name, value in ALL_SECRETS.items():
        monkeypatch.setenv(name, value)
    watcher = make_watcher(tmp_path)
    client = FakeVitalsClient()
    pusher = daemon_pusher(tmp_path, watcher, client)

    assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    (body,) = client.snapshots
    assert body["config"] == {
        "feedOwnerActorId": OWNER, "repos": ["acme/api", "acme/Widgets"],
        "reviewersRequested": [], "ghLogin": LOGIN,
    }
    scan(body)


def test_dry_run_describes_the_block_and_sends_nothing(tmp_path, caplog):
    watcher = make_watcher(tmp_path)
    client = FakeVitalsClient()
    pusher = daemon_pusher(tmp_path, watcher, client, dry_run=True)
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_SKIPPED
    assert client.calls == []


def test_a_failed_derivation_costs_the_block_not_the_vitals(tmp_path, caplog):
    watcher = make_watcher(tmp_path, github=BrokenLogin())
    client = FakeVitalsClient()
    pusher = daemon_pusher(tmp_path, watcher, client)
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    (body,) = client.snapshots
    assert "config" not in body and body["sessions"] is not None
    (warning,) = [r.getMessage() for r in caplog.records]
    assert "HTTP 502" in warning and "without it" in warning


def test_the_console_push_leaves_the_block_out(tmp_path):
    """The console process never resolves the feed authority nor refreshes
    the allowlist: its pusher has no reader, and its snapshot no block."""
    class PassedSources(FakeSources):
        def snapshots(self, limit):
            return [{"ts": WALL - 30, "duration_ms": 42}]

    client = FakeVitalsClient()
    pusher = FleetVitalsPusher(bows_config(tmp_path), PassedSources(), client,
                               clock=lambda: float(WALL))
    assert pusher.push_now() == VITALS_PUSHED
    assert "config" not in client.snapshots[0]


# -- c3: a Studio with and without the field ---------------------------------------

def unrecognized(*keys):
    """Studio's strict 400, as `.strict()` words it."""
    quoted = ", ".join(f"'{k}'" for k in keys)
    return AlissaError(400, {"error": "VALIDATION_ERROR", "issues": [
        {"code": "unrecognized_keys", "keys": list(keys), "path": [],
         "message": f"Unrecognized key(s) in object: {quoted}"}]})


class Studio:
    """A fleet-vitals endpoint whose strict schema knows `known` optional
    blocks; anything else optional is refused the way zod refuses it."""

    def __init__(self, known=("idle", "config")):
        self.known = set(known)
        self.attempts = []
        self.bodies = []

    def post_fleet_vitals(self, snapshot):
        self.attempts.append(dict(snapshot))
        unknown = [k for k in ("idle", "config")
                   if k in snapshot and k not in self.known]
        if unknown:
            raise unrecognized(*unknown)
        self.bodies.append(dict(snapshot))
        return {"seat": "revloop", "receivedAt": 1, "replaced": True}


def studio_pusher(tmp_path, studio, clock):
    config = bows_config(tmp_path)
    return FleetVitalsPusher(
        config, FakeSources(), studio, clock=lambda: clock[0],
        config_reader=lambda: config_block(config, gh_login=LOGIN, environ={}),
    )


def test_a_studio_that_knows_the_block_gets_it_first_time(tmp_path):
    studio = Studio()
    pusher = studio_pusher(tmp_path, studio, [float(WALL)])
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    assert len(studio.attempts) == 1 and "config" in studio.bodies[0]


def test_a_studio_that_predates_the_block_still_gets_its_vitals(
        tmp_path, caplog):
    """Refused once, resent without it in the same pass, left out for the
    reprobe window, then offered again -- and a Studio that learned the
    block gets it back."""
    clock = [float(WALL)]
    studio = Studio(known=("idle",))
    pusher = studio_pusher(tmp_path, studio, clock)

    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    assert ["config" in a for a in studio.attempts] == [True, False]
    assert "idle" in studio.bodies[0], "only the refused block is dropped"
    (warning,) = [r.getMessage() for r in caplog.records]
    assert "`config`" in warning and "`idle`" not in warning

    clock[0] += BLOCK_REPROBE_S - 1
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    assert ["config" in a for a in studio.attempts] == [True, False, False]

    clock[0] += 2
    studio.known.add("config")
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    assert "config" in studio.bodies[-1]


def test_a_studio_that_predates_both_blocks_drops_both_in_one_resend(tmp_path):
    studio = Studio(known=())
    pusher = studio_pusher(tmp_path, studio, [float(WALL)])
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_PUSHED
    assert len(studio.attempts) == 2
    assert {"idle", "config"}.isdisjoint(studio.bodies[0])


def test_a_400_that_merely_mentions_configuration_is_a_failure(tmp_path):
    """The match is on the key as a word: an unrelated 400 whose message
    contains `configuration` is not read as a refusal of the block."""
    class Refusing(Studio):
        def post_fleet_vitals(self, snapshot):
            self.attempts.append(dict(snapshot))
            raise AlissaError(400, {"error": "fleet configuration is locked"})

    studio = Refusing()
    pusher = studio_pusher(tmp_path, studio, [float(WALL)])
    assert pusher.push_once(heartbeat_at=WALL) == VITALS_FAILED
    assert len(studio.attempts) == 1


def test_the_idle_reprobe_window_is_unchanged():
    assert IDLE_REPROBE_S == BLOCK_REPROBE_S == 3600.0
