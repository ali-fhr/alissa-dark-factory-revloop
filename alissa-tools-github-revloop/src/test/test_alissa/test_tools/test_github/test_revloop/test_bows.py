"""`repos_source: bows` — the feed-derived allowlist (issue #119).

What is pinned here, in the order the issue lists it:

* derivation: 2 own active feeds + 1 foreign-owned + 1 cancelled + 1 malformed
  title -> exactly the 2 own repos, the foreign one counted in ONE aggregate
  WARNING, the malformed one NAMED; the static seed unioned; case-insensitive
  de-duplication;
* the two fail-safe rules: a refresh failure keeps the prior set (and a FIRST
  failure starts on the static list alone, at ERROR); a feed that disappears
  drops its repo only when no round is in flight for it;
* the REST client's two reads and the boot-time feed-authority resolution.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error

import pytest

from alissa.tools.github.revloop import alissa_client as client_module
from alissa.tools.github.revloop.alissa_client import (
    AlissaClient,
    AlissaError,
    AlissaTransient,
    BodyOfWork,
    Identity,
)
from alissa.tools.github.revloop.bows import (
    FEED_PREFIX,
    BowRepoSource,
    is_feed_title,
    parse_feed_repo,
    union_repos,
)
from alissa.tools.github.revloop.config import REPOS_BOWS, Config

OWN = "j5706fv7xe5jy1k5wdwzacab9s8axcd2"
FOREIGN = "k7706fv7xe5jy1k5wdwzacab9s8axcd2"


class FakeClient:
    """A listing the tests script: a list of BodyOfWork rows, or an exception
    to raise instead."""

    def __init__(self, bows=(), error=None):
        self.bows = list(bows)
        self.error = error
        self.calls = 0

    def list_bodies_of_work(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.bows)


def bow(title, *, owner=OWN, status="active", id_=None):
    return BodyOfWork(
        id=id_ or f"bow-{abs(hash(title)) % 10_000}",
        title=title,
        status=status,
        owner_id=owner,
    )


def bows_config(tmp_path, **overrides):
    data = {"repos_source": REPOS_BOWS, "bow_owners": [OWN]}
    data.update(overrides)
    return Config.build(tmp_path, data)


FIXTURE = [
    bow("autodev: acme/widgets"),                       # own, active
    bow("Autodev: acme/gadgets"),                        # own, active, prefix case
    bow("autodev: other/secret", owner=FOREIGN),        # foreign-owned
    bow("autodev: acme/old", status="completed"),        # own, not active
    bow("autodev: not a repo", id_="bow-malformed"),     # own, malformed title
    bow("Roadmap Q3"),                                   # not a feed at all
]


# -- the title grammar --------------------------------------------------------


@pytest.mark.parametrize(
    "title,repo",
    [
        ("autodev: acme/widgets", "acme/widgets"),
        ("AUTODEV: acme/widgets", "acme/widgets"),
        ("  autodev:  acme/studio.alissa.app ", "acme/studio.alissa.app"),
        ("autodev: acme/widgets.git", None),
        ("autodev: acme/..", None),
        ("autodev: widgets", None),
        ("autodev: -acme/widgets", None),
        ("autodev:acme/widgets", None),  # the trailing space is part of the prefix
        ("Roadmap", None),
    ],
)
def test_parse_feed_repo(title, repo):
    assert parse_feed_repo(title) == repo


def test_is_feed_title_is_case_insensitive_on_the_prefix_only():
    assert is_feed_title("AutoDev: x/y")
    assert not is_feed_title("autodev-x/y")
    assert FEED_PREFIX == "autodev: "


def test_union_keeps_static_first_and_folds_case():
    assert union_repos(("Acme/Widgets",), ("acme/widgets", "acme/gadgets")) == (
        "Acme/Widgets", "acme/gadgets",
    )


# -- derivation ---------------------------------------------------------------


def test_derives_exactly_the_own_active_feeds(tmp_path, caplog):
    src = BowRepoSource(bows_config(tmp_path), FakeClient(FIXTURE))
    with caplog.at_level(logging.WARNING):
        assert src.refresh() is True

    assert src.derived == ("acme/widgets", "acme/gadgets")
    assert src.repos() == ("acme/widgets", "acme/gadgets")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    foreign = [w for w in warnings if "NOT feeds" in w]
    assert len(foreign) == 1, "one AGGREGATE warning for foreign feeds"
    assert "1 body(ies) of work match the feed prefix" in foreign[0]
    assert "other/secret" not in foreign[0], "-v names them, WARN counts them"
    malformed = [w for w in warnings if "not `owner/repo`" in w]
    assert len(malformed) == 1
    assert "'autodev: not a repo'" in malformed[0] and "bow-malformed" in malformed[0]


def test_verbose_names_the_foreign_and_the_source_containers(tmp_path, caplog):
    src = BowRepoSource(bows_config(tmp_path), FakeClient(FIXTURE))
    with caplog.at_level(logging.DEBUG):
        src.refresh()
    debug = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert "ignoring body of work 'autodev: other/secret'" in debug
    assert "-> acme/widgets" in debug
    assert [s[0] for s in src.sources()] == ["acme/widgets", "acme/gadgets"]
    assert src.sources()[0][2] == "autodev: acme/widgets"


def test_the_static_seed_is_unioned_and_never_dropped(tmp_path):
    cfg = bows_config(tmp_path, repos=["acme/static", "ACME/WIDGETS"])
    src = BowRepoSource(cfg, FakeClient(FIXTURE))
    src.refresh()
    # static first, the operator's casing wins, derived duplicates fold away
    assert src.repos() == ("acme/static", "ACME/WIDGETS", "acme/gadgets")


def test_dedupe_is_case_insensitive_across_feeds(tmp_path):
    src = BowRepoSource(
        bows_config(tmp_path),
        FakeClient([bow("autodev: Acme/Widgets"), bow("autodev: acme/widgets")]),
    )
    src.refresh()
    assert src.derived == ("Acme/Widgets",)


def test_no_authority_derives_nothing_at_error(tmp_path, caplog):
    cfg = Config.build(tmp_path, {"repos_source": REPOS_BOWS})  # bow_owners empty
    client = FakeClient(FIXTURE)
    src = BowRepoSource(cfg, client)
    with caplog.at_level(logging.ERROR):
        assert src.refresh() is False
    assert client.calls == 0, "nothing is even listed without an authority"
    assert src.repos() == ()
    assert any("no feed authority" in r.getMessage() for r in caplog.records)


def test_owner_gate_compares_exactly(tmp_path):
    src = BowRepoSource(
        bows_config(tmp_path),
        FakeClient([bow("autodev: acme/widgets", owner=OWN.upper())]),
    )
    src.refresh()
    assert src.derived == (), "an id differing by case is a different actor"


def test_a_feed_with_no_owner_is_not_authoritative(tmp_path):
    src = BowRepoSource(
        bows_config(tmp_path), FakeClient([bow("autodev: acme/widgets", owner="")])
    )
    src.refresh()
    assert src.derived == ()


# -- the cadence --------------------------------------------------------------


def test_due_until_first_attempt_then_every_n_ticks(tmp_path):
    src = BowRepoSource(bows_config(tmp_path, bows_refresh_polls=3), FakeClient())
    assert src.due()
    src.refresh()
    for _ in range(2):
        src.tick()
        assert not src.due()
    src.tick()
    assert src.due()


def test_a_failed_attempt_still_resets_the_cadence(tmp_path):
    client = FakeClient(error=AlissaTransient(503, "down"))
    src = BowRepoSource(bows_config(tmp_path, bows_refresh_polls=2), client)
    src.refresh()
    src.tick()
    assert not src.due(), "a failing API is not re-listed every pass"


# -- never shrink on failure --------------------------------------------------


def test_a_refresh_failure_keeps_the_prior_set_and_warns(tmp_path, caplog):
    client = FakeClient(FIXTURE)
    src = BowRepoSource(bows_config(tmp_path), client)
    src.refresh()
    before = src.repos()

    client.error = AlissaTransient(503, "down")
    with caplog.at_level(logging.WARNING):
        assert src.refresh() is False

    assert src.repos() == before
    assert [r.levelno for r in caplog.records if "keeping the 2 repo(s)" in r.getMessage()] == [
        logging.WARNING
    ]


def test_a_first_refresh_failure_starts_on_the_static_list_at_error(tmp_path, caplog):
    cfg = bows_config(tmp_path, repos=["acme/static"])
    src = BowRepoSource(cfg, FakeClient(error=AlissaError(500, "boom")))
    with caplog.at_level(logging.ERROR):
        assert src.refresh() is False
    assert src.repos() == ("acme/static",)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "first refresh" in errors[0].getMessage()
    assert "acme/static" in errors[0].getMessage()


def test_a_first_failure_with_no_static_list_says_no_repos(tmp_path, caplog):
    src = BowRepoSource(bows_config(tmp_path), FakeClient(error=AlissaError(500, "boom")))
    with caplog.at_level(logging.ERROR):
        src.refresh()
    assert src.repos() == ()
    assert "empty, so NO repos" in caplog.records[-1].getMessage()


def test_a_malformed_listing_payload_is_a_failure_not_an_empty_set(tmp_path, monkeypatch):
    """A 200 with no `bodiesOfWork` list must route through the never-shrink
    rule rather than derive nothing."""
    monkeypatch.setattr(
        client_module, "_opener", FakeOpener(lambda req, timeout=None: FakeResponse(b'{"ok": 1}'))
    )
    client = AlissaClient(token="t", base="https://api.example")
    src = BowRepoSource(bows_config(tmp_path), client)
    src._derived = ("acme/widgets",)
    assert src.refresh() is False
    assert src.repos() == ("acme/widgets",)


# -- never drop mid-round -----------------------------------------------------


def test_a_disappeared_feed_drops_its_repo_when_no_round_is_in_flight(tmp_path):
    client = FakeClient(FIXTURE)
    src = BowRepoSource(bows_config(tmp_path), client)
    src.refresh()
    assert "acme/gadgets" in src.derived

    client.bows = [b for b in FIXTURE if "gadgets" not in b.title]
    asked = []

    def mid_round():
        asked.append(True)
        return frozenset()

    src.refresh(mid_round)
    assert src.derived == ("acme/widgets",)
    assert asked == [True], "the probe is consulted exactly once, when there is a candidate"
    assert [s[0] for s in src.sources()] == ["acme/widgets"]


def test_a_disappeared_feed_keeps_its_repo_while_a_round_is_in_flight(tmp_path, caplog):
    client = FakeClient(FIXTURE)
    src = BowRepoSource(bows_config(tmp_path), client)
    src.refresh()

    client.bows = [b for b in FIXTURE if "gadgets" not in b.title]
    with caplog.at_level(logging.INFO):
        src.refresh(lambda: frozenset({"acme/gadgets"}))
    assert src.derived == ("acme/widgets", "acme/gadgets")
    assert any("still has a round in flight" in r.getMessage() for r in caplog.records)

    # ...and drops on the refresh after the round finishes.
    src.refresh(lambda: frozenset())
    assert src.derived == ("acme/widgets",)


def test_the_probe_is_not_called_in_the_steady_state(tmp_path):
    src = BowRepoSource(bows_config(tmp_path), FakeClient(FIXTURE))
    src.refresh()

    def explode():  # pragma: no cover - must not be reached
        raise AssertionError("no drop candidate, no probe")

    src.refresh(explode)


def test_an_unanswerable_probe_drops_nothing(tmp_path):
    client = FakeClient(FIXTURE)
    src = BowRepoSource(bows_config(tmp_path), client)
    src.refresh()
    client.bows = []
    src.refresh(None)
    assert src.derived == ("acme/widgets", "acme/gadgets")


def test_a_completed_feed_is_treated_like_a_disappeared_one(tmp_path):
    client = FakeClient(FIXTURE)
    src = BowRepoSource(bows_config(tmp_path), client)
    src.refresh()
    client.bows = [
        bow(b.title, owner=b.owner_id, status="cancelled", id_=b.id)
        if "gadgets" in b.title else b
        for b in FIXTURE
    ]
    src.refresh(lambda: frozenset())
    assert src.derived == ("acme/widgets",)


# -- the REST client's two reads ---------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, fn):
        self._fn = fn

    def open(self, req, timeout=None):
        return self._fn(req, timeout=timeout)


def test_ping_is_an_authenticated_get_and_returns_the_actor(monkeypatch):
    seen = {}

    def fake_open(req, timeout=None):
        seen["req"] = req
        return FakeResponse(
            b'{"pong": true, "userId": "u1", "actorId": " a1 ", "displayName": "Rev"}'
        )

    monkeypatch.setattr(client_module, "_opener", FakeOpener(fake_open))
    identity = AlissaClient(token="tok", base="https://api.example/").ping()

    assert identity == Identity(actor_id="a1", user_id="u1", display_name="Rev")
    req = seen["req"]
    assert req.full_url == "https://api.example/v1/ping"
    assert req.get_method() == "GET"
    assert req.data is None
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("Content-type") is None, "a GET carries no body type"


def test_ping_without_an_actor_id_is_an_error_not_an_empty_authority(monkeypatch):
    monkeypatch.setattr(
        client_module, "_opener", FakeOpener(lambda req, timeout=None: FakeResponse(b'{"pong": true}'))
    )
    with pytest.raises(AlissaError, match="no actorId"):
        AlissaClient(token="tok").ping()


def test_list_bodies_of_work_reads_the_shared_listing(monkeypatch):
    seen = {}
    payload = {
        "bodiesOfWork": [
            {"_id": "b1", "title": "autodev: a/b", "status": "active", "ownerActorId": OWN},
            {"_id": "", "title": "no id"},
            "not-a-row",
            {"_id": "b2"},
        ]
    }

    def fake_open(req, timeout=None):
        seen["req"] = req
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(client_module, "_opener", FakeOpener(fake_open))
    rows = AlissaClient(token="tok", base="https://api.example").list_bodies_of_work()

    assert seen["req"].full_url == "https://api.example/v1/bodies-of-work?includeShared=true"
    assert seen["req"].get_method() == "GET"
    assert rows == [
        BodyOfWork(id="b1", title="autodev: a/b", status="active", owner_id=OWN),
        BodyOfWork(id="b2", title="", status="", owner_id=""),
    ]


def test_a_get_refuses_redirects_like_the_post_does(monkeypatch):
    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, io.BytesIO(b""))

    monkeypatch.setattr(client_module, "_opener", FakeOpener(fake_open))
    with pytest.raises(AlissaError) as info:
        AlissaClient(token="tok").ping()
    assert info.value.status == 302


# -- boot: resolving the feed authority --------------------------------------


def test_resolve_feed_authority_is_a_no_op_under_static(tmp_path):
    from alissa.tools.github.revloop.__main__ import resolve_feed_authority

    cfg = Config.build(tmp_path, {})

    class Explode:
        def ping(self):  # pragma: no cover - must not be reached
            raise AssertionError("static mode must not call the API")

    assert resolve_feed_authority(cfg, Explode()) is cfg


def test_resolve_feed_authority_keeps_an_explicit_list(tmp_path, caplog):
    from alissa.tools.github.revloop.__main__ import resolve_feed_authority

    cfg = bows_config(tmp_path)

    class Explode:
        def ping(self):  # pragma: no cover
            raise AssertionError("explicit owners need no whoami")

    with caplog.at_level(logging.INFO):
        assert resolve_feed_authority(cfg, Explode()).bow_owners == (OWN,)
    assert any("bow feed authority: explicit" in r.getMessage() for r in caplog.records)


def test_resolve_feed_authority_defaults_to_self(tmp_path, caplog):
    from alissa.tools.github.revloop.__main__ import resolve_feed_authority

    cfg = Config.build(tmp_path, {"repos_source": REPOS_BOWS})

    class Whoami:
        def ping(self):
            return Identity(actor_id="self-actor-id", display_name="Me")

    with caplog.at_level(logging.INFO):
        out = resolve_feed_authority(cfg, Whoami())
    assert out.bow_owners == ("self-actor-id",)
    assert any(
        "bow feed authority: self (self-actor-id)" in r.getMessage() for r in caplog.records
    )


def test_a_failed_whoami_is_fatal_at_boot(tmp_path, monkeypatch):
    """Never fall back to trusting everything, nor to trusting nothing."""
    from alissa.tools.github.revloop import __main__ as main_module

    cfg = Config.build(tmp_path, {"repos_source": REPOS_BOWS})

    class Down:
        def ping(self):
            raise AlissaTransient(0, "connection refused")

    with pytest.raises(AlissaTransient):
        main_module.resolve_feed_authority(cfg, Down())

    # ...and the CLI turns it into exit 2 with the reason named.
    (tmp_path / "revloop.config.json").write_text(json.dumps({"repos_source": "bows"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALISSA_REVIEW_REPOS_SOURCE", raising=False)
    monkeypatch.delenv("ALISSA_REVIEW_BOW_OWNERS", raising=False)
    monkeypatch.setattr(main_module, "AlissaClient", lambda base=None: Down())
    assert main_module.main(["--workspace-root", str(tmp_path), "--once"]) == 2
