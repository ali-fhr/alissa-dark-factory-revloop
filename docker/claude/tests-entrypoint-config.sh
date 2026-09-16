#!/usr/bin/env bash
# =============================================================================
# Tests for the entrypoint's revloop.config.json renderer (#30).
#
# Proves the pass-through-when-unset contract:
#   * unset optional knobs  -> key OMITTED (library default applies)
#   * set optional knobs    -> key present with the given value (override wins)
#   * structural keys        -> always present with their pinned container value
#
# Pure shell + jq for the structural assertions (runs anywhere jq exists). A
# final cross-check, run only when the daemon package is importable, boots the
# omitted-key config through the real Config.build and asserts the effective
# value equals the library default — the acceptance criterion's "effective
# daemon config equals library defaults", verified against the actual library.
#
# The `repos_source: bows` keys (issue #119) get two more layers: the three
# ALISSA_REVIEW_REPOS_SOURCE / _BOWS_REFRESH_POLLS / _BOW_OWNERS variables are
# pass-through like the others (typed as string / int / JSON array) but SKEW-
# GATED on the installed library, and the entrypoint's bows arm is exercised by
# booting the REAL entrypoint against stubbed CLIs (the tests-entrypoint-ui.sh
# shape): bows + empty ALISSA_REVIEW_REPOS boots and renders the three keys,
# static + empty still dies, and an old pin falls through with the named WARN.
# The library on the probe's PYTHONPATH is what decides "old" vs "new", so both
# sides are replayed without installing a second wheel.
#
# Usage: bash docker/claude/tests-entrypoint-config.sh
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=revloop-config.sh
. "${HERE}/revloop-config.sh"

REPOS='["fahera-mx/studio.alissa.app"]'
fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1" >&2; fail=1; }

# assert_key_absent <json> <key> <label>
assert_key_absent() {
  if printf '%s' "$1" | jq -e "has(\"$2\")" >/dev/null; then
    bad "$3 (expected key '$2' absent, but present)"
  else
    pass "$3"
  fi
}
# assert_eq <json> <jq-filter> <expected> <label>
assert_eq() {
  local got; got="$(printf '%s' "$1" | jq -c "$2")"
  if [ "${got}" = "$3" ]; then pass "$4"; else bad "$4 (got ${got}, want $3)"; fi
}

echo "== pass-through: optional knobs omitted when env unset =="
out="$(env -u ALISSA_POLL_INTERVAL -u ALISSA_ROUND_CAP \
        -u ALISSA_STABILITY_ROUNDS \
        -u ALISSA_REAP_GRACE_SECONDS -u ALISSA_REAP_SESSION_CAP \
        -u ALISSA_MAX_CONCURRENT_SESSIONS \
        -u ALISSA_CHECKS_WAIT_SECONDS -u ALISSA_CHECKS_SPAWN_WAIT_SECONDS \
        -u ALISSA_REVIEW_TASK_MISS_TTL_POLLS -u ALISSA_TASK_LIST_SELF_SCOPE \
        -u ALISSA_REV_LOOP_EVENTS_ENABLED -u ALISSA_REV_FLEET_VITALS_ENABLED \
        bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
assert_key_absent "${out}" poll_interval "poll_interval omitted when ALISSA_POLL_INTERVAL unset"
assert_key_absent "${out}" round_cap     "round_cap omitted when ALISSA_ROUND_CAP unset"
assert_key_absent "${out}" stability_rounds \
  "stability_rounds omitted when ALISSA_STABILITY_ROUNDS unset"
assert_key_absent "${out}" reap_grace_seconds "reap_grace_seconds omitted when ALISSA_REAP_GRACE_SECONDS unset"
assert_key_absent "${out}" reap_session_cap   "reap_session_cap omitted when ALISSA_REAP_SESSION_CAP unset"
assert_key_absent "${out}" checks_wait_seconds \
  "checks_wait_seconds omitted when ALISSA_CHECKS_WAIT_SECONDS unset"
assert_key_absent "${out}" checks_spawn_wait_seconds \
  "checks_spawn_wait_seconds omitted when ALISSA_CHECKS_SPAWN_WAIT_SECONDS unset"
assert_key_absent "${out}" max_concurrent_sessions \
  "max_concurrent_sessions omitted when ALISSA_MAX_CONCURRENT_SESSIONS unset"
assert_key_absent "${out}" review_task_miss_ttl_polls \
  "review_task_miss_ttl_polls omitted when ALISSA_REVIEW_TASK_MISS_TTL_POLLS unset"
assert_key_absent "${out}" task_list_self_scope \
  "task_list_self_scope omitted when ALISSA_TASK_LIST_SELF_SCOPE unset"
assert_key_absent "${out}" loop_events_enabled \
  "loop_events_enabled omitted when ALISSA_REV_LOOP_EVENTS_ENABLED unset"
assert_key_absent "${out}" fleet_vitals_enabled \
  "fleet_vitals_enabled omitted when ALISSA_REV_FLEET_VITALS_ENABLED unset"
assert_eq "${out}" '.on_missing_hub' '"add"'    "on_missing_hub always emitted (structural: add)"
assert_eq "${out}" '.agent_profile'  '"claude"' "agent_profile always emitted (structural: claude)"
assert_eq "${out}" '.repos'          "${REPOS}" "repos emitted from allowlist"
assert_key_absent "${out}" operators "operators omitted when no allowlist is passed"

echo "== operators: pass-through list (empty omitted, set emitted verbatim) =="
out="$(render_revloop_config "${REPOS}" '[]')"
assert_key_absent "${out}" operators "operators omitted when the list is empty"
out="$(render_revloop_config "${REPOS}" '["RHDZMOTA","ops-bot"]')"
assert_eq "${out}" '.operators' '["RHDZMOTA","ops-bot"]' "operators emitted from allowlist"

echo "== reviewer identity: pass-through, and the NAME never the token (#51) =="
out="$(env -u ALISSA_REVIEWER_LOGIN -u ALISSA_REVIEWER_TOKEN_ENV \
        bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
assert_key_absent "${out}" reviewer_login     "reviewer_login omitted when unset"
assert_key_absent "${out}" reviewer_token_env "reviewer_token_env omitted when unset"
out="$(ALISSA_REVIEWER_LOGIN=alissa-app \
       ALISSA_REVIEWER_TOKEN_ENV=REVLOOP_REVIEWER_GH_TOKEN \
       render_revloop_config "${REPOS}")"
assert_eq "${out}" '.reviewer_login'     '"alissa-app"' "reviewer_login emitted verbatim"
assert_eq "${out}" '.reviewer_token_env' '"REVLOOP_REVIEWER_GH_TOKEN"' \
  "reviewer_token_env carries the variable NAME"

echo "== empty-string env is treated as unset (Dockerfile bakes empty ENV) =="
out="$(ALISSA_ROUND_CAP="" ALISSA_POLL_INTERVAL="" ALISSA_STABILITY_ROUNDS="" \
       render_revloop_config "${REPOS}")"
assert_key_absent "${out}" round_cap     "round_cap omitted when ALISSA_ROUND_CAP is empty"
assert_key_absent "${out}" stability_rounds \
  "stability_rounds omitted when ALISSA_STABILITY_ROUNDS is empty"
