"""The `repos_source: bows` config surface (issue #119): the three env rails
(env > file > flag, BLANK falls through), the `on_missing_hub='add'` guard
narrowed to static, and the actor-id shape check on `bow_owners`."""

from __future__ import annotations

import json

import pytest

from alissa.tools.github.revloop.config import (
    BOW_OWNERS_ENV,
    BOWS_REFRESH_POLLS_ENV,
    CONFIG_FILENAME,
    HUB_ADD,
    REPOS_BOWS,
    REPOS_SOURCE_ENV,
    REPOS_STATIC,
    Config,
    normalize_bow_owners,
)
from alissa.tools.github.revloop.__main__ import build_parser, resolve_config

ID_A = "j5706fv7xe5jy1k5wdwzacab9s8axcd2"
ID_B = "k7706fv7xe5jy1k5wdwzacab9s8axcd2"


def cli(*argv):
    return build_parser().parse_args(list(argv))


# -- defaults and the static path ---------------------------------------------


def test_static_is_the_default_and_bows_keys_have_their_defaults(tmp_path):
    cfg = Config.build(tmp_path, {}, environ={})
    assert cfg.repos_source == REPOS_STATIC
    assert cfg.bows_refresh_polls == 5
    assert cfg.bow_owners == ()


def test_an_unknown_source_is_refused_by_name(tmp_path):
    with pytest.raises(ValueError, match="repos_source must be one of"):
        Config.build(tmp_path, {"repos_source": "feeds"}, environ={})


def test_refresh_polls_floor_is_one(tmp_path):
    with pytest.raises(ValueError, match="bows_refresh_polls must be >= 1"):
        Config.build(tmp_path, {"bows_refresh_polls": 0}, environ={})
    assert Config.build(tmp_path, {"bows_refresh_polls": 1}, environ={}).bows_refresh_polls == 1


# -- env rails ----------------------------------------------------------------


@pytest.mark.parametrize(
    "key,env,file_value,cli_flag,cli_value,env_value,expect_env",
    [
        ("repos_source", REPOS_SOURCE_ENV, "static", "--repos-source", "bows", "static", "static"),
        ("bows_refresh_polls", BOWS_REFRESH_POLLS_ENV, 2, "--bows-refresh-polls", "3", "7", 7),
        ("bow_owners", BOW_OWNERS_ENV, [ID_A], "--bow-owner", ID_B, f"{ID_A}|{ID_B}", (ID_A, ID_B)),
    ],
)
def test_env_wins_over_file_and_flag_and_blank_falls_through(
    tmp_path, monkeypatch, key, env, file_value, cli_flag, cli_value, env_value, expect_env
):
    """file < CLI < env, through the real entry point; a BLANK env value is
    the same as unset (an unset platform variable reference renders as "")."""
    (tmp_path / CONFIG_FILENAME).write_text(json.dumps({key: file_value}))
    monkeypatch.chdir(tmp_path)
    for name in (REPOS_SOURCE_ENV, BOWS_REFRESH_POLLS_ENV, BOW_OWNERS_ENV):
        monkeypatch.delenv(name, raising=False)

    def norm(v):
        return tuple(v) if isinstance(v, list) else v

    assert getattr(resolve_config(cli()), key) == norm(file_value)
    from_cli = getattr(resolve_config(cli(cli_flag, cli_value)), key)
    assert from_cli == (norm([cli_value]) if key == "bow_owners" else type(file_value)(cli_value))

    monkeypatch.setenv(env, "   ")
    assert getattr(resolve_config(cli(cli_flag, cli_value)), key) == from_cli, "blank falls through"

    monkeypatch.setenv(env, env_value)
    assert getattr(resolve_config(cli(cli_flag, cli_value)), key) == expect_env


def test_a_bad_env_source_names_the_variable(tmp_path):
    with pytest.raises(ValueError, match=REPOS_SOURCE_ENV):
        Config.build(tmp_path, {}, environ={REPOS_SOURCE_ENV: "feeds"})


