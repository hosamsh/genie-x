from __future__ import annotations

import json
from pathlib import Path

from src.extract.claude_code import ClaudeCodeLowLevelParser


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_claude_low_level_parser_preserves_tools_tokens_and_subagents(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    projects_dir = claude_dir / "projects"
    project_path = "C:/repo/demo"
    workspace_id = ClaudeCodeLowLevelParser._encode_project_path(project_path)
    workspace_root = projects_dir / workspace_id

    history_file = claude_dir / "history.jsonl"
    _write_jsonl(history_file, [{"project": project_path}])

    _write_jsonl(
        workspace_root / "session-main.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-06-25T10:00:00Z",
                "message": {
                    "role": "user",
                    "content": "<command-name>task</command-name><command-args>--fast</command-args>",
                },
                "uuid": "u1",
            },
            {
                "type": "assistant",
                "timestamp": "2026-06-25T10:00:02Z",
                "requestId": "req-1",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 4,
                        "service_tier": "standard",
                    },
                    "content": [
                        {"type": "text", "text": "Running tools"},
                        {"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": "C:/repo/demo/main.py"}},
                        {"type": "tool_use", "id": "toolu_task", "name": "Task", "input": {"description": "delegate"}},
                    ],
                },
                "uuid": "a1",
            },
            {
                "type": "user",
                "timestamp": "2026-06-25T10:00:03Z",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_read", "content": "file contents", "is_error": False}
                    ],
                },
                "uuid": "u2",
            },
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "tool_use_id": "toolu_task",
                "task_id": "agent-123",
                "timestamp": "2026-06-25T10:00:04Z",
            },
        ],
    )

    _write_jsonl(
        workspace_root / "agent-123.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-06-25T10:01:00Z",
                "parentSessionId": "session-main",
                "message": {"role": "user", "content": "child prompt"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-06-25T10:01:05Z",
                "message": {"role": "assistant", "content": "child result"},
            },
        ],
    )

    parser = ClaudeCodeLowLevelParser(claude_dir=claude_dir)
    parser._iter_claude_roots = lambda: [claude_dir]  # type: ignore[method-assign]
    workspaces = parser.scan_workspaces()

    assert [workspace.workspace_id for workspace in workspaces] == [workspace_id]

    parsed = parser.parse_workspace(workspace_id)

    assert len(parsed.sessions) == 2

    main_session = next(session for session in parsed.sessions if session.session_id == "session-main")
    child_session = next(session for session in parsed.sessions if session.session_id == "agent-123")

    assistant_event = next(event for event in main_session.events if event.role == "assistant")
    user_tool_result_event = next(event for event in main_session.events if event.tool_results)

    assert assistant_event.command is None
    assert assistant_event.token_usage is not None
    assert assistant_event.token_usage.cache_read_input_tokens == 10
    assert {tool.name for tool in assistant_event.tool_calls} == {"Read", "Task"}
    assert any(tool.arguments.get("file_path") == "C:/repo/demo/main.py" for tool in assistant_event.tool_calls)
    assert any(tool.spawned_session_id == "agent-123" for tool in assistant_event.tool_calls)
    assert any(link.target_session_id == "agent-123" for link in main_session.links)
    assert user_tool_result_event.tool_results[0].status == "success"
    assert user_tool_result_event.tool_results[0].tool_call_id == "toolu_read"
    assert child_session.parent_session_id == "session-main"
    assert child_session.relationship_type == "subagent"


