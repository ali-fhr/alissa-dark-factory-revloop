#!/usr/bin/env bash
# =============================================================================
# Render revloop.config.json from the environment.
#
# Precedence contract:  env var  >  daemon library default.  There is NO hidden
# entrypoint layer in between for optional tuning knobs — the entrypoint used to
# inject EVERY key with its own hardcoded fallback (e.g. `round_cap: 3`), which
# SHADOWED the library's own default: a library that raised its default (say to
# 10) could never take effect in the container, because the entrypoint always
# wrote the old value unless the operator happened to set ALISSA_ROUND_CAP.
#
# So keys fall into two classes:
#
#   * PASS-THROUGH (optional tuning knobs) — emitted ONLY when the env var is
#     set. When unset the key is omitted entirely and the daemon library applies
#     its own current default. These are pure tuning values where the library is
#     the authority: poll_interval, round_cap, stability_rounds (how many
#     consecutive request_changes rounds with an empty shipped-product diff stop
#     the loop -- a property of how the reviewed repos are worked, not of the
#     image, and 0 switches the guard off entirely), checks_wait_seconds (how long a
#     round holds its approve for an unsettled CI rollup, per condition waited on
#     -- so up to 2x it if an unreadable hold becomes a pending one; a property
#     of the watched repos' CI, not of the image), checks_spawn_wait_seconds (how
#     long an owed round waits for the head's checks to conclude before its
#     reviewer is queued at all -- the same property of the same CI, one stage
#     earlier), review_task_miss_ttl_polls (how many polls a PR with no review
#     task is taken on trust before the task corpus is searched again -- a
#     latency-for-reads trade that depends on how a deployment creates its review
#     tasks, not on the image), task_list_self_scope (whether this actor owns
#     EVERY review task it has to find -- a property of the deployment's actor
#     layout; a BOOLEAN pass-through, accepted as 1/0, true/false,
#     yes/no or on/off and REFUSED as anything else, because a silently-false
#     typo would be indistinguishable from the default it is trying to change),
#     loop_events_enabled (whether the daemon pushes loop telemetry to Studio
#     once per pass, issue #112 -- the other boolean pass-through, same accepted
#     spellings and same refusal; the library also reads
#     ALISSA_REV_LOOP_EVENTS_ENABLED directly and the env wins, so the render
#     is belt to that brace),
#     operators (an EMPTY operator
#     allowlist is the library's fail-closed default -- emitting `[]` would say
#     the same thing, but omitting it keeps "unset means the library decides"
#     true for every optional key without exception),
#     repos_source / bows_refresh_polls / bow_owners (issue #119: where the
#     allowlist comes from -- `static` or `bows` -- how often a bows-mode daemon
#     re-derives it, and which actor ids' feed Bodies of Work are authoritative;
#     a string, an int and a JSON array of `|`/`,`-split ids respectively,
#     exactly as devloop's generator emits them. The library ALSO reads the
#     three ALISSA_REVIEW_* variables directly and the env wins, so as with
#     loop_events_enabled the render is belt to that brace. These three are
#     the only keys here gated on a VERSION-SKEW probe (see revloop_dist_supports
#     below): an image whose entrypoint knows them while its pinned library does
#     not is the ordinary state between a release and its re-pin, and an
#     unknown key fails the daemon's config load outright).
#
#   * STRUCTURAL (container constants) — always emitted with an explicit value
#     the container requires, INDEPENDENT of the library default. Pass-through is
#     unsafe here (see the per-key rationale below), so the value is byte-pinned
#     and covered by tests-entrypoint-config.sh:
#       - on_missing_hub = add     the container's whole model is self-contained
#                                  hub-ify on demand; the library default is
#                                  `skip`, which would make a fresh volume review
#                                  nothing. (Bounded: `add` requires a non-empty
#                                  repos allowlist, which env-driven mode always
#                                  has.)
#       - agent_profile  = claude  must name a profile that exists in the baked
#                                  agents.yaml, which defines exactly `claude`;
#                                  drifting to some future library default would
#                                  select a profile the image does not ship.
#
# `repos` is required and always emitted. It is non-empty in env-driven mode;
# the bows path (render_revloop_config_bows) passes `[]` -- the allowlist then
# derives from the feed and the library's `add` guard is relaxed under bows.
#
# The two reviewer-identity keys are pass-through for the same reason as the
# tuning knobs -- unset means "the library decides" -- but they are worth
# calling out because the container is where their absence bites (issue #51):
#   - reviewer_login      the identity every review MUST be posted under.
#                          Unset, the daemon adopts whatever the gh credential
#                          resolves to at boot.
#   - reviewer_token_env  the NAME of the variable carrying that identity's
#                          token. Unset, every `gh` call inherits the
#                          container's default credential -- and this container
#                          holds more than one identity, which is exactly how a
#                          round's verdict landed under the implementer's login.
#
# Usage:  revloop-config.sh '<repos-json-array>' ['<operators-json-array>']
# Or source it and call render_revloop_config '<repos-json>' '<operators-json>'.
# =============================================================================
set -euo pipefail

