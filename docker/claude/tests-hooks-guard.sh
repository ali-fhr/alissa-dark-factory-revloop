#!/usr/bin/env bash
# =============================================================================
# Table test for the Claude Code hooks in docker/claude/hooks (devloop issue
# #125, ported to the reviewer seat by issue #138 -- the SAME deny/pass table).
#
#   1. guard-shell.py — a table of Bash commands → DENY / pass. Every DENY row
#      must produce the PreToolUse deny decision carrying the instructive
#      reason; every pass row must produce NO stdout and exit 0. Malformed
#      stdin → exit 0, no stdout (fail-open). All under 100 ms each.
#   2. note-waiting.py / clear-waiting.py — the marker is written (creating a
#      missing directory), carries the six fields, then is cleared; the tmux
#      session name wins over session_id when $TMUX is set, and
#      ALISSA_TMUX_SESSION (the managed seat's export) wins without asking
#      tmux. The seat running this script may itself export both, so every
#      marker call scrubs them explicitly.
#
# Same shape as tests-image-contract.sh (one `  ok ` / `  FAIL ` line per
# assertion, a count at the end), but needs no docker: python3 only. Wired
# into the check-tests workflow next to the unit tests.
#
#   docker/claude/tests-hooks-guard.sh
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS="${SCRIPT_DIR}/hooks"
GUARD="${HOOKS}/guard-shell.py"
NOTE="${HOOKS}/note-waiting.py"
CLEAR="${HOOKS}/clear-waiting.py"
CWD="/workspace/some-repo/TASK-1-DESC"

fail=0
asserts=0
pass() { printf '  ok   %s\n' "$*"; asserts=$((asserts + 1)); }
bad()  { printf '  FAIL %s\n' "$*"; fail=1; asserts=$((asserts + 1)); }

for f in "$GUARD" "$NOTE" "$CLEAR"; do
  if [ -x "$f" ]; then pass "executable: ${f#"${SCRIPT_DIR}/"}"; else bad "not executable: $f"; fi
done

# One hook payload for a Bash command, on stdin, exactly as claude sends it.
payload() {
  python3 -c 'import json, sys; print(json.dumps({"session_id": "s1", "cwd": sys.argv[1], "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": sys.argv[2]}}))' "$CWD" "$1"
}

REASON_HEAD="Unattended session: this rm shape opens an interactive prompt nobody can answer."

expect_deny() {
  local cmd="$1" out rc
  out="$(payload "$cmd" | ALISSA_WORKSPACE_ROOT=/workspace python3 "$GUARD" 2>/dev/null)"; rc=$?
  if [ "$rc" != "0" ]; then bad "DENY [$cmd]: exit $rc, expected 0 with a deny decision"; return; fi
  if printf '%s' "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin)["hookSpecificOutput"]
assert d["hookEventName"] == "PreToolUse", d
assert d["permissionDecision"] == "deny", d
assert d["permissionDecisionReason"].startswith(sys.argv[1]), d
assert "find <literal-dir> -mindepth 1 -delete" in d["permissionDecisionReason"], d
' "$REASON_HEAD" 2>/dev/null; then pass "DENY [$cmd]"; else bad "DENY [$cmd]: no deny decision with the instructive reason; stdout: ${out:-<empty>}"; fi
}

expect_pass() {
  local cmd="$1" out rc
  out="$(payload "$cmd" | ALISSA_WORKSPACE_ROOT=/workspace python3 "$GUARD" 2>/dev/null)"; rc=$?
  if [ "$rc" = "0" ] && [ -z "$out" ]; then pass "pass [$cmd]"; else bad "pass [$cmd]: exit $rc, stdout: ${out:-<empty>}"; fi
}

