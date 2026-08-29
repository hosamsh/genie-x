from __future__ import annotations

from src.web.shared_state import WorkspaceStatus


def test_workspace_status_extracted_at_uses_latest_activity_timestamp() -> None:
    status = WorkspaceStatus(
        workspace_id="ws-1",
        agent="copilot",
        first_timestamp="2026-01-01T09:00:00+00:00",
        last_timestamp="2026-01-01T10:30:00+00:00",
    )

    assert status.extracted_at is not None
    assert status.extracted_at.isoformat() == "2026-01-01T10:30:00+00:00"


def test_workspace_status_extracted_at_falls_back_to_first_timestamp() -> None:
    status = WorkspaceStatus(
        workspace_id="ws-1",
        agent="copilot",
        first_timestamp="2026-01-01T09:00:00+00:00",
    )

    assert status.extracted_at is not None
    assert status.extracted_at.isoformat() == "2026-01-01T09:00:00+00:00"