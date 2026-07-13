from __future__ import annotations

import asyncio

from src.shared.models.workspace import WorkspaceInfo
from src.web.routers.sessions import get_workspace_sessions


def test_workspace_sessions_hide_subagents_and_annotate_parent(monkeypatch) -> None:
    workspace_id = "workspace-1"
    metadata = WorkspaceInfo(workspace_id=workspace_id, agents=["claude_code"])

    sessions = [
        {
            "session_id": "parent-session",
            "session_name": "Parent Session",
            "turn_count": 10,
            "first_timestamp": "2026-06-25T19:40:00+00:00",
            "last_timestamp": "2026-06-25T19:45:00+00:00",
            "agents": ["claude_code"],
        },
        {
            "session_id": "agent-child-1",
            "session_name": "Child 1",
            "turn_count": 2,
            "first_timestamp": "2026-06-25T19:41:00+00:00",
            "last_timestamp": "2026-06-25T19:41:30+00:00",
            "agents": ["claude_code"],
            "parent_session_id": "parent-session",
            "relationship_type": "subagent",
        },
        {
            "session_id": "agent-child-2",
            "session_name": "Child 2",
            "turn_count": 2,
            "first_timestamp": "2026-06-25T19:42:00+00:00",
            "last_timestamp": "2026-06-25T19:42:30+00:00",
            "agents": ["claude_code"],
            "parent_session_id": "parent-session",
            "relationship_type": "subagent",
        },
        {
            "session_id": "root-session-2",
            "session_name": "Another Root",
            "turn_count": 4,
            "first_timestamp": "2026-06-25T19:30:00+00:00",
            "last_timestamp": "2026-06-25T19:35:00+00:00",
            "agents": ["copilot"],
        },
    ]

    monkeypatch.setattr(
        "src.web.routers.sessions.get_all_workspace_metadata",
        lambda: {workspace_id: metadata},
    )
    monkeypatch.setattr(
        "src.web.routers.sessions.get_session_source_metadata",
        lambda _workspace_id, _session_ids: {},
    )
    monkeypatch.setattr(
        "src.web.routers.sessions.get_sessions_for_workspace_by_folder",
        lambda _workspace_id, _agent: sessions,
    )

    result = asyncio.run(get_workspace_sessions(workspace_id))

    session_ids = [session["session_id"] for session in result["sessions"]]
    assert session_ids == ["parent-session", "root-session-2"]

    parent_session = result["sessions"][0]
    assert parent_session["subagent_count"] == 2
    assert parent_session["has_subagents"] is True


def test_workspace_sessions_hide_bootstrap_only_clear_sessions(monkeypatch) -> None:
    workspace_id = "workspace-1"
    metadata = WorkspaceInfo(workspace_id=workspace_id, agents=["claude_code"])

    sessions = [
        {
            "session_id": "clear-only-a",
            "session_name": "<local-command-caveat>Caveat: local commands only.",
            "turn_count": 1,
            "first_timestamp": "2026-06-25T19:40:00+00:00",
            "last_timestamp": "2026-06-25T19:40:00+00:00",
            "agents": ["claude_code"],
        },
        {
            "session_id": "clear-only-b",
            "session_name": "/clear",
            "turn_count": 1,
            "first_timestamp": "2026-06-25T19:41:00+00:00",
            "last_timestamp": "2026-06-25T19:41:00+00:00",
            "agents": ["claude_code"],
        },
        {
            "session_id": "real-session",
            "session_name": "actual task title",
            "turn_count": 4,
            "first_timestamp": "2026-06-25T19:42:00+00:00",
            "last_timestamp": "2026-06-25T19:50:00+00:00",
            "agents": ["claude_code"],
        },
    ]

    monkeypatch.setattr(
        "src.web.routers.sessions.get_all_workspace_metadata",
        lambda: {workspace_id: metadata},
    )
    monkeypatch.setattr(
        "src.web.routers.sessions.get_session_source_metadata",
        lambda _workspace_id, _session_ids: {},
    )
    monkeypatch.setattr(
        "src.web.routers.sessions.get_sessions_for_workspace_by_folder",
        lambda _workspace_id, _agent: sessions,
    )

    result = asyncio.run(get_workspace_sessions(workspace_id))

    assert [session["session_id"] for session in result["sessions"]] == ["real-session"]


def test_workspace_sessions_annotate_sdk_cli_and_clear_self_parent(monkeypatch) -> None:
    workspace_id = "workspace-1"
    metadata = WorkspaceInfo(workspace_id=workspace_id, agents=["claude_code"])

    sessions = [
        {
            "session_id": "headless-session",
            "session_name": "build driver",
            "turn_count": 2,
            "first_timestamp": "2026-06-27T00:00:00+00:00",
            "last_timestamp": "2026-06-27T00:00:10+00:00",
            "agents": ["claude_code"],
            "parent_session_id": "headless-session",
        },
    ]

    monkeypatch.setattr(
        "src.web.routers.sessions.get_all_workspace_metadata",
        lambda: {workspace_id: metadata},
    )
    monkeypatch.setattr(
        "src.web.routers.sessions.get_session_source_metadata",
        lambda _workspace_id, _session_ids: {
            "headless-session": {
                "entrypoint": "sdk-cli",
                "session_origin_label": "sdk-cli",
                "is_headless_session": True,
            }
        },
    )
    monkeypatch.setattr(
        "src.web.routers.sessions.get_sessions_for_workspace_by_folder",
        lambda _workspace_id, _agent: sessions,
    )

    result = asyncio.run(get_workspace_sessions(workspace_id))

    assert result["sessions"][0]["parent_session_id"] is None
    assert result["sessions"][0]["session_origin_label"] == "sdk-cli"
    assert result["sessions"][0]["is_headless_session"] is True