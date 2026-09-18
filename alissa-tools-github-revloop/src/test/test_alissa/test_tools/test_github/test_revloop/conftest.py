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
