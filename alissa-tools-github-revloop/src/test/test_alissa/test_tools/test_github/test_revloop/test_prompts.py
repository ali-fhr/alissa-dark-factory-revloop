"""The prompt responder's pure half (issue #138; devloop #127's suite on the
reviewer seat -- the fixtures are devloop's verbatim, the "own worktree" is
the reviewer's REVIEW-<task> checkout): the classifier over fixture panes --
every signature, a working pane, an idle pane, an empty pane, ANSI noise,
a scrollback that merely quotes a dialog -- the policy
table row by row (the inside/outside-worktree split, the unknown-dialog
ladder, the answer cap, the account-level kinds), the key allowlist, and
the scrubber."""

from __future__ import annotations

import pytest

from alissa.tools.github.revloop import prompts
from alissa.tools.github.revloop.alissa import (
    capture_pane_argv,
    check_session_name,
    send_keys_argv,
    tmux_target,
)
from alissa.tools.github.revloop.prompts import (
    ACCOUNT_HOLD_SECONDS,
    ACCOUNT_KINDS,
    ALLOWED_KEYS,
    KEY_ENTER,
    KEY_ESCAPE,
    KIND_DANGEROUS_RM,
    KIND_LOGIN_EXPIRED,
    KIND_OUT_OF_CREDITS,
    KIND_PERMISSION,
    KIND_RESUME_PICKER,
    KIND_TRUST,
    KIND_UNKNOWN_DIALOG,
    KIND_USAGE_LIMIT,
    PromptFinding,
    PromptOption,
    SessionContext,
    VERB_ACCEPT,
    VERB_DECLINE,
    VERB_ESCAPE,
    VERB_KILL,
    VERB_PAGE,
    VERB_WAIT,
    accept_keys,
    classify,
    decide,
    decline_keys,
    inside,
    keys_for,
    normalise_target,
    pane_hash,
    relative_to_hub,
    scrub,
)

WORKTREE = "/workspace/studio/REVIEW-TASK-500"
HUB = "/workspace/studio"

RM_PANE = f"""
● Bash(rm -rf sales/shots/*)
  ⎿  Dangerous rm operation on statically-unresolvable target: {WORKTREE}/sales/shots/*

╭──────────────────────────────────────────────────────────╮
│ Do you want to proceed?                                  │
│ ❯ 1. Yes                                                 │
│   2. No, and tell Claude what to do differently (esc)    │
╰──────────────────────────────────────────────────────────╯
"""

PERMISSION_PANE = """
● Bash(git push --force origin main)
╭──────────────────────────────────────────────────────────╮
│ Do you want to proceed?                                  │
│ ❯ 1. Yes                                                 │
│   2. Yes, and don't ask again for git push in this dir   │
│   3. No, and tell Claude what to do differently (esc)    │
╰──────────────────────────────────────────────────────────╯
"""

TRUST_PANE = """
╭──────────────────────────────────────────────────────────╮
│ Do you trust the files in this folder?                   │
│                                                          │
│ /workspace/studio                                        │
│                                                          │
│ ❯ 1. Yes, proceed                                        │
│   2. No, exit                                            │
│                                                          │
│ Enter to confirm · Esc to exit                           │
╰──────────────────────────────────────────────────────────╯
"""

TRUST_ELSEWHERE_PANE = TRUST_PANE.replace("/workspace/studio", "/home/alissa/junk")

BYPASS_PANE = """
 WARNING: Claude Code running in Bypass Permissions mode

   1. No, exit
 ❯ 2. Yes, I accept

 Enter to confirm · Esc to exit
"""

RESUME_PANE = """
 Resume Session

 ❯ 1. 2 hours ago  fix the widget cache
   2. 1 day ago    triage issue 12

 ↑/↓ to select · Enter to confirm · Esc to cancel
"""

