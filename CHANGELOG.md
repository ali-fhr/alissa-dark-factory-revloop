# Changelog

Releases of `alissa-tools-github-revloop`. The version of record is the
plain-text `version` file next to `version.py`; entries here start at 0.30.1
(earlier releases are described by their merge commits).

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
    the question above — is one WARNING per episode (ping kind
    `first-run-dialog:<session>`; a failed kill retries next poll at INFO),
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
