"""Pinning tests for `trust.py` — the daemon-side claude trust seeding
(issue #136): the merge into both claude state files, its idempotence and
never-remove contract, the compare-and-swap under a concurrent writer, the
derived-repos record the entrypoint reads at the next boot, the reviewer's
path shape (hub root, main/, the REVIEW-* checkout), and the pane classifier
the stale-round branch uses -- with devloop PR #124's dialog fixtures
verbatim, so the two seats agree on the shape check."""

import json
import os
from pathlib import Path

import pytest

from alissa.tools.github.revloop import trust
from alissa.tools.github.revloop.trust import (
    DERIVED_REPOS_FILENAME,
    SEED_TRUST_ATTEMPTS,
    claude_state_targets,
    hub_root,
    hub_trust_paths,
    pane_shows_first_run_dialog,
    review_checkout_name,
    seed_trust,
    write_derived_repos,
)


def state(path: Path) -> dict:
    return json.loads(path.read_text())


def trusted(path: Path) -> set:
    return {
        p for p, entry in state(path).get("projects", {}).items()
        if entry.get("hasTrustDialogAccepted") is True
    }


# -- targets ------------------------------------------------------------------


def test_targets_are_home_and_config_dir(tmp_path):
    home, ccdir = tmp_path / "h", tmp_path / "c"
    assert claude_state_targets(home, ccdir) == [
        home / ".claude.json", ccdir / ".claude.json"
    ]


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_config_dir_yields_home_alone(tmp_path, blank):
    home = tmp_path / "h"
    assert claude_state_targets(home, blank) == [home / ".claude.json"]


def test_targets_default_to_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "envhome"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "envcc"))
    assert claude_state_targets() == [
        tmp_path / "envhome" / ".claude.json",
        tmp_path / "envcc" / ".claude.json",
    ]


def test_a_config_dir_equal_to_home_is_not_listed_twice(tmp_path):
    assert claude_state_targets(tmp_path, tmp_path) == [tmp_path / ".claude.json"]


# -- which paths a reviewer hub needs ------------------------------------------
#
# The reviewer's shape, not the developer's: sessions start in {hub}/main and
# the review skill's throwaway worktree is REVIEW-TASK-<id> beside it.


def test_hub_root_is_the_parent_of_a_main_cwd(tmp_path):
    assert hub_root(tmp_path / "widgets" / "main") == tmp_path / "widgets"
    assert hub_root(tmp_path / "widgets") == tmp_path / "widgets", \
        "a template that starts sessions in the hub root names the hub itself"


def test_review_checkout_is_named_for_the_task():
    assert review_checkout_name("TASK-500") == "REVIEW-TASK-500"
    assert review_checkout_name(None) is None
    assert review_checkout_name("  ") is None


def test_hub_trust_paths_cover_root_main_checkout_and_existing_checkouts(tmp_path):
    hub = tmp_path / "widgets"
    (hub / "main").mkdir(parents=True)
    (hub / "REVIEW-TASK-7").mkdir()
    (hub / "REVIEW-TASK-3").mkdir()
    (hub / "REVIEW-FILE").write_text("not a dir")
    (hub / "TASK-9-DEV").mkdir()  # a developer worktree is not a review checkout
    assert hub_trust_paths(hub / "main", task_ref="TASK-500") == [
        hub, hub / "main", hub / "REVIEW-TASK-500", hub / "REVIEW-TASK-3", hub / "REVIEW-TASK-7",
    ]


def test_hub_trust_paths_list_root_main_and_checkout_before_they_exist(tmp_path):
    cwd = tmp_path / "not-yet" / "main"
    assert hub_trust_paths(cwd, task_ref="TASK-1") == [
        cwd.parent, cwd, cwd.parent / "REVIEW-TASK-1"
    ]
    assert hub_trust_paths(cwd) == [cwd.parent, cwd], "no task -> no named checkout"


def test_hub_trust_paths_keep_a_non_main_cwd_and_never_repeat(tmp_path):
    cwd = tmp_path / "widgets" / "work"
    assert hub_trust_paths(cwd) == [cwd, cwd / "main"]
    assert hub_trust_paths(tmp_path / "widgets") == [tmp_path / "widgets", tmp_path / "widgets" / "main"]


# -- the merge ----------------------------------------------------------------