def test_a_non_integer_env_refresh_is_a_config_error(tmp_path):
    with pytest.raises(ValueError, match=BOWS_REFRESH_POLLS_ENV):
        Config.build(tmp_path, {}, environ={BOWS_REFRESH_POLLS_ENV: "five"})


def test_env_owners_split_on_pipe_and_comma_and_dedupe_exactly(tmp_path):
    cfg = Config.build(
        tmp_path, {}, environ={BOW_OWNERS_ENV: f" {ID_A} , {ID_B}|{ID_A}, "}
    )
    assert cfg.bow_owners == (ID_A, ID_B)


# -- bow_owners shape ---------------------------------------------------------


@pytest.mark.parametrize(
    "entry,fragment",
    [
        ("rhdzmota", "'rhdzmota' is not an Alissa actor id"),
        ("Alissa Code PR Review", "looks like a username or a display name"),
        (ID_A.upper(), "did you mean"),
        ("j5706fv7xe5jy1k5wdwzacab9s8axcd", "31 characters, not 32"),
    ],
)
def test_non_id_owners_are_refused_by_name(tmp_path, entry, fragment):
    with pytest.raises(ValueError, match=fragment):
        Config.build(tmp_path, {"bow_owners": [entry]}, environ={})


def test_owners_may_arrive_as_one_string_or_a_list():
    assert normalize_bow_owners(f"{ID_A},{ID_B}") == (ID_A, ID_B)
    assert normalize_bow_owners([ID_A, f"{ID_B}|{ID_A}"]) == (ID_A, ID_B)
    assert normalize_bow_owners([]) == ()
    with pytest.raises(ValueError, match="must be a list"):
        normalize_bow_owners(42)


def test_trusts_feed_owner_is_exact_and_fails_closed(tmp_path):
    cfg = Config.build(tmp_path, {"bow_owners": [ID_A]}, environ={})
    assert cfg.trusts_feed_owner(ID_A)
    assert not cfg.trusts_feed_owner(ID_A.upper())
    assert not cfg.trusts_feed_owner("")
    assert not cfg.trusts_feed_owner(None)
    assert not Config.build(tmp_path, {}, environ={}).trusts_feed_owner(ID_A)


# -- the on_missing_hub guard -------------------------------------------------


def test_hub_add_guard_applies_under_static_only(tmp_path):
    with pytest.raises(ValueError, match="allowlist"):
        Config.build(tmp_path, {"on_missing_hub": "add", "repos": []}, environ={})

    cfg = Config.build(
        tmp_path, {"on_missing_hub": "add", "repos": [], "repos_source": REPOS_BOWS}, environ={}
    )
    assert cfg.on_missing_hub == HUB_ADD and cfg.repos == ()

    # ...also when the mode arrives from the environment
    cfg = Config.build(
        tmp_path, {"on_missing_hub": "add"}, environ={REPOS_SOURCE_ENV: "bows"}
    )
    assert cfg.repos_source == REPOS_BOWS


# -- watches(): the one place the two modes differ ---------------------------


def test_watches_empty_means_all_under_static_and_nothing_under_bows(tmp_path):
    static = Config.build(tmp_path, {}, environ={})
    assert static.watches("any/repo")
    bows = Config.build(tmp_path, {"repos_source": REPOS_BOWS}, environ={})
    assert not bows.watches("any/repo")


def test_watches_is_exact_under_static_and_casefolded_under_bows(tmp_path):
    static = Config.build(tmp_path, {"repos": ["Acme/Widgets"]}, environ={})
    assert static.watches("Acme/Widgets") and not static.watches("acme/widgets")
    bows = Config.build(
        tmp_path, {"repos": ["Acme/Widgets"], "repos_source": REPOS_BOWS}, environ={}
    )
    assert bows.watches("acme/widgets") and not bows.watches("acme/gadgets")


def test_replacing_repos_rederives_the_match_set(tmp_path):
    import dataclasses

    bows = Config.build(tmp_path, {"repos_source": REPOS_BOWS}, environ={})
    assert not bows.watches("acme/widgets")
    assert dataclasses.replace(bows, repos=("acme/widgets",)).watches("ACME/widgets")