# -- the version-skew probe (issue #119) ---------------------------------------
#
# This file ships in the image; the daemon comes from PyPI at ARG
# REVLOOP_VERSION, re-pinned only on a release. So an image whose renderer knows
# `repos_source` while its installed daemon does not is the ORDINARY state
# between a release and its re-pin -- and rendering the key anyway would fail
# the daemon's config load with "unknown config key(s)". The probe reads the
# INSTALLED library's CONFIG_KEYS once per boot; a key it does not list is
# dropped with a WARNING naming it and the variable that produced it.
#
# FAIL-OPEN when the library cannot be imported at all: "cannot tell" lets every
# set key through (today's behaviour) after saying so, because refusing would
# strand a deployment on a probe failure unrelated to the key.
#
# The probe is a plain python import, which is also its test seam: a harness
# puts a stub `alissa.tools.github.revloop.config` first on PYTHONPATH to
# replay either side of the skew without installing a second wheel.
_REVLOOP_DIST_LOADED=""
_REVLOOP_DIST_VERSION=""
_REVLOOP_DIST_KEYS=""

revloop_installed_dist_facts() {
  python3 - <<'PROBE'
import sys

try:
    from alissa.tools.github.revloop.config import CONFIG_KEYS
except Exception as exc:  # not installed / broken install / import error
    sys.exit(f"cannot import alissa.tools.github.revloop.config: {exc}")

try:
    from alissa.tools.github.revloop.version import version
    installed = version.value
except Exception:
    installed = "unknown"  # version plumbing is optional to the guard

print(installed)
print("\n".join(CONFIG_KEYS))
PROBE
}

revloop_load_dist_facts() {
  local out=""
  [ -z "${_REVLOOP_DIST_LOADED:-}" ] || return 0
  _REVLOOP_DIST_LOADED=1
  if out="$(revloop_installed_dist_facts 2>/dev/null)" && [ -n "${out}" ]; then
    _REVLOOP_DIST_VERSION="$(printf '%s\n' "${out}" | head -n 1)"
    _REVLOOP_DIST_KEYS="$(printf '%s\n' "${out}" | tail -n +2)"
  fi
  if [ -z "${_REVLOOP_DIST_KEYS}" ]; then
    printf '[revloop-config] WARN: could not read the supported config keys of the INSTALLED alissa-tools-github-revloop (python3 import of its config module failed) — emitting every set key UNFILTERED. If this image is newer than the installed daemon, an unsupported key can still fail the boot with "unknown config key(s)".\n' >&2
  fi
}

# revloop_dist_supports <config_key> — true when the installed dist accepts the
# key, or when the probe could not tell (fail-open; see above).
revloop_dist_supports() {
  revloop_load_dist_facts
  [ -n "${_REVLOOP_DIST_KEYS}" ] || return 0
  printf '%s\n' "${_REVLOOP_DIST_KEYS}" | grep -qxF "$1"
}

# revloop_dist_version — the installed library's version string, "" if unknown.
revloop_dist_version() {
  revloop_load_dist_facts
  printf '%s' "${_REVLOOP_DIST_VERSION}"
}

# bow_owners_lines — ALISSA_REVIEW_BOW_OWNERS as one actor id per line.
#
# `|` (the daemon-family separator, shared with ALISSA_REVIEW_REPOS) AND `,`,
# the pair the library's own `normalize_bow_owners` splits on. SURROUNDING
# whitespace only -- deliberately NOT the `sed 's/[[:space:]]//g'` repos_lines
# uses: removing an INTERIOR space would repair an entry the daemon was going to
# reject, turning a wrapped copy/paste into a well-formed id for a DIFFERENT
# actor on a trust gate. Not casefolded (ids are opaque; folding could only
# widen the gate), duplicates collapsed exactly, first-seen order. Exits 0 on
# no owners, for the reason operators_lines' caller neutralises grep's status.
bow_owners_lines() {
  # shellcheck disable=SC2020
  printf '%s' "${ALISSA_REVIEW_BOW_OWNERS:-}" \
    | tr '|,' '\n\n' \
    | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
    | grep -v '^$' \
    | awk '!seen[$0]++' || true
}

