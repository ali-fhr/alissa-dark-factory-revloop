# Changelog

Releases of `alissa-tools-github-revloop`. The version of record is the
plain-text `version` file next to `version.py`; entries here start at 0.30.1
(earlier releases are described by their merge commits).

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
