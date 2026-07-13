from __future__ import annotations

import json
from pathlib import Path

from src.extract.codex import CodexLowLevelParser
from src.pipeline.extraction.adapter import adapt_parsed_workspace
from src.pipeline.extraction.turn_enrichment import enrich_turns
from src.shared.workspace_discovery import _scan_agent_workspaces


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_codex_low_level_parser_scans_and_parses_rollout_files(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    sessions_root = codex_home / "sessions" / "2026" / "06" / "28"
    session_id = "67e55044-10b1-426f-9247-bb680e5fe0c8"
    rollout_path = sessions_root / f"rollout-2026-06-28T10-00-00-{session_id}.jsonl"

    _write_jsonl(
        rollout_path,
        [
            {
                "timestamp": "2026-06-28T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "session_id": session_id,
                    "id": session_id,
                    "timestamp": "2026-06-28T10:00:00Z",
                    "cwd": "c:/repo/codex-demo",
                    "originator": "codex",
                    "cli_version": "0.1.0",
                    "source": "cli",
                    "model_provider": "openai",
                    "base_instructions": None,
                },
                "git": {
                    "branch": "main",
                    "repository_url": "https://github.com/example/codex-demo.git",
                    "commit_hash": "abc123",
                },
            },
            {
                "timestamp": "2026-06-28T10:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Implement a codex parser",
                    "kind": "plain",
                },
            },
            {
                "timestamp": "2026-06-28T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "I will inspect the rollout format.",
                },
            },
            {
                "timestamp": "2026-06-28T10:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "exec_command_begin",
                    "call_id": "call-bash",
                    "turn_id": "turn-1",
                    "command": ["bash", "-lc", "ls"],
                    "cwd": "c:/repo/codex-demo",
                    "parsed_cmd": [],
                    "source": "agent",
                },
            },
            {
                "timestamp": "2026-06-28T10:00:04Z",
                "type": "event_msg",
                "payload": {
                    "type": "exec_command_end",
                    "call_id": "call-bash",
                    "turn_id": "turn-1",
                    "command": ["bash", "-lc", "ls"],
                    "cwd": "c:/repo/codex-demo",
                    "parsed_cmd": [],
                    "source": "agent",
                    "stdout": "file1.py\n",
                    "stderr": "",
                    "aggregated_output": "file1.py\n",
                    "exit_code": 0,
                    "duration": "PT1S",
                    "formatted_output": "file1.py\n",
                    "status": "success",
                },
            },
            {
                "timestamp": "2026-06-28T10:00:05Z",
                "type": "event_msg",
                "payload": {
                    "type": "collab_agent_spawn_end",
                    "call_id": "call-agent",
                    "sender_thread_id": session_id,
                    "new_thread_id": "agent-thread-1",
                    "prompt": "Review the parser",
                    "model": "gpt-5-codex",
                    "reasoning_effort": "medium",
                    "status": "completed",
                },
            },
            {
                "timestamp": "2026-06-28T10:00:06Z",
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "call-patch",
                    "stdout": "Success. Updated the following files:\nM c:/repo/codex-demo/main.py\n",
                    "stderr": "",
                    "success": True,
                    "changes": {
                        "c:/repo/codex-demo/main.py": {
                            "type": "update",
                            "unified_diff": "@@ -1,2 +1,3 @@\n line1\n-line2\n+line2_changed\n+line3\n",
                            "move_path": None,
                        }
                    },
                    "status": "completed",
                },
            },
        ],
    )

    with open(codex_home / "session_index.jsonl", "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": session_id, "thread_name": "Parser Thread", "updated_at": "2026-06-28T10:00:05Z"}) + "\n")

    parser = CodexLowLevelParser(codex_home=codex_home)
    descriptors = parser.scan_workspaces()

    assert len(descriptors) == 1
    descriptor = descriptors[0]
    assert descriptor.workspace_folder == "c:/repo/codex-demo"
    assert descriptor.workspace_name == "codex-demo"
    assert descriptor.metadata["session_count"] == 1

    parsed = parser.parse_workspace(descriptor.workspace_id)

    assert len(parsed.sessions) == 1
    session = parsed.sessions[0]
    assert session.session_id == session_id
    assert session.title == "Parser Thread"
    assert session.metadata["model_provider"] == "openai"
    assert session.metadata["git"]["branch"] == "main"
    assistant_events = [event for event in session.events if event.role == "assistant"]
    assert len(assistant_events) >= 2
    assert any(tool.name == "Bash" for event in assistant_events for tool in event.tool_calls)
    assert any(tool.name == "ApplyPatch" for event in assistant_events for tool in event.tool_calls)
    assert any(result.status == "success" for event in session.events for result in event.tool_results)
    assert any(link.target_session_id == "agent-thread-1" for link in session.links)

    adapted = adapt_parsed_workspace(parsed)
    enriched = enrich_turns(adapted.turns, calculate_metrics=False)
    patch_turn = next(turn for turn in enriched if any(edit.file_path == "c:/repo/codex-demo/main.py" for edit in turn.code_edits))
    patch_edit = next(edit for edit in patch_turn.code_edits if edit.file_path == "c:/repo/codex-demo/main.py")
    assert patch_edit.extra["delta_metrics"]["lines_added"] == 2
    assert patch_edit.extra["delta_metrics"]["lines_removed"] == 1
    assert patch_edit.diff


def test_codex_rollout_files_without_meaningful_events_are_filtered_from_workspace_discovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / ".codex"
    sessions_root = codex_home / "sessions" / "2025" / "09" / "15"
    rollout_path = sessions_root / "rollout-2025-09-15T17-14-05-empty.jsonl"

    _write_jsonl(
        rollout_path,
        [
            {
                "timestamp": "2025-09-15T17:14:05Z",
                "type": "session_meta",
                "payload": {
                    "session_id": "empty",
                    "cwd": "",
                    "source": "cli",
                },
            }
        ],
    )

    parser = CodexLowLevelParser(codex_home=codex_home)

    assert len(parser.scan_workspaces()) == 1
    assert parser.parse_workspace("rollout-2025-09-15T17-14-05-empty").sessions == []
    monkeypatch.setattr("src.shared.workspace_discovery.build_parser", lambda agent_name: parser)

    assert _scan_agent_workspaces("codex") == []