LOGIN_PANE = "\n  Login expired · Please run /login\n\n❯ \n"
LIMIT_PANE = "\n You've hit your weekly limit · resets Sep 22 at 3pm\n\n❯ \n"
CREDITS_PANE = "\n You're out of usage credits. Add more at console.anthropic.com\n\n❯ \n"
WORKING_PANE = "● Bash(pytest -q)\n  ⎿  Running…\n\n✻ Thinking… (esc to interrupt)\n"
SPINNER_PANE = "● Reading loop.py\n\n⠹ Crunching…\n"
IDLE_PANE = "● Done.\n\n╭───────╮\n│ ❯     │\n╰───────╯\n  ? for shortcuts\n"
UNKNOWN_PANE = "\n Something new asks a question?\n ❯ 1. Option A\n   2. Option B\n Esc to cancel\n"
ANSI_PANE = (
    "\x1b[1m\x1b[32m● Bash(rm -rf ../x)\x1b[0m\n"
    "  ⎿  \x1b[31mDangerous rm operation on statically-unresolvable target: ../x\x1b[0m\n\n"
    "│ Do you want to proceed?\n│ \x1b[36m❯ 1. Yes\x1b[0m\n"
    "│   2. No, and tell Claude what to do differently (esc)\n"
)
# A dialog that was answered, then more work: the words are in scrollback
# and the pane is idle at its prompt.
SCROLLBACK_PANE = RM_PANE + "\n● Bash(ls)\n  ⎿  a b c\n\n╭───────╮\n│ ❯     │\n╰───────╯\n"
# This repo's own README quoted mid-`cat`: the signature is on screen but
# the tail is the cat's output, not a dialog.
QUOTED_PANE = (
    "● Bash(cat README.md)\n"
    "  ⎿  `Dangerous rm operation on statically-unresolvable target: <path>`\n"
    "     the daemon answers it within one poll ...\n"
    "     more README text\n"
)
IDLE_BOX = "\n╭───────╮\n│ ❯     │\n╰───────╯\n  ? for shortcuts\n"
# The account-level kinds, QUOTED (PR #128 review round 1): a worker that
# read this README, grepped the classifier or merely talked about /login
# has the phrases on screen at rest, and none of these panes is a notice.
QUOTED_LOGIN_PANE = (
    "● Read(README.md)\n"
    "  ⎿  ... Login expired · Please run /login, the weekly\n"
    "     limit, out of usage credits) are paged, never answered with a key.\n"
    + IDLE_BOX
)
QUOTED_CREDITS_PANE = (
    '● Bash(grep -n "out of usage credits" prompts.py)\n'
    "  ⎿  112: CREDITS_PANE = \"You're out of usage credits. Add more\"\n"
    + IDLE_BOX
)
QUOTED_LIMIT_PANE = (
    "● Bash(cat CHANGELOG.md)\n"
    "  ⎿  `You've hit your weekly limit`, `You're out of usage credits` → NO keys\n"
    + IDLE_BOX
)
PROSE_LOGIN_PANE = (
    "● I checked the auth path; if the token expires you must run /login again.\n"
    + IDLE_BOX
)
# A worker's own turn text that STARTS with the phrase, at the bottom: a
# `●` row is a tool call or a turn, never a banner Claude Code drew.
TURN_LIMIT_PANE = "● Usage limit reached is the string the test pins.\n" + IDLE_BOX
# The banner was shown, then the session worked on (the account came
# back): the words are in scrollback behind ordinary output.
SCROLLBACK_LOGIN_PANE = LOGIN_PANE + "\n● Bash(ls)\n  ⎿  a b c\n" + IDLE_BOX
SCROLLBACK_LIMIT_PANE = LIMIT_PANE + "\n● Bash(ls)\n  ⎿  a b c\n" + IDLE_BOX
SCROLLBACK_CREDITS_PANE = CREDITS_PANE + "\n● Bash(ls)\n  ⎿  a b c\n" + IDLE_BOX
# The real shapes, with Claude Code's boxed input prompt and hint line
# under the banner instead of a bare `❯`.
BOXED_LOGIN_PANE = "● Done.\n\n Login expired · Please run /login\n" + IDLE_BOX
BOXED_CREDITS_PANE = (
    "● Done.\n\n You're out of usage credits.\n Add more at console.anthropic.com\n" + IDLE_BOX
)
# The reviewer's trust repro: a `/workspace/...` path in scrollback ABOVE
# the dialog, which names another folder. The gate must read its own line.
TRUST_SCROLLBACK_PANE = """
● Bash(cat /workspace/notes.txt)
  ⎿  /workspace/alissa-dark-factory-devloop/main
╭──────────────────────────────────────────────────────────╮
│ Do you trust the files in this folder?                   │
│                                                          │
│ /srv/untrusted-checkout                                  │
│                                                          │
│ ❯ 1. Yes, proceed                                        │
│   2. No, exit                                            │
╰──────────────────────────────────────────────────────────╯
"""
TRUST_NO_PATH_LINE_PANE = TRUST_PANE.replace(
    "│ /workspace/studio                                        │",
    "│ Claude Code may read files in /workspace/studio          │",
)


