from __future__ import annotations

from pathlib import Path

from src.extract.models import (
    ContentBlockRecord,
    ParsedSession,
    ParsedWorkspace,
    SessionEventRecord,
    SessionLinkRecord,
    TokenUsage,
    ToolCallRecord,
    ToolResultRecord,
    WorkspaceDescriptor,
)
from src.pipeline.extraction.adapter import adapt_parsed_workspace
from src.pipeline.extraction.turn_enrichment import enrich_turns
from src.pipeline.extraction.storage import store_extraction_result
from src.shared.database.db_extract import query_session_turns
from src.shared.database.db_schema import init_shared_db
from src.shared.models.workspace import WorkspaceInfo


def _make_workspace_info(tmp_path: Path) -> WorkspaceInfo:
    workspace = WorkspaceInfo(
        workspace_id="ws-1",
        workspace_name="demo",
        workspace_folder=str(tmp_path),
        agents=["claude_code"],
        session_count=2,
    )
    workspace._agent_workspace_ids = {"claude_code": ["ws-1"]}  # type: ignore[attr-defined]
    return workspace


def _make_parsed_workspace(tmp_path: Path) -> ParsedWorkspace:
    descriptor = WorkspaceDescriptor(
        workspace_id="ws-1",
        agent_name="claude_code",
        workspace_name="demo",
        workspace_folder=str(tmp_path),
        source_root=str(tmp_path / "source"),
    )

    parent_session = ParsedSession(
        session_id="parent-session",
        agent_name="claude_code",
        workspace_id="ws-1",
        workspace_name="demo",
        workspace_folder=str(tmp_path),
        title="Parent Session",
        source_path=str(tmp_path / "parent.jsonl"),
        links=[
            SessionLinkRecord(
                target_session_id="agent-123",
                relationship_type="subagent",
                trigger_event_index=1,
                trigger_tool_call_id="tool-task",
                extra={"tool_name": "Task"},
            )
        ],
        metadata={"source": "claude_code"},
        events=[
            SessionEventRecord(
                index=0,
                event_type="user",
                role="user",
                timestamp_ms=1700000000000,
                timestamp_iso="2023-11-14T22:13:20+00:00",
                text="main prompt",
                raw={"type": "user"},
            ),
            SessionEventRecord(
                index=1,
                event_type="assistant",
                role="assistant",
                timestamp_ms=1700000001000,
                timestamp_iso="2023-11-14T22:13:21+00:00",
                request_id="req-1",
                model_id="claude-sonnet",
                text="Running",
                token_usage=TokenUsage(input_tokens=50, output_tokens=20),
                content_blocks=[
                    ContentBlockRecord(index=0, kind="text", text="Running", raw={"text": "Running"}),
                    ContentBlockRecord(
                        index=1,
                        kind="tool_use",
                        text='Task({"description": "delegate"})',
                        raw={"type": "tool_use"},
                        extra={
                            "tool_call": ToolCallRecord(
                                call_id="tool-task",
                                name="Task",
                                kind="tool_use",
                                arguments={"description": "delegate"},
                                arguments_text='{"description": "delegate"}',
                                spawned_session_id="agent-123",
                                raw={"type": "tool_use"},
                            ).to_dict()
                        },
                    ),
                ],
                tool_calls=[
                    ToolCallRecord(
                        call_id="tool-task",
                        name="Task",
                        kind="tool_use",
                        arguments={"description": "delegate"},
                        arguments_text='{"description": "delegate"}',
                        spawned_session_id="agent-123",
                        raw={"type": "tool_use"},
                    )
                ],
                tool_results=[
                    ToolResultRecord(
                        tool_call_id="tool-task",
                        kind="tool_result",
                        text="delegate accepted",
                        structured_content={"stdout": "delegate accepted"},
                        status="success",
                        raw={"type": "tool_result"},
                    )
                ],
                raw={"type": "assistant"},
            ),
        ],
    )

    child_session = ParsedSession(
        session_id="agent-123",
        agent_name="claude_code",
        workspace_id="ws-1",
        workspace_name="demo",
        workspace_folder=str(tmp_path),
        title="Child Session",
        source_path=str(tmp_path / "agent-123.jsonl"),
        parent_session_id="parent-session",
        relationship_type="subagent",
        metadata={"source": "claude_code"},
        events=[
            SessionEventRecord(
                index=0,
                event_type="user",
                role="user",
                timestamp_ms=1700000002000,
                timestamp_iso="2023-11-14T22:13:22+00:00",
                text="child prompt",
                raw={"type": "user"},
            ),
            SessionEventRecord(
                index=1,
                event_type="assistant",
                role="assistant",
                timestamp_ms=1700000003000,
                timestamp_iso="2023-11-14T22:13:23+00:00",
                text="child result",
                raw={"type": "assistant"},
            ),
        ],
    )

    return ParsedWorkspace(descriptor=descriptor, sessions=[parent_session, child_session])


def test_store_extraction_persists_turn_tool_and_subagent_details(monkeypatch, tmp_path: Path) -> None:
    parsed_workspace = _make_parsed_workspace(tmp_path)
    extraction_result = adapt_parsed_workspace(parsed_workspace)
    extraction_result.turns = enrich_turns(list(extraction_result.turns))
    extraction_result.source_artifacts["parsed_workspaces"] = [parsed_workspace]

    monkeypatch.setattr("src.pipeline.extraction.storage.find_workspace", lambda workspace_id: _make_workspace_info(tmp_path))

    db_path = tmp_path / "turn-details.db"
    init_shared_db(db_path, verbose=False).close()
    store_result = store_extraction_result(extraction_result, db_path)

    assert store_result.success

    conn = init_shared_db(db_path, verbose=False)
    turns = query_session_turns(conn, "parent-session")
    conn.close()

    assert len(turns) == 2
    assistant_turn = turns[1]
    assert assistant_turn["tool_runs"][0]["name"] == "Task"
    assert assistant_turn["tool_runs"][0]["status"] == "success"
    assert assistant_turn["tool_runs"][0]["results"][0]["text"] == "delegate accepted"
    assert assistant_turn["subagent_runs"][0]["subagent_session_id"] == "agent-123"
    assert assistant_turn["subagent_runs"][0]["prompt_text"] == "child prompt"
    assert assistant_turn["subagent_runs"][0]["result_text"] == "child result"