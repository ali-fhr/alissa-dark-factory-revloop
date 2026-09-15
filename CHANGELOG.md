# Changelog

Releases of `alissa-tools-github-revloop`. The version of record is the
plain-text `version` file next to `version.py`; entries here start at 0.29.1
(earlier releases are described by their merge commits).

## 0.29.1

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
  read at most once per candidate PR per poll. New config key
  `verdict_cooldown_s` / flag `--verdict-cooldown-s`.
