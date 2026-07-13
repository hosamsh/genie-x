from __future__ import annotations

import json
import platform
from pathlib import Path

from src.extract.codex import CodexLowLevelParser
from src.extract.copilot_cli import CopilotCliLowLevelParser


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_codex_parser_scans_multiple_storage_roots(tmp_path: Path) -> None:
    root_a = tmp_path / "codex-a"
    root_b = tmp_path / "codex-b"
    session_id_a = "11111111-1111-1111-1111-111111111111"
    session_id_b = "22222222-2222-2222-2222-222222222222"

    _write_jsonl(
        root_a / "sessions" / "2026" / "06" / "28" / f"rollout-2026-06-28T10-00-00-{session_id_a}.jsonl",
        [
            {"timestamp": "2026-06-28T10:00:00Z", "type": "session_meta", "payload": {"session_id": session_id_a, "id": session_id_a, "timestamp": "2026-06-28T10:00:00Z", "cwd": "c:/repo/a", "originator": "codex", "cli_version": "0.1.0", "source": "cli", "base_instructions": None}},
            {"timestamp": "2026-06-28T10:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello a", "kind": "plain"}},
        ],
    )
    _write_jsonl(
        root_b / "sessions" / "2026" / "06" / "28" / f"rollout-2026-06-28T10-00-00-{session_id_b}.jsonl",
        [
            {"timestamp": "2026-06-28T10:00:00Z", "type": "session_meta", "payload": {"session_id": session_id_b, "id": session_id_b, "timestamp": "2026-06-28T10:00:00Z", "cwd": "c:/repo/b", "originator": "codex", "cli_version": "0.1.0", "source": "cli", "base_instructions": None}},
            {"timestamp": "2026-06-28T10:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello b", "kind": "plain"}},
        ],
    )

    parser = CodexLowLevelParser(codex_homes=[root_a, root_b])
    descriptors = parser.scan_workspaces()

    assert {descriptor.workspace_folder for descriptor in descriptors} == {"c:/repo/a", "c:/repo/b"}


def test_codex_parser_auto_adds_wsl_storage_roots_on_windows(tmp_path: Path, monkeypatch) -> None:
    root_a = tmp_path / "codex-a"
    wsl_root = tmp_path / "wsl-codex"
    session_id_a = "33333333-3333-3333-3333-333333333333"
    session_id_b = "44444444-4444-4444-4444-444444444444"

    _write_jsonl(
        root_a / "sessions" / "2026" / "07" / "05" / f"rollout-2026-07-05T10-00-00-{session_id_a}.jsonl",
        [
            {"timestamp": "2026-07-05T10:00:00Z", "type": "session_meta", "payload": {"session_id": session_id_a, "id": session_id_a, "timestamp": "2026-07-05T10:00:00Z", "cwd": "c:/repo/a", "originator": "codex", "cli_version": "0.1.0", "source": "cli", "base_instructions": None}},
            {"timestamp": "2026-07-05T10:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello a", "kind": "plain"}},
        ],
    )
    _write_jsonl(
        wsl_root / "sessions" / "2026" / "07" / "05" / f"rollout-2026-07-05T10-00-00-{session_id_b}.jsonl",
        [
            {"timestamp": "2026-07-05T10:00:00Z", "type": "session_meta", "payload": {"session_id": session_id_b, "id": session_id_b, "timestamp": "2026-07-05T10:00:00Z", "cwd": "/home/hosam/code/projects/lmpool", "originator": "codex", "cli_version": "0.1.0", "source": "cli", "base_instructions": None}},
            {"timestamp": "2026-07-05T10:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello b", "kind": "plain"}},
        ],
    )

    monkeypatch.setattr(platform, "system", lambda: "Windows")

    parser = CodexLowLevelParser()
    parser._configured_roots = [root_a]
    parser._codex_home = root_a
    parser._sessions_root = root_a / "sessions"
    parser._session_index_path = root_a / "session_index.jsonl"
    parser._auto_discover_wsl = True
    parser._iter_wsl_codex_roots = lambda: [wsl_root]  # type: ignore[method-assign]
    descriptors = parser.scan_workspaces()

    assert {descriptor.workspace_folder for descriptor in descriptors} == {"c:/repo/a", "/home/hosam/code/projects/lmpool"}


def test_copilot_cli_parser_scans_multiple_storage_roots(tmp_path: Path) -> None:
    root_a = tmp_path / "cli-a"
    root_b = tmp_path / "cli-b"
    _write_jsonl(
        root_a / "sess-a.jsonl",
        [{"workspaceFolder": "c:/repo/a", "workspaceName": "repo-a", "sessionId": "sess-a", "type": "event", "role": "user", "text": "hello"}],
    )
    _write_jsonl(
        root_b / "sess-b.jsonl",
        [{"workspaceFolder": "c:/repo/b", "workspaceName": "repo-b", "sessionId": "sess-b", "type": "event", "role": "user", "text": "hello"}],
    )

    parser = CopilotCliLowLevelParser(base_dirs=[root_a, root_b])
    descriptors = parser.scan_workspaces()

    assert {descriptor.workspace_folder for descriptor in descriptors} == {"c:/repo/a", "c:/repo/b"}