# _skew_value <config_key> <env_var> <value> — <value> if the installed dist
# supports <config_key>, else "" after a WARNING naming both. Used for the three
# bows keys only (see the header).
_skew_value() {
  local key="$1" var="$2" value="$3"
  [ -n "${value}" ] || { printf ''; return 0; }
  if revloop_dist_supports "${key}"; then
    printf '%s' "${value}"
  else
    printf '[revloop-config] WARN: %s=%s is set, but the INSTALLED alissa-tools-github-revloop %s does NOT support the `%s` config key — dropping it from revloop.config.json so the daemon can still load. bows mode landed in revloop 0.29.0 — re-pin ARG REVLOOP_VERSION.\n' \
      "${var}" "${value}" "$(revloop_dist_version)" "${key}" >&2
    printf ''
  fi
}

render_revloop_config() {
  local repos_json="$1"
  # Operator logins allowed to re-open a capped PR with an
  # `alissa-review: re-enter +N` comment. Absent/empty -> the key is omitted and
  # the daemon honours no ack at all (see the daemon README).
  local operators_json="${2:-[]}"
  # The three bows keys, each skew-gated (empty = omitted, exactly like every
  # other optional key). bow_owners is a LIST: "[]" is the no-owners answer
  # and is NOT emitted -- an explicit empty list is not the same as unset (the
  # daemon resolves an absent bow_owners to "trust this token's own actor").
  local reposrc refreshpolls owners_json
  reposrc="$(_skew_value repos_source ALISSA_REVIEW_REPOS_SOURCE "$(printf '%s' "${ALISSA_REVIEW_REPOS_SOURCE:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')")"
  refreshpolls="$(_skew_value bows_refresh_polls ALISSA_REVIEW_BOWS_REFRESH_POLLS "$(printf '%s' "${ALISSA_REVIEW_BOWS_REFRESH_POLLS:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')")"
  owners_json="$(bow_owners_lines | jq -R . | jq -s -c .)"
  [ "${owners_json}" != "[]" ] || owners_json=""
  owners_json="$(_skew_value bow_owners ALISSA_REVIEW_BOW_OWNERS "${owners_json}")"
  # --arg (string) + tonumber for the numeric pass-through keys: an unset/empty
  # env var yields "" and the key is dropped, so the library default wins.
  jq -n \
    --argjson repos     "${repos_json}" \
    --argjson operators "${operators_json}" \
    --arg     reposrc   "${reposrc}" \
    --arg     rpolls    "${refreshpolls}" \
    --arg     owners    "${owners_json}" \
    --arg     hub    "${ALISSA_ON_MISSING_HUB:-add}" \
    --arg     agent  "${ALISSA_AGENT_PROFILE:-claude}" \
    --arg     poll   "${ALISSA_POLL_INTERVAL:-}" \
    --arg     cap    "${ALISSA_ROUND_CAP:-}" \
    --arg     stab   "${ALISSA_STABILITY_ROUNDS:-}" \
    --arg     grace  "${ALISSA_REAP_GRACE_SECONDS:-}" \
    --arg     scap   "${ALISSA_REAP_SESSION_CAP:-}" \
    --arg     gate   "${ALISSA_MAX_CONCURRENT_SESSIONS:-}" \
    --arg     cwait  "${ALISSA_CHECKS_WAIT_SECONDS:-}" \
    --arg     swait  "${ALISSA_CHECKS_SPAWN_WAIT_SECONDS:-}" \
    --arg     missttl "${ALISSA_REVIEW_TASK_MISS_TTL_POLLS:-}" \
    --arg     selfsc  "${ALISSA_TASK_LIST_SELF_SCOPE:-}" \
    --arg     levents "${ALISSA_REV_LOOP_EVENTS_ENABLED:-}" \
    --arg     rlogin "${ALISSA_REVIEWER_LOGIN:-}" \
    --arg     rtoken "${ALISSA_REVIEWER_TOKEN_ENV:-}" \
    '{ repos: $repos, on_missing_hub: $hub, agent_profile: $agent }
     + (if $poll  == "" then {} else { poll_interval:      ($poll  | tonumber) } end)
     + (if $cap   == "" then {} else { round_cap:          ($cap   | tonumber) } end)
     + (if $stab  == "" then {} else { stability_rounds:   ($stab  | tonumber) } end)
     + (if $grace == "" then {} else { reap_grace_seconds: ($grace | tonumber) } end)
     + (if $scap  == "" then {} else { reap_session_cap:   ($scap  | tonumber) } end)
     + (if $gate  == "" then {} else { max_concurrent_sessions: ($gate  | tonumber) } end)
     + (if $cwait == "" then {} else { checks_wait_seconds: ($cwait | tonumber) } end)
     + (if $swait == "" then {} else { checks_spawn_wait_seconds: ($swait | tonumber) } end)
     + (if $missttl == "" then {} else { review_task_miss_ttl_polls: ($missttl | tonumber) } end)
     + (if $selfsc == "" then {} else { task_list_self_scope: ($selfsc | ascii_downcase |
         if . == "1" or . == "true" or . == "yes" or . == "on" then true
         elif . == "0" or . == "false" or . == "no" or . == "off" then false
         else error("ALISSA_TASK_LIST_SELF_SCOPE must be a boolean (1/0, true/false, yes/no, on/off)")
         end) } end)
     + (if $levents == "" then {} else { loop_events_enabled: ($levents | ascii_downcase |
         if . == "1" or . == "true" or . == "yes" or . == "on" then true
         elif . == "0" or . == "false" or . == "no" or . == "off" then false
         else error("ALISSA_REV_LOOP_EVENTS_ENABLED must be a boolean (1/0, true/false, yes/no, on/off)")
         end) } end)
     + (if $rlogin == "" then {} else { reviewer_login:     $rlogin } end)
     + (if $rtoken == "" then {} else { reviewer_token_env: $rtoken } end)
     + (if ($operators | length) == 0 then {} else { operators: $operators } end)
     + (if $reposrc == "" then {} else { repos_source:       $reposrc } end)
     + (if $rpolls  == "" then {} else { bows_refresh_polls: ($rpolls | tonumber) } end)
     + (if $owners  == "" then {} else { bow_owners:         ($owners | fromjson) } end)'
}