assert_key_absent "${out}" poll_interval "poll_interval omitted when ALISSA_POLL_INTERVAL is empty"

echo "== override: set env still wins, emitted as a JSON number =="
out="$(ALISSA_ROUND_CAP=7 ALISSA_POLL_INTERVAL=90 ALISSA_STABILITY_ROUNDS=5 \
       render_revloop_config "${REPOS}")"
assert_eq "${out}" '.round_cap'     '7'  "round_cap override present as number"
assert_eq "${out}" '.stability_rounds' '5' \
  "stability_rounds override present as number"
assert_eq "${out}" '.poll_interval' '90' "poll_interval override present as number"
out="$(ALISSA_STABILITY_ROUNDS=0 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.stability_rounds' '0' \
  "stability_rounds=0 (guard OFF) is emitted, not treated as unset"
out="$(ALISSA_REAP_GRACE_SECONDS=900 ALISSA_REAP_SESSION_CAP=3 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.reap_grace_seconds' '900' "reap_grace_seconds override present as number"
assert_eq "${out}" '.reap_session_cap'   '3'   "reap_session_cap override present as number"
out="$(ALISSA_CHECKS_WAIT_SECONDS=600 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.checks_wait_seconds' '600' "checks_wait_seconds override present as number"
# The PRE-SPAWN bound (issue #84) is a separate knob from the verdict-side one
# above: it is paid as latency on every round whose head is still building,
# while that one is paid only by a round that has finished reviewing. Both are
# properties of the watched repos' CI, so both pass through.
out="$(ALISSA_CHECKS_SPAWN_WAIT_SECONDS=300 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.checks_spawn_wait_seconds' '300' \
  "checks_spawn_wait_seconds override present as number"
# The spawn gate. Rendered alongside a coherent reap_session_cap on purpose: the
# daemon refuses a config whose ALARM sits below its spawn LIMIT, so an operator
# lowering one has to look at the other, and the pair is what the container ships.
out="$(ALISSA_MAX_CONCURRENT_SESSIONS=2 ALISSA_REAP_SESSION_CAP=4 \
       render_revloop_config "${REPOS}")"
assert_eq "${out}" '.max_concurrent_sessions' '2' \
  "max_concurrent_sessions override present as number"
assert_eq "${out}" '.reap_session_cap' '4' "and the alarm it must not exceed"

# The task-list bounds (issue #87). The TTL is an ordinary numeric pass-through;
# the self-scope is this renderer's only BOOLEAN one, so its accepted spellings
# and -- more importantly -- its refusal of anything else are pinned here: a
# typo that quietly rendered `false` would be indistinguishable from the default
# it was trying to change.
out="$(ALISSA_REVIEW_TASK_MISS_TTL_POLLS=4 render_revloop_config "${REPOS}")"
assert_eq "${out}" '.review_task_miss_ttl_polls' '4' \
  "review_task_miss_ttl_polls override present as number"
for truthy in 1 true TRUE yes on; do
  out="$(ALISSA_TASK_LIST_SELF_SCOPE="${truthy}" render_revloop_config "${REPOS}")"
  assert_eq "${out}" '.task_list_self_scope' 'true' \
    "task_list_self_scope=${truthy} renders JSON true"
done
for falsy in 0 false FALSE no off; do
  out="$(ALISSA_TASK_LIST_SELF_SCOPE="${falsy}" render_revloop_config "${REPOS}")"
  assert_eq "${out}" '.task_list_self_scope' 'false' \
    "task_list_self_scope=${falsy} renders JSON false"
done
if ALISSA_TASK_LIST_SELF_SCOPE=ture render_revloop_config "${REPOS}" >/dev/null 2>&1; then
  bad "a non-boolean ALISSA_TASK_LIST_SELF_SCOPE is refused, not silently false"
else
  pass "a non-boolean ALISSA_TASK_LIST_SELF_SCOPE is refused, not silently false"
fi

# Loop telemetry (issue #112): the renderer's second boolean pass-through, with
# the same accepted spellings and the same refusal of anything else. The daemon
# library also reads ALISSA_REV_LOOP_EVENTS_ENABLED directly (env wins over the
# rendered file), so this pin is about the render never CONTRADICTING the env.
for truthy in 1 true TRUE yes on; do
  out="$(ALISSA_REV_LOOP_EVENTS_ENABLED="${truthy}" render_revloop_config "${REPOS}")"
  assert_eq "${out}" '.loop_events_enabled' 'true' \
    "loop_events_enabled=${truthy} renders JSON true"
done
for falsy in 0 false FALSE no off; do
  out="$(ALISSA_REV_LOOP_EVENTS_ENABLED="${falsy}" render_revloop_config "${REPOS}")"
  assert_eq "${out}" '.loop_events_enabled' 'false' \
    "loop_events_enabled=${falsy} renders JSON false"
done
if ALISSA_REV_LOOP_EVENTS_ENABLED=enable render_revloop_config "${REPOS}" >/dev/null 2>&1; then
  bad "a non-boolean ALISSA_REV_LOOP_EVENTS_ENABLED is refused, not silently false"
else
  pass "a non-boolean ALISSA_REV_LOOP_EVENTS_ENABLED is refused, not silently false"
fi

# Fleet vitals (issue #126): the third boolean pass-through. Its garbage case
# is pinned HERE, against whatever library the probe finds (including an old
# pin that does not know the key): the spelling is refused BEFORE the skew
# gate, so a typo can never be swallowed as "unsupported, dropped". The
# set -> rendered cases run below, once SRC_TREE (a library that knows the
# key) is defined, because the skew gate rightly drops the key on an old pin.
if ALISSA_REV_FLEET_VITALS_ENABLED=enable render_revloop_config "${REPOS}" >/dev/null 2>"${TMPDIR:-/tmp}/fv-garbage.err"; then
  bad "a non-boolean ALISSA_REV_FLEET_VITALS_ENABLED is refused, not silently false"
else
  pass "a non-boolean ALISSA_REV_FLEET_VITALS_ENABLED is refused, not silently false"
fi
if grep -qF "ALISSA_REV_FLEET_VITALS_ENABLED must be a boolean" "${TMPDIR:-/tmp}/fv-garbage.err"; then
  pass "...and the refusal names the variable"
else
  bad "the fleet-vitals refusal did not name the variable: $(cat "${TMPDIR:-/tmp}/fv-garbage.err")"
fi
rm -f "${TMPDIR:-/tmp}/fv-garbage.err"

echo "== override: structural keys still overridable =="
out="$(ALISSA_ON_MISSING_HUB=skip ALISSA_AGENT_PROFILE=custom render_revloop_config "${REPOS}")"
assert_eq "${out}" '.on_missing_hub' '"skip"'   "on_missing_hub override wins"
assert_eq "${out}" '.agent_profile'  '"custom"' "agent_profile override wins"

