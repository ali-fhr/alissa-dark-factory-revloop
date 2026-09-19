#!/usr/bin/env python3
"""Claude Code `Notification` hook (matchers `permission_prompt` and
`idle_prompt`): leave a "this session is waiting for input" marker on disk.

WHY (devloop issue #125, ported to the reviewer seat by issue #138). A
reviewer session that still ends up on an interactive prompt — the guard
refuses the rm shapes it knows, not every prompt Claude Code can raise —
sits there until something notices. The supervisor's
`promptPatterns` in agents.yaml only detect a prompt by scraping the pane;
this marker is the signal the daemon's prompt responder
(`ReviewWatcher._respond_to_prompts`) reads FIRST, the pane being its
confirmation. The responder answers from a policy table; this file only makes
the waiting visible.

WHAT IT WRITES. `${ALISSA_WAITING_DIR:-/workspace/.waiting}/<name>.json`,
where `<name>` is the tmux session name — that is how the daemon addresses
the seat: `$ALISSA_TMUX_SESSION` (exported by `alissa tmux new` into every
managed seat; no subprocess), else `tmux display-message -p '#S'` when
`$TMUX` is set, else the hook's `session_id`. Content: `{session,
sessionId, cwd, kind, message, at}`,
`kind` being the notification type. Written atomically (temp file + rename in
the same directory) so a reader never sees a torn file; the directory is
created when missing. Its counterpart, clear-waiting.py, removes the marker on
`UserPromptSubmit` and `PostToolUse` — the two events that prove the session
moved on. The marker is a FIRST signal, not proof: a background sub-agent's
tool call clears it while the main loop still sits on the dialog, so a
present marker means "probably waiting, confirm by the pane" and an absent
one means nothing at all.

FAIL-OPEN. Malformed stdin or any exception → exit 0, one line on stderr.
Notification hooks cannot block anything anyway; this must never slow a
session down either. Stdlib only; the single subprocess is `tmux`, bounded by
a short timeout, and skipped whenever `ALISSA_TMUX_SESSION` is set or `$TMUX`
is not.
"""

import datetime
import json
import os
import subprocess
import sys

DEFAULT_DIR = "/workspace/.waiting"


def waiting_dir():
    return os.environ.get("ALISSA_WAITING_DIR", "").strip() or DEFAULT_DIR


def session_name(data):
    """`$ALISSA_TMUX_SESSION`, else the tmux session name when inside tmux,
    else the hook's session_id."""
    name = os.environ.get("ALISSA_TMUX_SESSION", "").strip()
    if name:
        return name
    if os.environ.get("TMUX"):
        try:
            done = subprocess.run(["tmux", "display-message", "-p", "#S"],
                                  capture_output=True, text=True, timeout=2)
            name = done.stdout.strip()
            if done.returncode == 0 and name:
                return name
        except (OSError, subprocess.SubprocessError):
            pass
    sid = data.get("session_id") if isinstance(data, dict) else None
    return str(sid) if sid else "unknown-session"


def safe_name(name):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip(".") or "unknown-session"


def marker_path(data):
    return os.path.join(waiting_dir(), safe_name(session_name(data)) + ".json")


def write_marker(data):
    path = marker_path(data)
    payload = {
        "session": os.path.splitext(os.path.basename(path))[0],
        "sessionId": data.get("session_id"),
        "cwd": data.get("cwd"),
        "kind": data.get("notification_type"),
        "message": data.get("message"),
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return path


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("hook payload is not an object")
        write_marker(data)
    except Exception as exc:  # fail open
        sys.stderr.write(f"note-waiting: no marker written — {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