echo "1. guard-shell.py — the deny/pass table (cwd ${CWD})"
# The issue's table, verbatim.
expect_deny 'rm -rf sales/screenshots/x/*'
expect_deny 'rm -rf "$S"/shot'
expect_deny 'cd a && rm -rf ./*'
expect_deny '(rm -rf $PWD/tmp)'
expect_deny 'rm -rf ~/x'
expect_deny 'rm -rf /'
expect_deny 'rm -rf ..'
expect_deny "find . -name '*.png' -exec rm {} +"
expect_deny 'ls | xargs rm'
expect_deny 'sudo rm -rf build/*'
# Beyond the table: further shapes (the rows are the claim, nothing more).
expect_deny 'rm -rf .'
expect_deny 'rm -rf /workspace'
expect_deny 'rm -rf /workspace/some-repo'
expect_deny 'rm -rf /workspace/some-repo/main'
expect_deny 'rm -rf /workspace/some-repo/.source/objects'
expect_deny 'cd .. && rm -rf TASK-1-DESC'
expect_deny 'cd /tmp; rm -rf scratch'
expect_deny 'echo $(rm -rf $x)'
expect_deny 'echo `rm -f "$f"`'
expect_deny 'rm -f `ls`'
expect_deny 'true || rm -rf ${DIR}/x'
expect_deny 'rm -rf x; rm -rf y/?'
expect_deny 'make && { rm -rf out/[ab]; }'
expect_deny 'FOO=1 env -u X rm -rf x/*'
expect_deny 'command rm -rf x/*'
expect_deny 'rmdir ../sibling'
expect_deny 'rm -rf /etc/passwd'
expect_deny 'find build -exec rm -rf {} \;'
expect_deny 'find . -type f | xargs -0 rm -f'
expect_deny 'rm -rf "a b"/*'
expect_deny 'rm -rf "'"'"'*'"'"'"'
# Reserved words inside one segment (PR #126 review round 1): the one-line
# loop/conditional must be judged like its multi-line spelling.
expect_deny 'for d in build dist; do rm -rf $d/*; done'
expect_deny 'for f in *.png; do rm -rf $f; done'
expect_deny 'if [ -d build ]; then rm -rf build/*; fi'
expect_deny 'while read f; do rm -rf $f; done < list'
expect_deny 'if x; then y; else rm -rf *; fi'
expect_deny 'if rm -rf *; then echo gone; fi'
expect_deny '! rm -rf out/*'
expect_deny 'case $x in a) rm -rf $x/*;; esac'
expect_deny 'case $x in a) echo ok;; b|c) rm -rf $x/*;; esac'
expect_deny 'do sudo rm -rf $d/*'
# The table's pass rows, verbatim.
expect_pass 'rm -f sales/screenshots/x/a.png'
expect_pass 'rm -rf build/cache'
expect_pass 'git rm -r --cached x'
expect_pass 'npm rm lodash'
expect_pass 'echo "rm -rf *"'
expect_pass 'grep -r "rm -rf" .'
expect_pass 'find build -mindepth 1 -delete'
# Beyond the table.
expect_pass 'rm -rf x 2>/dev/null'
expect_pass 'rm -f a.txt > /dev/null 2>&1 && echo ok'
expect_pass 'rm -rf -- a b c'
expect_pass 'cd sub && rm -rf out'
expect_pass '(cd sub && rm -rf out)'
expect_pass 'timeout 5 rm -f a.txt'
expect_pass "rm -f 'a b.txt'"
expect_pass 'rm -rf /workspace/some-repo/TASK-1-DESC/build'
expect_pass 'ls *.png | grep x; rm -f a.png'
expect_pass 'find . -name "*.png" -delete'
expect_pass 'git clean -fdx -- build'
expect_pass 'cat "$HOME/notes" | grep rm'
expect_pass 'ls'
expect_pass 'if [ -d build ]; then rm -rf build; fi'
expect_pass 'for f in a b; do echo $f; done; rm -rf build/cache'
expect_pass 'case $x in a) rm -rf build/cache;; esac'
expect_pass 'rm -rf main/src'

echo "1b. guard-shell.py — the protected hub paths from a cwd AT the workspace root"
# The boundary rule cannot cover these (the hubs are INSIDE the boundary);
# only the structural protection does (PR #126 review round 1).
CWD=/workspace expect_deny 'rm -rf some-repo'
CWD=/workspace expect_deny 'rm -rf /workspace/some-repo'
CWD=/workspace expect_deny 'rm -rf some-repo/.source'
CWD=/workspace expect_deny 'rm -rf some-repo/.source/objects'
CWD=/workspace expect_deny 'rm -rf /workspace/some-repo/main'
CWD=/workspace expect_pass 'rm -rf some-repo/TASK-9-Y/build'
CWD=/workspace expect_pass 'rm -rf some-repo/main/src'
CWD=/workspace/some-repo expect_deny 'rm -rf .source/objects'
CWD=/workspace/some-repo expect_pass 'rm -rf main/src'