def test_claude_low_level_parser_covers_system_events_and_remaining_block_variants(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    projects_dir = claude_dir / "projects"
    project_path = "C:/repo/demo"
    workspace_id = ClaudeCodeLowLevelParser._encode_project_path(project_path)
    workspace_root = projects_dir / workspace_id

    _write_jsonl(claude_dir / "history.jsonl", [{"project": project_path}])
    _write_jsonl(
        workspace_root / "coverage.jsonl",
        [
            {
                "type": "summary",
                "summary": "Session summary",
                "timestamp": "2026-06-25T10:00:00Z",
                "sessionId": "coverage",
            },
            {
                "type": "progress",
                "timestamp": "2026-06-25T10:00:01Z",
                "data": {"type": "agent_progress", "message": "Working", "agentId": "agent-900"},
            },
            {
                "type": "queue-operation",
                "timestamp": "2026-06-25T10:00:02Z",
                "operation": "enqueue",
                "tool_use_id": "toolu_task",
                "sourceSessionId": "root-session",
            },
            {
                "type": "file-history-snapshot",
                "timestamp": "2026-06-25T10:00:03Z",
                "files": [{"path": "C:/repo/demo/a.py"}, {"path": "C:/repo/demo/b.py"}],
            },
            {
                "type": "assistant",
                "uuid": "a-coverage",
                "parentUuid": "u-coverage",
                "isMeta": False,
                "isCompactSummary": False,
                "sessionId": "coverage",
                "cliSessionId": "cli-coverage",
                "gitBranch": "main",
                "permissionMode": "acceptEdits",
                "cwd": "C:/repo/demo",
                "timestamp": "2026-06-25T10:00:04Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [
                        {"type": "redacted_thinking"},
                        {"type": "server_tool_use", "id": "srv1", "name": "Search", "input": {"query": "docs"}},
                        {"type": "web_search_tool_result", "content": [{"title": "Doc A", "url": "https://a.example"}, {"url": "https://b.example"}]},
                        {"type": "web_fetch_tool_result", "content": {"url": "https://example.com", "text": "page body"}},
                        {"type": "code_execution_tool_result", "content": {"stdout": "ok", "stderr": "warn", "exit_code": 0}},
                        {"type": "text_editor_code_execution_tool_result", "content": {"operation": "write", "path": "C:/repo/demo/out.py"}},
                        {"type": "tool_search_tool_result", "content": "search"},
                        {"type": "image", "title": "diagram"},
                        {"type": "document", "title": "Spec"},
                        {"type": "search_result", "title": "Issue 123"},
                        {"type": "mcp_tool_use", "id": "mcp1", "server_name": "fs", "tool_name": "read", "input": {"path": "C:/repo/demo/main.py"}},
                        {"type": "mcp_tool_result", "tool_use_id": "mcp1", "content": {"text": "done"}},
                    ],
                },
            },
        ],
    )

    parser = ClaudeCodeLowLevelParser(claude_dir=claude_dir)
    parser._iter_claude_roots = lambda: [claude_dir]  # type: ignore[method-assign]
    parsed = parser.parse_workspace(workspace_id)

    assert len(parsed.sessions) == 1
    session = parsed.sessions[0]

    summary_event = next(event for event in session.events if event.event_type == "summary")
    progress_event = next(event for event in session.events if event.event_type == "progress")
    queue_event = next(event for event in session.events if event.event_type == "queue-operation")
    snapshot_event = next(event for event in session.events if event.event_type == "file-history-snapshot")
    assistant_event = next(event for event in session.events if event.message_id == "a-coverage")

    assert summary_event.role == "system"
    assert summary_event.text == "Session summary"
    assert progress_event.role == "system"
    assert progress_event.text == "Working"
    assert progress_event.extra["progress"]["agentId"] == "agent-900"
    assert queue_event.role == "system"
    assert "enqueue" in queue_event.text
    assert queue_event.extra["queue_operation"]["source_session_id"] == "root-session"
    assert snapshot_event.role == "system"
    assert snapshot_event.file_paths == ["c:/repo/demo/a.py", "c:/repo/demo/b.py"]

    assert assistant_event.extra["uuid"] == "a-coverage"
    assert assistant_event.extra["parent_uuid"] == "u-coverage"
    assert assistant_event.extra["session_id"] == "coverage"
    assert assistant_event.extra["cli_session_id"] == "cli-coverage"
    assert assistant_event.extra["git_branch"] == "main"
    assert assistant_event.extra["permission_mode"] == "acceptEdits"
    assert assistant_event.extra["cwd"] == "C:/repo/demo"

    block_kinds = [block.kind for block in assistant_event.content_blocks]
    assert "redacted_thinking" in block_kinds
    assert "server_tool_use" in block_kinds
    assert "web_search_tool_result" in block_kinds
    assert "web_fetch_tool_result" in block_kinds
    assert "code_execution_tool_result" in block_kinds
    assert "text_editor_code_execution_tool_result" in block_kinds
    assert "tool_search_tool_result" in block_kinds
    assert "image" in block_kinds
    assert "document" in block_kinds
    assert "search_result" in block_kinds
    assert "mcp_tool_use" in block_kinds
    assert "mcp_tool_result" in block_kinds

    assert any(tool.name == "Search" for tool in assistant_event.tool_calls)
    assert any(tool.name == "fs.read" for tool in assistant_event.tool_calls)
    assert any(result.kind == "web_search_tool_result" and "Doc A" in result.text for result in assistant_event.tool_results)
    assert any(result.kind == "web_fetch_tool_result" and "https://example.com" in result.text for result in assistant_event.tool_results)
    assert any(result.kind == "code_execution_tool_result" and result.status == "success" for result in assistant_event.tool_results)
    assert any(result.kind == "mcp_tool_result" and result.text == "done" for result in assistant_event.tool_results)
    assert "c:/repo/demo/main.py" in assistant_event.file_paths


