"""The classed Merge-Readiness grammar (issue #142).

    Merge-Readiness: auto
    Merge-Readiness: operator — <class>: <one-line reason>

`<class>` is one token from a closed, severity-ordered enum. This module pins
the enum itself, the parse of every token through both parsers and both dash
separators, the unclassed operator (valid grammar, `klass=None`, reason kept
whole), the directive's one-statement table, the byte-equality of the copied
native line with the envelope's, the class-free fallback, and the class on
every narration surface. The pre-#142 readiness tests (value and reason)
stay in test_loop.py; the shared fakes are imported from there.
"""

from __future__ import annotations

import logging

import pytest

from alissa.tools.github.revloop.alissa import (
    READINESS_AUTO,
    READINESS_CLASSES,
    READINESS_OPERATOR,
    READINESS_TRAILER_LABEL,
    VerdictEnvelope,
    classify_readiness_reason,
    parse_readiness,
    parse_trailer,
)
from alissa.tools.github.revloop.loop import (
    READINESS_MISSING_REASON,
    READINESS_UNCLASSED,
    ROUND_1_DIRECTIVE,
    ROUND_K_DIRECTIVE,
    Action,
    readiness_class_term,
    readiness_trailer,
)
from test_loop import (  # the fakes and the PR scenario the loop tests share
    NUMBER,
    OWNER,
    REPO,
    SLUG,
    VERDICT_APPROVE,
    activity_comments,
    config,  # noqa: F401 -- fixture, used by name
    envelope,
    envelope_ahead,
    envelope_from,
    make_pr,
    no_post_grace,  # noqa: F401 -- fixture, used by name
    readiness_records,
    readiness_rows,
    session_approve,
    trailer_of,
    watcher,
)

# The issue's table, top to bottom. Pinned as a literal so a reorder or a
# respelling in the source is a test failure, not a silent grammar change.
ISSUE_142_ENUM = (
    "schema-migration",
    "data-backfill",
    "secrets-env",
    "infra-deploy",
    "billing",
    "security",
    "unverified-ux",
    "unverified-runtime",
    "release-act",
)


def test_the_enum_is_the_issues_nine_tokens_in_severity_order():
    assert READINESS_CLASSES == ISSUE_142_ENUM
    assert len(set(READINESS_CLASSES)) == 9
    for klass in READINESS_CLASSES:
        assert klass == klass.lower() and " " not in klass and ":" not in klass


# -- every class, both parsers, both separators ------------------------------


@pytest.mark.parametrize("klass", READINESS_CLASSES)
@pytest.mark.parametrize("dash", ["—", "-"], ids=["em-dash", "hyphen"])
def test_every_class_parses_off_the_envelope_line(klass, dash):
    line = f"- **Merge-Readiness:** operator {dash} {klass}: touches convex/schema.ts"
    assert parse_readiness(f"# Review verdict: o/r#1 — approve\n\n{line}\n") == (
        READINESS_OPERATOR, f"{klass}: touches convex/schema.ts", klass,
    )


@pytest.mark.parametrize("klass", READINESS_CLASSES)
@pytest.mark.parametrize("dash", ["—", "-"], ids=["em-dash", "hyphen"])
def test_every_class_parses_off_the_bare_trailer(klass, dash):
    line = f"{READINESS_TRAILER_LABEL} operator {dash} {klass}: the reason"
    assert parse_trailer(f"verdict\n\n{line}\n") == (
        READINESS_OPERATOR, f"{klass}: the reason", klass,
    )


@pytest.mark.parametrize("klass", READINESS_CLASSES)
def test_the_class_survives_the_reason_cleaning(klass):
    """Backticks, runs of whitespace and a stray newline are flattened AROUND
    the prefix, never through it: the class is read off the cleaned reason."""
    value, reason, got = parse_readiness(
        f"- **Merge-Readiness:** operator — `{klass}`:   touches   `convex/schema.ts`\nnext\n"
    )
    assert (value, got) == (READINESS_OPERATOR, klass)
    assert reason == f"{klass}: touches convex/schema.ts"


