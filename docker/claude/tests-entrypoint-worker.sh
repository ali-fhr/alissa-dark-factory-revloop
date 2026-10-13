#!/usr/bin/env bash
# =============================================================================
# Tests for the entrypoint's stale WORKER-pidfile sweep (issue #169).
#
# The defect (devloop, 2026-10-13): `alissa worker start` refuses to start when
# ${XDG_STATE_HOME:-$HOME/.local/state}/alissa/worker.pid names a live pid, and
# "live" is a bare kill(pid, 0). A platform restart keeps the container
# filesystem, the new boot runs in a fresh pid namespace, and the dead worker's
# pid is held by some unrelated boot process — so the CLI says "A worker is
# already running (pid 240)", the entrypoint's `|| die` fires, and every restart
# dies the same way.
#
# This boots the REAL entrypoint.sh (CLIs stubbed, no docker needed) against an
# `alissa` stub that models the CLI's own pidfile check faithfully, so a stale
# file the entrypoint failed to clear would fail the boot exactly as production
# did:
#
#   0. control: the stub refuses a start over a pidfile naming a live pid
#   1. pidfile naming a LIVE unrelated pid  -> cleared, logged with that pid,
#      worker.root cleared too, the worker starts
#   2. no pidfile                           -> no log line, the worker starts
#   3. XDG_STATE_HOME set                   -> the file under IT is cleared
#      (resolved as the CLI resolves it); one under $HOME/.local/state is not
#   4. CONTAINER_ROLE=executor              -> the sweep never runs (that role
#      execs the bridge before step 4 and has no worker)
#
# Usage: bash docker/claude/tests-entrypoint-worker.sh
# Needs: bash, python3 (the entrypoint's own config seeding uses it).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENTRYPOINT="${HERE}/entrypoint.sh"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1" >&2; fail=1; }
info() { printf '%s\n' "$*"; }

assert_contains() {
  if grep -qF -- "$2" "$1"; then pass "$3"; else bad "$3 (not in log: $2)"; fi
}
assert_not_contains() {
  if grep -qF -- "$2" "$1"; then bad "$3 (unexpected in log: $2)"; else pass "$3"; fi
}
assert_file()    { if [ -e "$1" ]; then pass "$2"; else bad "$2 (missing: $1)"; fi; }
assert_no_file() { if [ -e "$1" ]; then bad "$2 (unexpected file: $1)"; else pass "$2"; fi; }

TMPROOT="$(mktemp -d)"
BIN="${TMPROOT}/bin"; mkdir -p "${BIN}"
FAKE_HOME="${TMPROOT}/home"; mkdir -p "${FAKE_HOME}/.config/alissa"
WORKSPACE="${TMPROOT}/workspace"; mkdir -p "${WORKSPACE}"
SPY="${TMPROOT}/spy"; mkdir -p "${SPY}"
XDG_STATE="${TMPROOT}/xdg-state"
cp "${HERE}/agents.yaml" "${FAKE_HOME}/.config/alissa/agents.yaml"

# The "unrelated live process" that inherited the dead worker's pid.
sleep 600 &
SQUATTER_PID=$!

cleanup() {
  kill "${SQUATTER_PID}" 2>/dev/null || true
  rm -rf "${TMPROOT}"
}
trap cleanup EXIT

cat > "${BIN}/gh" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  "api user -q .login") echo alissa-app ;;
esac
exit 0
STUB

