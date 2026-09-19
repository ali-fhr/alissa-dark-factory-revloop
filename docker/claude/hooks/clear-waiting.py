#!/usr/bin/env python3
"""Claude Code `UserPromptSubmit` / `PostToolUse` hook: remove the
"waiting for input" marker note-waiting.py left for this session.

Either event proves the session is no longer parked on a prompt — a prompt
was submitted, or a tool ran to completion — so the marker at
`${ALISSA_WAITING_DIR:-/workspace/.waiting}/<name>.json` is stale. `<name>`
is resolved exactly as note-waiting.py resolves it (`$ALISSA_TMUX_SESSION`,
else the tmux session name asked of `tmux` when `$TMUX` is set, else the
hook's `session_id`); the two scripts are kept independent on purpose, so a
defect in one cannot take the other down.

Runs on EVERY tool call. In a managed seat (`alissa tmux new` exports
`ALISSA_TMUX_SESSION`) the hot path is one env lookup and one stat-free
unlink — no subprocess, no directory creation, no output. Only outside one,
with `$TMUX` set but `ALISSA_TMUX_SESSION` unset, does it ask `tmux` for the
name first: a subprocess per tool call, bounded by a 2 s timeout. A missing
marker is the normal case and not an error. Any exception → exit 0 with one
line on stderr (fail-open).
"""

import json
import os
import subprocess
import sys

DEFAULT_DIR = "/workspace/.waiting"


def waiting_dir():
    return os.environ.get("ALISSA_WAITING_DIR", "").strip() or DEFAULT_DIR


def session_name(data):
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


def main():
    try:
        data = json.load(sys.stdin)
        try:
            os.unlink(marker_path(data))
        except FileNotFoundError:
            pass
    except Exception as exc:  # fail open
        sys.stderr.write(f"clear-waiting: marker not cleared — {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