def test_claude_low_level_parser_scans_multiple_claude_roots(tmp_path: Path, monkeypatch) -> None:
    primary_root = tmp_path / ".claude-primary"
    secondary_root = tmp_path / ".claude-secondary"

    primary_project = "C:/repo/autocue"
    secondary_project = "/home/hosam/code/projects/autocue"

    primary_workspace_id = ClaudeCodeLowLevelParser._encode_project_path(primary_project)
    secondary_workspace_id = ClaudeCodeLowLevelParser._encode_project_path(secondary_project)

    _write_jsonl(primary_root / "history.jsonl", [{"project": primary_project}])

    _write_jsonl(
        primary_root / "projects" / primary_workspace_id / "older.jsonl",
        [{"type": "assistant", "timestamp": "2026-05-01T10:00:00Z", "message": {"role": "assistant", "content": "old"}}],
    )
    secondary_distro_root = tmp_path / "wsl.localhost" / "Ubuntu" / "home" / "hosam" / ".claude"
    _write_jsonl(
        secondary_distro_root / "projects" / secondary_workspace_id / "newer.jsonl",
        [{"type": "assistant", "timestamp": "2026-06-25T10:00:00Z", "message": {"role": "assistant", "content": "new"}}],
    )

    parser = ClaudeCodeLowLevelParser(claude_dir=primary_root)
    monkeypatch.setattr(parser, "_iter_claude_roots", lambda: [primary_root, secondary_distro_root])

    workspaces = parser.scan_workspaces()

    assert {workspace.workspace_id for workspace in workspaces} == {primary_workspace_id, secondary_workspace_id}
    secondary_descriptor = next(workspace for workspace in workspaces if workspace.workspace_id == secondary_workspace_id)
    assert secondary_descriptor.workspace_folder == "/home/hosam/code/projects/autocue"

    parsed = parser.parse_workspace(secondary_workspace_id)
    assert len(parsed.sessions) == 1
    assert parsed.sessions[0].session_id == "newer"
    assert parsed.sessions[0].source_path.endswith("newer.jsonl")


