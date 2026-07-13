from __future__ import annotations

import json
from pathlib import Path

from src.extract.copilot import CopilotLowLevelParser
from src.shared.workspace_discovery import _scan_agent_workspaces


def test_copilot_low_level_parser_preserves_response_items(tmp_path: Path) -> None:
    workspace_storage = tmp_path / "workspaceStorage"
    global_storage = tmp_path / "globalStorage"
    workspace_folder = workspace_storage / "ws-001"
    chat_dir = workspace_folder / "chatSessions"
    chat_dir.mkdir(parents=True)

    (workspace_folder / "workspace.json").write_text(
        json.dumps({"folder": "file:///C:/repo/demo"}),
        encoding="utf-8",
    )

    session_payload = {
        "customTitle": "Demo Session",
        "requests": [],
    }
    session_payload["requests"] = [
        {
            "requestId": "req-1",
            "timestamp": 1700000000000,
            "message": {"text": "Open file"},
            "modelId": "gpt-4o",
            "response": [
                {"kind": "markdownContent", "value": "Here is the file"},
                {
                    "kind": "toolInvocation",
                    "toolId": "read-1",
                    "toolName": "ReadFile",
                    "invocationMessage": {"uris": [{"path": "C:/repo/demo/main.py"}]},
                    "value": "tool invoked",
                },
            ],
            "result": {"timings": {"totalElapsed": 321}},
            "editedFileEvents": [{"uri": {"path": "C:/repo/demo/main.py"}}],
        }
    ]
    (chat_dir / "session-a.json").write_text(json.dumps(session_payload), encoding="utf-8")

    parser = CopilotLowLevelParser(workspace_storage=workspace_storage, global_storage=global_storage)
    workspaces = parser.scan_workspaces()
    assert [workspace.workspace_id for workspace in workspaces] == ["ws-001"]

    parsed = parser.parse_workspace("ws-001")
    assert len(parsed.sessions) == 1

    session = parsed.sessions[0]
    assert session.title == "Demo Session"
    assert len(session.events) == 2

    user_event = session.events[0]
    assistant_event = session.events[1]

    assert user_event.role == "user"
    assert assistant_event.role == "assistant"
    assert assistant_event.extra["response_time_ms"] == 321
    assert assistant_event.tool_calls[0].name == "ReadFile"
    assert assistant_event.tool_results[0].tool_call_id == "read-1"
    assert assistant_event.tool_results[0].text == "tool invoked"
    assert assistant_event.tool_calls[0].file_paths == ["c:/repo/demo/main.py"]
    assert any(block.kind == "toolInvocation" for block in assistant_event.content_blocks)
    assert "c:/repo/demo/main.py" in assistant_event.file_paths


def test_copilot_empty_session_files_are_filtered_from_workspace_discovery(tmp_path: Path, monkeypatch) -> None:
    workspace_storage = tmp_path / "workspaceStorage"
    global_storage = tmp_path / "globalStorage"
    workspace_folder = workspace_storage / "ws-empty"
    chat_dir = workspace_folder / "chatSessions"
    chat_dir.mkdir(parents=True)

    (workspace_folder / "workspace.json").write_text(
        json.dumps({"folder": "file:///C:/repo/empty"}),
        encoding="utf-8",
    )
    (chat_dir / "empty.json").write_text(json.dumps({"requests": []}), encoding="utf-8")

    parser = CopilotLowLevelParser(workspace_storage=workspace_storage, global_storage=global_storage)

    assert len(parser.scan_workspaces()) == 1
    assert parser.parse_workspace("ws-empty").sessions == []
    monkeypatch.setattr("src.shared.workspace_discovery.build_parser", lambda agent_name: parser)

    assert _scan_agent_workspaces("copilot") == []