def test_seed_writes_both_targets_and_creates_missing_files(tmp_path):
    home, ccdir = tmp_path / "h", tmp_path / "c"
    hub = tmp_path / "ws" / "widgets"

    changed = seed_trust([hub, hub / "main"], home=home, config_dir=ccdir)

    assert changed == [home / ".claude.json", ccdir / ".claude.json"]
    for target in changed:
        assert trusted(target) == {str(hub), str(hub / "main")}


def test_seed_preserves_everything_else_in_the_file(tmp_path):
    target = tmp_path / ".claude.json"
    target.write_text(json.dumps({
        "oauthAccount": {"email": "x@y"},
        "hasCompletedOnboarding": True,
        "projects": {
            "/old/hub": {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]},
            "/untrusted": {"hasTrustDialogAccepted": False},
        },
    }))

    seed_trust(["/new/hub"], home=tmp_path, config_dir="")

    d = state(target)
    assert d["oauthAccount"] == {"email": "x@y"}
    assert d["hasCompletedOnboarding"] is True
    assert d["projects"]["/old/hub"] == {
        "hasTrustDialogAccepted": True, "allowedTools": ["Bash"]
    }, "existing entries keep their other keys"
    assert d["projects"]["/untrusted"] == {"hasTrustDialogAccepted": False}, \
        "a path the operator left untrusted is never touched"
    assert d["projects"]["/new/hub"] == {"hasTrustDialogAccepted": True}


def test_seed_is_idempotent_and_does_not_rewrite_a_complete_file(tmp_path):
    target = tmp_path / ".claude.json"
    seed_trust(["/hub", "/hub/main"], home=tmp_path, config_dir="")
    before = target.read_text()
    mtime = os.stat(target).st_mtime_ns
    os.utime(target, ns=(mtime - 10_000_000_000, mtime - 10_000_000_000))
    stamped = os.stat(target).st_mtime_ns

    changed = seed_trust(["/hub", "/hub/main"], home=tmp_path, config_dir="")

    assert changed == [], "nothing new to trust -> nothing written"
    assert target.read_text() == before
    assert os.stat(target).st_mtime_ns == stamped, "the file was not rewritten"


def test_seed_flips_a_false_entry_to_true(tmp_path):
    target = tmp_path / ".claude.json"
    target.write_text(json.dumps({"projects": {"/hub": {"hasTrustDialogAccepted": False}}}))
    changed = seed_trust(["/hub"], home=tmp_path, config_dir="")
    assert changed == [target]
    assert trusted(target) == {"/hub"}


def test_seed_with_no_paths_touches_nothing(tmp_path):
    assert seed_trust([], home=tmp_path, config_dir="") == []
    assert not (tmp_path / ".claude.json").exists()


def test_seed_de_duplicates_its_input(tmp_path):
    target = tmp_path / ".claude.json"
    seed_trust(["/hub", Path("/hub"), "/hub"], home=tmp_path, config_dir="")
    assert list(state(target)["projects"]) == ["/hub"]


def test_seed_repairs_a_non_object_projects_key(tmp_path):
    target = tmp_path / ".claude.json"
    target.write_text(json.dumps({"projects": []}))
    seed_trust(["/hub"], home=tmp_path, config_dir="")
    assert trusted(target) == {"/hub"}


def test_seed_refuses_to_replace_an_unparseable_file(tmp_path, caplog):
    target = tmp_path / ".claude.json"
    target.write_text("{not json")

    with caplog.at_level("WARNING", logger=trust.log.name):
        changed = seed_trust(["/hub"], home=tmp_path, config_dir="")

    assert changed == []
    assert target.read_text() == "{not json", "a corrupt state file is never overwritten"
    assert any("could not read" in r.message and "/hub" in r.message
               for r in caplog.records if r.levelname == "WARNING")


def test_seed_write_failure_is_a_warning_not_an_exception(tmp_path, caplog, monkeypatch):
    def boom(path, data, expected):
        raise OSError("read-only")
    monkeypatch.setattr(trust, "_write_atomic", boom)

    with caplog.at_level("WARNING", logger=trust.log.name):
        changed = seed_trust(["/hub"], home=tmp_path, config_dir="")

    assert changed == []
    assert any("could not write" in r.message for r in caplog.records)


def test_seed_write_is_atomic(tmp_path):
    """The temp file is renamed over the target, so a reader never sees a
    half-written state file; and no temp file is left behind."""
    seed_trust(["/hub"], home=tmp_path, config_dir="")
    assert sorted(p.name for p in tmp_path.iterdir()) == [".claude.json"]


