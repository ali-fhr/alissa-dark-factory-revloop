# Changelog

Releases of `alissa-tools-github-revloop`. The version of record is the
plain-text `version` file next to `version.py`; entries here start at 0.30.1
(earlier releases are described by their merge commits).

## 0.31.12

- **The `config` block on the fleet-vitals snapshot** (issue #159; origin
  TASK-1127468277, implementation TASK-427421490) — the revloop part of lane
  L9 of the Studio design `docs/design/managed-dark-factory-provisioner.md`
  (§1.4, §2.7), mirroring devloop 0.8.38. The smoke's SM2–SM4 compare what
  Studio provisioned with what each seat derived; this release gives the
  reviewer seat's word.
  - **`{feedOwnerActorId, repos, reviewersRequested, ghLogin}`**: the
    bodies-of-work authority under `repos_source: bows` (null in static
    mode, or when several owners are trusted), the effective allowlist the
    last feed refresh bound (sorted case-insensitively), an empty
    `reviewersRequested` (the reviewer seat requests no reviewer; the key
    keeps the block one shape across seats) and the reviewer identity's
    own GitHub login. Read off the config the watcher RUNS on, not the boot
    config the pusher was built with.
  - **No secret.** The block is built from typed config fields, and any
    value carrying a credential the process holds (`GH_TOKEN`,
    `GITHUB_TOKEN`, `ALISSA_API_TOKEN`, `ALISSA_UI_PASSCODE`, the
    Anthropic/Claude credentials, and the variable `reviewer_token_env`
    names) is blanked before sending, with a WARNING naming the field only.
  - **Daemon only.** The console's out-of-pass push (the drain handshake)
    leaves the block out: that process never resolves the feed authority
    nor refreshes the allowlist. A derivation that fails costs the block,
    never the snapshot.
  - **An old Studio still gets its vitals.** Until Studio's schema carries
    the block (lane L5), a strict 400 naming `config` is answered like the
    `idle` one: resent without it, left out for an hour, then offered again.
    A 400 naming both blocks drops both in one resend; the match is on the
    whole word, so a message merely containing "configuration" is an
    ordinary failure.

## 0.31.11

- **Seat parking for the reviewer seat** (issue #157; origin TASK-1827515064,
  implementation TASK-193024056) — lane L5 of the Studio design
  `docs/design/managed-dark-factory-seat-parking.md` (§2.1, §2.2, §2.4), the
  devloop L4 contract for rounds. A leaf: nothing changes in production until
  orcloop (lane L6) calls the drain and reads the block.
  - **The idle block** `{asOf, passAt, owed, live, managed, queued, timers,
    drainedAt}` on the console's `GET /api/state` (`idle`) and in the
    fleet-vitals snapshot. Every `Decision` now carries an owed verdict and an
    optional seat-owned timer, recorded on the poll snapshot's stage, so the
    block costs no GitHub call. Owed: a round spawned, queued for a slot,
    held on CI (`checks_hold` timer), held by the drain or in flight inside
    its stale window (`stale_window` timer); a verdict still to post
    natively; an escalation posted this pass; the post-verdict cooldown
    (`stale_window` timer); a round with no hub or no review task; an
    evaluation that failed. Settled: converged, capped, already
    stability-held, waiting on a fresh re-request, out of scope. Unknown is
    never idle: no pass yet is `owed: null`, an unlistable roster or queue is
    a null count. `live` / `managed` follow the vitals `sessions` rule;
    `queued` counts undispatched `alissa tmux queue` items.
  - **The drain.** `POST /action/drain {ttlS, reason}` and
    `POST /action/undrain`, behind the passcode session and the CSRF token,
    audited. The drain raises the flag first, then reads the block; if D2 is
    broken or a round spawn holds a claim it lowers the flag and answers
    `drained: false` with `refusals`. If it holds, it pushes a vitals
    snapshot carrying `idle.drainedAt` before it answers (`vitals:
    pushed|failed|skipped`). `ttlS` defaults to 300 and is capped at 900; a
    bad `ttlS`/`reason` is a 400, an unreachable `state.db` a 503.
  - **Round spawning honours it.** The one spawn path (`_spawn`) passes the
    drain gate before any side effect — hub add, `alissa tmux queue add`,
    census, ledger row, activity line. A drained round is `QUEUED` with the
    `drained` stage: owed, no spawn row, no round number burned, and kept out
    of the slot-gate stall summary. Claim and drain are serialised by one
    `BEGIN IMMEDIATE` transaction on each side (`spawn_claims`,
    `seat_drain`). Dry-run reads the flag and claims nothing.
  - **TTL and boot.** The flag is one `seat_drain` row with an absolute
    expiry; `run_forever` clears it and every spawn claim at boot (never in
    dry-run, and not in the `--once` / `--pr` one-shots).
  - **Studio compatibility.** Until Studio's strict vitals schema carries
    `idle` (lane L3), a 400 naming `idle` makes the pusher resend without the
    block and leave it out for an hour.

