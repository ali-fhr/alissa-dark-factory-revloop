"""Shared fixtures for the revloop test package."""

import pytest


@pytest.fixture(autouse=True)
def isolated_claude_home(tmp_path_factory, monkeypatch):
    """Point Claude Code's state files at scratch directories for EVERY test.

    Issue #136: the loop pre-trusts hub directories for claude by merging
    into `$HOME/.claude.json` and `$CLAUDE_CONFIG_DIR/.claude.json` before
    each spawn and at hub-ify time. Without this fixture a spawn in a test
    would write the test's scratch hubs into the developer's (or the CI
    runner's) REAL claude state. Tests that drive a shell helper build their
    own subprocess env explicitly and are unaffected."""
    home = tmp_path_factory.mktemp("claude-home")
    ccdir = tmp_path_factory.mktemp("claude-config-dir")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ccdir))
    return home, ccdir


@pytest.fixture(autouse=True)
def scratch_upgrade_dir(tmp_path_factory, monkeypatch):
    """Point the self-upgrade request file (issue #193) at a scratch
    directory for EVERY test.

    Every `ReviewWatcher` builds a `SelfUpgrader` whose default directory is
    the container's `/workspace/.alissa-upgrade`; a test that feeds it a
    vitals answer must never read or write the seat's real request file."""
    root = tmp_path_factory.mktemp("alissa-upgrade")
    monkeypatch.setenv("ALISSA_UPGRADE_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def isolated_boot_probes(monkeypatch):
    """Keep the boot probes (issue #212) off the host for EVERY test.

    A watcher with fleet vitals on runs R1 on its first completed pass, and
    R1's spawn legs shell out to `claude` and to tmux on the reviewers' own
    socket. A test that is not about the probe must not start the
    developer's (or the CI runner's) tmux server or read its Claude login, so
    the probe's default runner refuses; `test_probes.py` passes its own."""
    from alissa.tools.github.revloop import probes
    from alissa.tools.github.revloop.proc import CommandError

    def refuse(argv, **kwargs):
        raise CommandError(argv, -1, "not run in tests")

    monkeypatch.setattr(probes, "proc_run", refuse)
