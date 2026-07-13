from __future__ import annotations

from src.shared.database.db_schema import init_shared_db
from src.web.data_providers.extraction_provider import ExtractionDataProvider


def test_code_timeline_and_model_usage_use_delta_metrics_or_turn_fallback(tmp_path) -> None:
    db_path = tmp_path / "provider.db"
    conn = init_shared_db(db_path, verbose=False)

    conn.execute(
        """
        INSERT INTO workspace_info (
            workspace_id, workspace_name, workspace_folder, agent_used,
            extraction_duration_ms, session_count, turn_count,
            total_code_loc, total_doc_loc, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("ws-1", "demo", "c:/repo/demo", "claude_code", 1, 1, 1, 100, 0, "2026-06-28T00:00:00", "2026-06-28T00:00:00"),
    )
    conn.execute(
        """
        INSERT INTO turns (
            session_id, turn, role, text, original_text, workspace_id, workspace_name, workspace_folder,
            session_name, agent_used, model_id, request_id, timestamp_iso, total_lines_added, total_lines_removed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("sess-1", 0, "assistant", "x", "x", "ws-1", "demo", "c:/repo/demo", "demo session", "claude_code", "claude-sonnet", "req-1", "2026-06-24T12:00:00+00:00", 25, 5),
    )
    conn.execute(
        """
        INSERT INTO code_metrics (
            request_id, session_id, file_path, workspace_id, agent_used, model_id, lines_added, lines_removed, delta_metrics
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("req-1", "sess-1", "c:/repo/demo/app.py", "ws-1", "claude_code", "claude-sonnet", None, None, '{"lines_added": 25, "lines_removed": 5}'),
    )
    conn.commit()

    provider = ExtractionDataProvider(conn, "ws-1", workspace_folder="c:/repo/demo")

    timeline = provider.get_code_timeline()
    model_usage = provider.get_model_usage()
    conn.close()

    assert timeline == [{"date": "2026-06-24", "added": 25, "removed": 5}]
    assert model_usage == [{"model": "claude-sonnet", "locs": 25}]