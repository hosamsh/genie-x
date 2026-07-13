from __future__ import annotations

import json
from pathlib import Path

from src.extract.copilot_cli import CopilotCliLowLevelParser


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_copilot_cli_low_level_parser_preserves_tool_events_and_aliases(tmp_path: Path) -> None:
    base_dir = tmp_path / "session-state"
    session_dir = base_dir / "sess-1"
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {
                "type": "session.start",
                "sessionId": "sess-1",
                "workspaceFolder": "C:/repo/demo",
                "workspaceName": "demo",
                "model": "gpt-4o",
                "timestamp": 1700000000000,
            },
            {
                "type": "userPromptSubmitted",
                "prompt": "Deploy now",
                "timestamp": 1700000001000,
            },
            {
                "type": "assistantResponse",
                "output": "Running deployment",
                "timestamp": 1700000002000,
                "model": "gpt-4.1",
            },
            {
                "type": "tool.execution_complete",
                "toolId": "bash-1",
                "toolName": "bash",
                "input": {"command": "deploy.sh"},
                "output": "ok",
                "exitCode": 0,
                "timestamp": 1700000002500,
            },
        ],
    )

    parser = CopilotCliLowLevelParser(base_dir=base_dir)
    workspaces = parser.scan_workspaces()
    assert len(workspaces) == 1

    parsed = parser.parse_workspace(workspaces[0].workspace_id)
    assert len(parsed.sessions) == 1

    session = parsed.sessions[0]
    assert len(session.events) == 4
    assert session.events[1].role == "user"
    assert session.events[1].text == "Deploy now"
    assert session.events[2].role == "assistant"
    assert session.events[2].text == "Running deployment"

    tool_event = session.events[3]
    assert tool_event.tool_calls[0].name == "bash"
    assert tool_event.tool_calls[0].arguments["command"] == "deploy.sh"
    assert tool_event.tool_results[0].status == "success"
    assert tool_event.tool_results[0].text == "ok"


def test_copilot_cli_low_level_parser_parses_legacy_json_events_array(tmp_path: Path) -> None:
    session_state_dir = tmp_path / "session-state"
    history_dir = tmp_path / "history-session-state"
    session_state_dir.mkdir(parents=True)
    history_dir.mkdir(parents=True)

    (history_dir / "legacy-1.json").write_text(
        json.dumps(
            {
                "sessionId": "legacy-1",
                "cwd": "C:/repo/demo",
                "events": [
                    {"type": "user.message", "content": "Question", "timestamp": 1700000001000},
                    {"type": "assistant.message", "content": "Answer", "timestamp": 1700000002000},
                ],
            }
        ),
        encoding="utf-8",
    )

    parser = CopilotCliLowLevelParser(base_dir=session_state_dir)
    workspaces = parser.scan_workspaces()
    assert len(workspaces) == 1

    parsed = parser.parse_workspace(workspaces[0].workspace_id)
    assert len(parsed.sessions) == 1
    session = parsed.sessions[0]
    assert session.session_id == "legacy-1"
    assert [event.role for event in session.events] == ["user", "assistant"]
    assert session.events[0].text == "Question"
    assert session.events[1].text == "Answer"
    assert session.metadata["storage_shape"] == "legacy-json"


def test_copilot_cli_low_level_parser_parses_legacy_json_history_array(tmp_path: Path) -> None:
    session_state_dir = tmp_path / "session-state"
    history_dir = tmp_path / "history-session-state"
    session_state_dir.mkdir(parents=True)
    history_dir.mkdir(parents=True)

    (history_dir / "legacy-2.json").write_text(
        json.dumps(
            {
                "id": "legacy-2",
                "workspacePath": "C:/repo/demo",
                "history": [
                    {"type": "prompt", "prompt": "Deploy this", "timestamp": 1700000100000},
                    {"type": "completion", "result": "Done", "timestamp": 1700000101000},
                ],
            }
        ),
        encoding="utf-8",
    )

    parser = CopilotCliLowLevelParser(base_dir=session_state_dir)
    workspaces = parser.scan_workspaces()
    assert len(workspaces) == 1

    parsed = parser.parse_workspace(workspaces[0].workspace_id)
    assert len(parsed.sessions) == 1
    session = parsed.sessions[0]
    assert session.session_id == "legacy-2"
    assert [event.role for event in session.events] == ["user", "assistant"]
    assert session.events[0].text == "Deploy this"
    assert session.events[1].text == "Done"