# The alissa CLI stub. `worker start` reproduces the CLI's pidPath()/runningPid():
# the file is ${XDG_STATE_HOME:-$HOME/.local/state}/alissa/worker.pid, and any pid
# kill(pid, 0) accepts refuses the start with a non-zero exit. A successful start
# records the spy and writes a fresh pidfile + worker.root, as the real CLI does.
cat > "${BIN}/alissa" <<'STUB'
#!/usr/bin/env bash
case "$1 $2" in
  "auth login")
    echo "Authenticated."
    exit 0
    ;;
  "worker start")
    dir="${XDG_STATE_HOME:-${HOME}/.local/state}/alissa"
    if [ -f "${dir}/worker.pid" ] && kill -0 "$(cat "${dir}/worker.pid" 2>/dev/null)" 2>/dev/null; then
      echo "✗ A worker is already running (pid $(cat "${dir}/worker.pid"))" >&2
      exit 1
    fi
    mkdir -p "${dir}"
    printf '%s\n' "$$" > "${dir}/worker.pid"
    printf '%s\n' "/fresh/queue" > "${dir}/worker.root"
    echo "start" >> "${SPY_DIR}/worker-start"
    exit 0
    ;;
  "worker status")
    echo "worker is running"
    exit 0
    ;;
  "bridge start")
    echo "start" >> "${SPY_DIR}/bridge-start"
    trap 'exit 0' TERM INT
    sleep 600 &
    wait $!
    exit 0
    ;;
esac
exit 0
STUB

cat > "${BIN}/alissa-revloop" <<'STUB'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
sleep 600 &
wait $!
STUB

# The API reachability probe: always up, this suite is about the worker start.
cat > "${BIN}/curl" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod 0755 "${BIN}/gh" "${BIN}/alissa" "${BIN}/alissa-revloop" "${BIN}/curl"

HOME_STATE="${FAKE_HOME}/.local/state/alissa"

