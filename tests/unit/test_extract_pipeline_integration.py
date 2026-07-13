from __future__ import annotations

from pathlib import Path

from src.extract.models import ParsedSession, ParsedWorkspace, SessionEventRecord, WorkspaceDescriptor
from src.pipeline.extraction.extractor import extract_workspace
from src.pipeline.extraction.turn_enrichment import enrich_turns
from src.pipeline.extraction.storage import store_extraction_result
from src.shared.database.db_parsed import get_parsed_workspace
from src.shared.database.db_schema import init_shared_db
from src.shared.models.turn import Turn
from src.shared.models.workspace import WorkspaceInfo


def _make_parsed_workspace(tmp_path: Path) -> ParsedWorkspace:
    descriptor = WorkspaceDescriptor(
        workspace_id="ws-1",
        agent_name="copilot",
        workspace_name="demo",
        workspace_folder=str(tmp_path),
        source_root=str(tmp_path / "source"),
    )
    session = ParsedSession(
        session_id="sess-1",
        agent_name="copilot",
        workspace_id="ws-1",
        workspace_name="demo",
        workspace_folder=str(tmp_path),
        title="Demo session",
        source_path=str(tmp_path / "session.json"),
        started_at_ms=1700000000000,
        ended_at_ms=1700000001000,
        events=[
            SessionEventRecord(index=0, event_type="user.request", role="user", timestamp_ms=1700000000000, text="hello", raw={"type": "user"}),
            SessionEventRecord(index=1, event_type="assistant.response", role="assistant", timestamp_ms=1700000001000, text="world", raw={"type": "assistant"}),
        ],
    )
    return ParsedWorkspace(descriptor=descriptor, sessions=[session])


def _make_workspace_info(tmp_path: Path) -> WorkspaceInfo:
    workspace = WorkspaceInfo(
        workspace_id="ws-1",
        workspace_name="demo",
        workspace_folder=str(tmp_path),
        agents=["copilot"],
        session_count=1,
    )
    workspace._agent_workspace_ids = {"copilot": "ws-1"}  # type: ignore[attr-defined]
    return workspace


def test_extract_workspace_prefers_source_parser(monkeypatch, tmp_path: Path) -> None:
    parsed_workspace = _make_parsed_workspace(tmp_path)
    base_turns = [
        Turn(session_id="sess-1", turn=0, role="user", original_text="hello", workspace_id="ws-1", workspace_name="demo", workspace_folder=str(tmp_path), session_name="Demo session", agent_used="copilot", timestamp_ms=1700000000000),
        Turn(session_id="sess-1", turn=1, role="assistant", original_text="world", workspace_id="ws-1", workspace_name="demo", workspace_folder=str(tmp_path), session_name="Demo session", agent_used="copilot", timestamp_ms=1700000001000),
    ]

    def fake_find_workspace(workspace_id: str):
        assert workspace_id == "ws-1"
        return _make_workspace_info(tmp_path)

    def fake_supports(agent_name: str) -> bool:
        return agent_name == "copilot"

    def fake_extract_from_source(agent_name: str, workspace_id: str):
        from src.shared.models.workspace import ExtractedWorkspace

        assert agent_name == "copilot"
        assert workspace_id == "ws-1"
        return parsed_workspace, ExtractedWorkspace(
            turns=base_turns,
            session_count=1,
            agent_name="copilot",
            workspace_id="ws-1",
            source_artifacts={"parsed_workspaces": [parsed_workspace]},
        )

    monkeypatch.setattr("src.pipeline.extraction.extractor.find_workspace", fake_find_workspace)
    monkeypatch.setattr("src.pipeline.extraction.extractor.supports_agent", fake_supports)
    monkeypatch.setattr("src.pipeline.extraction.extractor.extract_workspace_from_source", fake_extract_from_source)

    result = extract_workspace("ws-1")

    assert result.session_count == 1
    assert len(result.turns) == 2
    assert result.source_artifacts["parsed_workspaces"][0] is parsed_workspace


def test_store_extraction_result_persists_parsed_raw(monkeypatch, tmp_path: Path) -> None:
    from src.pipeline.extraction.adapter import adapt_parsed_workspace

    parsed_workspace = _make_parsed_workspace(tmp_path)
    extraction_result = adapt_parsed_workspace(parsed_workspace)
    extraction_result.turns = enrich_turns(list(extraction_result.turns))
    extraction_result.source_artifacts["parsed_workspaces"] = [parsed_workspace]

    def fake_find_workspace(workspace_id: str):
        assert workspace_id == "ws-1"
        return _make_workspace_info(tmp_path)

    monkeypatch.setattr("src.pipeline.extraction.storage.find_workspace", fake_find_workspace)

    db_path = tmp_path / "integration.db"
    init_shared_db(db_path, verbose=False).close()
    store_result = store_extraction_result(extraction_result, db_path)

    assert store_result.success

    conn = init_shared_db(db_path, verbose=False)
    loaded = get_parsed_workspace(conn, "ws-1", "copilot")
    conn.close()

    assert loaded is not None
    assert loaded.descriptor.workspace_id == "ws-1"
    assert len(loaded.sessions) == 1