def _claude_rewrite(path: Path, **extra):
    """What Claude Code's own full rewrite of the state file looks like:
    the whole document replaced, carrying its OWN view of `projects`."""
    doc = {"projects": {"/theirs": {"hasTrustDialogAccepted": True}}}
    doc.update(extra)
    path.write_text(json.dumps(doc))


def test_a_writer_between_the_load_and_the_rename_is_not_clobbered(
    tmp_path, monkeypatch, caplog
):
    """The rename protects readers, not the other writer. A claude process
    that rewrites the file after the merge loaded it and before the rename
    must keep its update: the compare-and-swap refuses the stale write and
    the merge starts over on the newer file."""
    target = tmp_path / ".claude.json"
    target.write_text(json.dumps({"projects": {"/old": {"hasTrustDialogAccepted": True}}}))
    real = trust._write_atomic
    calls = []

    def racing(path, data, expected):
        calls.append(path)
        if len(calls) == 1:
            _claude_rewrite(path, oauthAccount={"emailAddress": "op@example.com"})
        return real(path, data, expected)
    monkeypatch.setattr(trust, "_write_atomic", racing)

    with caplog.at_level("INFO", logger=trust.log.name):
        changed = seed_trust(["/hub"], home=tmp_path, config_dir="")

    assert changed == [target]
    assert len(calls) == 2, "one refused swap, one landed"
    assert trusted(target) == {"/theirs", "/hub"}, "their entries AND ours"
    assert state(target)["oauthAccount"] == {"emailAddress": "op@example.com"}, \
        "the login the other writer persisted survives"
    assert not any(r.levelname == "WARNING" for r in caplog.records)
    assert any("changed underneath the merge" in r.message for r in caplog.records)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".claude.json"], \
        "the refused temp file is removed"


def test_a_writer_right_after_the_rename_that_dropped_our_entry_is_merged_over(
    tmp_path, monkeypatch, caplog
):
    """The other hazard: a claude rewrite landing right AFTER our rename
    discards our entries. The re-read after the rename notices and the
    merge runs again on top of the newer file."""
    target = tmp_path / ".claude.json"
    real = trust._write_atomic
    calls = []

    def racing(path, data, expected):
        landed = real(path, data, expected)
        calls.append(landed)
        if len(calls) == 1:
            _claude_rewrite(path)
        return landed
    monkeypatch.setattr(trust, "_write_atomic", racing)

    with caplog.at_level("INFO", logger=trust.log.name):
        changed = seed_trust(["/hub"], home=tmp_path, config_dir="")

    assert changed == [target]
    assert calls == [True, True]
    assert trusted(target) == {"/theirs", "/hub"}
    assert not any(r.levelname == "WARNING" for r in caplog.records)
    assert any("lost /hub" in r.message for r in caplog.records)


def test_a_writer_that_keeps_winning_is_one_warning_not_a_loop(
    tmp_path, monkeypatch, caplog
):
    """Bounded: a file that is rewritten after every one of our renames
    gets SEED_TRUST_ATTEMPTS merges, then one WARNING -- never an
    exception, never a spin."""
    target = tmp_path / ".claude.json"
    real = trust._write_atomic
    calls = []

    def always_losing(path, data, expected):
        landed = real(path, data, expected)
        calls.append(landed)
        _claude_rewrite(path)
        return landed
    monkeypatch.setattr(trust, "_write_atomic", always_losing)

    with caplog.at_level("WARNING", logger=trust.log.name):
        changed = seed_trust(["/hub"], home=tmp_path, config_dir="")

    assert changed == [target], "our writes did land, they just did not survive"
    assert calls == [True] * SEED_TRUST_ATTEMPTS
    gave_up = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(gave_up) == 1 and "gave up" in gave_up[0].message


def test_one_unwritable_target_does_not_stop_the_other(tmp_path, caplog):
    home, ccdir = tmp_path / "h", tmp_path / "c"
    home.mkdir()
    (home / ".claude.json").write_text("{corrupt")

    with caplog.at_level("WARNING", logger=trust.log.name):
        changed = seed_trust(["/hub"], home=home, config_dir=ccdir)

    assert changed == [ccdir / ".claude.json"]
    assert trusted(ccdir / ".claude.json") == {"/hub"}


# -- the derived-repos record ------------------------------------------------