# -- the classifier -----------------------------------------------------


@pytest.mark.parametrize("pane,kind", [
    (RM_PANE, KIND_DANGEROUS_RM),
    (PERMISSION_PANE, KIND_PERMISSION),
    (TRUST_PANE, KIND_TRUST),
    (BYPASS_PANE, KIND_TRUST),
    (RESUME_PANE, KIND_RESUME_PICKER),
    (LOGIN_PANE, KIND_LOGIN_EXPIRED),
    (LIMIT_PANE, KIND_USAGE_LIMIT),
    (CREDITS_PANE, KIND_OUT_OF_CREDITS),
    (UNKNOWN_PANE, KIND_UNKNOWN_DIALOG),
    (ANSI_PANE, KIND_DANGEROUS_RM),
    (BOXED_LOGIN_PANE, KIND_LOGIN_EXPIRED),
    (BOXED_CREDITS_PANE, KIND_OUT_OF_CREDITS),
    (TRUST_SCROLLBACK_PANE, KIND_TRUST),
])
def test_each_signature_classifies(pane, kind):
    finding = classify(pane)
    assert finding is not None and finding.kind == kind


@pytest.mark.parametrize("pane", [
    WORKING_PANE, SPINNER_PANE, IDLE_PANE, "", "   \n\n", SCROLLBACK_PANE, QUOTED_PANE,
    QUOTED_LOGIN_PANE, QUOTED_CREDITS_PANE, QUOTED_LIMIT_PANE, PROSE_LOGIN_PANE,
    TURN_LIMIT_PANE, SCROLLBACK_LOGIN_PANE, SCROLLBACK_LIMIT_PANE, SCROLLBACK_CREDITS_PANE,
])
def test_working_idle_empty_and_quoting_panes_are_not_prompts(pane):
    """A spinner or "esc to interrupt" in the tail is WORKING; the bare
    input prompt is idle; an empty capture is nothing; a dialog in the
    SCROLLBACK behind an idle prompt was answered; a README that quotes the
    signature is a cat, not a dialog -- and the same for the three
    account-level banners, whose spawn hold is fleet-wide: quoted in a
    tool-output row, embedded in a sentence, grepped, started by a `●`
    turn, or left in scrollback behind later work, none is a notice."""
    assert classify(pane) is None


def test_an_account_banner_must_be_the_last_thing_on_screen():
    """The same words classify only when nothing but the input prompt
    and chrome sits below them: one line of ordinary output under the
    banner, and the pane is a working session that once saw it."""
    assert classify(LOGIN_PANE).kind == KIND_LOGIN_EXPIRED
    below = LOGIN_PANE.rstrip("❯ \n") + "\n\n● Fetching the next issue\n❯ \n"
    assert classify(below) is None
    boxed = BOXED_LOGIN_PANE.replace(" Login expired", "● Login expired")
    assert classify(boxed) is None, "a tool row is never a banner"
    continuation = (
        "● Bash(cat README.md)\n"
        "  ⎿  | kind | signature |\n"
        "     | login | Login expired |\n"
        "     | limit | You've hit your weekly limit |\n"
        "     | credits | You're out of usage credits |\n"
        "     You're out of usage credits is the third.\n"
        + IDLE_BOX
    )
    assert classify(continuation) is None, "an indented continuation line is still tool output"


def test_dangerous_rm_carries_the_target_and_the_options():
    finding = classify(RM_PANE)
    assert finding.target == f"{WORKTREE}/sales/shots/*"
    assert [o.number for o in finding.options] == [1, 2]
    assert finding.selected.label == "Yes"
    assert finding.options[1].label.startswith("No")
    assert finding.signature.startswith("Dangerous rm operation")
    assert len(finding.excerpt) <= 3