def test_copilot_low_level_parser_extracts_serialized_tool_results_and_unique_call_ids(tmp_path: Path) -> None:
    workspace_storage = tmp_path / "workspaceStorage"
    global_storage = tmp_path / "globalStorage"
    workspace_folder = workspace_storage / "ws-serialized"
    chat_dir = workspace_folder / "chatSessions"
    chat_dir.mkdir(parents=True)

    (workspace_folder / "workspace.json").write_text(
        json.dumps({"folder": "file:///C:/repo/demo"}),
        encoding="utf-8",
    )

    session_payload = {
        "customTitle": "Serialized Session",
        "requests": [
            {
                "requestId": "req-serialized",
                "timestamp": 1700000000000,
                "message": {"text": "Read config"},
                "response": [
                    {
                        "kind": "toolInvocationSerialized",
                        "toolId": "copilot_readFile",
                        "toolCallId": "call-123",
                        "toolName": "copilot_readFile",
                        "invocationMessage": {
                            "value": "Reading [](file:///C:/repo/demo/config.yaml)",
                            "uris": {
                                "file:///C:/repo/demo/config.yaml": {"path": "C:/repo/demo/config.yaml"}
                            },
                        },
                        "pastTenseMessage": {"value": "Read [](file:///C:/repo/demo/config.yaml)"},
                        "toolSpecificData": {"kind": "fileRead", "bytes": 42},
                        "isComplete": True,
                        "generatedTitle": "Read config",
                    }
                ],
            }
        ],
    }
    (chat_dir / "session-serialized.json").write_text(json.dumps(session_payload), encoding="utf-8")

    parser = CopilotLowLevelParser(workspace_storage=workspace_storage, global_storage=global_storage)
    parsed = parser.parse_workspace("ws-serialized")

    assistant_event = parsed.sessions[0].events[1]

    assert assistant_event.tool_calls[0].call_id == "call-123"
    assert assistant_event.tool_calls[0].file_paths == ["c:/repo/demo/config.yaml"]
    assert assistant_event.tool_results[0].tool_call_id == "call-123"
    assert assistant_event.tool_results[0].text == "Read [](file:///C:/repo/demo/config.yaml)"
    assert assistant_event.tool_results[0].structured_content["toolSpecificData"]["bytes"] == 42
    assert assistant_event.tool_results[0].status == "success"


def test_copilot_low_level_parser_handles_delta_jsonl(tmp_path: Path) -> None:
    workspace_storage = tmp_path / "workspaceStorage"
    global_storage = tmp_path / "globalStorage"
    workspace_folder = workspace_storage / "ws-delta"
    chat_dir = workspace_folder / "chatSessions"
    chat_dir.mkdir(parents=True)

    delta_lines = [
        {"kind": 0, "v": {"requests": []}},
        {
            "kind": 2,
            "k": ["requests"],
            "v": [{
                "requestId": "r1",
                "timestamp": 1700000000000,
                "message": {"text": "Hi"},
                "response": [{"kind": "markdownContent", "value": "Hello"}],
            }],
        },
    ]
    with open(chat_dir / "delta.jsonl", "w", encoding="utf-8") as handle:
        for line in delta_lines:
            handle.write(json.dumps(line) + "\n")

    parser = CopilotLowLevelParser(workspace_storage=workspace_storage, global_storage=global_storage)
    parsed = parser.parse_workspace("ws-delta")

    assert len(parsed.sessions) == 1
    assert len(parsed.sessions[0].events) == 2
    assert parsed.sessions[0].events[0].text == "Hi"
    assert parsed.sessions[0].events[1].text == "Hello"


def test_copilot_low_level_parser_preserves_string_request_messages_and_edit_sessions(tmp_path: Path) -> None:
    workspace_storage = tmp_path / "workspaceStorage"
    global_storage = tmp_path / "globalStorage"
    workspace_folder = workspace_storage / "ws-edits"
    chat_dir = workspace_folder / "chatSessions"
    chat_dir.mkdir(parents=True)

    (workspace_folder / "workspace.json").write_text(
        json.dumps({"folder": "file:///C:/repo/demo"}),
        encoding="utf-8",
    )

    session_payload = {
        "requests": [
            {
                "requestId": "req-1",
                "timestamp": 1700000000000,
                "message": "Create hello.py",
                "modelId": "gpt-4o",
                "response": [{"kind": "markdownContent", "value": "Done"}],
            },
            {
                "requestId": "req-2",
                "timestamp": 1700000005000,
                "message": "Add a test",
                "modelId": "gpt-4o",
                "response": [{"kind": "markdownContent", "value": "Updated"}],
            },
        ]
    }
    (chat_dir / "session-edits.json").write_text(json.dumps(session_payload), encoding="utf-8")

    edits_dir = workspace_folder / "chatEditingSessions" / "session-edits"
    contents_dir = edits_dir / "contents"
    contents_dir.mkdir(parents=True)

    empty_hash = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    first_hash = "abc123hash1"
    second_hash = "def456hash2"
    (contents_dir / empty_hash).write_text("", encoding="utf-8")
    (contents_dir / first_hash).write_text("def hello():\n    return 'Hello World'\n", encoding="utf-8")
    (contents_dir / second_hash).write_text(
        "def hello():\n    return 'Hello World'\n\ndef test_hello():\n    assert hello() == 'Hello World'\n",
        encoding="utf-8",
    )

    file_uri = "file:///C:/repo/demo/hello.py"
    state_payload = {
        "initialFileContents": [[file_uri, empty_hash]],
        "timeline": {
            "fileBaselines": [
                [f"{file_uri}::req-1", {"requestId": "req-1", "epoch": 1, "content": empty_hash}],
                [f"{file_uri}::req-2", {"requestId": "req-2", "epoch": 2, "content": first_hash}],
            ]
        },
        "recentSnapshot": {
            "entries": [{"resource": file_uri, "currentHash": second_hash}],
        },
    }
    (edits_dir / "state.json").write_text(json.dumps(state_payload), encoding="utf-8")

    parser = CopilotLowLevelParser(workspace_storage=workspace_storage, global_storage=global_storage)
    parsed = parser.parse_workspace("ws-edits")

    assert len(parsed.sessions) == 1
    events = parsed.sessions[0].events
    assert events[0].text == "Create hello.py"
    assert [block.kind for block in events[0].content_blocks] == ["text"]

    first_assistant_event = events[1]
    first_edit_block = next(block for block in first_assistant_event.content_blocks if block.kind == "textEditGroup")
    assert "def hello():" in first_edit_block.text
    assert "c:/repo/demo/hello.py" in first_assistant_event.file_paths

    second_assistant_event = events[3]
    second_edit_block = next(block for block in second_assistant_event.content_blocks if block.kind == "textEditGroup")
    assert "def test_hello():" in second_edit_block.text
    assert "c:/repo/demo/hello.py" in second_assistant_event.file_paths