echo "2. guard-shell.py — fail-open"
out="$(printf 'not json' | python3 "$GUARD" 2>/dev/null)"; rc=$?
if [ "$rc" = "0" ] && [ -z "$out" ]; then pass "malformed stdin: exit 0, no stdout"; else bad "malformed stdin: exit $rc, stdout: ${out:-<empty>}"; fi
err="$(printf 'not json' | python3 "$GUARD" 2>&1 >/dev/null)"
if [ "$(printf '%s\n' "$err" | grep -c .)" = "1" ]; then pass "malformed stdin: one stderr line"; else bad "malformed stdin: expected one stderr line, got: ${err:-<none>}"; fi
out="$(printf '' | python3 "$GUARD" 2>/dev/null)"; rc=$?
if [ "$rc" = "0" ] && [ -z "$out" ]; then pass "empty stdin: exit 0, no stdout"; else bad "empty stdin: exit $rc"; fi
out="$(printf '{"tool_name":"Bash","cwd":"%s"}' "$CWD" | python3 "$GUARD" 2>/dev/null)"; rc=$?
if [ "$rc" = "0" ] && [ -z "$out" ]; then pass "payload without tool_input: exit 0, no stdout"; else bad "payload without tool_input: exit $rc, stdout: ${out:-<empty>}"; fi
out="$(printf '{"tool_name":"Write","cwd":"%s","tool_input":{"command":"rm -rf *"}}' "$CWD" | python3 "$GUARD" 2>/dev/null)"; rc=$?
if [ "$rc" = "0" ] && [ -z "$out" ]; then pass "non-Bash tool: exit 0, no stdout"; else bad "non-Bash tool: exit $rc, stdout: ${out:-<empty>}"; fi

echo "3. guard-shell.py — budget"
start="$(date +%s%N)"
for _ in 1 2 3 4 5; do payload 'rm -rf sales/x/*' | python3 "$GUARD" >/dev/null 2>&1; done
elapsed_ms=$(( ($(date +%s%N) - start) / 5000000 ))
if [ "$elapsed_ms" -lt 100 ]; then pass "mean run ${elapsed_ms} ms (< 100 ms)"; else bad "mean run ${elapsed_ms} ms, budget is 100 ms"; fi