def test_claude_low_level_parser_skips_caveat_and_clear_in_title(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    projects_dir = claude_dir / "projects"
    project_path = "C:/repo/demo"
    workspace_id = ClaudeCodeLowLevelParser._encode_project_path(project_path)
    workspace_root = projects_dir / workspace_id

    _write_jsonl(claude_dir / "history.jsonl", [{"project": project_path}])
    _write_jsonl(
        workspace_root / "session-main.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-06-25T10:00:00Z",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": "<local-command-caveat>Caveat: local commands only.</local-command-caveat>",
                },
            },
            {
                "type": "user",
                "timestamp": "2026-06-25T10:00:01Z",
                "message": {
                    "role": "user",
                    "content": "<command-name>/clear</command-name><command-message>clear</command-message>",
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-06-25T10:00:02Z",
                "message": {"role": "assistant", "content": "Ready"},
            },
            {
                "type": "user",
                "timestamp": "2026-06-25T10:00:03Z",
                "message": {
                    "role": "user",
                    "content": "real question starts here and should become the title",
                },
            },
        ],
    )

    _write_jsonl(
        workspace_root / "session-clear-only.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-06-25T10:01:00Z",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": "<local-command-caveat>Caveat: local commands only.</local-command-caveat>",
                },
            },
            {
                "type": "user",
                "timestamp": "2026-06-25T10:01:01Z",
                "message": {
                    "role": "user",
                    "content": "<command-name>/clear</command-name><command-message>clear</command-message>",
                },
            },
        ],
    )

    parser = ClaudeCodeLowLevelParser(claude_dir=claude_dir)
    parser._iter_claude_roots = lambda: [claude_dir]  # type: ignore[method-assign]
    parsed = parser.parse_workspace(workspace_id)

    titles = {session.session_id: session.title for session in parsed.sessions}
    assert titles["session-main"] == "real question starts here and should become the title"
    assert titles["session-clear-only"] == "/clear"