def test_ansi_noise_is_stripped_from_target_signature_and_excerpt():
    finding = classify(ANSI_PANE)
    assert finding.target == "../x"
    assert "\x1b" not in finding.signature
    assert all("\x1b" not in line for line in finding.excerpt)


def test_trust_dialog_names_its_path_and_the_bypass_gate_has_none():
    assert classify(TRUST_PANE).target == "/workspace/studio"
    assert classify(TRUST_ELSEWHERE_PANE).target == "/home/alissa/junk"
    assert classify(BYPASS_PANE).target is None


def test_trust_path_is_read_from_the_dialog_body_never_from_scrollback():
    """PR #128 review round 1: a `/workspace/...` path above the question
    is the worker's own output; the folder being trusted is the path line
    between the question and its options, and only a line that IS a
    path counts."""
    finding = classify(TRUST_SCROLLBACK_PANE)
    assert finding.target == "/srv/untrusted-checkout"
    action = decide(finding, SessionContext(session="s"))
    assert action.verb == VERB_DECLINE and action.reason == "path not under /workspace"
    assert classify(TRUST_NO_PATH_LINE_PANE).target is None, "a path mid-sentence is not the path line"
    assert decide(classify(TRUST_NO_PATH_LINE_PANE), SessionContext(session="s")).verb == VERB_DECLINE


def test_account_kinds_are_banners_with_no_options_and_not_answerable():
    for pane in (LOGIN_PANE, LIMIT_PANE, CREDITS_PANE):
        finding = classify(pane)
        assert finding.options == ()
        assert finding.answerable is False
        assert finding.kind in ACCOUNT_KINDS


def test_pane_hash_is_stable_across_redraw_and_moves_with_the_dialog():
    assert pane_hash(RM_PANE) == pane_hash(RM_PANE + "\n\n")
    assert pane_hash(RM_PANE) == classify(RM_PANE).pane_hash
    assert pane_hash(RM_PANE) != pane_hash(PERMISSION_PANE)
    assert pane_hash(RM_PANE) != pane_hash(SCROLLBACK_PANE)


# -- keys ---------------------------------------------------------------


def test_accept_and_decline_resolve_against_the_dialog_on_screen():
    rm = classify(RM_PANE)
    assert accept_keys(rm) == (KEY_ENTER,), "Yes is preselected"
    assert decline_keys(rm) == ("2", KEY_ENTER), "the numbered No"
    perm = classify(PERMISSION_PANE)
    assert decline_keys(perm) == ("3", KEY_ENTER), (
        "on a 3-option prompt `2` would be 'Yes, and don't ask again'"
    )
    bypass = classify(BYPASS_PANE)
    assert accept_keys(bypass) == (KEY_ENTER,), "the cursor already sits on Yes"
    bypass_on_no = classify(BYPASS_PANE.replace("   1. No, exit", " ❯ 1. No, exit")
                            .replace(" ❯ 2. Yes, I accept", "   2. Yes, I accept"))
    assert accept_keys(bypass_on_no) == ("2", KEY_ENTER), "Yes is option 2, not selected"
    assert decline_keys(bypass_on_no) == (KEY_ENTER,), "the cursor sits on No"
    unknown = classify(UNKNOWN_PANE)
    assert decline_keys(unknown) == (KEY_ESCAPE,), "no No option: Escape"
    assert keys_for(rm, VERB_ESCAPE) == (KEY_ESCAPE,)
    assert keys_for(rm, VERB_ACCEPT) == accept_keys(rm)
    assert keys_for(rm, VERB_DECLINE) == decline_keys(rm)
    with pytest.raises(ValueError):
        keys_for(rm, "type")