echo "== cross-check: omitted keys resolve to the LIBRARY default =="
if python3 -c 'import alissa.tools.github.revloop.config' 2>/dev/null; then
  # ALISSA_STABILITY_ROUNDS, ALISSA_CHECKS_WAIT_SECONDS,
  # ALISSA_CHECKS_SPAWN_WAIT_SECONDS,
  # ALISSA_MAX_CONCURRENT_SESSIONS, ALISSA_REVIEW_TASK_MISS_TTL_POLLS,
  # ALISSA_TASK_LIST_SELF_SCOPE, ALISSA_REV_LOOP_EVENTS_ENABLED and
  # ALISSA_REV_FLEET_VITALS_ENABLED are unset here
  # too: the library this cross-check imports is the Dockerfile-PINNED release,
  # which predates those keys and would reject them as unknown. Rendering either
  # into the config would then fail the cross-check for a version skew rather
  # than for a default drift.
  out="$(env -u ALISSA_POLL_INTERVAL -u ALISSA_ROUND_CAP \
          -u ALISSA_STABILITY_ROUNDS \
          -u ALISSA_CHECKS_WAIT_SECONDS -u ALISSA_CHECKS_SPAWN_WAIT_SECONDS \
          -u ALISSA_MAX_CONCURRENT_SESSIONS \
          -u ALISSA_REVIEW_TASK_MISS_TTL_POLLS -u ALISSA_TASK_LIST_SELF_SCOPE \
          -u ALISSA_REV_LOOP_EVENTS_ENABLED -u ALISSA_REV_FLEET_VITALS_ENABLED \
          bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
  # Pass the rendered JSON via an env var (not a pipe) so the heredoc can own
  # stdin as the python program.
  if CONFIG_JSON="${out}" python3 <<'PY'
import json, os
from alissa.tools.github.revloop.config import Config
data = json.loads(os.environ["CONFIG_JSON"])
built = Config.build(workspace_root=".", file_data=data)
ref = Config(workspace_root=".")  # library defaults (dataclass fields)
assert "round_cap" not in data and "poll_interval" not in data, data
assert built.round_cap == ref.round_cap, (built.round_cap, ref.round_cap)
assert built.poll_interval == ref.poll_interval, (built.poll_interval, ref.poll_interval)
print(f"  ok   effective round_cap={built.round_cap} poll_interval={built.poll_interval} "
      f"== library defaults")
PY
  then :; else fail=1; fi
else
  echo "  skip (revloop package not importable — structural checks above still ran)"
fi

# =============================================================================
# repos_source: bows (issue #119)
# =============================================================================
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
SRC_TREE="${REPO_ROOT}/alissa-tools-github-revloop/src/main"
OWN_ID="j5706fv7xe5jy1k5wdwzacab9s8axcd2"
OTHER_ID="k7706fv7xe5jy1k5wdwzacab9s8axcd2"
BOWS_VARS=(-u ALISSA_REVIEW_REPOS_SOURCE -u ALISSA_REVIEW_BOWS_REFRESH_POLLS -u ALISSA_REVIEW_BOW_OWNERS)

TMPROOT="$(mktemp -d)"
cleanup() { rm -rf "${TMPROOT}"; }
trap cleanup EXIT

# A stub of the INSTALLED library as it looks on a pin that PREDATES the keys:
# the renderer's probe imports `alissa.tools.github.revloop.config.CONFIG_KEYS`
# and `.version.version.value`, nothing else, so a two-module package on
# PYTHONPATH replays the skew exactly.
STUB_OLD="${TMPROOT}/old-lib"
mkdir -p "${STUB_OLD}/alissa/tools/github/revloop"
for d in alissa alissa/tools alissa/tools/github alissa/tools/github/revloop; do
  : > "${STUB_OLD}/${d}/__init__.py"
done
cat > "${STUB_OLD}/alissa/tools/github/revloop/config.py" <<'STUB'
CONFIG_KEYS = ("hub_template", "poll_interval", "round_cap", "repos", "operators",
               "agent_profile", "reviewer_login", "reviewer_token_env", "state_path",
               "on_missing_review_task", "on_missing_hub", "dry_run")
STUB
cat > "${STUB_OLD}/alissa/tools/github/revloop/version.py" <<'STUB'
class _V:
    value = "0.16.14"
version = _V()
STUB

# render_with <pythonpath> <repos-json> [operators-json] — a fresh shell per
# render, because the probe memoises per process.
render_with() {
  local pp="$1" repos="$2" ops="${3:-[]}"
  PYTHONPATH="${pp}" bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config "$1" "$2"' _ "${repos}" "${ops}"
}
render_bows_with() {
  local pp="$1" repos="$2" ops="${3:-[]}"
  PYTHONPATH="${pp}" bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config_bows "$1" "$2"' _ "${repos}" "${ops}"
}

echo "== bows keys: omitted when unset (static render byte-identical) =="
base="$(env "${BOWS_VARS[@]}" bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
assert_key_absent "${base}" repos_source       "repos_source omitted when ALISSA_REVIEW_REPOS_SOURCE unset"
assert_key_absent "${base}" bows_refresh_polls "bows_refresh_polls omitted when ALISSA_REVIEW_BOWS_REFRESH_POLLS unset"
assert_key_absent "${base}" bow_owners         "bow_owners omitted when ALISSA_REVIEW_BOW_OWNERS unset"
assert_key_absent "${base}" _generated_by      "the static render carries NO provenance stamp"
blank="$(env "${BOWS_VARS[@]}" ALISSA_REVIEW_REPOS_SOURCE="" ALISSA_REVIEW_BOWS_REFRESH_POLLS=" " ALISSA_REVIEW_BOW_OWNERS="|," \
  bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
if [ "${blank}" = "${base}" ]; then
  pass "blank / separator-only values render byte-identically to unset"
else
  bad "blank values changed the render (Dockerfile bakes empty ENV)"
fi

echo "== bows keys: set -> rendered with their types (library that knows them) =="
out="$(env "${BOWS_VARS[@]}" ALISSA_REVIEW_REPOS_SOURCE=bows ALISSA_REVIEW_BOWS_REFRESH_POLLS=3 \
      ALISSA_REVIEW_BOW_OWNERS=" ${OWN_ID} | ${OTHER_ID},${OWN_ID}, " \
      PYTHONPATH="${SRC_TREE}" bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'' 2>"${TMPROOT}/render.err")"
assert_eq "${out}" '.repos_source'       '"bows"' "repos_source rendered as a string"
assert_eq "${out}" '.bows_refresh_polls' '3'      "bows_refresh_polls rendered as a JSON number"
assert_eq "${out}" '.bow_owners' "[\"${OWN_ID}\",\"${OTHER_ID}\"]" \
  "bow_owners rendered as a JSON array: |/, split, whitespace stripped, exact dedupe"
assert_eq "${out}" '.repos' "${REPOS}" "the static seed still rides along under bows"
if [ -s "${TMPROOT}/render.err" ]; then bad "no WARN when the library supports the keys ($(cat "${TMPROOT}/render.err"))"; else pass "no WARN when the library supports the keys"; fi
out="$(env "${BOWS_VARS[@]}" ALISSA_REVIEW_BOW_OWNERS="${OWN_ID}" PYTHONPATH="${SRC_TREE}" \
      bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
assert_eq "${out}" '.bow_owners' "[\"${OWN_ID}\"]" "a single owner needs no separator"
assert_key_absent "${out}" repos_source "owners alone do not imply the mode (the library decides)"
if env "${BOWS_VARS[@]}" ALISSA_REVIEW_BOWS_REFRESH_POLLS=five PYTHONPATH="${SRC_TREE}" \
     bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'' >/dev/null 2>&1; then
  bad "a non-numeric ALISSA_REVIEW_BOWS_REFRESH_POLLS is refused"
