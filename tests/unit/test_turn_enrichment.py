from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from src.pipeline.extraction import turn_enrichment
from src.pipeline.extraction.turn_enrichment import enrich_turn, enrich_turns
from src.shared.models.turn import CodeEdit, Turn
from src.shared.text.text_shrinker import TextShrinker, ShrinkConfig


@pytest.fixture(autouse=True)
def _isolate_text_shrinker():
    """Provide a TextShrinker with defaults so tests don't need config/config.yaml."""
    shrinker = TextShrinker(config=ShrinkConfig())
    with patch.object(turn_enrichment, "_text_shrinker", shrinker):
        yield
    # Reset the module-level singleton after each test
    turn_enrichment._text_shrinker = None


def _make_turn(**kwargs: Any) -> Turn:
    payload: dict[str, Any] = {
        "session_id": "session-1",
        "turn": 0,
        "role": "assistant",
        "original_text": "hello world",
    }
    payload.update(kwargs)
    return Turn(**payload)


def test_tool_categories_are_added_to_extra() -> None:
    turn = _make_turn(tools=["Read", "copilot_runInTerminal", "runSubagent"])

    enriched = enrich_turn(turn)

    assert enriched.extra["tool_categories"] == ["Bash", "Read", "Task"]


def test_source_output_tokens_override_assistant_estimate() -> None:
    turn = _make_turn(extra={"source_output_tokens": 77})

    enriched = enrich_turn(turn)

    assert enriched.original_text_tokens == 77
    assert enriched.session_history_tokens == 0


def test_source_input_tokens_override_user_estimate() -> None:
    turn = _make_turn(role="user", extra={"source_input_tokens": 33})

    enriched = enrich_turn(turn)

    assert enriched.original_text_tokens == 33
    assert enriched.session_history_tokens == 0


def test_unknown_extra_fields_are_preserved() -> None:
    turn = _make_turn(extra={"custom_field": "keep", "source_output_tokens": 9})

    enriched = enrich_turn(turn)

    assert enriched.extra["custom_field"] == "keep"
    assert enriched.extra["source_output_tokens"] == 9


def test_parent_session_relationship_fields_are_preserved() -> None:
    turn = _make_turn(
        parent_session_id="parent-session-1",
        relationship_type="subagent",
    )

    enriched = enrich_turn(turn)

    assert enriched.parent_session_id == "parent-session-1"
    assert enriched.relationship_type == "subagent"


def test_snapshot_edit_chaining_is_scoped_per_session() -> None:
    turns = [
        _make_turn(
            session_id="session-1",
            turn=1,
            timestamp_ms=1,
            code_edits=[CodeEdit(file_path="src/app.py", language="python", code_after="old")],
        ),
        _make_turn(
            session_id="session-2",
            turn=1,
            timestamp_ms=2,
            code_edits=[CodeEdit(file_path="src/app.py", language="python", code_after="new")],
        ),
    ]

    enriched = enrich_turns(turns, calculate_metrics=False)

    assert enriched[0].code_edits[0].code_before is None
    assert enriched[1].code_edits[0].code_before is None
