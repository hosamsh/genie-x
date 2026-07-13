from __future__ import annotations

import asyncio

from src.shared.models.workspace import WorkspaceInfo
from src.web.routers.workspaces import get_browse_workspaces


def test_get_browse_workspaces_filters_by_agent_before_pagination(monkeypatch) -> None:
    workspaces = {
        "ws-alpha": WorkspaceInfo(
            workspace_id="ws-alpha",
            workspace_name="Alpha",
            workspace_folder="C:/alpha",
            agents=["copilot"],
        ),
        "ws-beta": WorkspaceInfo(
            workspace_id="ws-beta",
            workspace_name="Beta",
            workspace_folder="C:/beta",
            agents=["claude_code"],
        ),
        "ws-gamma": WorkspaceInfo(
            workspace_id="ws-gamma",
            workspace_name="Gamma",
            workspace_folder="C:/gamma",
            agents=["codex", "copilot"],
        ),
    }

    monkeypatch.setattr(
        "src.web.routers.workspaces.get_all_workspace_metadata",
        lambda: workspaces,
    )

    result = asyncio.run(
        get_browse_workspaces(page=1, page_size=1, include_live=True, agent="CoPiLoT")
    )

    assert result["total_count"] == 2
    assert result["total_pages"] == 2
    assert [workspace["workspace_id"] for workspace in result["workspaces"]] == ["ws-alpha"]