def test_a_class_with_no_space_after_the_colon_still_reads():
    assert parse_readiness("- **Merge-Readiness:** operator — billing:quota bump")[2] == "billing"
    assert parse_trailer("Merge-Readiness: operator — billing :quota bump")[2] == "billing"


# -- unclassed: valid grammar, klass=None, reason whole ------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "touches convex/schema.ts",
        "Schema-Migration: capitalised token",
        "SECURITY: shouting",
        "schema-migration — no colon after the token",
        "schema-migration removes users.legacyId",
        "see schema-migration: mid-reason, not leading",
        "schema-migrations: near miss",
        "data-backfill-ish: near miss",
        "",
    ],
    ids=["prose", "capitalised", "upper", "dash-not-colon", "no-colon", "mid-reason",
         "plural", "suffixed", "empty"],
)
def test_an_unclassed_operator_still_parses_with_its_reason_whole(reason):
    """An operator line without a recognised leading token is not an error:
    it is `operator`, its reason untouched, `klass=None` -- the consumer's
    hard hold. A reviewer that forgets the class loses throughput, nothing
    else."""
    tail = f" — {reason}" if reason else ""
    assert parse_readiness(f"- **Merge-Readiness:** operator{tail}\n") == (
        READINESS_OPERATOR, reason, None,
    )
    assert parse_trailer(f"Merge-Readiness: operator{tail}\n") == (
        READINESS_OPERATOR, reason, None,
    )


def test_auto_never_carries_a_class_even_when_the_reason_names_one():
    assert parse_readiness("- **Merge-Readiness:** auto — schema-migration: x") == (
        READINESS_AUTO, "schema-migration: x", None,
    )
    assert parse_trailer("Merge-Readiness: auto — billing: y") == (READINESS_AUTO, "billing: y", None)
    assert classify_readiness_reason(READINESS_AUTO, "billing: y") is None


def test_classify_is_pure_over_the_value_and_the_reason():
    assert classify_readiness_reason(READINESS_OPERATOR, "billing: y") == "billing"
    assert classify_readiness_reason(READINESS_OPERATOR, "prose") is None
    assert classify_readiness_reason(READINESS_OPERATOR, None) is None
    assert classify_readiness_reason(None, "billing: y") is None
    assert classify_readiness_reason("missing", "billing: y") is None


def test_no_line_at_all_is_the_class_free_triple():
    assert parse_readiness("no line here") == (None, "", None)
    assert parse_readiness(None) == (None, "", None)
    assert parse_trailer("no line here") == (None, "", None)


# -- the envelope carries the class ------------------------------------------


def test_the_envelope_carries_its_class_with_the_verdict(monkeypatch):
    item = envelope("approve", 2, "2026-09-27T20:20:00Z")
    item["markdownContent"] += "\n- **Merge-Readiness:** operator — infra-deploy: Dockerfile FROM pin\n"
    assert envelope_from(monkeypatch, {"evidence": [item]}) == VerdictEnvelope(
        "approve", "operator", "infra-deploy: Dockerfile FROM pin", "infra-deploy",
    )


def test_an_unclassed_envelope_carries_none(monkeypatch):
    item = envelope("approve", 2, "2026-09-27T20:20:00Z")
    item["markdownContent"] += "\n- **Merge-Readiness:** operator — touches convex/schema.ts\n"
    got = envelope_from(monkeypatch, {"evidence": [item]})
    assert got.readiness_class is None
    assert got.readiness_reason == "touches convex/schema.ts"


def test_the_class_is_read_from_the_title_when_the_body_has_none(monkeypatch):
    item = envelope("approve", 1, "2026-09-27T20:20:00Z")
    item["title"] += "\nMerge-Readiness: operator — release-act: VERSION 0.31.4"
    assert envelope_from(monkeypatch, {"evidence": [item]}).readiness_class == "release-act"


# -- the directive states the table once ---------------------------------------


