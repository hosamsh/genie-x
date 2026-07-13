from __future__ import annotations

from src.extract import (
    CommandEnvelope,
    ContentBlockRecord,
    ParsedSession,
    SessionEventRecord,
    TokenUsage,
    ToolCallRecord,
    WorkspaceDescriptor,
)


def test_source_of_truth_models_round_trip() -> None:
    descriptor = WorkspaceDescriptor(
        workspace_id="ws-1",
        agent_name="claude_code",
        workspace_name="demo",
        source_root="/tmp/demo",
    )
    event = SessionEventRecord(
        index=0,
        event_type="assistant",
        role="assistant",
        text="done",
        token_usage=TokenUsage(input_tokens=10, output_tokens=20),
        command=CommandEnvelope(name="review", normalized_text="/review"),
        content_blocks=[ContentBlockRecord(index=0, kind="text", text="done")],
        tool_calls=[ToolCallRecord(call_id="tool-1", name="Read", arguments={"path": "/tmp/x"})],
    )
    session = ParsedSession(
        session_id="sess-1",
        agent_name="claude_code",
        workspace_id="ws-1",
        events=[event],
        metadata={"source": "test"},
    )

    payload = session.to_dict()

    assert payload["session_id"] == "sess-1"
    assert payload["metadata"]["source"] == "test"
    assert payload["events"][0]["token_usage"]["input_tokens"] == 10
    assert payload["events"][0]["tool_calls"][0]["name"] == "Read"
    assert descriptor.workspace_name == "demo"