def test_claude_workspace_scan_counts_root_sessions_only(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    project_path = "C:/repo/demo"
    workspace_id = ClaudeCodeLowLevelParser._encode_project_path(project_path)
    workspace_root = claude_dir / "projects" / workspace_id

    _write_jsonl(claude_dir / "history.jsonl", [{"project": project_path}])
    _write_jsonl(workspace_root / "root-session.jsonl", [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    _write_jsonl(workspace_root / "root-session" / "subagents" / "agent-1.jsonl", [{"type": "user", "message": {"role": "user", "content": "child"}}])

    parser = ClaudeCodeLowLevelParser(claude_dir=claude_dir)
    parser._iter_claude_roots = lambda: [claude_dir]  # type: ignore[method-assign]

    descriptors = parser.scan_workspaces()

    assert len(descriptors) == 1
    assert descriptors[0].metadata["session_count"] == 1


def test_claude_low_level_parser_ignores_self_parent_and_marks_sdk_cli(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    project_path = "C:/repo/demo"
    workspace_id = ClaudeCodeLowLevelParser._encode_project_path(project_path)
    workspace_root = claude_dir / "projects" / workspace_id

    _write_jsonl(claude_dir / "history.jsonl", [{"project": project_path}])
    _write_jsonl(
        workspace_root / "headless.jsonl",
        [
            {
                "type": "queue-operation",
                "operation": "dequeue",
                "sessionId": "headless",
                "timestamp": "2026-06-27T00:00:00Z",
            },
            {
                "type": "user",
                "timestamp": "2026-06-27T00:00:01Z",
                "entrypoint": "sdk-cli",
                "userType": "external",
                "isSidechain": False,
                "cwd": "C:/repo/demo",
                "version": "2.1.191",
                "message": {
                    "role": "user",
                    "content": "headless build driver",
                },
            },
        ],
    )

    parser = ClaudeCodeLowLevelParser(claude_dir=claude_dir)
    parser._iter_claude_roots = lambda: [claude_dir]  # type: ignore[method-assign]
    parsed = parser.parse_workspace(workspace_id)

    session = parsed.sessions[0]
    assert session.parent_session_id == ""
    assert session.metadata["entrypoint"] == "sdk-cli"
    assert session.metadata["session_origin_label"] == "sdk-cli"
    assert session.metadata["is_headless_session"] is True


def test_claude_low_level_parser_assigns_unique_ids_to_nested_workflow_journals(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    project_path = "/home/hosam/code/projects/lmpool"
    workspace_id = ClaudeCodeLowLevelParser._encode_project_path(project_path)
    workspace_root = claude_dir / "projects" / workspace_id

    _write_jsonl(claude_dir / "history.jsonl", [{"project": project_path}])
    _write_jsonl(
        workspace_root / "root-session.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-06-28T10:00:00Z",
                "message": {"role": "user", "content": "root prompt"},
            },
        ],
    )
    _write_jsonl(
        workspace_root / "root-session" / "subagents" / "workflows" / "wf_one" / "journal.jsonl",
        [{"type": "started"}],
    )
    _write_jsonl(
        workspace_root / "root-session" / "subagents" / "workflows" / "wf_two" / "journal.jsonl",
        [{"type": "started"}],
    )

    parser = ClaudeCodeLowLevelParser(claude_dir=claude_dir)
    parser._iter_claude_roots = lambda: [claude_dir]  # type: ignore[method-assign]
    parsed = parser.parse_workspace(workspace_id)

    workflow_sessions = [session for session in parsed.sessions if session.source_path.endswith("journal.jsonl")]
    session_ids = {session.session_id for session in workflow_sessions}

    assert len(workflow_sessions) == 2
    assert len(session_ids) == 2
    assert all(session.parent_session_id == "root-session" for session in workflow_sessions)


def test_claude_low_level_parser_prefers_nested_agent_file_stem_over_parent_session_id(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    project_path = "/home/hosam/code/projects/lmpool"
    workspace_id = ClaudeCodeLowLevelParser._encode_project_path(project_path)
    workspace_root = claude_dir / "projects" / workspace_id

    _write_jsonl(claude_dir / "history.jsonl", [{"project": project_path}])
    _write_jsonl(
        workspace_root / "root-session.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-06-28T10:00:00Z",
                "sessionId": "root-session",
                "message": {"role": "user", "content": "root prompt"},
            },
        ],
    )
    _write_jsonl(
        workspace_root / "root-session" / "subagents" / "workflows" / "wf_one" / "agent-a123.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": "2026-06-28T10:00:01Z",
                "sessionId": "root-session",
                "message": {"role": "assistant", "content": "child result"},
            },
        ],
    )

    parser = ClaudeCodeLowLevelParser(claude_dir=claude_dir)
    parser._iter_claude_roots = lambda: [claude_dir]  # type: ignore[method-assign]
    parsed = parser.parse_workspace(workspace_id)

    nested_agent = next(session for session in parsed.sessions if session.source_path.endswith("agent-a123.jsonl"))

    assert nested_agent.session_id == "agent-a123"
    assert nested_agent.parent_session_id == "root-session"


def test_claude_projects_dir_scan_prefers_cwd_over_lossy_workspace_id_decode(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    workspace_id = "c--code-tools-product-ideas"
    workspace_root = claude_dir / "projects" / workspace_id

    _write_jsonl(
        workspace_root / "session-main.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-06-28T10:00:00Z",
                "cwd": "c:\\code\\tools\\product-ideas",
                "message": {
                    "role": "user",
                    "content": "hello",
                },
            },
        ],
    )

    parser = ClaudeCodeLowLevelParser(claude_dir=claude_dir)
    parser._iter_claude_roots = lambda: [claude_dir]  # type: ignore[method-assign]

    descriptors = parser.scan_workspaces()

    assert len(descriptors) == 1
    assert descriptors[0].workspace_id == workspace_id
    assert descriptors[0].workspace_folder == "c:/code/tools/product-ideas"
    assert descriptors[0].workspace_name == "product-ideas"