else
  pass "a non-numeric ALISSA_REVIEW_BOWS_REFRESH_POLLS is refused"
fi
out="$(render_bows_with "${SRC_TREE}" '[]' '["ops-bot"]')"
assert_eq "${out}" '.repos'         '[]'         "the bows render takes an empty seed"
assert_eq "${out}" '._generated_by' '"docker/claude/revloop-config.sh"' "the bows render is stamped with its provenance"
assert_eq "${out}" '.operators'     '["ops-bot"]' "operators pass through the bows render"
assert_eq "${out}" '.on_missing_hub' '"add"'     "on_missing_hub stays structural under bows (self-hub on demand)"

echo "== fleet vitals (issue #126): set -> rendered on a library that knows the key =="
for truthy in 1 true TRUE yes on; do
  out="$(env -u ALISSA_REV_FLEET_VITALS_ENABLED "${BOWS_VARS[@]}" ALISSA_REV_FLEET_VITALS_ENABLED="${truthy}" PYTHONPATH="${SRC_TREE}" \
        bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'' 2>"${TMPROOT}/fv.err")"
  assert_eq "${out}" '.fleet_vitals_enabled' 'true' \
    "fleet_vitals_enabled=${truthy} renders JSON true"
done
for falsy in 0 false FALSE no off; do
  out="$(env -u ALISSA_REV_FLEET_VITALS_ENABLED "${BOWS_VARS[@]}" ALISSA_REV_FLEET_VITALS_ENABLED="${falsy}" PYTHONPATH="${SRC_TREE}" \
        bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'' 2>"${TMPROOT}/fv.err")"
  assert_eq "${out}" '.fleet_vitals_enabled' 'false' \
    "fleet_vitals_enabled=${falsy} renders JSON false"
done
if [ -s "${TMPROOT}/fv.err" ]; then bad "no WARN when the library supports fleet_vitals_enabled ($(cat "${TMPROOT}/fv.err"))"; else pass "no WARN when the library supports fleet_vitals_enabled"; fi
out="$(env -u ALISSA_REV_FLEET_VITALS_ENABLED "${BOWS_VARS[@]}" ALISSA_REV_FLEET_VITALS_ENABLED="" PYTHONPATH="${SRC_TREE}" \
      bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'')"
assert_key_absent "${out}" fleet_vitals_enabled "a blank ALISSA_REV_FLEET_VITALS_ENABLED renders as unset (Dockerfile bakes empty ENV)"
if env -u ALISSA_REV_FLEET_VITALS_ENABLED "${BOWS_VARS[@]}" ALISSA_REV_FLEET_VITALS_ENABLED=enable PYTHONPATH="${SRC_TREE}" \
     bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'' >/dev/null 2>&1; then
  bad "a non-boolean ALISSA_REV_FLEET_VITALS_ENABLED is refused on a library that knows the key"
else
  pass "a non-boolean ALISSA_REV_FLEET_VITALS_ENABLED is refused on a library that knows the key"
fi
# The Dockerfile must carry the ARG (and its pass-through ENV line) -- and this
# PR must NOT re-pin REVLOOP_VERSION: CI installs the pinned release from PyPI,
# which cannot yet carry 0.30.0 (a sibling task re-pins once it is published).
if grep -qE '^ARG ALISSA_REV_FLEET_VITALS_ENABLED=""$' "${HERE}/Dockerfile" \
   && grep -qE '^\s*ALISSA_REV_FLEET_VITALS_ENABLED=\$\{ALISSA_REV_FLEET_VITALS_ENABLED\}' "${HERE}/Dockerfile"; then
  pass "Dockerfile carries ARG ALISSA_REV_FLEET_VITALS_ENABLED and passes it through ENV"
else
  bad "Dockerfile is missing the ALISSA_REV_FLEET_VITALS_ENABLED ARG / ENV pass-through"
fi
pin="$(grep -E '^ARG REVLOOP_VERSION=' "${HERE}/Dockerfile" | head -n 1)"
if [ -n "${pin}" ] && ! printf '%s' "${pin}" | grep -qF '0.30.0'; then
  pass "ARG REVLOOP_VERSION is untouched by the fleet-vitals change (${pin}; the re-pin is a sibling task's)"
else
  bad "ARG REVLOOP_VERSION was re-pinned to the unpublished release: ${pin}"
fi

echo "== fleet vitals: skew guard — an old pin drops the key with a WARN naming the re-pin =="
out="$(env -u ALISSA_REV_FLEET_VITALS_ENABLED "${BOWS_VARS[@]}" ALISSA_REV_FLEET_VITALS_ENABLED=1 \
      PYTHONPATH="${STUB_OLD}" bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'' 2>"${TMPROOT}/fv-skew.err")"
assert_key_absent "${out}" fleet_vitals_enabled "fleet_vitals_enabled dropped on an old pin"
if grep -qF "fleet vitals landed in revloop 0.30.0 — re-pin ARG REVLOOP_VERSION" "${TMPROOT}/fv-skew.err" \
   && grep -qF "ALISSA_REV_FLEET_VITALS_ENABLED=1" "${TMPROOT}/fv-skew.err"; then
  pass "the drop is WARNed by variable and re-pin"
else
  bad "fleet-vitals skew WARN missing or unnamed: $(cat "${TMPROOT}/fv-skew.err")"
fi
if env -u ALISSA_REV_FLEET_VITALS_ENABLED "${BOWS_VARS[@]}" ALISSA_REV_FLEET_VITALS_ENABLED=enable PYTHONPATH="${STUB_OLD}" \
     bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'' >/dev/null 2>&1; then
  bad "garbage ALISSA_REV_FLEET_VITALS_ENABLED is refused even on an old pin (never swallowed by the skew gate)"
else
  pass "garbage ALISSA_REV_FLEET_VITALS_ENABLED is refused even on an old pin (never swallowed by the skew gate)"
fi

echo "== bows keys: skew guard — an old pin drops them with a WARN naming the re-pin =="
out="$(env "${BOWS_VARS[@]}" ALISSA_REVIEW_REPOS_SOURCE=bows ALISSA_REVIEW_BOWS_REFRESH_POLLS=3 ALISSA_REVIEW_BOW_OWNERS="${OWN_ID}" \
      PYTHONPATH="${STUB_OLD}" bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'' 2>"${TMPROOT}/skew.err")"
assert_key_absent "${out}" repos_source       "repos_source dropped on an old pin"
assert_key_absent "${out}" bows_refresh_polls "bows_refresh_polls dropped on an old pin"
assert_key_absent "${out}" bow_owners         "bow_owners dropped on an old pin"
assert_eq "${out}" '.repos' "${REPOS}" "the static allowlist still renders on an old pin"
if grep -qF "bows mode landed in revloop 0.29.0 — re-pin ARG REVLOOP_VERSION" "${TMPROOT}/skew.err" \
   && grep -qF "0.16.14" "${TMPROOT}/skew.err" && grep -qF "ALISSA_REVIEW_REPOS_SOURCE=bows" "${TMPROOT}/skew.err"; then
  pass "the drop is WARNed by variable, installed version and re-pin"
