"""The `repos_source: bows` config surface (issue #119): the three env rails
(env > file > flag, BLANK falls through), the `on_missing_hub='add'` guard
narrowed to static, and the actor-id shape check on `bow_owners`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alissa.tools.github.revloop import config as config_module
from alissa.tools.github.revloop.config import (
    BOW_OWNERS_ENV,
    BOWS_REFRESH_POLLS_ENV,
    CONFIG_FILENAME,
    CONFIG_KEYS,
    DEFAULT_PLAN_REPOS,
    PLAN_AUTHORS_ENV,
    PLAN_REPOS_ENV,
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


# -- the prompt responder's knobs (issue #138) --------------------------------


def test_prompt_responder_knobs_default_on_with_the_documented_values(tmp_path):
    cfg = Config.build(tmp_path, {}, environ={})
    assert cfg.prompt_responder == "on"
    assert cfg.prompt_quiet_seconds == 90 and cfg.prompt_quiet_floor == 90
    assert cfg.prompt_kill_minutes == 10 and cfg.prompt_kill_seconds == 600
    assert cfg.prompt_max_answers == 5 and cfg.prompt_answer_cap == 5
    assert cfg.waiting_dir == "/workspace/.waiting"


def test_prompt_responder_knobs_from_the_config_file(tmp_path):
    cfg = Config.build(tmp_path, {
        "prompt_responder": "observe", "prompt_quiet_seconds": 120,
        "prompt_kill_minutes": 3, "prompt_max_answers": 2, "waiting_dir": "/tmp/w",
    }, environ={})
    assert cfg.prompt_responder == "observe"
    assert cfg.prompt_quiet_seconds == 120 and cfg.prompt_kill_seconds == 180
    assert cfg.prompt_max_answers == 2 and cfg.waiting_dir == "/tmp/w"


@pytest.mark.parametrize("mode", ["on", "observe", "off"])
def test_prompt_responder_accepts_its_three_modes(tmp_path, mode):
    assert Config.build(tmp_path, {"prompt_responder": mode}, environ={}).prompt_responder == mode


def test_prompt_responder_rejects_an_unknown_mode_by_name(tmp_path):
    """A typo must not read as 'off' -- the one outcome an operator setting
    this key cannot want."""
    with pytest.raises(ValueError, match="prompt_responder must be one of"):
        Config.build(tmp_path, {"prompt_responder": "yes"}, environ={})


def test_prompt_knobs_below_their_floors_are_clamped_up_with_a_warning(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="alissa.tools.github.revloop.config"):
        cfg = Config.build(tmp_path, {
            "prompt_quiet_seconds": 5, "prompt_kill_minutes": 0, "prompt_max_answers": -1,
        }, environ={})
    assert cfg.prompt_quiet_seconds == 30 and cfg.prompt_quiet_floor == 30
    assert cfg.prompt_kill_minutes == 1 and cfg.prompt_kill_seconds == 60
    assert cfg.prompt_max_answers == 1 and cfg.prompt_answer_cap == 1
    messages = [r.message for r in caplog.records]
    assert any("prompt_quiet_seconds=5 is below the 30 s floor" in m for m in messages), messages
    assert any("prompt_kill_minutes=0 is below the 1 min floor" in m for m in messages), messages
    assert any("prompt_max_answers=-1 is below the floor of 1" in m for m in messages), messages


def test_prompt_floors_apply_without_build(tmp_path):
    """The properties the loop reads floor on their own, so a Config
    assembled around build() (dataclasses.replace in a test) can never read
    a window the daemon would act wrongly on."""
    import dataclasses
    cfg = dataclasses.replace(
        Config.build(tmp_path, {}, environ={}), prompt_quiet_seconds=1, prompt_kill_minutes=0,
        prompt_max_answers=0,
    )
    assert cfg.prompt_quiet_floor == 30
    assert cfg.prompt_kill_seconds == 60
    assert cfg.prompt_answer_cap == 1


def test_waiting_dir_must_be_a_non_empty_path(tmp_path):
    with pytest.raises(ValueError, match="waiting_dir must be a non-empty path"):
        Config.build(tmp_path, {"waiting_dir": "   "}, environ={})


def test_the_prompt_keys_are_known_config_keys_and_in_the_example_file(tmp_path):
    """The renderer's skew probe reads CONFIG_KEYS, and the example file is
    what an operator copies: both must know every knob."""
    from pathlib import Path
    from alissa.tools.github.revloop.config import CONFIG_KEYS
    keys = ("prompt_responder", "prompt_quiet_seconds", "prompt_kill_minutes",
            "prompt_max_answers", "waiting_dir")
    for key in keys:
        assert key in CONFIG_KEYS, key
    example = json.loads((Path(__file__).resolve().parents[7] / "revloop.config.example.json").read_text())
    for key in keys:
        assert key in example, key
    assert Config.build(tmp_path, {k: v for k, v in example.items() if not k.startswith("_")}, environ={})


# -- the plan allowlists (issue #148) -----------------------------------------
#
# The plans repository is fleet CONFIGURATION: the default names it by name
# under the `<owner>` placeholder, a deployment names its own with
# ALISSA_REVIEW_PLAN_REPOS, and no literal plans repository survives in the
# code outside config.py. `plan_authors` is fail-closed: empty means no PR is
# a plan PR.


def test_the_plan_defaults_select_nothing(tmp_path):
    cfg = Config.build(tmp_path, {}, environ={})
    assert cfg.plan_repos == ("<owner>/alissa-dark-factory-plans",)
    assert cfg.plan_repos == DEFAULT_PLAN_REPOS
    assert cfg.plan_authors == ()
    assert cfg.is_plan_repo("ali-fhr/alissa-dark-factory-plans")
    assert cfg.is_plan_repo("someone-else/alissa-dark-factory-plans")
    assert not cfg.is_plan_repo("ali-fhr/alissa-dark-factory-revloop")
    # Fail-closed: nobody is a plan author until one is named.
    assert not cfg.is_plan_author("genlissa-app")
    assert not cfg.is_plan_author("")


def test_the_env_rails_name_the_fleets_plans_repository_and_author(tmp_path):
    cfg = Config.build(
        tmp_path,
        {"plan_repos": ["file/plans"], "plan_authors": ["file-bot"]},
        environ={
            PLAN_REPOS_ENV: "ali-fhr/alissa-dark-factory-plans",
            PLAN_AUTHORS_ENV: " genlissa-app | planner[bot] ,",
        },
    )
    # env > file, split on | and , with whitespace and empties dropped.
    assert cfg.plan_repos == ("ali-fhr/alissa-dark-factory-plans",)
    assert cfg.plan_authors == ("genlissa-app", "planner[bot]")
    assert cfg.is_plan_repo("ALI-FHR/Alissa-Dark-Factory-Plans")
    assert not cfg.is_plan_repo("other/alissa-dark-factory-plans")
    assert cfg.is_plan_author("GenLissa-App")
    assert not cfg.is_plan_author("vlissa-app")


def test_a_blank_env_rail_falls_through_to_the_file(tmp_path):
    cfg = Config.build(
        tmp_path,
        {"plan_repos": ["acme/plans"], "plan_authors": ["genlissa-app"]},
        environ={PLAN_REPOS_ENV: "  ", PLAN_AUTHORS_ENV: ""},
    )
    assert cfg.plan_repos == ("acme/plans",)
    assert cfg.plan_authors == ("genlissa-app",)


def test_a_separator_only_env_rail_is_an_empty_list(tmp_path):
    """Said, and what was said is "nothing": fail closed, not the default."""
    cfg = Config.build(tmp_path, {}, environ={PLAN_REPOS_ENV: "|,"})
    assert cfg.plan_repos == ()
    assert not cfg.is_plan_repo("ali-fhr/alissa-dark-factory-plans")


@pytest.mark.parametrize(
    "key, value",
    [
        ("plan_repos", ["alissa-dark-factory-plans"]),   # no owner
        ("plan_repos", ["a/b/c"]),
        ("plan_repos", ["*/plans"]),
        ("plan_repos", ["<org>/plans"]),                 # the placeholder is <owner>
        ("plan_authors", ["not a login"]),
        ("plan_authors", ["-leading-dash"]),
        ("plan_repos", "acme/plans"),                    # a string, not a list
    ],
)
def test_a_malformed_plan_entry_is_refused_at_load(tmp_path, key, value):
    with pytest.raises(ValueError, match=key):
        Config.build(tmp_path, {key: value}, environ={})


def test_the_plan_keys_are_config_file_keys():
    assert "plan_repos" in CONFIG_KEYS and "plan_authors" in CONFIG_KEYS


def test_no_plans_repository_literal_survives_outside_config():
    """The operator's decision on issue #148: the plans repository is
    configuration. Neither the design's superseded name nor this fleet's
    appears anywhere in the daemon's code but config.py -- which itself names
    it only under the placeholder."""
    package = Path(config_module.__file__).parent
    literals = ("alissa-dark-factory-plans", "alissa-plans", "ali-fhr/")
    offenders = []
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for literal in literals:
            if literal in text and not (
                path.name == "config.py" and literal == "alissa-dark-factory-plans"
            ):
                offenders.append(f"{path.relative_to(package)}: {literal}")
    assert offenders == []
    config_text = Path(config_module.__file__).read_text(encoding="utf-8")
    assert "ali-fhr/alissa-dark-factory-plans" not in config_text
    assert "ali-fhr/alissa-plans" not in config_text