## 0.31.10

- **`round.verdict` is posted for every verdict of record** (issue #155;
  origin TASK-934123099, implementation TASK-1627052337) — lane L10 of the
  Studio design `docs/design/loop-operator-surface.md` (§1.3, §6.4). Until
  now the kind derived only from `verdict_posts`, the native-fallback
  obligation record, so a fleet whose sessions post their own reviews
  emitted none. Every `round.verdict` now carries the six data keys
  `verdict`, `headSha`, `readiness`, `readinessClass`, `contractVersion`
  and `taskRef` (null where unknown) and is keyed on
  `(repo, pr, round, head)`.
  - **The ledger row** is one `round-verdict:` ping per (PR, round)
    (`loop_events.round_verdict_kind`), written by the native post at post
    time and, for a round the SESSION closed, on the first pass that sees
    the envelope and the session's review together. First record wins; a
    dry run writes none. A native post's `verdict_posts` row and its ping
    derive ONE event.
  - **Session-closed rounds are found off the spawn ledger**, not only
    through the review-requested search: a session's own review consumes the
    request, so its PR leaves the search as the round closes, and a terminal
    round (the final approve, an un-re-requested `request_changes`) would
    never be seen. `sweep_round_verdicts` runs every poll after the evaluate
    loop over the newest spawn row per PR within 24 h that has no
    `round-verdict:` row and that the search did not return. It reads the
    review task by the ledger's ref (a validated task is still readable), and
    fetches the PR and its reviews only once the round's envelope has landed.
    Telemetry only: never fatal to the pass.
  - **The head** of a session-closed round is the review the round count
    reaches (`countable_rounds`), not the newest review — which is the next
    round's while its session has reviewed but not yet written its envelope.
  - **`verdict` is what GitHub holds**: a native approve the CI gate
    downgraded reports `request_changes` or `comment`, not `approve`.
  - **Readiness** is what the approve carries: the native post's trailer
    read back with the trailer grammar, or the session envelope mapped
    through the same fail-closed rule — so an approve whose envelope names
    no readiness is `operator` with `readinessClass: "unclassed"`. A
    `request_changes` (or an approve the CI gate downgraded) carries null.
    A plan PR reads its `Commit-Readiness`.
  - **`contractVersion`** is the envelope's `Contract: v<n>` line — the `v`
    required, so prose like `Contract 3 criteria` sets nothing
    (`alissa.parse_contract_version`, on `VerdictEnvelope.contract_version`);
    null until reviewers record it (design lane L11). `TaskDetail` now carries
    the newest envelope whole, so the session path costs no extra task read.
  - `taskRef` is the review task, as on `round.spawned`.
  - A `verdict_posts` row that predates the verdict column now carries
    `data.verdict: null` instead of omitting the key.

## 0.31.9