def test_every_key_the_policy_can_emit_is_on_the_allowlist():
    """The property the whole lane rests on: no path through the policy
    can produce a key `send_keys` would refuse."""
    findings = [classify(p) for p in (RM_PANE, PERMISSION_PANE, TRUST_PANE, BYPASS_PANE,
                                      RESUME_PANE, UNKNOWN_PANE, ANSI_PANE)]
    contexts = [
        SessionContext("s", hub=HUB, worktree=WORKTREE),
        SessionContext("s", hub=HUB, worktree=None),
        SessionContext("s", hub=HUB, worktree=WORKTREE, sightings=2),
        SessionContext("s", hub=HUB, worktree=WORKTREE, sightings=3, waiting_for=9999),
    ]
    for finding in findings:
        for ctx in contexts:
            action = decide(finding, ctx)
            assert set(action.keys) <= ALLOWED_KEYS, (finding.kind, action)
        for verb in prompts.CONSOLE_VERBS:
            assert set(keys_for(finding, verb)) <= ALLOWED_KEYS


def test_send_keys_argv_refuses_anything_off_the_allowlist():
    assert send_keys_argv("review-widgets-pr7-r1-abcdef", ("2", "Enter"))[-4:] == [
        "-t", "=ali-review-widgets-pr7-r1-abcdef:", "2", "Enter",
    ]
    for bad in (("y",), ("rm -rf /",), ("Enter", "q"), (), ("enter",)):
        with pytest.raises(ValueError):
            send_keys_argv("review-widgets-pr7-r1-abcdef", bad)