def test_write_derived_repos_is_one_repo_per_line(tmp_path):
    """The file body IS the contract: the entrypoint's 3a seeding parses it
    inline (one `owner/repo` per line, case kept; `tests-entrypoint-config.sh`
    section 1d boots that reader against a file written in this shape)."""
    written = write_derived_repos(tmp_path, ["acme/widgets", "Acme/Gadgets"])
    assert written == tmp_path / DERIVED_REPOS_FILENAME
    assert written.read_text() == "acme/widgets\nAcme/Gadgets\n"


def test_write_derived_repos_skips_an_unchanged_file(tmp_path):
    write_derived_repos(tmp_path, ["acme/widgets"])
    assert write_derived_repos(tmp_path, ["acme/widgets"]) is None


def test_the_derived_repos_file_honours_the_env_override(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere" / "repos.txt"
    monkeypatch.setenv("ALISSA_DERIVED_REPOS_FILE", str(elsewhere))
    assert write_derived_repos(tmp_path / "root", ["acme/widgets"]) == elsewhere
    assert elsewhere.read_text() == "acme/widgets\n"
    assert not (tmp_path / "root" / DERIVED_REPOS_FILENAME).exists()


def test_write_derived_repos_failure_is_a_warning(tmp_path, caplog, monkeypatch):
    def boom(src, dst):
        raise OSError("read-only")
    monkeypatch.setattr(trust.os, "replace", boom)
    with caplog.at_level("WARNING", logger=trust.log.name):
        assert write_derived_repos(tmp_path, ["acme/widgets"]) is None
    assert any("could not record the derived allowlist" in r.message for r in caplog.records)


# -- the pane classifier -------------------------------------------------------
#
# The gate's words are quoted by this repository's README, CHANGELOG,
# `trust.py` and issue #136, so "the words appear" cannot be the test (devloop
# PR #124 review round 1): a session that cats any of them has them on screen
# while at work, and a match kills the session. The classifier asks for the
# gate's SHAPE -- the accept option among the last lines, only the gate's
# chrome below it, the question STRICTLY above it (PR #137 review round 1:
# the option line itself never corroborates) -- and these fixtures (devloop
# PR #124's, verbatim: boxed, numbered, accept-first, footer variants) pin
# both sides.

TRUST_DIALOG = """\
 Quick safety check: Is this a project you created or one you trust?

 ❯ No, exit
   Yes, I trust this folder
"""

BYPASS_GATE = """\
 WARNING: Claude Code running in Bypass Permissions mode
 ❯ No, exit
   Yes, I accept
"""

# The same gates as other Claude Code versions draw them: boxed, numbered,
# accept option first, a confirm footer, trailing blank lines.
TRUST_DIALOG_BOXED = """\
╭──────────────────────────────────────────────────────────────────╮
│ Quick safety check: Is this a project you created or one you     │
│ trust?                                                           │
│                                                                  │
│ ❯ 1. Yes, I trust this folder                                    │
│   2. No, exit                                                    │
│                                                                  │
│ Enter to confirm · Esc to exit                                   │
╰──────────────────────────────────────────────────────────────────╯


"""

BYPASS_GATE_NUMBERED = """\
 WARNING: Claude Code running in Bypass Permissions mode

 In Bypass Permissions mode, Claude Code will not ask for your approval
 before running potentially dangerous commands.

   1) No, exit
 ❯ 2) Yes, I accept
 Enter to confirm · Esc to cancel
"""

# A gate at the bottom of a pane that still shows earlier output above it
# (the capture is 40 lines; the session printed before it was gated).
EARLIER_OUTPUT = "".join(f"line {i} of an earlier command's output\n" for i in range(30))

# What a session AT WORK or IDLE shows after it printed the gate's words:
# none of them is the gate.
README_QUOTE = """\
● Bash(sed -n 490,512p README.md)
  ⎿  #### Sitting on the first-run dialog (`wedged:first-run-dialog`)
     **"trust this folder?"** dialog (and its one-time bypass-permissions gate),
     `bypass permissions mode`, classifies the round `wedged:first-run-dialog`
     ❯ No, exit
     Yes, I trust this folder
"""
WORKING_PANE = README_QUOTE + "\n⠋ Reading files… (esc to interrupt)\n"
IDLE_PANE = README_QUOTE + """\
╭──────────────────────────────────────────────────────────────────╮
│ >                                                                │
╰──────────────────────────────────────────────────────────────────╯
  ? for shortcuts
"""
ISSUE_BODY_PANE = """\
● Bash(gh issue view 136 --json body --jq .body)
  ⎿  the first review session on a repo whose hub did not exist at boot starts
     Claude Code in an untrusted directory and parks on the first-run dialog — *"Quick
     safety check: Is this a project you created or one you trust? … ❯ No, exit /
     Yes, I trust this folder"*
     …
> review the diff
"""
DIFF_PANE = """\
● Bash(git diff)
  ⎿  +FIRST_RUN_DIALOG_MARKERS = (
     +    "trust this folder",
     +    "bypass permissions mode",
     +)
     +FIRST_RUN_DIALOG_OPTIONS = (
     +    "yes, i trust this folder",
     +    "yes, i accept",
     +)
"""
TOOL_CALL_AFTER_THE_GATE = TRUST_DIALOG + "● Bash(cat README.md)\n  ⎿  # revloop\n"
SHELL_PROMPT_AFTER_CLAUDE_EXITED = TRUST_DIALOG + "alissa@box:~/hub/main$ \n"
GATE_TOO_FAR_UP = TRUST_DIALOG + "".join(f"progress {i}\n" for i in range(6))
QUESTION_WITHOUT_OPTIONS = "quick safety check: is this a project you created or one you trust?\n"
ACCEPT_WITHOUT_ITS_QUESTION = " ❯ No, exit\n   Yes, I accept\n"
# The accept option alone -- what a pane echoing the option (a `send-keys`
# transcript, a grep hit) shows: it contains the words `trust this folder`
# and it is NOT a gate, because no question stands above it.
TRUST_ACCEPT_WITHOUT_ITS_QUESTION = " ❯ No, exit\n   Yes, I trust this folder\n"
BARE_ACCEPT_LINE = "YES, I TRUST THIS FOLDER"
# The older trust dialog wording, which a pinned Claude Code may still draw.
OLDER_TRUST_DIALOG = """\
 Do you trust the files in this folder?

 ❯ Yes, proceed
   Yes, I trust this folder
   No, exit
"""


@pytest.mark.parametrize("pane", [
    TRUST_DIALOG,
    BYPASS_GATE,
    OLDER_TRUST_DIALOG,
    TRUST_DIALOG_BOXED,
    BYPASS_GATE_NUMBERED,
    EARLIER_OUTPUT + TRUST_DIALOG,
    EARLIER_OUTPUT + BYPASS_GATE_NUMBERED,
])
def test_the_first_run_gates_are_recognised(pane):
    assert pane_shows_first_run_dialog(pane)


@pytest.mark.parametrize("pane", [
    "",
    "Reviewing acme/widgets#7 …\n⠋ Reading files",
    "Error: OAuth token has expired. Please run /login",
    "You've hit your usage limit",
    "Allow Bash(rm -rf build)? (y/n)",
])
def test_other_panes_including_other_wedges_are_not_the_dialog(pane):
    """Only the first-run gates are classified; the other alive-but-idle
    causes keep the existing not-respawning defer."""
    assert not pane_shows_first_run_dialog(pane)


@pytest.mark.parametrize("pane", [
    WORKING_PANE,
    IDLE_PANE,
    ISSUE_BODY_PANE,
    DIFF_PANE,
    TOOL_CALL_AFTER_THE_GATE,
    SHELL_PROMPT_AFTER_CLAUDE_EXITED,
    GATE_TOO_FAR_UP,
    QUESTION_WITHOUT_OPTIONS,
    ACCEPT_WITHOUT_ITS_QUESTION,
    TRUST_ACCEPT_WITHOUT_ITS_QUESTION,
    BARE_ACCEPT_LINE,
])
def test_a_pane_that_merely_mentions_a_gate_is_not_the_gate(pane):
    """A reviewer that cats this README or issue #136, greps the tree or
    shows the PR's diff has the gate's words on screen and is NOT parked on
    a gate: something of its own follows the words (a spinner, a tool
    call, the input prompt, a shell prompt), or the words are not shaped
    like a gate at all. Killing such a session would double the round."""
    assert not pane_shows_first_run_dialog(pane)


def test_the_option_line_never_corroborates_itself():
    """PR #137 review round 1: the third leg reads STRICTLY above the option
    line, so the words the accept option carries (`trust this folder`)
    cannot stand in for the gate's question. The same pane with the question
    one line up is the gate."""
    assert not pane_shows_first_run_dialog(TRUST_ACCEPT_WITHOUT_ITS_QUESTION)
    assert pane_shows_first_run_dialog(
        "Quick safety check: Is this a project you created or one you trust?\n"
        + TRUST_ACCEPT_WITHOUT_ITS_QUESTION
    )