reset_state() {
  rm -rf "${FAKE_HOME}/.local/state" "${XDG_STATE}" "${SPY:?}"/*
  rm -rf "${WORKSPACE:?}"/* "${WORKSPACE:?}"/.alissa-config
}

# plant_stale <dir> — the leftovers of a worker that died with its container:
# a pidfile naming a pid that is live (but unrelated) in the new boot.
plant_stale() {
  mkdir -p "$1"
  printf '%s\n' "${SQUATTER_PID}" > "$1/worker.pid"
  printf '%s\n' "/stale/queue" > "$1/worker.root"
}

# run_boot <logfile> <marker> [env assignments...] -> runs to the marker (or to
# its death), then stops it. Sets BOOT_STATUS (0 = still alive at the marker).
BOOT_STATUS=0
run_boot() {
  local log="$1" marker="$2"; shift 2
  env -i \
    PATH="${BIN}:/usr/local/bin:/usr/bin:/bin" \
    HOME="${FAKE_HOME}" \
    TMUX_TMPDIR="${TMPROOT}/tmux" \
    ALISSA_WORKSPACE_ROOT="${WORKSPACE}" \
    GH_TOKEN=dev-default-token \
    ALISSA_API_TOKEN=stub-alissa-token \
    ALISSA_REVIEW_REPOS="fahera-mx/example-repo" \
    SPY_DIR="${SPY}" \
    "$@" \
    bash "${ENTRYPOINT}" > "${log}" 2>&1 &
  local pid=$! i
  BOOT_STATUS=0
  for i in $(seq 1 30); do
    grep -qF "${marker}" "${log}" && break
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "${pid}" 2>/dev/null; then
    kill -TERM "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  else
    wait "${pid}" 2>/dev/null || BOOT_STATUS=$?
    [ "${BOOT_STATUS}" != "0" ] || BOOT_STATUS=1   # died before the marker
  fi
}

DAEMON_UP="alissa worker is running"
CLEARED="cleared a stale worker pidfile"

# -----------------------------------------------------------------------------
info "0. control: the stub refuses a start over a live pidfile (the production failure)"
# -----------------------------------------------------------------------------
reset_state
plant_stale "${HOME_STATE}"
if env -i PATH="${BIN}:/usr/bin:/bin" HOME="${FAKE_HOME}" SPY_DIR="${SPY}" \
     alissa worker start > "${TMPROOT}/control.log" 2>&1; then
  bad "the stub should refuse a start over a pidfile naming a live pid"
else
  pass "the stub refuses, as the CLI does"
fi
assert_contains "${TMPROOT}/control.log" "A worker is already running (pid ${SQUATTER_PID})" \
  "...with the CLI's message"

# -----------------------------------------------------------------------------
info ""
info "1. a pidfile naming a LIVE unrelated pid -> cleared, logged, the worker starts"
# -----------------------------------------------------------------------------
reset_state
plant_stale "${HOME_STATE}"
LOG1="${TMPROOT}/stale.log"
run_boot "${LOG1}" "${DAEMON_UP}"
[ "${BOOT_STATUS}" = "0" ] && pass "the boot survives" || bad "the boot died (status ${BOOT_STATUS})"
assert_contains "${LOG1}" "[entrypoint] ${CLEARED} (pid ${SQUATTER_PID}) left by a previous run of this container" \
  "one log line names the pid the stale file held"
assert_not_contains "${LOG1}" "A worker is already running" "the CLI never saw the stale file"
assert_not_contains "${LOG1}" "FATAL" "nothing is fatal"
assert_file "${SPY}/worker-start" "alissa worker start ran and succeeded"
assert_contains "${LOG1}" "${DAEMON_UP}" "the boot reaches the worker-up milestone"
if grep -qxF "${SQUATTER_PID}" "${HOME_STATE}/worker.pid" 2>/dev/null; then
  bad "the pidfile still names the stale pid"
else
  pass "the pidfile now belongs to the new worker"
fi
assert_not_contains "${HOME_STATE}/worker.root" "/stale/queue" "worker.root was cleared with it"
kill -0 "${SQUATTER_PID}" 2>/dev/null && pass "the unrelated process was left alone" \
  || bad "the unrelated process was killed"

# -----------------------------------------------------------------------------
info ""
info "2. no pidfile -> no log line, the worker starts"
# -----------------------------------------------------------------------------
reset_state
LOG2="${TMPROOT}/clean.log"
run_boot "${LOG2}" "${DAEMON_UP}"
[ "${BOOT_STATUS}" = "0" ] && pass "the boot survives" || bad "the boot died (status ${BOOT_STATUS})"
assert_not_contains "${LOG2}" "${CLEARED}" "no stale-pidfile line on a clean boot"
assert_file "${SPY}/worker-start" "alissa worker start ran and succeeded"

# -----------------------------------------------------------------------------
info ""
info "3. XDG_STATE_HOME set -> the file under it is cleared, as the CLI resolves it"
# -----------------------------------------------------------------------------
reset_state
plant_stale "${XDG_STATE}/alissa"
plant_stale "${HOME_STATE}"   # a decoy the CLI would not read under XDG_STATE_HOME
LOG3="${TMPROOT}/xdg.log"
run_boot "${LOG3}" "${DAEMON_UP}" XDG_STATE_HOME="${XDG_STATE}"
[ "${BOOT_STATUS}" = "0" ] && pass "the boot survives" || bad "the boot died (status ${BOOT_STATUS})"
assert_contains "${LOG3}" "${CLEARED} (pid ${SQUATTER_PID})" "the sweep logged"
assert_file "${SPY}/worker-start" "alissa worker start ran and succeeded"
assert_contains "${HOME_STATE}/worker.root" "/stale/queue" \
  "the \$HOME/.local/state file is not the CLI's under XDG_STATE_HOME and is untouched"

# -----------------------------------------------------------------------------
info ""
info "4. CONTAINER_ROLE=executor -> the sweep never runs"
# -----------------------------------------------------------------------------
reset_state
plant_stale "${HOME_STATE}"
LOG4="${TMPROOT}/executor.log"
run_boot "${LOG4}" "starting alissa bridge start" \
  CONTAINER_ROLE=executor ALISSA_BRIDGE_EXECUTOR=1
assert_file "${SPY}/bridge-start" "the executor role reached the bridge"
assert_not_contains "${LOG4}" "${CLEARED}" "no stale-pidfile line in the executor role"
assert_no_file "${SPY}/worker-start" "the worker is not started in the executor role"
assert_contains "${HOME_STATE}/worker.pid" "${SQUATTER_PID}" "the pidfile is untouched"

info ""
if [ "${fail}" = "0" ]; then
  info "all worker-pidfile tests passed"
else
  info "SOME worker-pidfile tests FAILED" >&2
fi
exit "${fail}"