def test_copilot_low_level_parser_types_known_response_items_and_request_metadata(tmp_path: Path) -> None:
    workspace_storage = tmp_path / "workspaceStorage"
    global_storage = tmp_path / "globalStorage"
    workspace_folder = workspace_storage / "ws-rich"
    chat_dir = workspace_folder / "chatSessions"
    chat_dir.mkdir(parents=True)

    (workspace_folder / "workspace.json").write_text(
        json.dumps({"folder": "file:///C:/repo/demo"}),
        encoding="utf-8",
    )

    session_payload = {
        "customTitle": "Rich Session",
        "requests": [
            {
                "requestId": "req-rich",
                "timestamp": 1700000000000,
                "message": {
                    "text": "Refactor this",
                    "parts": [{"text": "Part A"}, {"text": "Part B", "kind": "piece"}],
                },
                "variableData": {
                    "variables": [
                        {
                            "kind": "file",
                            "name": "main.py",
                            "value": {"path": "C:/repo/demo/main.py"},
                        }
                    ]
                },
                "response": [
                    {"kind": "markdownContent", "value": "Before code:"},
                    {"kind": "thinking", "value": "Need to inspect the file"},
                    {
                        "kind": "inlineReference",
                        "inlineReference": {"name": "normalize_shape(shape)"},
                    },
                    {
                        "kind": "codeblockUri",
                        "codeblockUri": {"path": "C:/repo/demo/main.py"},
                    },
                    {
                        "kind": "textEditGroup",
                        "uri": {"path": "C:/repo/demo/main.py"},
                        "edits": [[{"text": "def hello():\n    return 1"}]],
                    },
                ],
            }
        ],
    }
    (chat_dir / "session-rich.json").write_text(json.dumps(session_payload), encoding="utf-8")

    parser = CopilotLowLevelParser(workspace_storage=workspace_storage, global_storage=global_storage)
    parsed = parser.parse_workspace("ws-rich")

    assert len(parsed.sessions) == 1
    session = parsed.sessions[0]
    assert len(session.events) == 2

    user_event = session.events[0]
    assistant_event = session.events[1]

    assert [block.kind for block in user_event.content_blocks] == ["text", "requestPart", "requestPart", "variableRef"]
    assert user_event.extra["variable_data"]["variables"][0]["name"] == "main.py"
    assert user_event.attachments[0].path == "c:/repo/demo/main.py"

    block_kinds = [block.kind for block in assistant_event.content_blocks]
    assert block_kinds == ["markdownContent", "thinking", "inlineReference", "codeblockUri", "textEditGroup"]
    assert assistant_event.thinking_text == "Need to inspect the file"
    inline_block = next(block for block in assistant_event.content_blocks if block.kind == "inlineReference")
    assert inline_block.extra["inline_reference"]["name"] == "normalize_shape(shape)"
    edit_block = next(block for block in assistant_event.content_blocks if block.kind == "textEditGroup")
    assert "def hello():" in edit_block.text
    assert "c:/repo/demo/main.py" in assistant_event.file_paths
    assert "Before code:" in assistant_event.text
    assert "`normalize_shape(shape)`" in assistant_event.text
