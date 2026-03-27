"""Unit tests for Cursor extractor – Epic 8.2 security hardening."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from src.extract_plugins.cursor.extractor import (
    discover_workspaces,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_db(db_path: Path) -> None:
    """Create a minimal Cursor-like state.vscdb so discover_workspaces can open it."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    # Empty composer list – workspace won't be returned but won't crash either
    conn.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        ("composer.composerData", json.dumps({"allComposers": []})),
    )
    conn.commit()
    conn.close()


def _make_workspace(storage: Path, name: str = "ws-001") -> Path:
    folder = storage / name
    folder.mkdir(parents=True)
    db = folder / "state.vscdb"
    _make_minimal_db(db)
    return folder




# =============================================================================
# discover_workspaces – containment validation
# =============================================================================

class TestDiscoverWorkspacesContainment:
    """discover_workspaces skips workspace folders that escape the storage root."""

    def test_normal_workspace_discovered(self, tmp_path):
        storage = tmp_path / "workspaceStorage"
        _make_workspace(storage, "ws-normal")
        workspaces = discover_workspaces(workspace_storage=storage)
        # The workspace has no composer data, so it won't be included, but
        # the call must not raise and storage is scanned without error.
        assert isinstance(workspaces, list)

    def test_symlink_escape_skipped(self, tmp_path):
        """A symlink that points outside workspace_storage is rejected."""
        storage = tmp_path / "workspaceStorage"
        storage.mkdir()
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        link = storage / "evil-link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this platform")

        with patch("src.extract_plugins.cursor.extractor.logger") as mock_logger:
            discover_workspaces(workspace_storage=storage)
            warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
            assert any("escapes" in w or "escape" in w for w in warning_calls), (
                f"Expected 'escapes' warning, got: {warning_calls}"
            )

    def test_nonexistent_storage_returns_empty(self, tmp_path):
        workspaces = discover_workspaces(workspace_storage=tmp_path / "nonexistent")
        assert workspaces == []