def test_tmux_argv_pins_the_socket_the_exact_target_and_the_name_rule(monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", "/home/alissa/.tmux")
    argv = capture_pane_argv("review-widgets-pr7-r1-abcdef", 40)
    assert argv[:3] == ["tmux", "-S", argv[2]]
    assert argv[2].startswith("/home/alissa/.tmux/tmux-") and argv[2].endswith("/default")
    assert argv[3:] == ["capture-pane", "-p", "-t", "=ali-review-widgets-pr7-r1-abcdef:", "-S", "-40"]
    assert tmux_target("ali-x") == "=ali-x:", "an already-prefixed name is not doubled"
    assert tmux_target("review-pr-7") == "=ali-review-pr-7:", "the skill's bare shape is addressed the same way"
    for bad in ("", "-rf", "has space", "a;b", "x/../y", "x" * 201):
        with pytest.raises(ValueError):
            check_session_name(bad)


# -- the policy, row by row ---------------------------------------------


def ctx(**over):
    base = dict(session="review-widgets-pr7-r1-abcdef", hub=HUB, worktree=WORKTREE)
    base.update(over)
    return SessionContext(**base)


def rm_finding(target):
    return PromptFinding(
        kind=KIND_DANGEROUS_RM, signature="Dangerous rm operation …", target=target,
        options=(PromptOption("Yes", 1, True), PromptOption("No, and tell Claude", 2, False)),
        excerpt=(), pane_hash="h",
    )


@pytest.mark.parametrize("target", [
    f"{WORKTREE}/sales/shots/*",
    f"{WORKTREE}/build/**/*.o",
    "sales/shots/*",            # relative: resolved against the worktree
    "./dist/*.tmp",
    f"{WORKTREE}/a/../b/*",     # `..` that stays inside
])
def test_dangerous_rm_inside_the_lanes_own_worktree_is_accepted(target):
    action = decide(rm_finding(target), ctx())
    assert action.verb == VERB_ACCEPT and action.keys == (KEY_ENTER,)
    assert action.reason == "target inside worktree"


@pytest.mark.parametrize("target,why", [
    (f"{HUB}/main/x", "target outside worktree"),
    ("/tmp/x", "target outside worktree"),
    ("../x", "target outside worktree"),
    (f"{HUB}/REVIEW-TASK-501/a/*", "target outside worktree"),   # another round's checkout
    (f"{HUB}/TASK-2-Y/a/*", "target outside worktree"),   # a developer's worktree on the same hub
    (f"{WORKTREE}", "target outside worktree"),           # the worktree itself
    (f"{WORKTREE}/../REVIEW-TASK-500-copy/*", "target outside worktree"),
    ("~/x", "target unresolvable"),
    ("$HOME/x", "target unresolvable"),
    ("*", "target outside worktree"),                     # `*` alone = the worktree root
])
def test_dangerous_rm_outside_or_unresolvable_is_declined(target, why):
    action = decide(rm_finding(target), ctx())
    assert action.verb == VERB_DECLINE, (target, action)
    assert action.keys == ("2", KEY_ENTER)
    assert action.reason == why


def test_dangerous_rm_with_an_unknown_lane_worktree_is_declined():
    """The loop could not name this round's review checkout: the policy
    cannot tell it from another round's, so it declines and the reviewer
    adapts. The reason string is devloop's verbatim (one vocabulary)."""
    action = decide(rm_finding(f"{HUB}/REVIEW-TASK-500/sales/*"), ctx(worktree=None))
    assert action.verb == VERB_DECLINE and action.reason == "lane worktree unknown"
    action = decide(rm_finding("sales/*"), ctx(worktree=None))
    assert action.verb == VERB_DECLINE and action.reason == "target unresolvable"


def test_permission_prompts_are_always_declined():
    finding = classify(PERMISSION_PANE)
    action = decide(finding, ctx())
    assert action.verb == VERB_DECLINE and action.keys == ("3", KEY_ENTER)
    assert "never auto-accepted" in action.reason


def test_trust_is_accepted_under_workspace_only():
    assert decide(classify(TRUST_PANE), ctx()).verb == VERB_ACCEPT
    assert decide(classify(TRUST_ELSEWHERE_PANE), ctx()).verb == VERB_DECLINE
    assert decide(classify(BYPASS_PANE), ctx()).verb == VERB_DECLINE, "no path shown"


def test_resume_picker_is_escaped():
    action = decide(classify(RESUME_PANE), ctx())
    assert action.verb == VERB_ESCAPE and action.keys == (KEY_ESCAPE,)


@pytest.mark.parametrize("pane", [LOGIN_PANE, LIMIT_PANE, CREDITS_PANE])
def test_account_kinds_page_hold_and_send_no_keys(pane):
    action = decide(classify(pane), ctx())
    assert action.verb == VERB_PAGE
    assert action.keys == () and action.sends_keys is False
    assert action.page is True and action.hold_spawns is True


@pytest.mark.parametrize("pane", [LOGIN_PANE, LIMIT_PANE, CREDITS_PANE])
def test_account_kinds_are_not_subject_to_the_answer_cap(pane):
    """The cap counts answers; an account notice gets none, so it pages
    even on a session that already burned its answers."""
    action = decide(classify(pane), ctx(answers=99))
    assert action.verb == VERB_PAGE


@pytest.mark.parametrize("pane", [LOGIN_PANE, LIMIT_PANE, CREDITS_PANE])
def test_the_account_hold_expires_into_a_kill_on_the_same_pane(pane):
    """The hold can never outlive one pane by more than
    ACCOUNT_HOLD_SECONDS: the same banner, seen again past it, is killed
    (no page, no hold on that pass) so the respawn re-confirms. A first
    sighting never kills however old the ledger says the pane is, and
    inside the window the row keeps paging and holding."""
    finding = classify(pane)
    inside_window = decide(finding, ctx(sightings=5, waiting_for=ACCOUNT_HOLD_SECONDS - 1))
    assert inside_window.verb == VERB_PAGE and inside_window.hold_spawns is True
    expired = decide(finding, ctx(sightings=2, waiting_for=ACCOUNT_HOLD_SECONDS))
    assert expired.verb == VERB_KILL and expired.keys == ()
    assert expired.hold_spawns is False and expired.page is False
    assert "hold expires after 60 min" in expired.reason
    first = decide(finding, ctx(sightings=1, waiting_for=99999))
    assert first.verb == VERB_PAGE and first.hold_spawns is True


def test_unknown_dialog_ladder_wait_then_escape_then_kill():
    finding = classify(UNKNOWN_PANE)
    first = decide(finding, ctx(sightings=1, waiting_for=0))
    assert first.verb == VERB_WAIT and first.keys == ()
    second = decide(finding, ctx(sightings=2, waiting_for=60))
    assert second.verb == VERB_ESCAPE and second.keys == (KEY_ESCAPE,)
    third = decide(finding, ctx(sightings=3, waiting_for=120))
    assert third.verb == VERB_ESCAPE, "still inside kill_after: keep dismissing"
    last = decide(finding, ctx(sightings=12, waiting_for=601, kill_after=600))
    assert last.verb == VERB_KILL and last.keys == ()
    assert "kill after 10 min" in last.reason


def test_unknown_dialog_never_kills_on_a_first_sighting_however_old():
    finding = classify(UNKNOWN_PANE)
    action = decide(finding, ctx(sightings=1, waiting_for=99999))
    assert action.verb == VERB_WAIT


def test_the_answer_cap_kills_every_answerable_kind():
    for pane in (RM_PANE, PERMISSION_PANE, TRUST_PANE, RESUME_PANE, UNKNOWN_PANE):
        action = decide(classify(pane), ctx(answers=5, max_answers=5))
        assert action.verb == VERB_KILL and action.keys == (), pane
        assert "diverging" in action.reason
    assert decide(classify(RM_PANE), ctx(answers=4, max_answers=5)).verb == VERB_ACCEPT


# -- helpers -------------------------------------------------------------


def test_normalise_target_and_inside():
    assert normalise_target("sales/shots/*", WORKTREE) == f"{WORKTREE}/sales/shots"
    assert normalise_target("../x", WORKTREE) == f"{HUB}/x"
    assert normalise_target("/tmp/x", None) == "/tmp/x"
    assert normalise_target("'/tmp/q u/*'", None) == "/tmp/q u"
    assert normalise_target("rel/*", None) is None
    assert normalise_target("~/x", WORKTREE) is None
    assert normalise_target("$HOME/x", WORKTREE) is None
    assert normalise_target("", WORKTREE) is None
    assert inside(f"{WORKTREE}/a", WORKTREE) is True
    assert inside(WORKTREE, WORKTREE) is False
    assert inside(f"{WORKTREE}-copy/a", WORKTREE) is False, "a sibling sharing the prefix"
    assert inside(None, WORKTREE) is False and inside("/x", None) is False


def test_relative_to_hub_never_leaks_an_absolute_path():
    assert relative_to_hub(f"{WORKTREE}/sales/shots", HUB) == "REVIEW-TASK-500/sales/shots"
    assert relative_to_hub("/tmp/secret-dir/x", HUB) == "x"
    assert relative_to_hub("/etc/passwd", None) == "passwd"
    assert relative_to_hub(None, HUB) is None


@pytest.mark.parametrize("raw,leaked", [
    ("ghp_" + "a" * 36, "ghp_"),
    ("github_pat_" + "b" * 40, "github_pat_"),
    ("alissa_" + "c" * 32, "alissa_c"),
    ("sk-ant-api03-" + "d" * 40, "sk-ant"),
    ("Authorization: Bearer eyJhbGciOi.xx.yy", "eyJ"),
    ("export ALISSA_API_TOKEN=abcdef123456", "abcdef123456"),
    ("passcode: hunter22", "hunter22"),
    ('api_key="s3cr3tvalue"', "s3cr3tvalue"),
    ("xoxb-1234567890-abcdef", "xoxb-"),
    ("AKIAIOSFODNN7EXAMPLE", "AKIA"),
])
def test_scrub_redacts_credential_shapes(raw, leaked):
    out = scrub(f"line before {raw} line after")
    assert leaked not in out, out
    assert "<redacted>" in out
    assert "line before" in out and "line after" in out


def test_scrub_leaves_ordinary_text_and_paths_alone():
    text = "Dangerous rm operation on statically-unresolvable target: /workspace/x/TASK-1-A/tmp/*"
    assert scrub(text) == text
    assert scrub("token count: 12") == "token count: 12"


def test_classifier_output_is_scrubbed():
    pane = "  ⎿  echo ghp_" + "z" * 36 + "\n" + UNKNOWN_PANE
    finding = classify(pane)
    assert all("ghp_" not in line for line in finding.excerpt)
    assert "ghp_" not in finding.signature


def test_kinds_and_verbs_vocabulary_is_pinned():
    """The vocabulary Studio, the console and the activity lines share."""
    assert prompts.KINDS == (
        "dangerous_rm", "permission", "trust", "resume_picker", "login_expired",
        "usage_limit", "out_of_credits", "unknown_dialog",
    )
    assert prompts.CONSOLE_VERBS == ("accept", "decline", "escape")
    assert ALLOWED_KEYS == frozenset(("Enter", "Escape", "1", "2", "3", "Down", "Up"))
