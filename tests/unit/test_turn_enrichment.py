from __future__ import annotations

from typing import Any

from src.pipeline.extraction.turn_enrichment import enrich_turn
from src.shared.models.turn import Turn


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
