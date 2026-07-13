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
from src.pipeline.extraction.adapter import adapt_parsed_workspace
from src.shared.models.workspace import ExtractedWorkspace


def _sample_workspace(tmp_path: Path) -> ParsedWorkspace:
    descriptor = WorkspaceDescriptor(
        workspace_id="ws-1",
        agent_name="claude_code",
        workspace_name="demo",
        workspace_folder="c:/repo/demo",
        source_root=str(tmp_path / "source"),
        metadata={"project_path": "c:/repo/demo"},
    )
    session = ParsedSession(
        session_id="session-main",
        agent_name="claude_code",
        workspace_id="ws-1",
        workspace_name="demo",
        workspace_folder="c:/repo/demo",
        title="Session Main",
        source_path="c:/repo/demo/session-main.jsonl",
        started_at_ms=1700000000000,
        ended_at_ms=1700000003000,
        parent_session_id="",
        relationship_type="",
        metadata={"source": "claude_code"},
        issues=[ParserIssue(level="warning", code="session-example", message="session issue")],
        links=[
            SessionLinkRecord(
                target_session_id="agent-1",
                relationship_type="subagent",
                trigger_event_index=1,
                trigger_tool_call_id="tool-task",
                extra={"tool_name": "Task"},
            )
        ],
        events=[
            SessionEventRecord(
                index=0,
                event_type="summary",
                role="system",
                timestamp_ms=1700000000000,
                timestamp_iso="2023-11-14T22:13:20+00:00",
                text="summary",
                raw={"type": "summary"},
            ),
            SessionEventRecord(
                index=1,
                event_type="user",
                role="user",
                timestamp_ms=1700000001000,
                timestamp_iso="2023-11-14T22:13:21+00:00",
                message_id="u1",
                request_id="req-1",
                text="<command-name>review</command-name>",
                command=CommandEnvelope(name="review", normalized_text="/review", raw_text="<command-name>review</command-name>"),
                content_blocks=[ContentBlockRecord(index=0, kind="text", text="/review", raw={"text": "/review"})],
                raw={"type": "user"},
                extra={"source": "event1"},
            ),
            SessionEventRecord(
                index=2,
                event_type="assistant",
                role="assistant",
                timestamp_ms=1700000002000,
                timestamp_iso="2023-11-14T22:13:22+00:00",
                message_id="a1",
                request_id="req-1",
                model_id="claude-sonnet",
                text="Running",
                thinking_text="think 1",
                token_usage=TokenUsage(input_tokens=100, output_tokens=25, cache_read_input_tokens=5),
                tool_calls=[
                    ToolCallRecord(
                        call_id="tool-write",
                        name="Write",
                        kind="tool_use",
                        arguments={"file_path": "c:/repo/demo/app.py", "content": "print('x')"},
                        arguments_text='{"content": "print(\'x\')", "file_path": "c:/repo/demo/app.py"}',
                        file_paths=["c:/repo/demo/app.py"],
                        raw={"type": "tool_use"},
                    ),
                    ToolCallRecord(
                        call_id="tool-task",
                        name="Task",
                        kind="tool_use",
                        arguments={"description": "delegate"},
                        arguments_text='{"description": "delegate"}',
                        file_paths=[],
                        spawned_session_id="agent-1",
                        raw={"type": "tool_use"},
                    ),
                ],
                tool_results=[
                    ToolResultRecord(
                        tool_call_id="tool-write",
                        kind="tool_result",
                        text="ok",
                        structured_content={"stdout": "ok"},
                        is_error=False,
                        status="success",
                        raw={"type": "tool_result"},
                    )
                ],
                attachments=[AttachmentRef(kind="attachments", path="c:/repo/demo/app.py", title="app.py", raw={"path": "c:/repo/demo/app.py"})],
                content_blocks=[ContentBlockRecord(index=0, kind="text", text="Running", raw={"text": "Running"})],
                file_paths=["c:/repo/demo/app.py"],
                raw={"type": "assistant"},
                extra={"uuid": "a1"},
            ),
            SessionEventRecord(
                index=3,
                event_type="assistant",
                role="assistant",
                timestamp_ms=1700000002500,
                timestamp_iso="2023-11-14T22:13:22.500000+00:00",
                message_id="a2",
                request_id="req-2",
                model_id="claude-sonnet",
                text="Done",
                thinking_text="think 2",
                token_usage=TokenUsage(input_tokens=10, output_tokens=5, cache_creation_input_tokens=2),
                content_blocks=[ContentBlockRecord(index=0, kind="text", text="Done", raw={"text": "Done"})],
                raw={"type": "assistant"},
                extra={"source": "event2"},
            ),
        ],
    )
    return ParsedWorkspace(
        descriptor=descriptor,
        sessions=[session],
        issues=[ParserIssue(level="warning", code="workspace-example", message="workspace issue")],
        metadata={"source_root": str(tmp_path / "source")},
    )