echo "4. note-waiting.py / clear-waiting.py"
T="$(mktemp -d)"
trap 'rm -rf "${T}"' EXIT
WD="${T}/nested/.waiting"   # does not exist yet: the hook must create it
notif() {
  python3 -c 'import json, sys; print(json.dumps({"session_id": sys.argv[1], "cwd": sys.argv[2], "hook_event_name": "Notification", "message": "Claude needs your permission", "notification_type": sys.argv[3]}))' "$@"
}
notif sess-abc "$CWD" permission_prompt | env -u TMUX -u ALISSA_TMUX_SESSION ALISSA_WAITING_DIR="$WD" python3 "$NOTE"; rc=$?
if [ "$rc" = "0" ]; then pass "note-waiting exits 0"; else bad "note-waiting exit $rc"; fi
if [ -f "${WD}/sess-abc.json" ]; then pass "marker written at \$ALISSA_WAITING_DIR/<session_id>.json (dir created)"; else bad "no marker at ${WD}/sess-abc.json: $(ls -la "$WD" 2>&1)"; fi
if python3 - "${WD}/sess-abc.json" "$CWD" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["session"] == "sess-abc", d
assert d["sessionId"] == "sess-abc", d
assert d["cwd"] == sys.argv[2], d
assert d["kind"] == "permission_prompt", d
assert d["message"] == "Claude needs your permission", d
assert d["at"].endswith("+00:00") and "T" in d["at"], d
PY
then pass "marker carries {session, sessionId, cwd, kind, message, at}"; else bad "marker content wrong: $(cat "${WD}/sess-abc.json" 2>&1)"; fi
if [ -z "$(ls "$WD" | grep -v '^sess-abc.json$')" ]; then pass "no temp file left behind"; else bad "stray files in ${WD}: $(ls "$WD")"; fi
notif sess-abc "$CWD" idle_prompt | env -u TMUX -u ALISSA_TMUX_SESSION ALISSA_WAITING_DIR="$WD" python3 "$NOTE"
if grep -q '"kind": "idle_prompt"' "${WD}/sess-abc.json"; then pass "a second notification overwrites the marker (kind=idle_prompt)"; else bad "marker not refreshed"; fi
printf '{"session_id":"sess-abc","cwd":"%s","hook_event_name":"UserPromptSubmit","prompt":"go"}' "$CWD" | env -u TMUX -u ALISSA_TMUX_SESSION ALISSA_WAITING_DIR="$WD" python3 "$CLEAR"; rc=$?
if [ "$rc" = "0" ] && [ ! -e "${WD}/sess-abc.json" ]; then pass "clear-waiting removes the marker (exit $rc)"; else bad "clear-waiting: exit $rc, marker present: $([ -e "${WD}/sess-abc.json" ] && echo yes || echo no)"; fi
printf '{"session_id":"sess-abc","cwd":"%s","hook_event_name":"PostToolUse","tool_name":"Bash"}' "$CWD" | env -u TMUX -u ALISSA_TMUX_SESSION ALISSA_WAITING_DIR="$WD" python3 "$CLEAR"; rc=$?
if [ "$rc" = "0" ]; then pass "clear-waiting on an absent marker exits 0"; else bad "clear-waiting on an absent marker: exit $rc"; fi
# The tmux session name wins over session_id when $TMUX is set: shadow tmux.
mkdir -p "${T}/bin"
printf '#!/usr/bin/env bash\necho ali-review-widgets-pr7-r1-abcdef\n' > "${T}/bin/tmux"; chmod 0755 "${T}/bin/tmux"
notif sess-abc "$CWD" permission_prompt | env -u ALISSA_TMUX_SESSION PATH="${T}/bin:$PATH" TMUX=/tmp/tmux-1000/default,1,0 ALISSA_WAITING_DIR="$WD" python3 "$NOTE"
if [ -f "${WD}/ali-review-widgets-pr7-r1-abcdef.json" ] && grep -q '"session": "ali-review-widgets-pr7-r1-abcdef"' "${WD}/ali-review-widgets-pr7-r1-abcdef.json"; then pass "inside tmux the marker is named after the tmux session"; else bad "tmux-named marker missing: $(ls "$WD" 2>&1)"; fi
printf '{"session_id":"sess-abc","cwd":"%s","hook_event_name":"UserPromptSubmit"}' "$CWD" | env -u ALISSA_TMUX_SESSION PATH="${T}/bin:$PATH" TMUX=/tmp/tmux-1000/default,1,0 ALISSA_WAITING_DIR="$WD" python3 "$CLEAR"
if [ ! -e "${WD}/ali-review-widgets-pr7-r1-abcdef.json" ]; then pass "clear-waiting resolves the same tmux name"; else bad "tmux-named marker not cleared"; fi
# ALISSA_TMUX_SESSION wins and tmux is never asked: the shadow now fails.
printf '#!/usr/bin/env bash\nexit 1\n' > "${T}/bin/tmux"
notif sess-abc "$CWD" permission_prompt | env PATH="${T}/bin:$PATH" TMUX=/tmp/tmux-1000/default,1,0 ALISSA_TMUX_SESSION=ali-fix-owner-repo-pr1-r1-a1 ALISSA_WAITING_DIR="$WD" python3 "$NOTE"
if [ -f "${WD}/ali-fix-owner-repo-pr1-r1-a1.json" ]; then pass "ALISSA_TMUX_SESSION names the marker without asking tmux"; else bad "ALISSA_TMUX_SESSION-named marker missing: $(ls "$WD" 2>&1)"; fi
printf '{"session_id":"sess-abc","cwd":"%s","hook_event_name":"PostToolUse"}' "$CWD" | env PATH="${T}/bin:$PATH" TMUX=/tmp/tmux-1000/default,1,0 ALISSA_TMUX_SESSION=ali-fix-owner-repo-pr1-r1-a1 ALISSA_WAITING_DIR="$WD" python3 "$CLEAR"
if [ ! -e "${WD}/ali-fix-owner-repo-pr1-r1-a1.json" ]; then pass "clear-waiting resolves ALISSA_TMUX_SESSION the same way"; else bad "ALISSA_TMUX_SESSION-named marker not cleared"; fi
out="$(printf 'not json' | env -u ALISSA_TMUX_SESSION ALISSA_WAITING_DIR="$WD" python3 "$NOTE" 2>/dev/null)"; rc=$?
if [ "$rc" = "0" ] && [ -z "$out" ]; then pass "note-waiting on malformed stdin: exit 0, no stdout"; else bad "note-waiting malformed: exit $rc, stdout ${out:-<empty>}"; fi
out="$(printf 'not json' | env -u ALISSA_TMUX_SESSION ALISSA_WAITING_DIR="$WD" python3 "$CLEAR" 2>/dev/null)"; rc=$?
if [ "$rc" = "0" ] && [ -z "$out" ]; then pass "clear-waiting on malformed stdin: exit 0, no stdout"; else bad "clear-waiting malformed: exit $rc, stdout ${out:-<empty>}"; fi

echo ""
if [ "${fail}" = "0" ]; then
  echo "ALL PASS — ${asserts} assertions"
  exit 0
else
  echo "FAILURES — ${asserts} assertions run, at least one failed (grep '  FAIL ' above)"
  exit 1
fi