def _formatted(template):
    return template.format(
        assignment="You've been assigned TASK-1.", round=2, cap=3,
        session="review-widgets-pr7-r2", credential="", poll=60, wait=30,
        checks="", stability="",
    )


@pytest.mark.parametrize("template", [ROUND_1_DIRECTIVE, ROUND_K_DIRECTIVE], ids=["round-1", "round-k"])
def test_the_directive_names_every_enum_token_exactly_once(template):
    """Both the verdict round and every fix-round re-review carry the table,
    and each token appears exactly once -- the enum is stated, not scattered."""
    text = _formatted(template)
    for klass in READINESS_CLASSES:
        assert text.count(klass) == 1, klass
    # in the issue's severity order, so "the row listed first" is unambiguous
    positions = [text.index(klass) for klass in READINESS_CLASSES]
    assert positions == sorted(positions)


@pytest.mark.parametrize("template", [ROUND_1_DIRECTIVE, ROUND_K_DIRECTIVE], ids=["round-1", "round-k"])
def test_the_directive_states_the_rules_of_the_class(template):
    text = _formatted(template)
    assert "operator — <class>: <one-line reason>" in text
    assert "exactly ONE token" in text
    assert "MOST SEVERE" in text
    assert "validation work" in text and "not merge risk" in text, "the honesty reasoning"
    assert "UNCLASSED" in text and "hard hold" in text
    assert "`auto` never carries a class" in text
    assert "posts as `operator` with no class" in text


# -- the copied native line is the envelope's, byte for byte ------------------


@pytest.mark.parametrize("klass", READINESS_CLASSES)
def test_the_native_trailer_equals_the_envelope_line_class_included(klass):
    """One judgment, two grammars: the emitter copies the envelope's cleaned
    reason whole, so `Merge-Readiness: operator — <class>: <reason>` on the
    native review is the envelope's value, class and reason byte for byte."""
    envelope_line = f"- **Merge-Readiness:** operator — {klass}: touches convex/schema.ts"
    value, reason, got = parse_readiness(f"# Review verdict: o/r#1 — approve\n\n{envelope_line}\n")
    trailer = readiness_trailer(VerdictEnvelope(VERDICT_APPROVE, value, reason, got))
    assert trailer == f"Merge-Readiness: operator — {klass}: touches convex/schema.ts"
    assert trailer == envelope_line.replace("- **Merge-Readiness:**", "Merge-Readiness:")
    assert parse_trailer(trailer) == (READINESS_OPERATOR, reason, klass), "and reads back whole"


def test_the_posted_approve_carries_the_class_byte_for_byte(config, no_post_grace):
    w, gh, _ = envelope_ahead(
        config, VERDICT_APPROVE,
        readiness=READINESS_OPERATOR, readiness_reason="secrets-env: needs RAILWAY_TOKEN",
    )

    assert w.evaluate(OWNER, REPO, NUMBER).action is Action.POSTED
    assert gh.submitted[0]["event"] == "APPROVE"
    line = trailer_of(gh.submitted[0]["body"])
    assert line == "Merge-Readiness: operator — secrets-env: needs RAILWAY_TOKEN"
    assert parse_trailer(line) == (READINESS_OPERATOR, "secrets-env: needs RAILWAY_TOKEN", "secrets-env")


# -- the fallback carries no class --------------------------------------------


@pytest.mark.parametrize(
    "envelope_",
    [None, VerdictEnvelope(VERDICT_APPROVE), VerdictEnvelope(VERDICT_APPROVE, None, "", None)],
    ids=["no-envelope", "no-line", "explicit-none"],
)
def test_the_envelope_less_fallback_is_operator_with_no_class(envelope_):
    trailer = readiness_trailer(envelope_)
    assert trailer == f"Merge-Readiness: operator — {READINESS_MISSING_REASON}"
    assert parse_trailer(trailer) == (READINESS_OPERATOR, READINESS_MISSING_REASON, None)


