from __future__ import annotations

import sqlite3

from src.extract_plugins.claude_code.dag import DagEntry, detect_forks
from src.extract_plugins.claude_code.extractor import ClaudeCodeExtractor
from src.shared.database.db_extract import get_session_file_meta, upsert_session_file_meta
from src.shared.database.db_schema import ensure_session_file_meta_table, ensure_turns_table
from src.shared.models.turn import Turn


def _make_message(
    msg_type: str,
    uuid: str = "",
    parent_uuid: str = "",
    text: str = "",
    content: list[dict] | None = None,
    timestamp: str = "2025-01-01T00:00:00Z",
    **extra,
) -> dict:
    message_content = content if content is not None else [{"type": "text", "text": text}]
    payload = {
        "type": msg_type,
        "timestamp": timestamp,
        "message": {"content": message_content, "usage": extra.pop("usage", {})},
    }
    if uuid:
        payload["uuid"] = uuid
    if parent_uuid:
        payload["parentUuid"] = parent_uuid
    payload.update(extra)
    return payload


def _extractor() -> ClaudeCodeExtractor:
    extractor = object.__new__(ClaudeCodeExtractor)
    extractor.workspace_id = "workspace-1"
    extractor.config = None
    return extractor


def test_detect_forks_linear_chain() -> None:
    entries = [
        DagEntry("u1", "", "user", 0, {}),
        DagEntry("a1", "u1", "assistant", 1, {}),
        DagEntry("u2", "a1", "user", 2, {}),
    ]

    branches = detect_forks(entries)

    assert len(branches) == 1
    assert branches[0].entry_indices == [0, 1, 2]


def test_detect_forks_small_retry_follows_latest_child() -> None:
    entries = [
        DagEntry("u1", "", "user", 0, {}),
        DagEntry("a1", "u1", "assistant", 1, {}),
        DagEntry("u2", "a1", "user", 2, {}),
        DagEntry("a2", "u2", "assistant", 3, {}),
        DagEntry("u3", "a1", "user", 4, {}),
        DagEntry("a3", "u3", "assistant", 5, {}),
    ]

    branches = detect_forks(entries)

    assert len(branches) == 1
    assert branches[0].entry_indices == [0, 1, 4, 5]


def test_detect_forks_large_branch_splits() -> None:
    entries = [
        DagEntry("u1", "", "user", 0, {}),
        DagEntry("a1", "u1", "assistant", 1, {}),
        DagEntry("u2", "a1", "user", 2, {}),
        DagEntry("a2", "u2", "assistant", 3, {}),
        DagEntry("u3", "a2", "user", 4, {}),
        DagEntry("a3", "u3", "assistant", 5, {}),
        DagEntry("u4", "a3", "user", 6, {}),
        DagEntry("a4", "u4", "assistant", 7, {}),
        DagEntry("u6", "a4", "user", 8, {}),
        DagEntry("a6", "u6", "assistant", 9, {}),
        DagEntry("u5", "a1", "user", 10, {}),
        DagEntry("a5", "u5", "assistant", 11, {}),
    ]

    branches = detect_forks(entries)

    assert len(branches) == 2
    assert branches[0].entry_indices == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert branches[1].entry_indices == [0, 1, 10, 11]
    assert branches[1].branch_uuid == "u5"


def test_detect_forks_invalid_parent_falls_back_to_linear() -> None:
    entries = [
        DagEntry("u1", "", "user", 0, {}),
        DagEntry("a1", "missing", "assistant", 1, {}),
    ]

    branches = detect_forks(entries)

    assert len(branches) == 1
    assert branches[0].entry_indices == [0, 1]


def test_convert_session_normalizes_commands_and_filters_compact_summary() -> None:
    extractor = _extractor()
    turns = extractor._convert_session(
        "session-1",
        [
            _make_message("user", text="<command-name>help</command-name>"),
            _make_message("assistant", uuid="a1", parent_uuid="u1", text="hi"),
            _make_message("user", uuid="u2", parent_uuid="a1", text="keep", isCompactSummary=True),
        ],
        "workspace-1",
        "c:/repo",
    )

    assert turns[0].original_text == "/help"
    assert all("keep" not in turn.original_text for turn in turns)


def test_convert_session_sets_source_tokens_and_relationship_fields() -> None:
    extractor = _extractor()
    turns = extractor._convert_session(
        "session-2",
        [
            _make_message("user", uuid="u1", text="hello"),
            _make_message(
                "assistant",
                uuid="a1",
                parent_uuid="u1",
                text="world",
                usage={"input_tokens": 12, "output_tokens": 34},
            ),
        ],
        "workspace-1",
        "c:/repo",
        parent_session_id="parent-1",
        relationship_type="fork",
    )

    assert turns[0].parent_session_id == "parent-1"
    assert turns[0].relationship_type == "fork"
    assert turns[1].extra["source_input_tokens"] == 12
    assert turns[1].extra["source_output_tokens"] == 34


def test_turn_relationship_fields_round_trip() -> None:
    turn = Turn(session_id="s", turn=0, role="user", parent_session_id="p", relationship_type="subagent")

    restored = Turn.from_dict(turn.to_dict())

    assert restored.parent_session_id == "p"
    assert restored.relationship_type == "subagent"


def test_schema_migration_adds_relationship_columns() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn INTEGER NOT NULL,
            role TEXT,
            text TEXT,
            original_text TEXT,
            workspace_id TEXT,
            workspace_name TEXT,
            workspace_folder TEXT,
            session_name TEXT,
            agent_used TEXT,
            model_id TEXT,
            request_id TEXT,
            timestamp_ms INTEGER,
            timestamp_iso TEXT,
            ts TEXT,
            original_text_tokens INTEGER DEFAULT 0,
            cleaned_text_tokens INTEGER DEFAULT 0,
            code_tokens INTEGER DEFAULT 0,
            tool_tokens INTEGER DEFAULT 0,
            system_tokens INTEGER DEFAULT 0,
            session_history_tokens INTEGER DEFAULT 0,
            thinking_tokens INTEGER DEFAULT 0,
            thinking_text TEXT,
            thinking_duration_ms INTEGER,
            primary_language TEXT,
            languages TEXT,
            files TEXT,
            tools TEXT,
            merged_request_ids TEXT,
            responding_to_turn INTEGER,
            response_time_ms INTEGER,
            total_lines_added INTEGER,
            total_lines_removed INTEGER,
            total_nloc_change INTEGER,
            weighted_complexity_change REAL
        )
        """
    )

    ensure_turns_table(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(turns)").fetchall()}

    assert "parent_session_id" in columns
    assert "relationship_type" in columns


def test_session_file_meta_round_trip() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_session_file_meta_table(conn)

    upsert_session_file_meta(
        conn,
        session_id="session-1",
        agent="claude_code",
        file_path="c:/repo/session.jsonl",
        file_size=123,
        last_offset=120,
        message_count=10,
    )

    stored = get_session_file_meta(conn, "session-1", "claude_code")

    assert stored is not None
    assert stored["file_path"] == "c:/repo/session.jsonl"
    assert stored["file_size"] == 123
    assert stored["last_offset"] == 120
    assert stored["message_count"] == 10