- **Every reviewer spawn is stamped with the origin task and the repo**
  (issue #153; origin TASK-98337484, implementation TASK-1158257469) — the
  revloop part of the Studio loop cost meter's lane L4
  (`docs/design/loop-cost-meter.md` §2.3, §2.4), mirroring devloop's merged
  lane (devloop issue #149, PR #150). The enqueue is `alissa tmux queue add …
  --task TASK-<origin> --repo <owner/name>` (repo lowercased), so the Studio
  session row carries the origin as `focusTask` and the repo from its first
  second; the reviewer's own `current_task` write of its review task may
  follow and win, which the meter already resolves.
  - **`Alissa.enqueue_reviewer`** gains `repo`, and its `task_ref` now means
    the ORIGIN (it used to carry the review task as `--task`, unconditionally).
    The flags go only on a CLI whose `queue add --help` lists BOTH
    (`accepts_spawn_stamps`: probed once per process and memoized; a probe
    that cannot run is not memoized and that spawn goes unstamped). A CLI
    with the older `--task` but no `--repo` — 0.3.0 — gets neither. A dry run
    runs nothing, the probe included, and logs the stamps it would pass.
  - **Origin** (`loop.review_origin_task_ref`): the review task's CR2 title
    (`Review PR <org>/<repo>#<n> (TASK-<origin>)`) first, then the PR body's
    `Alissa-Task:` or `Origin [Alissa] task:` line — never the
    `Implementation task:`. None found (a plan PR, a bare body): `--repo` alone.
  - Ledger rows (the spawn row keeps the review task) and the `round.*` loop
    events derived from them are unchanged.
  - `ARG REVLOOP_VERSION` follows the version file (the `dockerfile-pin-sync`
    rule since 0.31.8).

## 0.31.8

- **The seat image is published to GHCR on every release** (issue #151;
  origin TASK-1046190284, implementation TASK-234076312) — the pilot for the
  fleet's published images, written to be copied by orcloop, devloop and
  genloop (seat-specific values at the top of the workflow).
  - **`image-publish.yaml`.** On a pull request merged into `main` that
    touches `alissa-tools-github-revloop/**`, `docker/**` or the workflow:
    read the version file at the merge commit; wait (bounded, 15 min, loud
    on timeout) until PyPI serves `alissa-tools-github-revloop==<version>`;
    exit green without pushing if `ghcr.io/ali-fhr/alissa-dark-factory-revloop:<version>`
    already exists (an unclassifiable registry answer fails the run rather
    than risk an overwrite); build `docker/claude/Dockerfile` for
    `linux/amd64` with `--build-arg REVLOOP_VERSION=<version>` and the OCI
    `source` / `version` / `revision` / `title` / `description` / `licenses`
    labels; run `tests-image-contract.sh` against that image; push
    `:X.Y.Z`, `:X.Y`, `:X`; read the digest back and write it with the
    ready-to-paste `<tag>@sha256:<digest>` pin to the job summary.
    `packages: write` on the publishing job only; `GITHUB_TOKEN` the only
    credential; nothing runs on unmerged code.
  - **One version axis.** `check-version-bump.yaml` now requires a version
    bump for `docker/**` changes (the image is published under that number
    and an existing tag is never overwritten), and its new
    `dockerfile-pin-sync` job fails any pull request on which the
    Dockerfile's `ARG REVLOOP_VERSION` default differs from the version
    file. The ARG moves from `0.29.0` to the version file's value.
  - **`check-image.yaml`** builds the pull request's Dockerfile at the
    newest *published* release when the pinned one is not on PyPI yet (a
    release pull request), and says so; the release build is
    image-publish's. `tests-image-contract.sh` gains two env overrides for
    that: `REVLOOP_VERSION` (build-arg + expected installed version) and
    `IMAGE_PREBUILT=1` (assert an existing image instead of building one).
    `tests-entrypoint-config.sh` now asserts the ARG equals the version
    file, where it used to assert a feature PR left the pin alone.
  - **Docs.** `docker/claude/README.md` gains *Published image* — image
    name, tag scheme, labels, the publish order of events, `docker run`
    with the runtime env contract, and the operator lever: pin Railway to
    `ghcr.io/ali-fhr/alissa-dark-factory-revloop:<version>@sha256:<digest>`
    instead of bumping `REVLOOP_VERSION`.
  - **Publish ordering (review round 1).** `:X.Y` / `:X` are pushed before
    `:X.Y.Z`, so the version tag the existence check keys on is the commit
    marker and a re-run after a partial push re-pushes all three; the
    `concurrency` group sits on the `publish` job (the PyPI wait and unmerged
    closes stay out of it) and its one-pending-run limit is documented.
    `check-version-bump.yaml` ignores an empty `image_prefix` instead of
    matching every path. README *On Railway* and the Dockerfile name the
    release-merge race of a Dockerfile-source deploy against PyPI.
  - **Operator, after merge:** confirm the first *Container Image Publish*
    run; set the GHCR package `alissa-dark-factory-revloop` to public;
    verify an anonymous manifest pull; switch `dark-revloop-shared` to the
    image source pinned by digest. Out of scope here by design.

## 0.31.7

- **Plan PRs get a plan directive, a plan-lint gate and a `Commit-Readiness`
  trailer** (issue #148; genloop design `docs/design/genloop.md` v0.2 in
  `ali-fhr/studio.alissa.app`, D8, D18, §6, §7.1, lane L4; origin
  TASK-2001160633, implementation TASK-1824369549).
  - **Four signals, or it is code.** A PR is a plan PR only when its
    repository is in `plan_repos`, its author in `plan_authors`, its head ref
    matches `^PLAN-\d{8}T\d{6}Z-[a-z0-9-]+$` and its body carries
    `<!-- alissa-genloop:plan v1 -->` on a line of its own
    (`loop.plan_signals` / `loop.is_plan_pr`). Any other PR keeps the code
    directive, trailer and events byte for byte.
  - **The plans repository is configuration.** `plan_repos`
    (`ALISSA_REVIEW_PLAN_REPOS`, default `<owner>/alissa-dark-factory-plans`
    — the repository name under any owner) and `plan_authors`
    (`ALISSA_REVIEW_PLAN_AUTHORS`, default empty = **no plan PRs**,
    fail-closed); `|`/`,`-separated env rails that win over the file, blank
    falls through, malformed entries refused at load. A test proves no
    plans-repository literal survives in the package outside `config.py`.
  - **The round waits for `plan-lint`.** A plan round is held as the
    ordinary pre-spawn `checks.held` until the `plan-lint` check run on the
    head concluded `success` — missing, running, failed, skipped, neutral or
    unreadable all hold, unbounded — and no session spawns
    (`ReviewWatcher._gate_spawn_on_plan_lint`). `CheckRollup` now carries
    every context it read (`contexts`).
  - **The plan directives.** `PLAN_ROUND_1_DIRECTIVE` /
    `PLAN_ROUND_K_DIRECTIVE` beside the code ones in `loop.py`: the
    `alissa-code-review` protocol with the plan rubric of
    `alissa-code-review:references/plan-directive.md` (quoted by path), the
    six questions in order (`PLAN_RUBRIC`, byte-pinned; both templates pinned
    by digest), no automatic rule (the mechanics are the lint's), target code
    read with `gh api` / `gh search code` / `gh issue list` / `gh pr list` and
    never cloned, wrong scopes and dependencies as `[major]`, and the review
    task titled `Review plan <owner>/<repo>#<n> (<plan id>)` downstream of
    nothing (the id from a body `Alissa-Plan:` line in the id grammar, never
    quoted otherwise). The task search and `alissa-pr-review` match
    `Review plan …` titles (`alissa.review_task_title_pattern`).
  - **`Commit-Readiness`.** `alissa.COMMIT_READINESS_CLASSES` (eleven, in
    severity order, the first six never auto-commit), `parse_commit_trailer`
    / `parse_commit_readiness` (the Merge-Readiness grammar with the other
    key; a class on an `auto` line is refused whole), the envelope's
    `commit_readiness` pair, and `loop.commit_readiness_trailer`: a plan PR's
    native approve carries `Commit-Readiness:` and never `Merge-Readiness:`,
    and an envelope with no line posts the unclassed
    `operator — envelope carries no Commit-Readiness line`. A session-posted
    plan approve is observed for the same trailer.
  - **`data.kind: "plan"`.** The gate records a `plan-round:<n>` ping, and
    every round event of that round (`round.spawned`, `round.verdict`,
    `round.abandoned`, `round.capped`, `checks.held`) carries
    `data.kind: "plan"`; code rounds carry no `kind`.
  - **Operator, after merge:** re-pin `REVLOOP_VERSION`; set
    `ALISSA_REVIEW_PLAN_REPOS=ali-fhr/alissa-dark-factory-plans` and
    `ALISSA_REVIEW_PLAN_AUTHORS=<genloop login>` once that login exists.

## 0.31.6

- **A reviewer refused by Claude is a named `auth.rejected` loop event, not
  a silent stale round** (issue #146, devloop #140's lane ported; Studio
  design `managed-dark-factory-claude-auth.md` §2.6, lane L5; origin
  TASK-2055621468, implementation TASK-1389509388). Two detectors feed one
  signal:
  - **The pane detector.** `prompts._AUTH_REJECTED_RE` beside
    `_LOGIN_EXPIRED_RE` matches the five documented 401 wordings
    (`API Error: 401`, `Invalid authentication credentials`, `Invalid API
    key`, `OAuth token has expired`, `OAuth token revoked`) as a new
    `auth_rejected` account kind, with `matched` (`api_error_401` /
    `invalid_api_key` / `oauth_expired` / `oauth_revoked`); the existing
    `login_expired` banner is the same signal (`matched: "login_expired"`).
    The classifier is devloop's byte for byte, including its two review
    rounds' rules for telling Claude Code's own `⎿ API Error: 401` row from
    a reviewer quoting the wording.
  - **The first-turn death check.** A reviewer session this ledger spawned
    inside its round's first stale window whose pane's current command is a
    shell (`alissa.SHELL_COMMANDS`, read with `Alissa.pane_command`) has its
    last lines captured **once** and read by `prompts.classify_exit`
    (`source: "exit"`); a match is re-asserted from memory while the pane
    stays a shell, and no match is the ordinary "died, not auth" and emits
    nothing new.
  - Either finding runs the `login_expired` row: the PR is paged once per
    kind per six hours (the page names the `auth_rejected` remedy), new
    reviewer spawns are held, and the one-hour expiry kills the session and
    ages its spawn row so the stale-round edge re-enters **the same round**
    — no verdict was submitted, so an auth death never burns a round number.
  - **The event.** One `auth-rejected:` ping row per session, written at
    observation, derives `auth.rejected` (`seat: "revloop"`, `prNumber`,
    `round`, `session`, a fixed `reason`, dedupe key
    `revloop:auth.rejected:<session>`) with devloop's `data: { status: 401,
    source, matched, envName, tokenSuffix }`. `envName` is which of
    `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` the container carries,
    read by name; `tokenSuffix` is its last four characters and nothing
    more, captured when the refusal is seen so a later backfill still names
    the token that was refused. `observe` mode and dry-run write no row.
  - No config key. After merge the operator re-pins `REVLOOP_VERSION` on
    `dark-revloop-shared`.

## 0.31.5

- **The directive says what to do when no class row fits, and the envelope
  keeps the class in one place** (follow-up to PR #143 round 1, issue #142;
  origin TASK-173324447, implementation TASK-1831487098). The class enum is
  closed, and the round-1 review noted that a directive which makes the class
  mandatory but never says what to do when **no row applies** would push a
  reviewer to shoehorn such a hold — a dependency change, a deleted or
  renamed public surface, a waived `[major]`, a judgment residual, an
  operator gate that is a human action — into the nearest row, which for a
  vague reason is one of the `unverified-*` pair the merge policy may merge
  unattended. Both round directives now name the way out: **no row, no
  class** — an unclassed operator line is the correct hard hold, never a
  failure. `test_readiness` pins the sentence and its named triggers next to
  the exactly-once enum check; the README (*The operator class*) and
  `docker/claude/README.md` carry the rule.
  - `VerdictEnvelope.readiness_class` is removed. Nothing read it: the
    native trailer copies the reason whole, and the narration re-derives the
    class from the emitted line with `parse_trailer`, so the field was a
    second copy of one fact with no consumer. The class is a function of the
    reason (`classify_readiness_reason`); `parse_readiness` and
    `parse_trailer` still return `(value, reason, klass | None)`. No config
    key; the grammar, the enum and the consumer regex are unchanged.

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
