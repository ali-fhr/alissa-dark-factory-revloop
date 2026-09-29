"""The prompt responder's PURE half (issue #138 -- devloop #127's `prompts.py`
ported to the reviewer seat, kinds, verbs, keys and reasons byte-compatible):
read a reviewer session's pane, say what dialog it is parked on, and decide
-- from a table -- what to do.

Nothing here touches tmux, GitHub, the ledger or the clock. `classify`
takes pane TEXT and answers a `PromptFinding` (or None for a pane that is
working or idle); `decide` takes a finding plus the round's `SessionContext`
and answers an `Action` whose keys are drawn from `ALLOWED_KEYS` and nothing
else. The loop (`ReviewWatcher._respond_to_prompts`) and the console
(`webui.server.App.answer`) both run the same two functions, so the daemon
and an operator's bounded verb can never disagree about what a pane is.

THE SEAT DIFFERENCE, and the only one: devloop's "lane worktree" is the
developer's `<hub>/TASK-*` worktree; here `SessionContext.worktree` is the
reviewer's own REVIEW CHECKOUT -- the throwaway `<hub>/REVIEW-<task>`
worktree the alissa-code-review skill lets a reviewer that must run code
make beside `main/`. A `dangerous_rm` target inside it is `accept`; anything
else -- `main/`, the hub, another round's checkout, `/tmp`, an unresolvable
target, a session whose checkout the loop could not name -- is `decline`.
The reason strings are devloop's verbatim ("target inside worktree", "lane
worktree unknown") so the sentinel reads one vocabulary across the seats.

Why a classifier and not a hook. The shell guard (docker/claude/hooks,
same issue) stops the KNOWN prompt from appearing -- it denies the `rm`
before Claude Code can ask. This half is the cure for whatever still
appears: Claude Code's critical-path removal check prompts in EVERY mode and
no allow rule or hook can approve it, and beyond it sit the trust gate, the
resume picker, an expired login, a weekly limit, an empty credit balance,
and dialogs nobody has seen yet. A reviewer parked on any of them reads
ALIVE forever -- the stale-round probe declines to respawn over a live
session, every poll (the sentinel's corpus records
`wedge-dialog:revloop:pr808-r10`) -- and until now the only responder was a
human over `railway ssh` + `tmux send-keys`.

The signatures below are the wedge corpus -- devloop's incidents (#107,
#123, studio #1358), this seat's PR #808, and the sentinel skill's
checklist -- and each one is matched against the pane's TAIL, not its
scrollback: this repo's README, CHANGELOG and tests quote every one of these
strings, so a session that cats or diffs any of them (a reviewer reads diffs
for a living) has the words on screen while at work, and answering THAT
pane would land keystrokes in a working session. A dialog is
a question AND its options at the bottom of the screen with nothing but the
dialog's own chrome below them; the words alone are never enough. The
account-level notices are held to the same bar in their own shape: a banner
is a paragraph of its own, drawn by Claude Code (never a `●` tool call or a
`⎿` tool-output row, nor a continuation line under one), and it is the LAST
thing on screen -- only the bare input prompt and chrome below it. A `Please run /login` inside a sentence,
a `grep "out of usage credits"` command line, a README quoted mid-`cat`
match nothing, and the fleet-wide spawn hold those kinds raise is never
raised by a working session's words.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
from dataclasses import dataclass

# -- kinds ---------------------------------------------------------------

KIND_DANGEROUS_RM = "dangerous_rm"
KIND_PERMISSION = "permission"
KIND_TRUST = "trust"
KIND_RESUME_PICKER = "resume_picker"
KIND_LOGIN_EXPIRED = "login_expired"
# A 401 from the API on a reviewer's turn (issue #146, devloop #140, Studio design
# managed-dark-factory-claude-auth §2.6): the credential the container
# carries -- an env token or the persisted login -- was REFUSED. Same
# family as login_expired (an account-level notice: page, hold spawns, no
# keys) and the same loop-event signal (`auth.rejected`, see `matched`).
KIND_AUTH_REJECTED = "auth_rejected"
KIND_USAGE_LIMIT = "usage_limit"
KIND_OUT_OF_CREDITS = "out_of_credits"
KIND_UNKNOWN_DIALOG = "unknown_dialog"

KINDS = (
    KIND_DANGEROUS_RM,
    KIND_PERMISSION,
    KIND_TRUST,
    KIND_RESUME_PICKER,
    KIND_LOGIN_EXPIRED,
    KIND_AUTH_REJECTED,
    KIND_USAGE_LIMIT,
    KIND_OUT_OF_CREDITS,
    KIND_UNKNOWN_DIALOG,
)

# The account-level kinds: nothing a keystroke can fix. The policy pages the
# operator and holds NEW spawns while the condition stands, so attempts are
# not burned against a dead account.
ACCOUNT_KINDS = frozenset((
    KIND_LOGIN_EXPIRED, KIND_AUTH_REJECTED, KIND_USAGE_LIMIT, KIND_OUT_OF_CREDITS,
))

# The two kinds that ARE the `auth.rejected` loop event (issue #146, devloop #140): a
# refused credential, whichever wording Claude Code chose for it. The
# existing login_expired banner is the same signal under its old name.
AUTH_KINDS = frozenset((KIND_LOGIN_EXPIRED, KIND_AUTH_REJECTED))

# -- the auth.rejected vocabulary (design §2.6) ---------------------------

# `data.matched` -- WHICH documented wording the pane showed. A closed list,
# settled in the design before any daemon emits it; Studio's reducer reads
# the suffix, not this, so the token is for the operator's eyes.
MATCHED_API_ERROR_401 = "api_error_401"
MATCHED_INVALID_API_KEY = "invalid_api_key"
MATCHED_OAUTH_EXPIRED = "oauth_expired"
MATCHED_OAUTH_REVOKED = "oauth_revoked"
MATCHED_LOGIN_EXPIRED = "login_expired"
AUTH_MATCHED = (
    MATCHED_API_ERROR_401,
    MATCHED_INVALID_API_KEY,
    MATCHED_OAUTH_EXPIRED,
    MATCHED_OAUTH_REVOKED,
    MATCHED_LOGIN_EXPIRED,
)

# `data.source` -- where the detector read the wording: a still-open pane
# (the banner classifier) or the last lines of a pane whose `claude`
# already exited back to the shell (the first-turn death check).
SOURCE_PANE = "pane"
SOURCE_EXIT = "exit"

# -- keys ----------------------------------------------------------------

KEY_ENTER = "Enter"
KEY_ESCAPE = "Escape"
KEY_1 = "1"
KEY_2 = "2"
KEY_3 = "3"
KEY_DOWN = "Down"
KEY_UP = "Up"

# THE allowlist. `Alissa.send_keys` refuses anything outside it, so the
# responder can press a dialog's buttons and can never type text into a
# worker: no directive, no path, no `y`, nothing a pane could talk it into.
ALLOWED_KEYS = frozenset((KEY_ENTER, KEY_ESCAPE, KEY_1, KEY_2, KEY_3, KEY_DOWN, KEY_UP))

# -- verbs ---------------------------------------------------------------

VERB_ACCEPT = "accept"
VERB_DECLINE = "decline"
VERB_ESCAPE = "escape"
VERB_WAIT = "wait"
VERB_PAGE = "page"
VERB_KILL = "kill"

# The console's bounded verbs (issue #138 §3; devloop #127 §7) -- what an operator may ask
# for, never a keystroke. `accept` and `decline` resolve against the dialog
# on screen (`keys_for`); `escape` is the one-key universal "no".
CONSOLE_VERBS = (VERB_ACCEPT, VERB_DECLINE, VERB_ESCAPE)

# How many pane lines the responder reads. The trust gate is a ~12-line box,
# the permission dialogs ~8, and a session parked on one has printed nothing
# since, so 40 lines hold the whole dialog with room for the banner above.
PANE_LINES = 40

# How many non-blank lines from the bottom a dialog's OPTION line may sit at.
# Below the options a dialog draws only its own chrome (a closing box line,
# "Esc to cancel", the hint line), so a wider window would admit a working
# session whose scrollback still shows a dialog it already answered.
TAIL_LINES = 8

# How many lines an account-level BANNER may span. The notice is the last
# thing a parked session drew: a paragraph of its own above the bare input
# prompt (a blank line or chrome above it, no `●` tool call or `⎿`
# tool-output row inside it -- those, and their indented continuation
# lines, are a worker's output, never a banner), and its signature is one
# of the paragraph's last few lines.
BANNER_LINES = 3

# How many lines of pane the finding keeps around the signature (the DEBUG
# excerpt the issue allows: three lines, secrets-scrubbed, never more).
EXCERPT_LINES = 3

# How many non-blank lines from the bottom the first-turn death check reads
# (issue #146, devloop #140). A `claude` that 401s prints the error and exits; the shell
# then draws its prompt (and, on some shells, a blank line or two) under it,
# so the wording sits a few lines above the bottom -- never as the LAST
# thing on screen, which is why the banner rule cannot see it. Twelve lines
# hold the error, the JSON body Claude Code sometimes wraps it in, and the
# shell chrome, and nothing older: the scrollback above is the reviewer's own
# turn, where a quoted phrase would be a false positive.
EXIT_SCAN_LINES = 12

# The default per-kind page cadence for the account-level kinds: one
# operator page per kind per six hours through the escalation ledger.
PAGE_WINDOW_SECONDS = 6 * 3600

# How long the spawn hold an account-level notice raises may stand on ONE
# pane. A banner never changes on its own -- a real limit stays on screen
# past its reset, a misread one is an idle pane -- so a hold that lifted
# only when the notice left the screen would outlive the condition every
# time, and it refuses every spawn daemon-wide. Past this, on a second or
# later sighting of the SAME pane, the session is killed through the
# responder's ordinary kill path (that pass raises no hold) and the retry
# edge's respawn is the re-confirmation: a fresh pane showing the banner
# re-arms the hold, a working one proves the account is back. One hour
# burns at most one attempt per lane per hour against a dead account and
# stalls the fleet at most one hour on a misread pane.
ACCOUNT_HOLD_SECONDS = 3600

# -- signatures ----------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>]")
# Claude Code's spinner glyphs and the working-line hints. A pane whose tail
# carries any of these is a session AT WORK, whatever else it shows.
_WORKING_RE = re.compile(
    r"esc to interrupt|ctrl\+c to interrupt|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]|[✻✳✶✽✢·]\s+\w+…",
)
_DANGEROUS_RM_RE = re.compile(
    r"Dangerous rm operation on statically-unresolvable target:\s*(?P<target>\S.*?)\s*$"
)
# The account-level banners, ANCHORED at the start of the line: a banner is
# a line of its own, and every one of these phrases is also a fragment of
# ordinary prose (this README's, an issue's, a grep command line's) when
# it sits mid-sentence. `_banner_line` supplies the other half of the
# evidence -- the line is the last thing on screen and not a tool row.
_LOGIN_EXPIRED_RE = re.compile(
    r"^(?:Login expired\b|Please run /login\b|Not logged in\b|"
    r"Authentication (?:expired|failed|required)\b|"
    r"(?:Your )?(?:session|login|token) (?:has )?expired\b)",
    re.IGNORECASE,
)
# The 401 wordings Anthropic documents (issue #146, devloop #140; design §1.4: the Claude
# Code *Errors* page and the Platform *Authentication* page): `API Error:
# 401 …`, `Invalid authentication credentials`, `Invalid API key`, `OAuth
# token has expired`, `OAuth token revoked`. Anchored like the banners
# above (an optional error glyph is the only thing allowed before the
# wording), and read on the de-decorated line: Claude Code draws the API
# error on a `⎿` row of its own under the turn that failed, so
# `_banner_line` admits THAT row for this one family -- a `●` turn or tool
# call above it in the same paragraph still makes it a reviewer's output.
_AUTH_REJECTED_RE = re.compile(
    r"^(?:[✗✘⚠]\s*)?(?:API Error:\s*401\b|Invalid authentication credentials\b|"
    r"Invalid API key\b|OAuth token (?:has )?expired\b|"
    r"OAuth token (?:has been |was )?revoked\b)",
    re.IGNORECASE,
)
# The specific wordings, tested on an auth line AFTER `_AUTH_REJECTED_RE`
# admitted it, most specific first; a line none of them names is the bare
# 401 (`api_error_401`), which is also what `API Error: 401 Invalid
# authentication credentials` reads as -- the credentials sentence is the
# 401's own message.
_AUTH_MATCHED_RES = (
    (MATCHED_INVALID_API_KEY, re.compile(r"Invalid API key\b", re.IGNORECASE)),
    (MATCHED_OAUTH_EXPIRED, re.compile(r"OAuth token (?:has )?expired\b", re.IGNORECASE)),
    (MATCHED_OAUTH_REVOKED, re.compile(r"OAuth token (?:has been |was )?revoked\b", re.IGNORECASE)),
)
# A `⎿` tool-OUTPUT row specifically (the `●` turn row is the other half of
# `_OUTPUT_ROW_RE`): the row Claude Code draws its own API error on.
_TOOL_OUTPUT_ROW_RE = re.compile(r"^\s*⎿")
_USAGE_LIMIT_RE = re.compile(
    r"^(?:(?:You've|You have) hit your (?:(?:weekly|daily|5-hour|session|usage) )?limit\b|"
    r"(?:Weekly|Daily|Session|Usage) limit reached\b|Usage limit\b|"
    r"(?:You've|You have) reached your (?:(?:weekly|daily|5-hour|session|usage) )?limit\b)",
    re.IGNORECASE,
)
_OUT_OF_CREDITS_RE = re.compile(
    r"^(?:(?:You're|You are|You've run|You have run) out of (?:usage )?credits\b|"
    r"Out of (?:usage )?credits\b|Insufficient credits\b|"
    r"(?:Your )?credit balance is (?:too low|empty|zero)\b|"
    r"(?:Your )?(?:usage )?credits? (?:balance )?(?:is|are|has been) (?:exhausted|depleted|used up)\b)",
    re.IGNORECASE,
)
# A tool-call row (`● Bash(...)`, `● Read(...)`) or a tool-output row
# (`⎿  ...`): a worker's output, which Claude Code never uses to draw a
# notice of its own. Tested on the ANSI-stripped line, decoration intact.
_OUTPUT_ROW_RE = re.compile(r"^\s*[●⎿]")
# The row Claude Code draws for a SUBMITTED prompt (`> implement issue
# #7`) or the bare input prompt at column 0 (`❯`): the boundary between
# one reviewer turn and the next, which is what releases the lines below
# it from the `●` above (issue #146; devloop #140, its PR #141 review round 2).
_USER_PROMPT_ROW_RE = re.compile(r"^[>❯](?:\s|$)")
# The dialog's path line, as the trust gate draws it: the path and nothing
# else on the line.
_PATH_LINE_RE = re.compile(r"^/[^\s'\"]+$")
_TRUST_RE = re.compile(
    r"trust (the files in )?this folder|bypass permissions mode", re.IGNORECASE
)
_RESUME_RE = re.compile(
    r"Resume Session|Select a (session|conversation) to resume|"
    r"Choose a (session|conversation) to resume",
    re.IGNORECASE,
)
_PERMISSION_RE = re.compile(
    r"Do you want to (proceed|make this edit|create|run|allow|continue)|"
    r"Allow .* to (read|write|run|execute)\??|"
    r"Do you want to .*\?$",
    re.IGNORECASE,
)
# A dialog OPTION line: the selection cursor and/or a number, then a label.
# `❯ 1. Yes`, `2. No, and tell Claude what to do differently (esc)`,
# `❯ Yes`. A bare `❯` is Claude Code's empty input prompt, never an option.
_OPTION_RE = re.compile(
    r"^(?P<cursor>❯|>)?\s*(?:(?P<number>\d+)[.)]\s+)?(?P<label>\S[^\n]*)$"
)
# One fragment of a dialog's hint line (`Enter to confirm · Esc to exit`,
# `↑/↓ to select`): the fragments are split on the middle dot and each must
# match, so a hint line reads as chrome however Claude Code composes it.
_CHROME_FRAGMENT_RE = re.compile(
    r"^(?:esc to (cancel|exit|go back|interrupt)|enter to (confirm|select|continue)|"
    r"\(esc\)|[↑↓/ ]+ to (select|navigate)|use arrow keys.*|press enter.*|"
    r"tab to .*|space to .*|\? for shortcuts|[│┃║╭╮╰╯┌┐└┘├┤┬┴┼─━═ ]*)$",
    re.IGNORECASE,
)
_DECORATION = " \t│┃║╭╮╰╯┌┐└┘├┤┬┴┼─━═⎿"

# The secrets scrubber (devloop #127 §5): what may not leave a pane. Applied
# to every excerpt, signature and console pane line -- a worker's screen
# holds file contents it read, issue text, tokens it printed.
_SECRET_RES = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\balissa_[A-Za-z0-9]{20,}"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b[A-Za-z0-9_-]*(?:token|secret|password|passwd|passcode|api[_-]?key|"
        r"access[_-]?key|private[_-]?key)\b(\s*[=:]\s*)(?:\"[^\"]{4,}\"|'[^']{4,}'|\S{4,})"
    ),
)
REDACTED = "<redacted>"


def scrub(text: str) -> str:
    """`text` with anything that looks like a credential replaced by
    `<redacted>`. Conservative on purpose (a false positive hides a path; a
    false negative publishes a token): token prefixes, bearer headers, and
    `key=value` assignments whose key names a secret."""
    out = text
    for pattern in _SECRET_RES:
        if pattern.groups:
            out = pattern.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + m.group(1) + REDACTED, out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _plain(line: str) -> str:
    """One pane line with the ANSI, the box drawing and the cursor stripped
    from both ends -- the text a dialog line carries."""
    return strip_ansi(line).strip(_DECORATION).strip()


def _is_chrome(text: str) -> bool:
    """Whether a line is a dialog's own decoration: empty, box drawing, or
    a hint line made of the known fragments."""
    if not text:
        return True
    return all(
        _CHROME_FRAGMENT_RE.match(fragment.strip()) is not None
        for fragment in re.split(r"[·•|]", text)
    )


def _is_banner_chrome(text: str) -> bool:
    """Whether a line may sit BELOW an account banner on a parked pane: the
    dialog chrome `_is_chrome` admits, or the bare input prompt (`❯`) a
    session at rest draws under the notice."""
    return text in ("❯", ">") or _is_chrome(text)


def auth_matched(text: str) -> "str | None":
    """Which documented 401 wording this (plain) line is, as the
    `data.matched` token, or None when it is not an auth-rejected line at
    all. The login-expired banner is NOT read here (its own regex and kind
    already exist); `classify` stamps that finding `login_expired`."""
    if not _AUTH_REJECTED_RE.search(text):
        return None
    for token, pattern in _AUTH_MATCHED_RES:
        if pattern.search(text):
            return token
    return MATCHED_API_ERROR_401


def _banner_line(lines: "list[str]") -> "tuple[int, str] | None":
    """The one line an account-level banner could be, as (index into
    `lines`, plain text), or None. Bottom-up: chrome and the bare input
    prompt are skipped, then the contiguous paragraph above them is read
    up to the blank line or chrome that bounds it. A `●`/`⎿` tool row
    anywhere in that paragraph makes it a worker's output (the row and its
    indented continuation lines), never a banner -- with ONE exception: a
    `⎿` row that IS a documented 401 wording is how Claude Code draws its
    own API error (issue #146, devloop #140), so that row is admitted as a candidate,
    and a `●` turn above it in the same paragraph still voids the whole
    paragraph. Otherwise the paragraph's last BANNER_LINES lines are the
    candidates."""
    index = len(lines) - 1
    while index >= 0 and _is_banner_chrome(_plain(lines[index])):
        index -= 1
    paragraph: list[tuple[int, str]] = []
    while index >= 0:
        raw = strip_ansi(lines[index])
        text = _plain(raw)
        if _is_banner_chrome(text):
            break
        if _OUTPUT_ROW_RE.match(raw) and not (
            _TOOL_OUTPUT_ROW_RE.match(raw) and _AUTH_REJECTED_RE.search(text)
        ):
            return None
        paragraph.append((index, text))
        index -= 1
    for index, text in paragraph[:BANNER_LINES]:
        for pattern in (
            _LOGIN_EXPIRED_RE, _AUTH_REJECTED_RE, _USAGE_LIMIT_RE, _OUT_OF_CREDITS_RE,
        ):
            if pattern.search(text):
                return index, text
    return None


# -- findings ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromptOption:
    """One option line of a dialog: its number when it has one (1-based),
    its label, and whether the selection cursor sits on it."""

    label: str
    number: "int | None"
    selected: bool


@dataclass(frozen=True, slots=True)
class PromptFinding:
    """What a pane is parked on.

    `signature` is the ONE matched line (scrubbed) -- the only pane text a
    narration or a loop event may carry. `excerpt` is the three lines around
    it, scrubbed, for the DEBUG log and nothing else. `pane_hash` fingerprints
    the dialog so the loop can tell "the same pane, one poll later" from "a
    new prompt": the unknown-dialog ladder and the answered/unanswered
    re-capture both hang off it.

    `matched` and `source` are the `auth.rejected` event's vocabulary
    (issue #146, devloop #140), set on the two AUTH_KINDS only: which documented wording
    was seen (`AUTH_MATCHED`) and whether it was read off a live pane or
    the last lines of an exited one (`SOURCE_PANE` / `SOURCE_EXIT`)."""

    kind: str
    signature: str
    target: "str | None"
    options: "tuple[PromptOption, ...]"
    excerpt: "tuple[str, ...]"
    pane_hash: str
    matched: "str | None" = None
    source: str = SOURCE_PANE

    @property
    def selected(self) -> "PromptOption | None":
        for option in self.options:
            if option.selected:
                return option
        return None

    @property
    def answerable(self) -> bool:
        """Whether a keystroke is a meaningful answer at all."""
        return self.kind not in ACCOUNT_KINDS


def pane_hash(pane: str) -> str:
    """A fingerprint of the pane's non-blank, de-decorated tail -- stable
    across a redraw, sensitive to the dialog changing or going away."""
    lines = [_plain(ln) for ln in pane.splitlines()]
    body = "\n".join(t for t in lines if t)[-2000:]
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:16]


def _options_in(tail: "list[str]") -> "tuple[PromptOption, ...]":
    """The option lines in a dialog tail, in screen order."""
    found: list[PromptOption] = []
    for raw in tail:
        text = strip_ansi(raw).strip()
        if not text or text in ("❯", ">"):
            continue
        match = _OPTION_RE.match(text.strip(_DECORATION.replace("❯", "").replace(">", "")))
        if not match:
            continue
        cursor = match.group("cursor") is not None
        number = match.group("number")
        label = match.group("label").strip(_DECORATION).strip()
        if number is None and not cursor:
            continue
        if _is_chrome(label):
            continue
        found.append(PromptOption(label=label, number=int(number) if number else None, selected=cursor))
    return tuple(found)


def _tail_is_dialog(lines: "list[str]") -> "tuple[list[str], tuple[PromptOption, ...]] | None":
    """The dialog tail of a pane (the last TAIL_LINES non-blank lines) and
    its options, or None when the pane is not parked on a dialog: an option
    line must be among those lines and everything BELOW it must be the
    dialog's own chrome."""
    nonblank = [ln for ln in lines if _plain(ln)]
    tail = nonblank[-TAIL_LINES:]
    options = _options_in(tail)
    if not options:
        return None
    last_option = 0
    for index, raw in enumerate(tail):
        text = strip_ansi(raw).strip()
        if any(text.strip(_DECORATION).endswith(o.label) for o in options):
            last_option = index
    below = [_plain(ln) for ln in tail[last_option + 1:]]
    if not all(_is_chrome(t) for t in below):
        return None
    return tail, options


def _dialog_path(
    window: "list[str]", question: int, options: "tuple[PromptOption, ...]"
) -> "str | None":
    """The path a trust dialog names, read from the dialog's BODY only:
    the lines strictly between its question (`window[question]`) and its
    first option line, bottom-up, and only a line that IS a path (`/...`
    and nothing else on it, as the gate draws it). Nothing above the
    question is consulted -- scrollback routinely holds `/workspace/...`
    paths from the worker's own commands, and the rule this feeds is a
    path gate: it must test the path being trusted, not one nearby."""
    first = options[0].label if options else None
    end = len(window)
    if first is not None:
        for index in range(question + 1, len(window)):
            if _plain(window[index]).endswith(first):
                end = index
                break
    for index in range(end - 1, question, -1):
        plain = _plain(window[index])
        if _PATH_LINE_RE.match(plain):
            return plain
    return None


def _excerpt(lines: "list[str]", index: int) -> "tuple[str, ...]":
    lo = max(0, index - 1)
    hi = min(len(lines), index + EXCERPT_LINES - 1)
    return tuple(scrub(strip_ansi(ln).rstrip()) for ln in lines[lo:hi])


def _matched_for(kind: str, text: str) -> "str | None":
    """The `matched` token of a banner finding: the login-expired banner is
    reported as the same signal (`login_expired`), an auth line as the
    wording it carries, every other kind as None."""
    if kind == KIND_LOGIN_EXPIRED:
        return MATCHED_LOGIN_EXPIRED
    if kind == KIND_AUTH_REJECTED:
        return auth_matched(text)
    return None


def classify_exit(pane: str) -> "PromptFinding | None":
    """The first-turn death check's half of the classifier (issue #146, devloop #140):
    what a pane whose `claude` has already EXITED back to the shell says
    about why, or None for the ordinary "died, not auth".

    Only ever called on a pane the loop has established is a shell (the
    pane's current command is one of `alissa.SHELL_COMMANDS`, inside the
    first stale window of its round's spawn), so the banner rule --
    the wording must be the LAST thing on screen -- does not apply: the
    shell prompt is the last thing on screen, and the error sits a few
    lines above it. The last EXIT_SCAN_LINES non-blank lines are read
    bottom-up for a documented 401 wording or the login-expired banner; a
    `●` turn row is skipped, and so is EVERY line that belongs to a turn
    (`_under_a_turn`): every line below a `●` row until the next submitted
    prompt row (`>`), paragraph breaks included -- Claude Code renders one
    assistant message as blank-separated paragraphs and code blocks, and
    only the first line carries the `●`. So the reviewer's own prose
    wrapping onto the wording, a quoted error in a paragraph or code block
    of its own, a README quoted under a tool call, a `grep` for it, are
    all the reviewer's -- never Claude Code's error, which is drawn under
    the `>` prompt row (`⎿  API Error: 401 …`) or, on a start-up refusal,
    before any turn exists. The finding is an `auth_rejected` with
    `source: "exit"` and the wording under `matched` -- the same kind the
    pane detector raises, so the policy (page once, hold spawns) is the
    same row."""
    if not pane or not pane.strip():
        return None
    lines = pane.splitlines()
    indices = [i for i, ln in enumerate(lines) if _plain(ln)][-EXIT_SCAN_LINES:]
    for index in reversed(indices):
        raw = strip_ansi(lines[index])
        if _OUTPUT_ROW_RE.match(raw) and not _TOOL_OUTPUT_ROW_RE.match(raw):
            continue
        text = _plain(raw)
        matched = (
            MATCHED_LOGIN_EXPIRED if _LOGIN_EXPIRED_RE.search(text)
            else auth_matched(text)
        )
        if matched is None:
            continue
        if _under_a_turn(lines, index):
            # Any line under a `●` turn or tool call, up to the next `>`
            # prompt row, is that turn's: a `⎿` row is its output (a README
            # quoting the wording, a `grep` for it), a plain row is its own
            # prose -- wrapped onto the wording, or quoting it in a later
            # paragraph or code block. Claude Code's error is never drawn
            # under a `●` without a `>` between them.
            continue
        return PromptFinding(
            kind=KIND_AUTH_REJECTED,
            signature=scrub(text),
            target=None,
            options=(),
            excerpt=_excerpt(lines, index),
            pane_hash=pane_hash(pane),
            matched=matched,
            source=SOURCE_EXIT,
        )
    return None


def _under_a_turn(lines: "list[str]", index: int) -> bool:
    """Whether `lines[index]` belongs to a reviewer's turn: a `●` row sits
    above it, anywhere in the capture, with no submitted-prompt row
    (`_USER_PROMPT_ROW_RE`, the `>` Claude Code draws for the prompt that
    opens the NEXT turn) between them. Blank lines and chrome do NOT end
    a turn (devloop PR #141 review round 2): one assistant message is several
    blank-separated paragraphs and code blocks, `●` on its first line
    only, so the paragraph is no boundary -- only the next prompt row is.
    A `⎿` row above is the turn's output and bounds nothing either."""
    for above in range(index - 1, -1, -1):
        raw = strip_ansi(lines[above])
        if _USER_PROMPT_ROW_RE.match(raw):
            return False
        if _OUTPUT_ROW_RE.match(raw) and not _TOOL_OUTPUT_ROW_RE.match(raw):
            return True
    return False


def classify(pane: str) -> "PromptFinding | None":
    """What this pane is parked on, or None for a pane that is WORKING, idle
    at the input prompt, empty, or unreadable.

    Order matters, and it is the order of certainty: a working pane is
    never a dialog whatever its scrollback shows; the account-level notices
    are banners, not dialogs, and are recognised only as the LAST thing on
    screen (`_banner_line`: a line of its own, anchored signature, nothing
    but the bare input prompt and chrome below it, no tool row); then the
    dialogs, which all require the tail to be PARKED (options at the bottom,
    chrome below); and last the unknown dialog -- a parked tail that
    matched no signature.
    """
    if not pane or not pane.strip():
        return None
    lines = pane.splitlines()
    nonblank = [ln for ln in lines if _plain(ln)]
    if not nonblank:
        return None
    tail_text = [strip_ansi(ln) for ln in nonblank[-TAIL_LINES:]]
    if any(_WORKING_RE.search(t) for t in tail_text):
        return None
    digest = pane_hash(pane)
    window = nonblank[-(TAIL_LINES * 2):]

    banner = _banner_line(lines)
    if banner is not None:
        index, text = banner
        for kind, pattern in (
            (KIND_LOGIN_EXPIRED, _LOGIN_EXPIRED_RE),
            (KIND_AUTH_REJECTED, _AUTH_REJECTED_RE),
            (KIND_USAGE_LIMIT, _USAGE_LIMIT_RE),
            (KIND_OUT_OF_CREDITS, _OUT_OF_CREDITS_RE),
        ):
            if pattern.search(text):
                return PromptFinding(
                    kind=kind,
                    signature=scrub(text),
                    target=None,
                    options=(),
                    excerpt=_excerpt(lines, index),
                    pane_hash=digest,
                    matched=_matched_for(kind, text),
                )

    parked = _tail_is_dialog(lines)
    if parked is None:
        return None
    _tail, options = parked

    def match(pattern: "re.Pattern[str]") -> "tuple[int, str, re.Match[str]] | None":
        for index in range(len(window) - 1, -1, -1):
            text = _plain(window[index])
            found = pattern.search(text)
            if found:
                return index, text, found
        return None

    hit = match(_DANGEROUS_RM_RE)
    if hit:
        index, text, found = hit
        return PromptFinding(
            kind=KIND_DANGEROUS_RM,
            signature=scrub(text),
            target=found.group("target").strip(),
            options=options,
            excerpt=_excerpt(window, index),
            pane_hash=digest,
        )
    hit = match(_TRUST_RE)
    if hit:
        index, text, _ = hit
        return PromptFinding(
            kind=KIND_TRUST,
            signature=scrub(text),
            target=_dialog_path(window, index, options),
            options=options,
            excerpt=_excerpt(window, index),
            pane_hash=digest,
        )
    hit = match(_RESUME_RE)
    if hit:
        index, text, _ = hit
        return PromptFinding(
            kind=KIND_RESUME_PICKER,
            signature=scrub(text),
            target=None,
            options=options,
            excerpt=_excerpt(window, index),
            pane_hash=digest,
        )
    hit = match(_PERMISSION_RE)
    if hit:
        index, text, _ = hit
        return PromptFinding(
            kind=KIND_PERMISSION,
            signature=scrub(text),
            target=None,
            options=options,
            excerpt=_excerpt(window, index),
            pane_hash=digest,
        )
    # A parked dialog nobody has taught this module: its first option line
    # is the signature, the three lines around it the excerpt.
    first = options[0]
    for index in range(len(window) - 1, -1, -1):
        if _plain(window[index]).endswith(first.label):
            break
    else:
        index = len(window) - 1
    return PromptFinding(
        kind=KIND_UNKNOWN_DIALOG,
        signature=scrub(_plain(window[index])),
        target=None,
        options=options,
        excerpt=_excerpt(window, index),
        pane_hash=digest,
    )


# -- the policy ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionContext:
    """What the policy knows about the lane behind a finding.

    `hub` is the repo's worktree hub (`<root>/<repo>`); `worktree` is THIS
    reviewer's own `REVIEW-<task>` checkout under it (the seat difference,
    see the module docstring), or None when the loop could not resolve it
    (no waiting marker with a cwd, no spawn row naming the task, no pane
    path under the hub).
    `sightings` counts consecutive polls that saw this SAME pane, this one
    included; `answers` is how many keystroke answers this session has
    already received in its lifetime; `waiting_for` is how long the same
    pane has been on screen, in seconds."""

    session: str
    hub: "str | None" = None
    worktree: "str | None" = None
    sightings: int = 1
    answers: int = 0
    waiting_for: float = 0.0
    max_answers: int = 5
    kill_after: float = 600.0


@dataclass(frozen=True, slots=True)
class Action:
    """One row of the policy table, resolved: the verb, the exact keys to
    send (empty for `wait`, `page` and `kill`), the one-line reason the
    narration quotes, and the two account-level flags."""

    verb: str
    keys: "tuple[str, ...]"
    reason: str
    hold_spawns: bool = False
    page: bool = False

    @property
    def sends_keys(self) -> bool:
        return bool(self.keys)


def _strip_globs(target: str) -> str:
    """The literal prefix of a glob: `sales/shots/*` -> `sales/shots`,
    `build/**/*.o` -> `build`, `x[0-9]` -> `x`."""
    parts = target.split("/")
    kept: list[str] = []
    for part in parts:
        if any(ch in part for ch in "*?[{"):
            break
        kept.append(part)
    if not kept:
        return "/" if target.startswith("/") else "."
    joined = "/".join(kept)
    return joined or ("/" if target.startswith("/") else ".")


def normalise_target(target: str, worktree: "str | None") -> "str | None":
    """The absolute, glob-free, `..`-resolved path a dangerous-rm target
    names, or None when it cannot be resolved (a relative target with no
    worktree to resolve it against). `~` and `$VAR` are left unexpanded:
    they cannot be resolved for another process and never read as inside."""
    text = target.strip().strip("'\"")
    if not text or text.startswith("~") or "$" in text:
        return None
    literal = _strip_globs(text)
    if not posixpath.isabs(literal):
        if not worktree:
            return None
        literal = posixpath.join(worktree, literal)
    return posixpath.normpath(literal)


def inside(path: "str | None", root: "str | None") -> bool:
    """Whether `path` is `root` or lies strictly under it (after
    normalisation) -- and not the root itself, which is never a safe
    removal target: deleting the worktree's own directory is not "inside"."""
    if not path or not root:
        return False
    root_n = posixpath.normpath(root)
    path_n = posixpath.normpath(path)
    if path_n == root_n:
        return False
    return path_n.startswith(root_n.rstrip("/") + "/")


def _option_keys(finding: PromptFinding, prefix: str) -> "tuple[str, ...] | None":
    """The keys that choose the option whose label starts with `prefix`
    (case-insensitive): Enter when the cursor already sits on it, its
    number then Enter when it has one of 1..3, else None."""
    for option in finding.options:
        if option.label.lower().startswith(prefix):
            if option.selected:
                return (KEY_ENTER,)
            if option.number in (1, 2, 3):
                return (str(option.number), KEY_ENTER)
            return None
    return None


def accept_keys(finding: PromptFinding) -> "tuple[str, ...]":
    """The keys that say YES to this dialog: the "yes" option when the
    pane shows one (Enter if selected, its number + Enter otherwise), else
    Enter on whatever is preselected."""
    return _option_keys(finding, "yes") or (KEY_ENTER,)


def decline_keys(finding: PromptFinding) -> "tuple[str, ...]":
    """The keys that say NO: the "no" option's number + Enter when the pane
    shows one (Escape is that same option on Claude Code's dialogs, but a
    numbered choice is unambiguous on a 3-option prompt, where `2` would be
    "Yes, don't ask again"), else Escape."""
    return _option_keys(finding, "no") or (KEY_ESCAPE,)


def keys_for(finding: PromptFinding, verb: str) -> "tuple[str, ...]":
    """The console's verb -> keys mapping. `escape` is always the one key."""
    if verb == VERB_ACCEPT:
        return accept_keys(finding)
    if verb == VERB_DECLINE:
        return decline_keys(finding)
    if verb == VERB_ESCAPE:
        return (KEY_ESCAPE,)
    raise ValueError(f"unknown verb {verb!r}")


def decide(finding: PromptFinding, ctx: SessionContext) -> Action:
    """The policy table (devloop #127 §3, the reviewer seat's §2), one row per kind.

    dangerous_rm: the target (globs stripped, normalised, relative paths
        resolved against the reviewer's checkout) inside the reviewer's OWN
        review checkout -> accept; anything else -- another round's
        checkout, the hub's `main/`, `/tmp`, a `..` escape, an unresolvable
        target, a session whose checkout the loop could not name -> decline.
        The reviewer reads the refusal and adapts.
    permission: decline. Bypass mode means a prompt here is an explicit
        ask-rule or a critical path; never auto-accepted.
    trust: accept only for a path under /workspace (the dialog names it);
        anywhere else -> decline.
    resume_picker: Escape.
    login_expired / usage_limit / out_of_credits: NO keys -- page, and hold
        new spawns while the notice stands; the SAME pane still showing it
        past ACCOUNT_HOLD_SECONDS (second or later sighting) -> kill, which
        drops the hold and lets the retry edge's respawn re-confirm.
    unknown_dialog: first sighting -> wait one poll; the same pane again ->
        Escape; still there past `kill_after` -> kill, so the ordinary
        retry/resume edge takes over.
    Every kind: past `max_answers` answers on this session -> kill; a worker
        that keeps producing prompts is diverging.
    """
    kind = finding.kind
    if kind in ACCOUNT_KINDS:
        if ctx.waiting_for >= ACCOUNT_HOLD_SECONDS and ctx.sightings >= 2:
            return Action(
                VERB_KILL, (),
                f"{kind.replace('_', ' ')} banner unchanged for "
                f"{int(ctx.waiting_for // 60)} min — the spawn hold expires after "
                f"{ACCOUNT_HOLD_SECONDS // 60} min on one pane; kill-and-retry, and "
                "the respawn re-confirms the notice or proves the account is back",
            )
        return Action(
            VERB_PAGE, (), f"{kind.replace('_', ' ')} — an operator must act; "
            "no keystroke helps, new spawns held while it stands",
            hold_spawns=True, page=True,
        )
    if ctx.answers >= ctx.max_answers:
        return Action(
            VERB_KILL, (),
            f"answer cap reached ({ctx.answers}/{ctx.max_answers} answers on "
            f"this session) — a worker that keeps producing prompts is "
            f"diverging; kill-and-retry",
        )
    if kind == KIND_DANGEROUS_RM:
        resolved = normalise_target(finding.target or "", ctx.worktree)
        if ctx.worktree and inside(resolved, ctx.worktree):
            return Action(VERB_ACCEPT, accept_keys(finding), "target inside worktree")
        why = (
            "target unresolvable" if resolved is None
            else "lane worktree unknown" if not ctx.worktree
            else "target outside worktree"
        )
        return Action(VERB_DECLINE, decline_keys(finding), why)
    if kind == KIND_PERMISSION:
        return Action(
            VERB_DECLINE, decline_keys(finding),
            "permission prompts are never auto-accepted under bypass mode",
        )
    if kind == KIND_TRUST:
        target = finding.target
        if target and (target == "/workspace" or target.startswith("/workspace/")):
            return Action(VERB_ACCEPT, accept_keys(finding), "path under /workspace")
        return Action(VERB_DECLINE, decline_keys(finding), "path not under /workspace")
    if kind == KIND_RESUME_PICKER:
        return Action(VERB_ESCAPE, (KEY_ESCAPE,), "resume picker dismissed")
    # unknown_dialog: the ladder.
    if ctx.waiting_for >= ctx.kill_after and ctx.sightings >= 2:
        return Action(
            VERB_KILL, (),
            f"unknown dialog still on screen after {int(ctx.waiting_for // 60)} "
            f"min (kill after {int(ctx.kill_after // 60)} min)",
        )
    if ctx.sightings <= 1:
        return Action(VERB_WAIT, (), "unknown dialog — first sighting, waiting one poll")
    return Action(VERB_ESCAPE, (KEY_ESCAPE,), "unknown dialog seen twice — dismissed")


def relative_to_hub(path: "str | None", hub: "str | None") -> "str | None":
    """`path` relative to `hub` when it lies under it (what a loop event may
    carry), else the path's basename -- never an absolute path."""
    if not path:
        return None
    if hub:
        rel = os.path.relpath(path, hub)
        if not rel.startswith(".."):
            return rel
    return posixpath.basename(path.rstrip("/")) or path