else
  bad "skew WARN missing or unnamed: $(cat "${TMPROOT}/skew.err")"
fi
out="$(env "${BOWS_VARS[@]}" PYTHONPATH="${STUB_OLD}" bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config '"'${REPOS}'"'' 2>"${TMPROOT}/quiet.err")"
if [ -s "${TMPROOT}/quiet.err" ]; then bad "an old pin with the keys UNSET warns about nothing"; else pass "an old pin with the keys UNSET warns about nothing"; fi
# Fail-open: no library at all -> keys pass through, after saying so.
out="$(env "${BOWS_VARS[@]}" ALISSA_REVIEW_REPOS_SOURCE=bows PYTHONPATH="${TMPROOT}/nowhere" \
      bash -c 'unset PYTHONHOME; . "'"${HERE}"'/revloop-config.sh"; revloop_installed_dist_facts() { return 1; }; render_revloop_config '"'${REPOS}'"'' 2>"${TMPROOT}/open.err")"
assert_eq "${out}" '.repos_source' '"bows"' "an unprobeable library FAILS OPEN (key emitted)"
if grep -qF "emitting every set key UNFILTERED" "${TMPROOT}/open.err"; then pass "...and says so"; else bad "fail-open was silent"; fi

# -----------------------------------------------------------------------------
# Booting the REAL entrypoint against stubbed CLIs (tests-entrypoint-ui.sh's
# sandbox), so the bows arm, the static die and the skew fall-through are
# exercised as the container would run them.
# -----------------------------------------------------------------------------
echo "== entrypoint: the bows arm, the static die, the skew fall-through =="
ENTRYPOINT="${HERE}/entrypoint.sh"
BIN="${TMPROOT}/bin"; mkdir -p "${BIN}"
FAKE_HOME="${TMPROOT}/home"; mkdir -p "${FAKE_HOME}/.config/alissa"
MARKERS="${TMPROOT}/markers"; mkdir -p "${MARKERS}"
cp "${HERE}/agents.yaml" "${FAKE_HOME}/.config/alissa/agents.yaml"
cat > "${BIN}/gh" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  "api user -q .login") echo alissa-app ;;
esac
exit 0
STUB
cat > "${BIN}/alissa" <<STUB
#!/usr/bin/env bash
case "\$1 \$2" in
  "auth login")     ;;
  "worker start")   : > "${MARKERS}/worker-started" ;;
  "worker status")  [ -f "${MARKERS}/worker-started" ] && echo "worker is running" || echo "worker not running" ;;
  "worker stop")    : > "${MARKERS}/worker-stopped" ;;
  "code workspace") ;;
esac
exit 0
STUB
cat > "${BIN}/alissa-revloop" <<'STUB'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
sleep 600 &
wait $!
STUB
chmod 0755 "${BIN}/gh" "${BIN}/alissa" "${BIN}/alissa-revloop"

EP_PID=""
# boot <workspace> <logfile> <pythonpath> [env assignments...]
boot() {
  local ws="$1" log="$2" pp="$3"; shift 3
  mkdir -p "${ws}"
  env -i \
    PATH="${BIN}:/usr/local/bin:/usr/bin:/bin" \
    HOME="${FAKE_HOME}" \
    TMUX_TMPDIR="${TMPROOT}/tmux" \
    ALISSA_WORKSPACE_ROOT="${ws}" \
    GH_TOKEN=stub-gh-token \
    ALISSA_API_TOKEN=stub-alissa-token \
    PYTHONPATH="${pp}" \
    "$@" \
    bash "${ENTRYPOINT}" > "${log}" 2>&1 &
  EP_PID=$!
}
wait_for_log() {
  local i
  for i in $(seq 1 "$3"); do
    grep -qF -- "$2" "$1" && return 0
    sleep 1
  done
  return 1
}
stop_boot() {
  local pid="$1" i
  kill -TERM "${pid}" 2>/dev/null || true
  for i in $(seq 1 15); do
    kill -0 "${pid}" 2>/dev/null || return 0
    sleep 1
  done
  kill -KILL "${pid}" 2>/dev/null || true
}
assert_log() { if grep -qF -- "$2" "$1"; then pass "$3"; else bad "$3 (not in log: $2)"; fi; }
assert_no_log() { if grep -qF -- "$2" "$1"; then bad "$3 (unexpected in log: $2)"; else pass "$3"; fi; }