# -- the bows path's render (issue #119) ----------------------------------------
#
# PROVENANCE. The bows path is CREATE-OR-REFRESH-OUR-OWN, never overwrite the
# operator's: a mounted revloop.config.json (one carrying, say, a hand-written
# bow_owners) must survive an upgrade, while our own earlier output must be
# refreshed so a later env change applies. Telling the two apart needs a stamp,
# so this render adds `_generated_by`. `_`-prefixed on purpose: `Config.build`
# treats such keys as comments, so the marker is inert to every daemon version.
#
# ONLY the bows render is stamped. The static (env-driven) render above stays
# byte-identical for an unchanged env -- that path regenerates unconditionally
# on every boot and never needed provenance -- and a static-generated file found
# by the bows path is therefore unstamped and treated as the OPERATOR's (left
# alone, logged by name). That is the fail-safe direction: the daemon still
# reads its mode and authority straight from the environment, so a respected
# stale file costs at most a stale `repos` seed, never a lost trust list.
CONFIG_PROVENANCE_KEY="_generated_by"
CONFIG_PROVENANCE_VALUE="docker/claude/revloop-config.sh"

render_revloop_config_bows() {
  local repos_json="${1:-[]}" operators_json="${2:-[]}"
  render_revloop_config "${repos_json}" "${operators_json}" \
    | jq --arg k "${CONFIG_PROVENANCE_KEY}" --arg v "${CONFIG_PROVENANCE_VALUE}" '. + {($k): $v}'
}

# config_is_generated <path> — true when <path> carries this file's stamp, i.e.
# an earlier bows-path boot of this container wrote it. FAIL-SAFE: missing,
# unreadable, not JSON, or unstamped all answer FALSE ("the operator's").
config_is_generated() {
  [ -f "$1" ] || return 1
  [ "$(jq -r --arg k "${CONFIG_PROVENANCE_KEY}" '.[$k] // empty' "$1" 2>/dev/null)" \
    = "${CONFIG_PROVENANCE_VALUE}" ]
}

# Direct execution renders to stdout; sourcing just defines the function.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  render_revloop_config "${1:?usage: revloop-config.sh <repos-json-array>}"
fi
