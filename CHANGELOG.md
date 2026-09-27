# Changelog

Releases of `alissa-tools-github-revloop`. The version of record is the
plain-text `version` file next to `version.py`; entries here start at 0.30.1
(earlier releases are described by their merge commits).

## 0.31.4

- **Classed `Merge-Readiness`: `operator — <class>: <reason>`** (issue #142,
  origin TASK-173324447). The reviewer's operator judgment was binary in
  effect — `auto`, or `operator` with a prose reason the merge edge could
  not act on. Of 14 operator verdicts on the last ~40 studio PRs, 3 named
  real merge risk and 11 named validation work the human does at gate 2
  anyway; the orchestrator held all 14 alike. The operator reason now
  **leads with one token from a closed, severity-ordered enum** —
  `schema-migration`, `data-backfill`, `secrets-env`, `infra-deploy`,
  `billing`, `security`, `unverified-ux`, `unverified-runtime`,
  `release-act` — so the orchestrator's forthcoming merge policy (its own
  task) can merge the second kind and keep holding the first. Rules: exactly
  one class, the **most severe** applicable row (top of the table wins);
  `auto` never carries a class; an operator line whose reason leads with no
  recognised token is still valid grammar and reads as **unclassed** — the
  consumer's hard hold (fail closed), so a reviewer that forgets the class
  loses throughput, never safety. The consumer regex is unchanged: the class
  rides inside the reason group.
  - `parse_readiness` and `parse_trailer` (`alissa.py`) return
    `(value, reason, klass | None)`; `READINESS_CLASSES` is the enum in
    severity order, `classify_readiness_reason` reads the class off the
    cleaned reason, and `clean_readiness_reason` keeps the prefix intact so
    the native trailer stays byte-equal to the envelope's line, class
    included. `VerdictEnvelope` gains `readiness_class`. The envelope-less
    fallback (`operator — envelope carries no Merge-Readiness line`) carries
    no class.
  - Both round directives (`_MERGE_READINESS_LINE`) state the operator form,
    the compact table (each token exactly once — pinned by test), the
    most-severe rule and the reasoning that makes honest classification the
    reviewer's interest ("unverified-* holds are validation work, not merge
    risk"); the envelope-less fallback is named as `operator` with no class.
  - The round-close log line and activity row (daemon-posted path) and the
    session-posted observation (issue #134) name the class:
    `class=<token>` on a classed operator, `class=unclassed` on one without,
    nothing on `auto`.
  - README (*The `Merge-Readiness` trailer on a native approve* → *The
    operator class*) and `docker/claude/README.md` (*The `Merge-Readiness`
    class the reviewer writes*) document the enum, the most-severe rule and
    unclassed = hard hold. New `test_readiness.py`: every class through both
    parsers and both dash separators, the unclassed and near-miss shapes,
    the directive's one-statement table, byte-equality of the copied native
    line, the class-free fallback, and the class on every narration surface.
  - Not in this release: the `alissa-code-review` skill's own envelope line
    (`references/verdict-envelope.md`, `references/merge-readiness.md`) still
    reads `operator — <reason>`; it lives outside this repository and is the
    operator's follow-up. No config key.

## 0.31.3

- **The shell guard, the waiting marker and the prompt responder on the
  reviewer seat** (issue #138, origin TASK-1910446095; the reviewer-side port
  of devloop W1 — PR #126 / issue #125 — and W2 — PR #128 / issue #127 — with
  the seat difference the issue names and nothing else invented). A reviewer
  parked on an interactive Claude Code prompt read ALIVE forever: the
  stale-round probe declined to respawn over a live session every poll (the
  sentinel's corpus records `wedge-dialog:revloop:pr808-r10`), and the only
  responder was a human over `railway ssh` + `tmux send-keys`.
  - **Prevention (W1, byte for byte).** The image ships devloop's three
    Claude Code hooks at `/usr/local/share/alissa/hooks/`: `guard-shell.py`
    (`PreToolUse` on `Bash`; denies, with an instructive reason, an
    `rm`/`rmdir` on a glob, a variable, a substitution, `~`, `/`, `.`,
    `..`, an outside-cwd operand, the workspace root, a hub, a hub's
    `.source` subtree or `main/`, across compound commands and reserved
    words, fail-open), `note-waiting.py` (`Notification`
    `permission_prompt|idle_prompt` → `${ALISSA_WAITING_DIR:-/workspace/.waiting}/<tmux
    session>.json`) and `clear-waiting.py` (`UserPromptSubmit`,
    `PostToolUse`). The entrypoint's 3a seeding MERGES them into
    `~/.claude/settings.json` and `$CLAUDE_CONFIG_DIR/settings.json` —
    idempotent, foreign hooks kept in place, stale registrations under the
    image-owned dir pruned, `ALISSA_SHELL_GUARD=off` skips the guard AND
    removes one a previous boot persisted. The image contract asserts the
    hook bytes, mode, owner, one deny + one pass in the image's python and
    both registrations; `tests-hooks-guard.sh` (the same deny/pass table)
    runs as the new `hooks-guard` CI job. Both round directives carry
    devloop's shell rule word for word plus the reviewer's own line: *"You
    never push and never delete: a reviewer that needs scratch files writes
    them under its own checkout and leaves them."*
  - **Cure (W2, same kinds, verbs, keys and knob names).** New `prompts.py`
    (classifier + policy, devloop's verbatim); `Alissa.capture_pane` /
    `pane_path` / `send_keys` over raw `tmux -S $TMUX_TMPDIR/tmux-<uid>/default`
    with the allowlisted key enum (`Enter`, `Escape`, `1`, `2`, `3`, `Down`,
    `Up`) and the console's session-name rule (`alissa.SAFE_SESSION`, stated
    once); the roster's real tmux name rides on `ManagedSession.session`.
    Every poll, on the sweep's post-reap roster (every ALIVE session of this
    daemon's own grammar — its spawns and the skill's `review-pr-<n>`
    rounds — never a foreign session), a waiting marker OR a session quiet
    past `prompt_quiet_seconds` triggers capture → classify → decide → act;
    re-capture after 3 s; `prompt_responder` = `on` | `observe` | `off`;
    `prompt_quiet_seconds`, `prompt_kill_minutes`, `prompt_max_answers`,
    `waiting_dir` with devloop's floors. **The seat difference:** the
    "own worktree" a `dangerous_rm` target is measured against is the
    reviewer's `REVIEW-<task>` checkout (marker cwd — an observation, so a
    cwd anywhere else, `main/` included, declines outright; then the spawn
    row's task ref — an assumption, so it contains an absolute target only
    and never serves as the base of a relative one; then the pane path) —
    inside it `accept`, anything else `decline`; the reason strings stay
    devloop's so the sentinel reads one vocabulary. Narration is one line
    on the PR's marker-identified
    **Review-loop activity** comment (`prompt-answered: dangerous_rm →
    accept (target inside worktree) · target REVIEW-TASK-9/build · 14 s
    after it appeared`) and one `escalation.prompt` loop event (`prompt:`
    ping rows; `escalation.prompt_page` for `prompt-page:` rows) — never
    pane text beyond the scrubbed signature. Account-level kinds
    (`login_expired`, `usage_limit`, `out_of_credits`) press nothing: one
    operator page per kind per 6 h on the session's PR and a **hold on new
    review spawns** through the spawn gate (`prompt-held` stage; live
    rounds keep running; the hold expires into a kill after 1 h on one
    pane). **A reviewer killed by the ladder never consumes a round**
    (pinned): the kill ages the round's spawn row past the stale window, so
    the stale-round edge re-enters THE SAME round next pass, and the cap
    counts verdicts (`completed`), not attempts. The stalled comment now
    ends with what the responder classified the pane as. Dry-run
    classifies and logs, sends nothing, kills nothing, pages nobody, and
    keeps its sighting ladder in memory (the `_dry_run_drift` split) so a
    diagnostic pass can never advance production's clock.
  - **Console parity.** `GET /api/pane?session=<name>` (last 40 lines,
    scrubbed, plus the classification) and `POST /action/answer {session,
    verb}` with `verb` ∈ `accept | decline | escape` (`409 not_waiting`, `409
    not_answerable`), both behind the existing passcode + CSRF gate;
    `/api/state` gains `waiting: [{session, kind, since, sightings,
    answers}]`; a *Waiting on a prompt* panel with Pane / accept / decline /
    escape buttons, a Pane button on every roster row, and `prompt-page`
    rows in the operator inbox (linked to the PR). Scope as devloop's: any
    managed session for the operator, own sessions only for the daemon; the
    `answer` audit line records `managed` like the kill trail does.
  - **Review round 1 (PR #139).** A marker cwd outside a `REVIEW-` checkout
    now settles a `dangerous_rm` as `decline` instead of yielding to the
    named checkout (the spawn cwd `main/` was resolving relative targets
    into the shared mirror); the named leg contains absolute targets only.
    `prompt_responder = off` clears the sighting ladder, so the waiting
    panel empties instead of freezing. The per-act `prompt:` ping rows of a
    session that left the roster are dropped once per pass
    (`State.prune_pings`); the hooks join the style matrix.
  - **Container.** `ALISSA_PROMPT_RESPONDER`, `ALISSA_PROMPT_QUIET_SECONDS`,
    `ALISSA_PROMPT_KILL_MINUTES`, `ALISSA_PROMPT_MAX_ANSWERS` and
    `ALISSA_WAITING_DIR` render pass-through (skew-gated on this release)
    into `revloop.config.json`; `ALISSA_SHELL_GUARD` and `ALISSA_WAITING_DIR`
    are read by the entrypoint/hooks at run time. Docker README, README
    (*Behaviour* rows, *The prompt responder*, settings and telemetry
    tables, console, tests), example config.
  - **Tests.** `test_hooks_guard.py` + `tests-hooks-guard.sh` (96 shell
    assertions: devloop's table, the reviewer's `REVIEW-*` cwd rows),
    `test_entrypoint_hooks.py`, `test_prompts.py` (devloop's fixtures
    verbatim, the checkout as the worktree), `test_alissa.py` (the raw tmux
    surface), the responder wiring in `test_loop.py` (marker/quiet
    triggers, foreign sessions never read, observe/dry-run send nothing,
    narration + event rows, the account hold through `poll_once` and its
    expiry, the ladder and the cap ending in the daemon's kill, **the
    round-cap pin**), console (`/api/pane`, `/action/answer`, CSRF, 409s,
    `waiting`, the inbox split), state, loop-events, config, directives,
    and the renderer's shell suite.

## 0.31.2

- **Pre-trust hubs in bows mode; classify the first-run dialog as a wedge**
  (issue #136, origin TASK-683998090; the reviewer-side sibling of devloop PR
  #124 / issue #123). The entrypoint pre-trusts Claude Code's per-directory
  *"trust this folder?"* gate only for hubs it can name at boot, and under
  `repos_source: bows` `ALISSA_REVIEW_REPOS` is empty — so the first review on
  a repo the daemon hub-ifies at review time started claude in an untrusted
  `{hub}/main`, parked on the dialog, and the stale-round probe read the live
  tmux session as *"still active — not respawning over a live reviewer"* every
  poll. Three closures, the shape of devloop 0.8.24 with the reviewer's paths:
  - **Seed from the derived list.** New `trust.py`. After every refresh the
    daemon records the derived allowlist at
    `{workspace_root}/.alissa-derived-repos` (one `owner/repo` per line;
    `ALISSA_DERIVED_REPOS_FILE` relocates it) and pre-trusts `{root}/{repo}`
    and `{root}/{repo}/main` for each, hub-ified or not. The entrypoint's 3a
    seeding reads that file (and now trusts root + `main/` for the static
    list and every hub on disk, plus any `REVIEW-*` checkout), and its log line
    counts the derived repos it read.
  - **Seed at hub-ify time and before every spawn.** `_ensure_hub` trusts the
    hub root, `main/` (the spawn cwd) and the `REVIEW-<task>` checkout the
    review skill may create — immediately after `alissa code workspace add`
    and, for a hub that already exists, on every spawn. Load-then-update into
    both `~/.claude.json` and `$CLAUDE_CONFIG_DIR/.claude.json`, only ever
    adds `hasTrustDialogAccepted: true`, atomic, compare-and-swap against a
    concurrent claude rewrite (bounded retries, then one WARNING), rewrites
    nothing when every entry is present, never raises (a failed seed never
    costs the spawn), seeds nothing under dry-run.
  - **`wedged:first-run-dialog`.** On the stale-round branch, only when a
    successful listing names the round's session and it is not idle-finished,
    the pane is read (`alissa tmux tail`, 40 lines; new
    `Alissa.tail_session`). A pane *parked* on a gate — the accept option
    among the last non-blank lines, nothing but the gate's chrome below it,
    the question strictly above — is one WARNING per episode (ping kind
    `first-run-dialog:<session>` in production, a process-lifetime set under
    dry-run; a failed kill retries next poll at INFO),
    the round's own session killed, its hub seeded, one activity-comment
    line, and the round re-queued in the same pass exactly as a dead
    session's is (`reenqueued`, the `stale_reenqueued` bucket; the respawn
    site logs at INFO so the episode is one WARNING). Every other
    alive-but-idle pane — including one that merely quotes the gate's words —
    keeps the existing defer and `stalled` ping; the capture stays at DEBUG.
  - **Docs.** README (*Behaviour* row, *Sitting on the first-run dialog*, the
    bows-mode trust rule under *Deriving the allowlist*, *Tests*) and the
    docker README (trust rule under *claude auth*, the bows checklist, boot
    step 2b).
  - **Tests.** `test_trust.py` (merge, idempotence, never-remove, atomic
    write, compare-and-swap, derived-repos record, the reviewer path shape,
    the pane classifier with devloop #124's fixtures verbatim — boxed,
    numbered, accept-first, footer); loop tests (hub-ify seeds root / main /
    checkout before the enqueue, re-trust before every spawn, idempotent,
    dry-run, failed seed never costs the spawn; dialog → kill + seed +
    re-queue with one WARNING and the `stale_reenqueued` accounting; other
    panes, untailable panes, dead / unprobeable / idle-finished / fresh rows
    unchanged; failed kill retries; new episode warns again; dry-run; bows
    refresh records and trusts, failed first refresh and dry-run record
    nothing); `tests-entrypoint-config.sh` boots the real entrypoint with an
    empty `ALISSA_REVIEW_REPOS` and three derived repos (six trusted paths in
    both state files, an on-disk hub still trusted, the env override); the
    image contract runs the shipped seeding with one derived repo. A
    `conftest.py` autouse fixture points `HOME` / `CLAUDE_CONFIG_DIR` at
    scratch dirs for every test.
  - **Review round 1 (PR #137).** The wedge's one-WARNING gate recorded its
    ping-ledger row *before* the dry-run guard, so a `--once --dry-run` pass
    over the default state path silenced the WARNING production owed for the
    same episode; it now takes the identity-drift split (durable in
    production, process-lifetime in dry-run). The classifier's third leg read
    the option line itself and `Yes, I trust this folder` contains the old
    `trust this folder` marker, so for the trust gate the leg was
    self-satisfied; the markers are now the gates' question text (`quick
    safety check` / `is this a project you created` / the older `trust the
    files in this folder` / `bypass permissions mode`) and must stand
    strictly above the option line — stricter than devloop's `hubs.py`
    until devloop follows. `trust.read_derived_repos` (no production caller;
    the entrypoint parses the file inline) is dropped.

## 0.31.1

- **Session-posted verdicts carry the `Merge-Readiness` trailer too** (issue
  #134). 0.31.0 put the trailer on the native review only on the rounds where
  the daemon posts the verdict itself; on the normal path — the reviewer
  session submitting its own review — the body passes through nothing, and
  two post-0.31.0 approves (studio#1258, orcloop#111) reached the merge edge
  with no parseable line. Both round directives now require the session's
  OWN native review (every `gh pr review` form and the reviews-API POST) to
  END with the bare `Merge-Readiness: auto` or `Merge-Readiness: operator —
  <one-line reason>` line as its last non-empty line, byte-equal to the
  envelope's; `auto` only on an APPROVE of the reviewed head. The daemon now
  SEES it: the first poll that finds a reviewer-identity `APPROVE` on the
  current head reads the body with the trailer grammar and logs
  `readiness=auto|operator`, or — when no bare line parses (backticked, bold,
  bulleted or mid-sentence forms included) — one `WARNING` per (PR, head)
  (`… carries no Merge-Readiness trailer — the merge edge will hold it; the
  session must end its review body with the line (see directive)`) and a
  `readiness=missing` activity row. Observation only: no review is posted or
  edited and no round re-runs; a missing trailer is a hold on the merge edge,
  not a review failure. `REQUEST_CHANGES` never warns. The observer reads the
  newest reviewer-identity APPROVE on the head (a later `--comment` write-up
  by the same login does not hide it), emits before it records — the
  once-per-head flag lands only after the activity row did, so a failed row
  is retried next poll — and writes nothing under `--dry-run`. The guard is a
  nullable `readiness` column on the `verdicts` ledger row, migrated in
  place, stamped only with the review's own GitHub time (an unreadable stamp
  keeps the WARNING and skips the row rather than inventing a verdict at
  "now"). The trailer grammar (`parse_trailer`, `alissa.py`) is one regex shared
  by the emitter and the check; the daemon-posted path is otherwise unchanged.
  No config key.

## 0.31.0

- **`Merge-Readiness` trailer on native approves** (issue #130). Every native
  `APPROVE` review the daemon posts now ends with one line-anchored
  `Merge-Readiness: auto` or `Merge-Readiness: operator — <reason>` line, the
  last non-empty line before the hidden verdict marker, carried from the
  `- **Merge-Readiness:**` line of the reviewer's verdict envelope. An envelope
  without a parseable line posts `operator — envelope carries no
  Merge-Readiness line` (fail closed; the daemon never invents `auto`);
  `REQUEST_CHANGES`/`COMMENT` events — including an approve the checks gate
  downgraded — carry no trailer. Reasons are flattened to one backtick-free
  line of at most 200 characters. Both reviewer directives now ask for the
  envelope line; the round-close log line carries `readiness=…` and the
  activity row names it. No new config key.

## 0.30.1

- **Round admission: one round per re-request** (issue #128). A round on a
  head that already carries a verdict of record is queued only on a
  `review_requested` timeline event for the reviewer login **newer than that
  verdict**, and never inside the new `verdict_cooldown_s` (default 120 s) of
  it — the PR's `requested_reviewers` snapshot is a hint, no longer the
  trigger. Fixes the phantom round 2 on studio #1243, where the poll ten
  seconds after a `request_changes` verdict re-read the not-yet-consumed
  request and queued a second round on the same head. A push re-arms exactly
  as before. New ledger table `verdicts` (PR, head, posted_at) stamped from
  the daemon's native posts and from every reviewer-identity review observed
  on the current head; one INFO line per ignored request; the timeline is
  read at most once per candidate PR per poll, and a timeline that cannot be
  read — or runs past the 2,000-event bound — admits on the snapshot alone
  with a warning rather than wedging the PR. New config key
  `verdict_cooldown_s` / flag `--verdict-cooldown-s`.