# --- 1. bows + EMPTY ALISSA_REVIEW_REPOS boots and renders the three keys ----
WS1="${TMPROOT}/ws-bows"; LOG1="${TMPROOT}/bows.log"
rm -f "${MARKERS}"/*
boot "${WS1}" "${LOG1}" "${SRC_TREE}" ALISSA_REVIEW_REPOS="" ALISSA_REVIEW_REPOS_SOURCE=bows \
  ALISSA_REVIEW_BOWS_REFRESH_POLLS=3 ALISSA_REVIEW_BOW_OWNERS="${OWN_ID}|${OTHER_ID}"; PID1="${EP_PID}"
if wait_for_log "${LOG1}" "alissa worker is running" 45; then
  pass "bows + empty ALISSA_REVIEW_REPOS boots to the worker-up milestone"
else
  bad "bows + empty ALISSA_REVIEW_REPOS did not boot (see ${LOG1})"; sed 's/^/      | /' "${LOG1}" | tail -20 >&2
fi
assert_log "${LOG1}" "repos_source=bows with no static repos" "the bows arm is taken and named"
assert_no_log "${LOG1}" "ALISSA_REVIEW_REPOS is empty — nothing to work on" "the static die does NOT fire"
CFG1="${WS1}/revloop.config.json"
if [ -f "${CFG1}" ]; then
  assert_eq "$(cat "${CFG1}")" '.repos' '[]' "generated config has an empty static seed"
  assert_eq "$(cat "${CFG1}")" '.repos_source' '"bows"' "generated config renders repos_source"
  assert_eq "$(cat "${CFG1}")" '.bows_refresh_polls' '3' "generated config renders bows_refresh_polls as a number"
  assert_eq "$(cat "${CFG1}")" '.bow_owners' "[\"${OWN_ID}\",\"${OTHER_ID}\"]" "generated config renders bow_owners as an array"
  assert_eq "$(cat "${CFG1}")" '.on_missing_hub' '"add"' "on_missing_hub=add rides along (self-hub on the first request)"
  assert_eq "$(cat "${CFG1}")" '._generated_by' '"docker/claude/revloop-config.sh"' "the bows-path config is stamped"
else
  bad "no revloop.config.json generated on the bows path"
fi
if [ -f "${WS1}/alissa-workspace.yaml" ] && grep -qF 'repos: []' "${WS1}/alissa-workspace.yaml"; then
  pass "manifest written with an empty repo list"
else
  bad "manifest missing or not empty on the bows path"
fi
stop_boot "${PID1}"

# --- 1b. a second boot REFRESHES our own stamped config, RESPECTS a foreign one
LOG1B="${TMPROOT}/bows-refresh.log"
rm -f "${MARKERS}"/*
boot "${WS1}" "${LOG1B}" "${SRC_TREE}" ALISSA_REVIEW_REPOS="" ALISSA_REVIEW_REPOS_SOURCE=bows \
  ALISSA_REVIEW_BOWS_REFRESH_POLLS=7 ALISSA_REVIEW_BOW_OWNERS="${OWN_ID}"; PID1B="${EP_PID}"
wait_for_log "${LOG1B}" "alissa worker is running" 45 || bad "second bows boot did not come up"
assert_eq "$(cat "${CFG1}")" '.bows_refresh_polls' '7' "our own stamped config is regenerated from the changed env"
assert_log "${LOG1B}" "respecting the existing ${WS1}/alissa-workspace.yaml" "a manifest already on the volume is left alone"
stop_boot "${PID1B}"
printf '{"repos": ["mounted/repo"], "bow_owners": ["%s"]}\n' "${OTHER_ID}" > "${CFG1}"
LOG1C="${TMPROOT}/bows-mounted.log"
rm -f "${MARKERS}"/*
boot "${WS1}" "${LOG1C}" "${SRC_TREE}" ALISSA_REVIEW_REPOS="" ALISSA_REVIEW_REPOS_SOURCE=bows; PID1C="${EP_PID}"
wait_for_log "${LOG1C}" "alissa worker is running" 45 || bad "bows boot over a mounted config did not come up"
assert_log "${LOG1C}" "respecting the existing ${CFG1}" "an unstamped (operator) config is NOT overwritten, and the decision is logged by name"
assert_eq "$(cat "${CFG1}")" '.repos' '["mounted/repo"]' "the operator's config is byte-for-byte untouched"
stop_boot "${PID1C}"

# --- 2. static + EMPTY ALISSA_REVIEW_REPOS still dies -------------------------
WS2="${TMPROOT}/ws-static"; LOG2="${TMPROOT}/static.log"
rm -f "${MARKERS}"/*
boot "${WS2}" "${LOG2}" "${SRC_TREE}" ALISSA_REVIEW_REPOS=""; PID2="${EP_PID}"
set +e; wait "${PID2}"; rc2=$?; set -e
[ "${rc2}" -ne 0 ] && pass "static + empty ALISSA_REVIEW_REPOS exits non-zero (${rc2})" || bad "static + empty booted (must die)"
assert_log "${LOG2}" "ALISSA_REVIEW_REPOS is empty — nothing to work on" "...with the static path's own reason"
assert_log "${LOG2}" "required under repos_source=static" "...which now names the static requirement"
[ -f "${MARKERS}/worker-started" ] && bad "died AFTER starting the worker" || pass "dies before any worker starts"
[ -f "${WS2}/revloop.config.json" ] && bad "static + empty wrote a config" || pass "static + empty writes no config"

# --- 3. skew: bows requested on an OLD pin, ALISSA_REVIEW_REPOS set -> static, WARN
WS3="${TMPROOT}/ws-skew"; LOG3="${TMPROOT}/skew.log"
rm -f "${MARKERS}"/*
boot "${WS3}" "${LOG3}" "${STUB_OLD}" ALISSA_REVIEW_REPOS="fahera-mx/example-repo" ALISSA_REVIEW_REPOS_SOURCE=bows \
  ALISSA_REVIEW_BOW_OWNERS="${OWN_ID}"; PID3="${EP_PID}"
if wait_for_log "${LOG3}" "alissa worker is running" 45; then
  pass "an old pin with bows requested still boots (non-fatal)"
else
  bad "skew boot did not come up (see ${LOG3})"; sed 's/^/      | /' "${LOG3}" | tail -20 >&2
fi
assert_log "${LOG3}" "bows mode landed in revloop 0.29.0 — re-pin ARG REVLOOP_VERSION" "the skew guard WARNs by name (non-silent)"
assert_log "${LOG3}" "INSTALLED alissa-tools-github-revloop 0.16.14 does NOT support the 'repos_source' config key" "...naming the installed version"
assert_no_log "${LOG3}" "repos_source=bows with no static repos" "the bows arm is NOT taken"
CFG3="${WS3}/revloop.config.json"
assert_eq "$(cat "${CFG3}")" '.repos' '["fahera-mx/example-repo"]' "falls through to the static allowlist"
assert_key_absent "$(cat "${CFG3}")" repos_source "...and the old library is handed no key it cannot load"
assert_key_absent "$(cat "${CFG3}")" bow_owners   "...bow_owners included"
assert_key_absent "$(cat "${CFG3}")" _generated_by "...on the unstamped static render"
stop_boot "${PID3}"

# --- 3b. skew with NOTHING static: the WARN precedes the static die -----------
WS4="${TMPROOT}/ws-skew-empty"; LOG4="${TMPROOT}/skew-empty.log"
rm -f "${MARKERS}"/*
boot "${WS4}" "${LOG4}" "${STUB_OLD}" ALISSA_REVIEW_REPOS="" ALISSA_REVIEW_REPOS_SOURCE=bows; PID4="${EP_PID}"
set +e; wait "${PID4}"; rc4=$?; set -e
[ "${rc4}" -ne 0 ] && pass "an old pin with bows and no static list exits non-zero (${rc4})" || bad "expected the static die on an old pin with no allowlist"
assert_log "${LOG4}" "re-pin ARG REVLOOP_VERSION" "...after the skew WARN named the fix"
assert_log "${LOG4}" "ALISSA_REVIEW_REPOS is empty — nothing to work on" "...and the static die names its own reason"

# --- 3c. DOWNGRADE: our own stamped bows config on the volume, old pin, empty seed
# (PR #120 round-1 [minor]): an earlier >= 0.29.0 boot wrote the stamped
# config + empty manifest; the old pin must die by name at the guard, not
# crash-loop the daemon on the file's unknown keys.
WS5="${TMPROOT}/ws-downgrade"; LOG5="${TMPROOT}/downgrade.log"; mkdir -p "${WS5}"
env "${BOWS_VARS[@]}" ALISSA_REVIEW_REPOS_SOURCE=bows ALISSA_REVIEW_BOWS_REFRESH_POLLS=3 ALISSA_REVIEW_BOW_OWNERS="${OWN_ID}" \
  PYTHONPATH="${SRC_TREE}" bash -c '. "'"${HERE}"'/revloop-config.sh"; render_revloop_config_bows "[]" "[]"' > "${WS5}/revloop.config.json"
assert_eq "$(cat "${WS5}/revloop.config.json")" '.repos_source' '"bows"' "seeded: a >= 0.29.0 boot's stamped config carries the keys"
printf 'name: ws\ndescription: d\nrepos: []\nreviewers: []\nskills: []\nattributes: {}\n' > "${WS5}/alissa-workspace.yaml"
rm -f "${MARKERS}"/*
boot "${WS5}" "${LOG5}" "${STUB_OLD}" ALISSA_REVIEW_REPOS="" ALISSA_REVIEW_REPOS_SOURCE=bows; PID5="${EP_PID}"
set +e; wait "${PID5}"; rc5=$?; set -e
[ "${rc5}" -ne 0 ] && pass "a downgrade over our own stamped config exits non-zero (${rc5})" || bad "a downgrade over our own stamped config booted (the old daemon would crash-loop on its keys)"
assert_log "${LOG5}" "re-pin ARG REVLOOP_VERSION" "...after the skew WARN named the fix"
assert_log "${LOG5}" "downgrade: ${WS5}/revloop.config.json is this container's OWN bows-path output" "...and the die names the stamped file and the downgrade"
assert_log "${LOG5}" "could widen the watch to every PR" "...and why an empty static allowlist is not handed on"
assert_no_log "${LOG5}" "using mounted workspace" "the mounted-mode arm is NOT reached"
[ -f "${MARKERS}/worker-started" ] && bad "downgrade died AFTER starting the worker" || pass "downgrade dies before any worker starts"
assert_eq "$(cat "${WS5}/revloop.config.json")" '.repos_source' '"bows"' "the stamped config is left intact for the re-pin"

# --- 3d. ...but an UNSTAMPED (operator) config on the same boot is respected
printf '{"repos": ["mounted/repo"]}\n' > "${WS5}/revloop.config.json"
LOG5B="${TMPROOT}/downgrade-mounted.log"
rm -f "${MARKERS}"/*
boot "${WS5}" "${LOG5B}" "${STUB_OLD}" ALISSA_REVIEW_REPOS="" ALISSA_REVIEW_REPOS_SOURCE=bows; PID5B="${EP_PID}"
if wait_for_log "${LOG5B}" "alissa worker is running" 45; then
  pass "an old pin over an unstamped config with a mounted manifest still boots"
else
  bad "old pin over an unstamped config did not boot (see ${LOG5B})"; sed 's/^/      | /' "${LOG5B}" | tail -20 >&2
fi
assert_log "${LOG5B}" "using mounted workspace" "...through the mounted-mode arm"
assert_no_log "${LOG5B}" "downgrade:" "...without the downgrade die (the file is not ours)"
assert_eq "$(cat "${WS5}/revloop.config.json")" '.repos' '["mounted/repo"]' "the operator's config is untouched"
stop_boot "${PID5B}"

# -----------------------------------------------------------------------------
# The alissa skills-dir pin (issue #132): with CLAUDE_CONFIG_DIR set, Claude
# reads personal skills from $CLAUDE_CONFIG_DIR/skills, so the entrypoint pins
# the alissa CLI's `skillsDir` there — merged into the CLI's config.json (other
# keys preserved), the directory created as the runtime user, a hand-placed
# stop-gap symlink converted into a real directory, one log line. Blank
# CLAUDE_CONFIG_DIR: nothing changes. Same stubbed-CLI boots as above.
# -----------------------------------------------------------------------------
echo "== entrypoint: alissa skillsDir pinned to \$CLAUDE_CONFIG_DIR/skills (#132) =="
ALISSA_CFG="${FAKE_HOME}/.config/alissa/config.json"
PIN_LINE="(Claude reads personal skills there when CLAUDE_CONFIG_DIR is set)"
WS6="${TMPROOT}/ws-skills"

# --- 1. pin written: skillsDir merged, other keys preserved, dir owned by us --
CC1="${TMPROOT}/claude-config-1"; LOG6="${TMPROOT}/skills-pin.log"
printf '{"token": "stub-alissa-token", "apiBase": "https://api.example.test", "autoUpdate": false}\n' > "${ALISSA_CFG}"
chmod 0600 "${ALISSA_CFG}"     # the mode the CLI chose for its token file must survive the rewrite
rm -f "${MARKERS}"/*
boot "${WS6}" "${LOG6}" "${SRC_TREE}" ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app" \
  CLAUDE_CONFIG_DIR="${CC1}"; PID6="${EP_PID}"
if wait_for_log "${LOG6}" "alissa worker is running" 45; then
  pass "boot with CLAUDE_CONFIG_DIR set reaches the worker-up milestone"
else
  bad "boot with CLAUDE_CONFIG_DIR set did not come up (see ${LOG6})"; sed 's/^/      | /' "${LOG6}" | tail -20 >&2
fi
stop_boot "${PID6}"
assert_eq "$(cat "${ALISSA_CFG}")" '.skillsDir' "\"${CC1}/skills\"" "config.json carries skillsDir=\$CLAUDE_CONFIG_DIR/skills"
assert_eq "$(cat "${ALISSA_CFG}")" '.token'      '"stub-alissa-token"'          "...the verified token is preserved (merge, not clobber)"
assert_eq "$(cat "${ALISSA_CFG}")" '.apiBase'    '"https://api.example.test"'   "...apiBase is preserved"
assert_eq "$(cat "${ALISSA_CFG}")" '.autoUpdate' 'false'                        "...a boolean key is preserved with its type"
assert_eq "$(cat "${ALISSA_CFG}")" 'keys|length' '4'                            "...and no other key was added"
if [ "$(stat -c %a "${ALISSA_CFG}")" = "600" ]; then pass "...and the file keeps its 0600 mode"; else bad "...config.json mode is now $(stat -c %a "${ALISSA_CFG}"), expected 600"; fi
if [ -d "${CC1}/skills" ] && [ ! -L "${CC1}/skills" ]; then
  pass "\$CLAUDE_CONFIG_DIR/skills exists as a real directory"
else
  bad "\$CLAUDE_CONFIG_DIR/skills missing or not a real directory"
fi
if [ "$(stat -c %U "${CC1}/skills")" = "$(id -un)" ]; then
  pass "...owned by the user the entrypoint runs as (the runtime user after the drop)"
else
  bad "...owned by $(stat -c %U "${CC1}/skills"), expected $(id -un)"
fi
assert_log "${LOG6}" "skills dir pinned to ${CC1}/skills ${PIN_LINE}" "the pin is logged by path"
if [ "$(grep -cF -- "${PIN_LINE}" "${LOG6}")" = "1" ]; then
  pass "...exactly once"
else
  bad "the pin line appears $(grep -cF -- "${PIN_LINE}" "${LOG6}") times, expected 1"
fi
assert_no_log "${LOG6}" "converted the stop-gap symlink" "no symlink conversion is claimed when there was none"

# --- 2. CLAUDE_CONFIG_DIR unset (and blank): nothing changes --------------------
printf '{"token": "stub-alissa-token", "apiBase": "https://api.example.test"}\n' > "${ALISSA_CFG}"
CFG_BEFORE="$(cat "${ALISSA_CFG}")"
for blank in unset "" "   "; do
  LOG6B="${TMPROOT}/skills-unset-${#blank}.log"
  rm -f "${MARKERS}"/*
  if [ "${blank}" = "unset" ]; then
    boot "${WS6}" "${LOG6B}" "${SRC_TREE}" ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app"; PID6B="${EP_PID}"
  else
    boot "${WS6}" "${LOG6B}" "${SRC_TREE}" ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app" CLAUDE_CONFIG_DIR="${blank}"; PID6B="${EP_PID}"
  fi
  wait_for_log "${LOG6B}" "alissa worker is running" 45 || bad "boot with CLAUDE_CONFIG_DIR=${blank@Q} did not come up (see ${LOG6B})"
  stop_boot "${PID6B}"
  if [ "$(cat "${ALISSA_CFG}")" = "${CFG_BEFORE}" ]; then
    pass "CLAUDE_CONFIG_DIR ${blank@Q}: config.json is byte-for-byte untouched (no skillsDir)"
  else
    bad "CLAUDE_CONFIG_DIR ${blank@Q}: config.json changed: $(cat "${ALISSA_CFG}")"
  fi
  assert_no_log "${LOG6B}" "skills dir pinned" "CLAUDE_CONFIG_DIR ${blank@Q}: no pin is logged"
  if [ -e "${FAKE_HOME}/.claude/skills" ] || [ -e "${FAKE_HOME}/skills" ] || [ -e "${TMPROOT}/skills" ]; then
    bad "CLAUDE_CONFIG_DIR ${blank@Q}: a skills dir was created somewhere it should not be"
  else
    pass "CLAUDE_CONFIG_DIR ${blank@Q}: no skills dir is created anywhere"
  fi
done

# --- 3. a pre-existing stop-gap SYMLINK becomes a real dir, contents preserved -
CC3="${TMPROOT}/claude-config-3"; LOG6C="${TMPROOT}/skills-symlink.log"
LINK_TARGET="${TMPROOT}/home-skills"
mkdir -p "${CC3}" "${LINK_TARGET}/alissa-code-review/references"
printf 'name: alissa-code-review\n' > "${LINK_TARGET}/alissa-code-review/SKILL.md"
printf 'verdict envelope\n' > "${LINK_TARGET}/alissa-code-review/references/verdict-envelope.md"
ln -s "${LINK_TARGET}" "${CC3}/skills"
rm -f "${ALISSA_CFG}"     # no config.json at all: a fresh install, the stub login wrote nothing
rm -f "${MARKERS}"/*
boot "${WS6}" "${LOG6C}" "${SRC_TREE}" ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app" CLAUDE_CONFIG_DIR="${CC3}"; PID6C="${EP_PID}"
wait_for_log "${LOG6C}" "alissa worker is running" 45 || { bad "symlink boot did not come up (see ${LOG6C})"; sed 's/^/      | /' "${LOG6C}" | tail -20 >&2; }
stop_boot "${PID6C}"
if [ -d "${CC3}/skills" ] && [ ! -L "${CC3}/skills" ]; then
  pass "the stop-gap symlink was replaced by a real directory"
else
  bad "\$CLAUDE_CONFIG_DIR/skills is still a symlink (or missing)"
fi
if [ "$(cat "${CC3}/skills/alissa-code-review/SKILL.md" 2>/dev/null)" = "name: alissa-code-review" ] \
   && [ "$(cat "${CC3}/skills/alissa-code-review/references/verdict-envelope.md" 2>/dev/null)" = "verdict envelope" ]; then
  pass "...with the link target's contents copied in (nested files intact)"
else
  bad "...but the link target's contents were not carried over"
fi
if [ -f "${LINK_TARGET}/alissa-code-review/SKILL.md" ]; then
  pass "...and the former target is left untouched"
else
  bad "...the former link target was modified"
fi
assert_log "${LOG6C}" "converted the stop-gap symlink ${CC3}/skills -> ${LINK_TARGET} into a real directory" "the conversion is logged by name"
if [ -f "${ALISSA_CFG}" ]; then
  assert_eq "$(cat "${ALISSA_CFG}")" '.skillsDir' "\"${CC3}/skills\"" "a missing config.json is created with the pin"
  assert_eq "$(cat "${ALISSA_CFG}")" 'keys' '["skillsDir"]' "...and nothing else"
else
  bad "no config.json was created for the pin"
fi
assert_log "${LOG6C}" "skills dir pinned to ${CC3}/skills ${PIN_LINE}" "the pin is logged after the conversion"

# --- 4. a pre-existing REAL directory is left alone --------------------------
CC4="${TMPROOT}/claude-config-4"; LOG6D="${TMPROOT}/skills-realdir.log"
mkdir -p "${CC4}/skills/alissa-code-workspace"
printf 'name: alissa-code-workspace\n' > "${CC4}/skills/alissa-code-workspace/SKILL.md"
INODE_BEFORE="$(stat -c %i "${CC4}/skills")"
printf '{"token": "stub-alissa-token", "skillsDir": "/somewhere/stale"}\n' > "${ALISSA_CFG}"
rm -f "${MARKERS}"/*
boot "${WS6}" "${LOG6D}" "${SRC_TREE}" ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app" CLAUDE_CONFIG_DIR="${CC4}"; PID6D="${EP_PID}"
wait_for_log "${LOG6D}" "alissa worker is running" 45 || bad "real-dir boot did not come up (see ${LOG6D})"
stop_boot "${PID6D}"
if [ "$(stat -c %i "${CC4}/skills")" = "${INODE_BEFORE}" ] && [ ! -L "${CC4}/skills" ]; then
  pass "a pre-existing real directory is the same inode after boot (untouched)"
else
  bad "the pre-existing real directory was replaced"
fi
if [ "$(cat "${CC4}/skills/alissa-code-workspace/SKILL.md" 2>/dev/null)" = "name: alissa-code-workspace" ]; then
  pass "...its contents are intact"
else
  bad "...its contents were lost"
fi
assert_no_log "${LOG6D}" "converted the stop-gap symlink" "no conversion is logged for a real directory"
assert_eq "$(cat "${ALISSA_CFG}")" '.skillsDir' "\"${CC4}/skills\"" "a stale skillsDir is re-pinned to \$CLAUDE_CONFIG_DIR/skills"
assert_eq "$(cat "${ALISSA_CFG}")" '.token' '"stub-alissa-token"' "...the token still preserved"
if [ "$(grep -cF -- "${PIN_LINE}" "${LOG6D}")" = "1" ]; then pass "the pin line appears exactly once"; else bad "pin line count $(grep -cF -- "${PIN_LINE}" "${LOG6D}")"; fi

# --- 5. a config.json that is not JSON is never overwritten ------------------
CC5="${TMPROOT}/claude-config-5"; LOG6E="${TMPROOT}/skills-badjson.log"
printf 'not json {\n' > "${ALISSA_CFG}"
rm -f "${MARKERS}"/*
boot "${WS6}" "${LOG6E}" "${SRC_TREE}" ALISSA_REVIEW_REPOS="fahera-mx/studio.alissa.app" CLAUDE_CONFIG_DIR="${CC5}"; PID6E="${EP_PID}"
wait_for_log "${LOG6E}" "alissa worker is running" 45 || bad "bad-json boot did not come up (see ${LOG6E})"
stop_boot "${PID6E}"
if [ "$(cat "${ALISSA_CFG}")" = "not json {" ]; then pass "an unparseable config.json is left as it was"; else bad "an unparseable config.json was overwritten"; fi
assert_log "${LOG6E}" "not pinning skillsDir over it" "...and the refusal is logged"
assert_no_log "${LOG6E}" "skills dir pinned" "...no pin is claimed"
assert_log "${LOG6E}" "WARN: could not write skillsDir=${CC5}/skills" "...the WARN names the manual fix"
if [ -d "${CC5}/skills" ]; then pass "the directory is still created (it costs nothing and the CLI may pin later)"; else bad "the directory was not created"; fi
rm -f "${ALISSA_CFG}"

echo
[ "${fail}" = "0" ] && { echo "ALL PASS"; exit 0; } || { echo "FAILURES"; exit 1; }
