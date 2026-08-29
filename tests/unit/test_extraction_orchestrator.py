from __future__ import annotations

import asyncio
from pathlib import Path

from src.pipeline.extraction import orchestrator
from src.shared.models.workspace import WorkspaceExtractionResult


def _result(workspace_id: str) -> WorkspaceExtractionResult:
    return WorkspaceExtractionResult(
        status="success",
        workspace_id=workspace_id,
        workspace_name=workspace_id,
        workspace_folder="c:/repo/demo",
        session_count=1,
        turn_count=1,
        duration_ms=1,
    )


def test_extract_workspaces_skips_scan_cache_for_single_workspace(monkeypatch, tmp_path: Path) -> None:
    cache_calls: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "extract_single_workspace",
        lambda workspace_id, db_path, force_refresh=False, agent_filter=None: _result(workspace_id),
    )
    monkeypatch.setattr(orchestrator, "prime_workspace_scan_cache", lambda: cache_calls.append("prime"))
    monkeypatch.setattr(orchestrator, "clear_workspace_scan_cache", lambda: cache_calls.append("clear"))

    stats = asyncio.run(orchestrator.extract_workspaces(["ws-1"], str(tmp_path)))

    assert list(stats) == ["ws-1"]
    assert cache_calls == []


def test_extract_workspaces_uses_scan_cache_for_multi_workspace_batch(monkeypatch, tmp_path: Path) -> None:
    cache_calls: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "extract_single_workspace",
        lambda workspace_id, db_path, force_refresh=False, agent_filter=None: _result(workspace_id),
    )
    monkeypatch.setattr(orchestrator, "prime_workspace_scan_cache", lambda: cache_calls.append("prime"))
    monkeypatch.setattr(orchestrator, "clear_workspace_scan_cache", lambda: cache_calls.append("clear"))

    stats = asyncio.run(orchestrator.extract_workspaces(["ws-1", "ws-2"], str(tmp_path)))

    assert list(stats) == ["ws-1", "ws-2"]
    assert cache_calls == ["prime", "clear"]