def test_the_fallback_posts_unclassed_and_the_narration_says_so(config, no_post_grace, caplog):
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE)  # readiness=None

    with caplog.at_level(logging.INFO):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.POSTED

    line = trailer_of(gh.submitted[0]["body"])
    assert line == "Merge-Readiness: operator — envelope carries no Merge-Readiness line"
    assert parse_trailer(line)[2] is None
    closed = [r.getMessage() for r in caplog.records if "closed: native APPROVE" in r.getMessage()]
    assert len(closed) == 1 and f"class={READINESS_UNCLASSED}" in closed[0]
    assert f"class={READINESS_UNCLASSED}" in activity_comments(gh)[0].body


# -- the class on every narration surface -------------------------------------


def test_readiness_class_term_names_operator_only():
    assert readiness_class_term(READINESS_OPERATOR, "billing") == " class=billing"
    assert readiness_class_term(READINESS_OPERATOR, None) == f" class={READINESS_UNCLASSED}"
    assert readiness_class_term(READINESS_AUTO, None) == ""
    assert readiness_class_term(READINESS_AUTO, "billing") == "", "auto never carries a class"
    assert readiness_class_term(None, None) == ""


def test_the_round_close_log_and_activity_row_name_the_class(config, no_post_grace, caplog):
    w, gh, _ = envelope_ahead(
        config, VERDICT_APPROVE,
        readiness=READINESS_OPERATOR, readiness_reason="data-backfill: recountAll rewrites rows",
    )

    with caplog.at_level(logging.INFO):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.POSTED

    closed = [r.getMessage() for r in caplog.records if "closed: native APPROVE" in r.getMessage()]
    assert len(closed) == 1
    assert "readiness=operator — data-backfill: recountAll rewrites rows class=data-backfill" in closed[0]
    row = activity_comments(gh)[0].body
    assert "merge-readiness: `operator — data-backfill: recountAll rewrites rows` class=data-backfill" in row


def test_an_auto_close_names_no_class(config, no_post_grace, caplog):
    w, gh, _ = envelope_ahead(config, VERDICT_APPROVE, readiness=READINESS_AUTO)

    with caplog.at_level(logging.INFO):
        assert w.evaluate(OWNER, REPO, NUMBER).action is Action.POSTED

    assert not any("class=" in r.getMessage() for r in caplog.records)
    assert "class=" not in activity_comments(gh)[0].body


def test_a_session_approve_with_a_class_logs_it(config, caplog):
    w, gh, _ = watcher(
        config, make_pr(),
        [session_approve("Merge-Readiness: operator — unverified-runtime: live smoke not run")],
    )

    with caplog.at_level(logging.INFO):
        w.evaluate(OWNER, REPO, NUMBER)

    (record,) = readiness_records(caplog)
    assert (
        "carries readiness=operator — unverified-runtime: live smoke not run class=unverified-runtime"
        in record.getMessage()
    )
    (row,) = readiness_rows(gh)
    assert "readiness=operator — unverified-runtime: live smoke not run class=unverified-runtime" in row
    assert w.state.readiness_observed(SLUG, NUMBER, "abc123") == READINESS_OPERATOR


def test_a_session_approve_without_a_class_logs_unclassed(config, caplog):
    w, gh, _ = watcher(
        config, make_pr(), [session_approve("Merge-Readiness: operator — touches convex/schema.ts")],
    )

    with caplog.at_level(logging.INFO):
        w.evaluate(OWNER, REPO, NUMBER)

    (record,) = readiness_records(caplog)
    assert f"readiness=operator — touches convex/schema.ts class={READINESS_UNCLASSED}" in record.getMessage()
    (row,) = readiness_rows(gh)
    assert f"class={READINESS_UNCLASSED}" in row


def test_a_session_auto_approve_names_no_class(config, caplog):
    w, gh, _ = watcher(config, make_pr(), [session_approve("Merge-Readiness: auto")])

    with caplog.at_level(logging.INFO):
        w.evaluate(OWNER, REPO, NUMBER)

    (record,) = readiness_records(caplog)
    assert record.getMessage().endswith("carries readiness=auto")
    (row,) = readiness_rows(gh)
    assert "class=" not in row