def test_adapt_parsed_workspace_returns_genie_workspace(tmp_path: Path) -> None:
    parsed_workspace = _sample_workspace(tmp_path)

    adapted = adapt_parsed_workspace(parsed_workspace)

    assert isinstance(adapted, ExtractedWorkspace)
    assert adapted.workspace_id == "ws-1"
    assert adapted.agent_name == "claude_code"
    assert adapted.session_count == 1
    assert len(adapted.turns) == 2

    user_turn = adapted.turns[0]
    assistant_turn = adapted.turns[1]

    assert user_turn.role == "user"
    assert user_turn.original_text == "/review"
    assert user_turn.extra["source_event_indices"] == [1]
    assert user_turn.extra["source_session_metadata"]["source"] == "claude_code"
    assert user_turn.extra["source_workspace_issues"][0]["code"] == "workspace-example"

    assert assistant_turn.role == "assistant"
    assert assistant_turn.original_text == "Running\n\nDone"
    assert assistant_turn.thinking_text == "think 1\n\nthink 2"
    assert assistant_turn.request_id == "req-1"
    assert assistant_turn.merged_request_ids == ["req-1", "req-2"]
    assert assistant_turn.model_id == "claude-sonnet"
    assert assistant_turn.files == ["c:/repo/demo/app.py"]
    assert assistant_turn.tools == ["Task", "Write"]
    assert assistant_turn.parent_session_id is None
    assert assistant_turn.extra["source_input_tokens"] == 110
    assert assistant_turn.extra["source_output_tokens"] == 30
    assert assistant_turn.extra["token_usage"]["cache_read_input_tokens"] == 5
    assert assistant_turn.extra["token_usage"]["cache_creation_input_tokens"] == 2
    assert assistant_turn.extra["subagent_session_id"] == "agent-1"
    assert assistant_turn.extra["tool_calls"][0]["name"] == "Write"
    assert assistant_turn.extra["tool_results"][0]["status"] == "success"
    assert assistant_turn.code_edits[0].file_path == "c:/repo/demo/app.py"
    assert assistant_turn.code_edits[0].code_after == "print('x')"


def test_adapt_parsed_workspace_ignores_non_conversation_events(tmp_path: Path) -> None:
    parsed_workspace = _sample_workspace(tmp_path)
    parsed_workspace.sessions[0].events = [event for event in parsed_workspace.sessions[0].events if event.role == "system"]

    adapted = adapt_parsed_workspace(parsed_workspace)

    assert adapted.session_count == 0
    assert len(adapted.turns) == 0


def test_adapt_parsed_workspace_skips_pure_tool_result_user_events(tmp_path: Path) -> None:
    parsed_workspace = _sample_workspace(tmp_path)
    parsed_workspace.sessions[0].events.insert(
        3,
        SessionEventRecord(
            index=99,
            event_type="user",
            role="user",
            timestamp_ms=1700000001500,
            timestamp_iso="2023-11-14T22:13:21.500000+00:00",
            text="tool result",
            tool_results=[
                ToolResultRecord(
                    tool_call_id="tool-write",
                    kind="tool_result",
                    text="tool result",
                    status="success",
                    raw={"type": "tool_result"},
                )
            ],
            content_blocks=[
                ContentBlockRecord(index=0, kind="tool_result", text="tool result", raw={"type": "tool_result"}),
            ],
            raw={"type": "user"},
        ),
    )

    adapted = adapt_parsed_workspace(parsed_workspace)

    assert len(adapted.turns) == 2
    assert [turn.role for turn in adapted.turns] == ["user", "assistant"]
    assert len(adapted.turns[1].extra["tool_results"]) == 2
    assert adapted.turns[1].extra["tool_results"][1]["tool_call_id"] == "tool-write"
    assert adapted.turns[1].extra["tool_results"][1]["status"] == "success"


