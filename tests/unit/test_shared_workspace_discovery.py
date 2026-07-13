from __future__ import annotations

from src.shared.models.workspace import WorkspaceInfo
from src.shared.workspace_discovery import (
    _merge_workspaces,
    _normalize_folder,
    clear_find_workspace_cache,
    clear_workspace_folders_cache,
    get_all_workspace_folders,
    list_all_workspaces,
)


def test_merge_workspaces_consolidates_same_folder_across_agents() -> None:
    merged = _merge_workspaces(
        {
            "copilot": [
                WorkspaceInfo(
                    workspace_id="ws-copilot",
                    workspace_name="demo",
                    workspace_folder="C:/Repo/Demo",
                    agents=["copilot"],
                    session_count=2,
                )
            ],
            "copilot_cli": [
                WorkspaceInfo(
                    workspace_id="ws-cli",
                    workspace_name="demo",
                    workspace_folder="c:/repo/demo",
                    agents=["copilot_cli"],
                    session_count=3,
                )
            ],
        }
    )

    assert len(merged) == 1
    assert merged[0].workspace_folder == "C:/Repo/Demo"
    assert merged[0].session_count == 5
    assert merged[0].agents == ["copilot", "copilot_cli"]


def test_normalize_folder_lowercases_and_uses_posix() -> None:
    assert _normalize_folder(r"C:\Code\Project") == "c:/code/project"


def test_merge_workspaces_does_not_merge_same_name_different_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.shared.workspace_discovery.resolve_workspace_path",
        lambda folder: (folder, False),
    )
    monkeypatch.setattr(
        "src.shared.workspace_discovery._translate_linux_path_from_wsl",
        lambda folder: "\\\\wsl.localhost\\Ubuntu\\home\\hosam\\code\\tools\\product-ideas"
        if folder == "/home/hosam/code/tools/product-ideas"
        else "",
    )

    merged = _merge_workspaces(
        {
            "copilot": [
                WorkspaceInfo(
                    workspace_id="ws-windows",
                    workspace_name="product-ideas",
                    workspace_folder="c:/code/tools/product-ideas",
                    agents=["copilot"],
                    session_count=3,
                )
            ],
            "claude_code": [
                WorkspaceInfo(
                    workspace_id="ws-wsl",
                    workspace_name="product-ideas",
                    workspace_folder="/home/hosam/code/tools/product-ideas",
                    agents=["claude_code"],
                    session_count=4,
                )
            ],
        }
    )

    assert len(merged) == 2


def test_merge_workspaces_merges_same_resolved_path_across_uri_forms(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.shared.workspace_discovery.resolve_workspace_path",
        lambda folder: (
            "\\\\wsl.localhost\\Ubuntu\\home\\hosam\\code\\projects\\pig",
            True,
        )
        if folder == "vscode-remote://wsl+ubuntu/home/hosam/code/projects/pig"
        else (folder, False),
    )
    monkeypatch.setattr(
        "src.shared.workspace_discovery._translate_linux_path_from_wsl",
        lambda folder: "\\\\wsl.localhost\\Ubuntu\\home\\hosam\\code\\projects\\pig"
        if folder == "/home/hosam/code/projects/pig"
        else "",
    )

    merged = _merge_workspaces(
        {
            "copilot": [
                WorkspaceInfo(
                    workspace_id="ws-remote",
                    workspace_name="pig",
                    workspace_folder="vscode-remote://wsl+ubuntu/home/hosam/code/projects/pig",
                    agents=["copilot"],
                    session_count=5,
                )
            ],
            "claude_code": [
                WorkspaceInfo(
                    workspace_id="ws-cli",
                    workspace_name="pig",
                    workspace_folder="/home/hosam/code/projects/pig",
                    agents=["claude_code"],
                    session_count=7,
                )
            ],
        }
    )

    assert len(merged) == 1
    assert merged[0].agents == ["copilot", "claude_code"]
    assert merged[0].session_count == 12


def test_list_all_workspaces_populates_workspace_folder_cache(monkeypatch) -> None:
    clear_find_workspace_cache()
    clear_workspace_folders_cache()

    def fake_scan(agent_name: str) -> list[WorkspaceInfo]:
        assert agent_name == "copilot"
        return [
            WorkspaceInfo(
                workspace_id="ws-copilot",
                workspace_name="demo",
                workspace_folder=r"C:\Repo\Demo",
                agents=["copilot"],
                session_count=2,
            )
        ]

    monkeypatch.setattr("src.shared.workspace_discovery._list_all_agents", lambda: ["copilot"])
    monkeypatch.setattr("src.shared.workspace_discovery._scan_agent_workspaces", fake_scan)

    workspaces = list_all_workspaces()
    assert len(workspaces) == 1

    def fail_scan(agent_name: str) -> list[WorkspaceInfo]:
        raise AssertionError(f"unexpected rescan for {agent_name}")

    monkeypatch.setattr("src.shared.workspace_discovery._scan_agent_workspaces", fail_scan)

    assert get_all_workspace_folders() == {"c:/repo/demo"}

    clear_find_workspace_cache()
    clear_workspace_folders_cache()
