from __future__ import annotations

from pathlib import Path

from src.extract.models import (
    AttachmentRef,
    CommandEnvelope,
    ContentBlockRecord,
    ParsedSession,
    ParsedWorkspace,
    ParserIssue,
    SessionEventRecord,
    SessionLinkRecord,
    TokenUsage,
    ToolCallRecord,
    ToolResultRecord,
    WorkspaceDescriptor,
)
from src.shared.models.code_metric import CodeMetric
from src.shared.database.db_extract import upsert_metrics
from src.shared.database.db_parsed import get_parsed_workspace, upsert_parsed_workspace
from src.shared.database.db_schema import init_shared_db


def _sample_parsed_workspace(tmp_path: Path) -> ParsedWorkspace:
    descriptor = WorkspaceDescriptor(
        workspace_id="ws-raw-1",
        agent_name="claude_code",
        workspace_name="demo",
        workspace_folder="c:/repo/demo",
        source_root=str(tmp_path / "source"),
        metadata={"project_path": "c:/repo/demo"},
    )
    session = ParsedSession(
        session_id="sess-1",
        agent_name="claude_code",
        workspace_id="ws-raw-1",
        workspace_name="demo",
        workspace_folder="c:/repo/demo",
        title="Session 1",
        source_path="c:/repo/demo/session.jsonl",
        started_at_ms=1700000000000,
        ended_at_ms=1700000002000,
        parent_session_id="",
        relationship_type="",
        metadata={"source": "claude_code"},
        issues=[ParserIssue(level="warning", code="example", message="example issue")],
        links=[
            SessionLinkRecord(
                target_session_id="agent-1",
                relationship_type="subagent",
                trigger_event_index=1,
                trigger_tool_call_id="tool-1",
                extra={"tool_name": "Task"},
            )
        ],
        events=[
            SessionEventRecord(
                index=0,
                event_type="user",
                role="user",
                timestamp_ms=1700000000000,
                timestamp_iso="2023-11-14T22:13:20+00:00",
                message_id="u1",
                request_id="req-1",
                model_id="",
                text="/review",
                command=CommandEnvelope(name="review", normalized_text="/review"),
                content_blocks=[
                    ContentBlockRecord(index=0, kind="text", text="/review", raw={"text": "/review"}),
                ],
                raw={"type": "user"},
                extra={"is_meta": False},
            ),
            SessionEventRecord(
                index=1,
                event_type="assistant",
                role="assistant",
                timestamp_ms=1700000001000,
                timestamp_iso="2023-11-14T22:13:21+00:00",
                message_id="a1",
                request_id="req-1",
                model_id="claude-sonnet",
                text="Done",
                thinking_text="thinking",
                token_usage=TokenUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=2),
                content_blocks=[
                    ContentBlockRecord(index=0, kind="text", text="Done", raw={"text": "Done"}),
                ],
                tool_calls=[
                    ToolCallRecord(
                        call_id="tool-1",
                        name="Read",
                        kind="tool_use",
                        arguments={"file_path": "c:/repo/demo/app.py"},
                        arguments_text='{"file_path": "c:/repo/demo/app.py"}',
                        file_paths=["c:/repo/demo/app.py"],
                        spawned_session_id="agent-1",
                        raw={"type": "tool_use"},
                    )
                ],
                tool_results=[
                    ToolResultRecord(
                        tool_call_id="tool-1",
                        kind="tool_result",
                        text="ok",
                        structured_content={"stdout": "ok"},
                        is_error=False,
                        status="success",
                        raw={"type": "tool_result"},
                    )
                ],
                attachments=[
                    AttachmentRef(kind="attachments", path="c:/repo/demo/app.py", title="app.py", raw={"path": "c:/repo/demo/app.py"}),
                ],
                file_paths=["c:/repo/demo/app.py"],
                raw={"type": "assistant"},
                extra={"uuid": "a1"},
            ),
        ],
    )
    return ParsedWorkspace(
        descriptor=descriptor,
        sessions=[session],
        issues=[ParserIssue(level="warning", code="workspace", message="workspace issue")],
        metadata={"source_root": str(tmp_path / "source")},
    )


def test_parsed_storage_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "parsed.db"
    conn = init_shared_db(db_path, verbose=False)
    parsed_workspace = _sample_parsed_workspace(tmp_path)

    counts = upsert_parsed_workspace(conn, parsed_workspace)
    reloaded = get_parsed_workspace(conn, "ws-raw-1", "claude_code")
    conn.close()

    assert counts["workspaces"] == 1
    assert counts["sessions"] == 1
    assert counts["events"] == 2
    assert counts["content_blocks"] == 2
    assert counts["tool_calls"] == 1
    assert counts["tool_results"] == 1
    assert counts["attachments"] == 1
    assert counts["session_links"] == 1

    assert reloaded is not None
    assert reloaded.descriptor.workspace_name == "demo"
    assert reloaded.metadata["source_root"] == str(tmp_path / "source")
    assert reloaded.issues[0].code == "workspace"
    assert len(reloaded.sessions) == 1
    session = reloaded.sessions[0]
    assert session.issues[0].code == "example"
    assert session.links[0].target_session_id == "agent-1"
    assert len(session.events) == 2
    assistant_event = session.events[1]
    assert assistant_event.token_usage is not None
    assert assistant_event.token_usage.cache_read_input_tokens == 2
    assert assistant_event.tool_calls[0].spawned_session_id == "agent-1"
    assert assistant_event.tool_results[0].status == "success"
    assert assistant_event.attachments[0].title == "app.py"


def test_parsed_storage_replaces_existing_workspace_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "parsed.db"
    conn = init_shared_db(db_path, verbose=False)

    parsed_workspace = _sample_parsed_workspace(tmp_path)
    upsert_parsed_workspace(conn, parsed_workspace)

    replacement = _sample_parsed_workspace(tmp_path)
    replacement.sessions[0].title = "Session Updated"
    replacement.sessions[0].events = replacement.sessions[0].events[:1]
    counts = upsert_parsed_workspace(conn, replacement)

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM parsed_workspaces")
    workspace_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM parsed_sessions")
    session_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM parsed_events")
    event_count = cursor.fetchone()[0]
    reloaded = get_parsed_workspace(conn, "ws-raw-1", "claude_code")
    conn.close()

    assert counts["workspaces"] == 1
    assert workspace_count == 1
    assert session_count == 1
    assert event_count == 1
    assert reloaded is not None
    assert reloaded.sessions[0].title == "Session Updated"
    assert len(reloaded.sessions[0].events) == 1


def test_upsert_metrics_populates_line_counts_from_delta_metrics(tmp_path: Path) -> None:
    db_path = tmp_path / "metrics.db"
    conn = init_shared_db(db_path, verbose=False)

    metric = CodeMetric(
        request_id="req-1",
        session_id="sess-1",
        file_path="c:/repo/demo/app.py",
        workspace_id="ws-1",
        agent_used="claude_code",
        model_id="claude-sonnet",
        delta_metrics={"lines_added": 12, "lines_removed": 3, "nloc": 9, "cyclomatic_complexity": 1.5},
    )

    upsert_metrics(conn, [metric])
    row = conn.execute(
        "SELECT lines_added, lines_removed, delta_nloc, delta_complexity FROM code_metrics WHERE request_id = ? AND file_path = ?",
        ("req-1", "c:/repo/demo/app.py"),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 12
    assert row[1] == 3
    assert row[2] == 9
    assert row[3] == 1.5

