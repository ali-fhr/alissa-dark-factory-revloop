"""Pinning tests for the entrypoint's Claude Code hooks registration
(issue #138, devloop #125's merge on the reviewer seat) — the `hooks` block
the 3a settings merge writes.

The merge is the inline python heredoc in step 3a of
docker/claude/entrypoint.sh, lifted out of the shipped file verbatim (as
tests-image-contract.sh lifts it) and run against scratch $HOME /
$CLAUDE_CONFIG_DIR / workspace roots. What is pinned:

  * the three hooks are registered ONCE — the guard on `PreToolUse` (matcher
    `Bash`), the marker on `Notification` (`permission_prompt|idle_prompt`),
    the clearer on `UserPromptSubmit` and `PostToolUse` — at the paths the
    Dockerfile ships them to;
  * a second boot adds nothing (idempotent);
  * a pre-existing FOREIGN hook in the same event survives, in place;
  * `ALISSA_SHELL_GUARD=off` skips the guard AND removes one a previous boot
    wrote (the rollback lever must work against a persisted settings.json),
    while the marker pair stays;
  * a registration under the image-owned hooks dir that this boot does not
    make itself (another image version's script, event or matcher) is
    pruned, while a foreign hook sharing its group survives;
  * the block is mirrored into `$CLAUDE_CONFIG_DIR/settings.json`, and a
    blank CLAUDE_CONFIG_DIR writes $HOME alone;
  * the other seeded keys are untouched by the hooks merge.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[7]
ENTRYPOINT = REPO_ROOT / "docker" / "claude" / "entrypoint.sh"
DOCKERFILE = REPO_ROOT / "docker" / "claude" / "Dockerfile"

pytestmark = pytest.mark.skipif(
    shutil.which("python3") is None, reason="the seeding is a python3 heredoc"
)

HEREDOC_OPEN = "python3 - \"${WORKSPACE_ROOT}\" <<'PY' || true"
HOOKS_DIR = "/usr/local/share/alissa/hooks"
GUARD = f"{HOOKS_DIR}/guard-shell.py"
NOTE = f"{HOOKS_DIR}/note-waiting.py"
CLEAR = f"{HOOKS_DIR}/clear-waiting.py"


def seeding_block() -> str:
    lines = ENTRYPOINT.read_text().splitlines()
    opens = [i for i, ln in enumerate(lines) if ln == HEREDOC_OPEN]
    assert len(opens) == 1, f"expected one 3a seeding heredoc, found {len(opens)}"
    start = opens[0] + 1
    ends = [i for i, ln in enumerate(lines[start:], start) if ln == "PY"]
    assert ends, "no `PY` closes the seeding heredoc"
    block = "\n".join(lines[start:ends[0]])
    assert "ALISSA_SHELL_GUARD" in block
    assert "guard-shell.py" in block
    return block


def run_seeding(tmp_path, *, config_dir=True, extra_env=None):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    ccdir = tmp_path / "ccdir"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "ALISSA_REVIEW_REPOS": "",
    }
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(ccdir)
    env.update(extra_env or {})
    done = subprocess.run(
        ["python3", "-", str(root)], input=seeding_block(),
        capture_output=True, text=True, env=env, check=True,
    )
    return home / ".claude" / "settings.json", ccdir / "settings.json", done.stdout


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def commands(settings: dict, event: str) -> list:
    return [h["command"] for g in settings.get("hooks", {}).get(event, []) for h in g["hooks"]]


def group_for(settings: dict, event: str, script: str) -> dict:
    groups = [g for g in settings["hooks"][event] if any(h["command"] == script for h in g["hooks"])]
    assert len(groups) == 1, groups
    return groups[0]


def test_first_boot_registers_the_three_hooks_once(tmp_path):
    home_settings, cc_settings, out = run_seeding(tmp_path)
    for path in (home_settings, cc_settings):
        s = load(path)
        assert commands(s, "PreToolUse") == [GUARD]
        assert commands(s, "Notification") == [NOTE]
        assert commands(s, "UserPromptSubmit") == [CLEAR]
        assert commands(s, "PostToolUse") == [CLEAR]
        assert group_for(s, "PreToolUse", GUARD)["matcher"] == "Bash"
        assert group_for(s, "Notification", NOTE)["matcher"] == "permission_prompt|idle_prompt"
        # UserPromptSubmit has no matcher support; PostToolUse without one fires on every tool.
        assert "matcher" not in group_for(s, "UserPromptSubmit", CLEAR)
        assert "matcher" not in group_for(s, "PostToolUse", CLEAR)
        for event in ("PreToolUse", "Notification", "UserPromptSubmit", "PostToolUse"):
            for g in s["hooks"][event]:
                for h in g["hooks"]:
                    assert h["type"] == "command"
                    assert h["timeout"] == 10
        # The pre-existing seeded keys are still there.
        assert s["skipDangerousModePermissionPrompt"] is True
        assert s["theme"] == "dark"
        assert s["tui"] == "fullscreen"
    assert "shell guard ON" in out
    assert "waiting marker ON" in out
    assert "pre-trusted" in out and "reviewer dir(s)" in out, "the seeding line still reports the trust merge"


def test_shipped_paths_match_the_dockerfile():
    text = DOCKERFILE.read_text()
    for script in (GUARD, NOTE, CLEAR):
        assert f"COPY hooks/{os.path.basename(script)}" in text, script
        assert script in text
    chmod = text[text.index("RUN chmod 0755"):]
    chmod = chmod[:chmod.index("\n\n")]
    for script in (GUARD, NOTE, CLEAR):
        assert script in chmod, f"{script} not chmod 0755 in the Dockerfile"


def test_second_boot_is_idempotent(tmp_path):
    home_settings, cc_settings, _ = run_seeding(tmp_path)
    first = (load(home_settings), load(cc_settings))
    run_seeding(tmp_path)
    run_seeding(tmp_path)
    assert (load(home_settings), load(cc_settings)) == first
    for s in first:
        for event in ("PreToolUse", "Notification", "UserPromptSubmit", "PostToolUse"):
            assert len(s["hooks"][event]) == 1, (event, s["hooks"][event])


def test_foreign_hooks_survive_in_place(tmp_path):
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    foreign = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/operator/audit.sh"}]},
                {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "/opt/operator/lint.sh"}]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "/opt/operator/done.sh"}]}],
        },
        "theme": "light",
    }
    (home / "settings.json").write_text(json.dumps(foreign))
    home_settings, _, _ = run_seeding(tmp_path)
    s = load(home_settings)
    assert s["hooks"]["PreToolUse"][0] == foreign["hooks"]["PreToolUse"][0]
    assert s["hooks"]["PreToolUse"][1] == foreign["hooks"]["PreToolUse"][1]
    assert commands(s, "PreToolUse") == ["/opt/operator/audit.sh", "/opt/operator/lint.sh", GUARD]
    assert s["hooks"]["Stop"] == foreign["hooks"]["Stop"]
    assert s["theme"] == "light"  # setdefault, as before
    run_seeding(tmp_path)
    assert commands(load(home_settings), "PreToolUse") == ["/opt/operator/audit.sh", "/opt/operator/lint.sh", GUARD]


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no"])
def test_shell_guard_off_skips_the_guard_and_keeps_the_marker(tmp_path, value):
    home_settings, cc_settings, out = run_seeding(tmp_path, extra_env={"ALISSA_SHELL_GUARD": value})
    for path in (home_settings, cc_settings):
        s = load(path)
        assert "PreToolUse" not in s["hooks"], s["hooks"]
        assert commands(s, "Notification") == [NOTE]
        assert commands(s, "UserPromptSubmit") == [CLEAR]
        assert commands(s, "PostToolUse") == [CLEAR]
    assert "shell guard OFF (ALISSA_SHELL_GUARD)" in out


@pytest.mark.parametrize("value", ["", "on", "1", "yes", "anything-else"])
def test_shell_guard_other_values_keep_it_on(tmp_path, value):
    home_settings, _, _ = run_seeding(tmp_path, extra_env={"ALISSA_SHELL_GUARD": value})
    assert commands(load(home_settings), "PreToolUse") == [GUARD]


def test_shell_guard_off_removes_a_previous_boots_registration(tmp_path):
    """The rollback lever must work against the settings.json a previous boot
    persisted on the volume, and must leave a foreign hook in the same event."""
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/operator/audit.sh"}]},
    ]}}))
    home_settings, cc_settings, _ = run_seeding(tmp_path)
    assert commands(load(home_settings), "PreToolUse") == ["/opt/operator/audit.sh", GUARD]
    assert commands(load(cc_settings), "PreToolUse") == [GUARD]
    run_seeding(tmp_path, extra_env={"ALISSA_SHELL_GUARD": "off"})
    assert commands(load(home_settings), "PreToolUse") == ["/opt/operator/audit.sh"]
    assert "PreToolUse" not in load(cc_settings)["hooks"]
    # And back on: registered again, once.
    run_seeding(tmp_path)
    assert commands(load(home_settings), "PreToolUse") == ["/opt/operator/audit.sh", GUARD]
    assert commands(load(cc_settings), "PreToolUse") == [GUARD]


def test_guard_sharing_a_group_with_a_foreign_hook_is_removed_not_the_group(tmp_path):
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/opt/operator/audit.sh"},
            {"type": "command", "command": GUARD, "timeout": 10},
        ]},
    ]}}))
    home_settings, _, _ = run_seeding(tmp_path, extra_env={"ALISSA_SHELL_GUARD": "off"})
    s = load(home_settings)
    assert s["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/operator/audit.sh"}]},
    ]


def test_stale_registrations_under_the_hooks_dir_are_pruned(tmp_path):
    """What an image rollback / roll-forward leaves on the volume (PR #126
    review round 1): a script this image does not ship, one of ours under an
    event or matcher this image does not use. All pruned; the foreign hook
    sharing a group survives; an emptied event disappears."""
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    old = f"{HOOKS_DIR}/old-hook.py"
    (home / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [
                {"type": "command", "command": "/opt/operator/audit.sh"},
                {"type": "command", "command": old, "timeout": 10},
            ]},
            {"matcher": "Bash|Write", "hooks": [{"type": "command", "command": GUARD, "timeout": 10}]},
        ],
        "Stop": [{"hooks": [{"type": "command", "command": CLEAR, "timeout": 10}]}],
        "PostToolUse": [{"hooks": [{"type": "command", "command": f"python3 {old}"}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": "/opt/operator/done.sh"}]}],
    }}))
    home_settings, _, _ = run_seeding(tmp_path)
    s = load(home_settings)
    assert s["hooks"]["PreToolUse"] == [  # the stale script gone, the foreign hook kept, ours re-added
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/operator/audit.sh"}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": GUARD, "timeout": 10}]},
    ]
    assert "Stop" not in s["hooks"]
    assert commands(s, "PostToolUse") == [CLEAR]
    assert s["hooks"]["SubagentStop"] == [{"hooks": [{"type": "command", "command": "/opt/operator/done.sh"}]}]
    run_seeding(tmp_path)
    assert load(home_settings) == s


def test_blank_config_dir_writes_home_alone(tmp_path):
    home_settings, cc_settings, _ = run_seeding(tmp_path, config_dir=False)
    assert commands(load(home_settings), "PreToolUse") == [GUARD]
    assert not cc_settings.exists()


def test_a_non_dict_hooks_value_is_replaced_not_crashed_on(tmp_path):
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"hooks": "garbage"}))
    home_settings, _, _ = run_seeding(tmp_path)
    assert commands(load(home_settings), "PreToolUse") == [GUARD]


def test_entrypoint_documents_the_lever():
    readme = (REPO_ROOT / "docker" / "claude" / "README.md").read_text()
    assert "`ALISSA_SHELL_GUARD`" in readme
    assert "`ALISSA_WAITING_DIR`" in readme