def test_adapt_parsed_workspace_merges_non_conversation_tool_result_events(tmp_path: Path) -> None:
    parsed_workspace = _sample_workspace(tmp_path)
    parsed_workspace.sessions[0].events[2].tool_results = []
    parsed_workspace.sessions[0].events.insert(
        3,
        SessionEventRecord(
            index=100,
            event_type="tool.execution_complete",
            role=None,
            timestamp_ms=1700000002250,
            timestamp_iso="2023-11-14T22:13:22.250000+00:00",
            text="ok",
            tool_results=[
                ToolResultRecord(
                    tool_call_id="tool-write",
                    kind="tool_result",
                    text="ok",
                    status="success",
                    raw={"type": "tool_result"},
                )
            ],
            raw={"type": "tool.execution_complete"},
        ),
    )

    adapted = adapt_parsed_workspace(parsed_workspace)

    assert len(adapted.turns) == 2
    assert [turn.role for turn in adapted.turns] == ["user", "assistant"]
    assert len(adapted.turns[1].extra["tool_results"]) == 1
    assert adapted.turns[1].extra["tool_results"][0]["tool_call_id"] == "tool-write"
    assert adapted.turns[1].extra["tool_results"][0]["status"] == "success"


def test_adapt_parsed_workspace_drops_local_command_preamble(tmp_path: Path) -> None:
    parsed_workspace = _sample_workspace(tmp_path)
    parsed_workspace.sessions[0].title = "real question"
    parsed_workspace.sessions[0].events = [
        SessionEventRecord(
            index=0,
            event_type="user",
            role="user",
            timestamp_ms=1700000000000,
            text="<local-command-caveat>Caveat: local commands only.</local-command-caveat>",
            extra={"is_meta": True},
            raw={"type": "user"},
        ),
        SessionEventRecord(
            index=1,
            event_type="user",
            role="user",
            timestamp_ms=1700000000100,
            text="<command-name>/clear</command-name>",
            command=CommandEnvelope(name="clear", normalized_text="/clear", raw_text="<command-name>/clear</command-name>"),
            raw={"type": "user"},
        ),
        SessionEventRecord(
            index=2,
            event_type="user",
            role="user",
            timestamp_ms=1700000000200,
            text="<local-command-stdout>cleared</local-command-stdout>",
            raw={"type": "user"},
        ),
        SessionEventRecord(
            index=3,
            event_type="user",
            role="user",
            timestamp_ms=1700000000300,
            text="real question",
            raw={"type": "user"},
        ),
        SessionEventRecord(
            index=4,
            event_type="assistant",
            role="assistant",
            timestamp_ms=1700000000400,
            text="real answer",
            raw={"type": "assistant"},
        ),
    ]

    adapted = adapt_parsed_workspace(parsed_workspace)

    assert adapted.session_count == 1
    assert [turn.original_text for turn in adapted.turns] == ["real question", "real answer"]


def test_adapt_parsed_workspace_hides_command_only_local_command_sessions(tmp_path: Path) -> None:
    parsed_workspace = _sample_workspace(tmp_path)
    parsed_workspace.sessions[0].events = [
        SessionEventRecord(
            index=0,
            event_type="user",
            role="user",
            timestamp_ms=1700000000000,
            text="<local-command-caveat>Caveat: local commands only.</local-command-caveat>",
            extra={"is_meta": True},
            raw={"type": "user"},
        ),
        SessionEventRecord(
            index=1,
            event_type="user",
            role="user",
            timestamp_ms=1700000000100,
            text="<command-name>/effort</command-name>",
            command=CommandEnvelope(name="effort", normalized_text="/effort", raw_text="<command-name>/effort</command-name>"),
            raw={"type": "user"},
        ),
        SessionEventRecord(
            index=2,
            event_type="user",
            role="user",
            timestamp_ms=1700000000200,
            text="<local-command-stdout>Set effort level</local-command-stdout>",
            raw={"type": "user"},
        ),
    ]

    adapted = adapt_parsed_workspace(parsed_workspace)

    assert adapted.session_count == 0
    assert adapted.turns == []
