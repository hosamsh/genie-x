"""Convenience wrappers for parsed-workspace persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from src.extract.models import ParsedWorkspace
from src.shared.database.db_parsed import get_parsed_workspace, upsert_parsed_workspace
from src.shared.database.db_schema import connect_db, init_shared_db


def open_parsed_db(db_path: Path) -> sqlite3.Connection:
    """Open a shared DB with parsed-workspace tables ensured."""
    return init_shared_db(db_path, verbose=False) if not db_path.exists() else connect_db(db_path)


def store_parsed_workspace(db_path: Path, parsed_workspace: ParsedWorkspace) -> dict[str, int]:
    """Persist one parsed workspace into the shared database."""
    conn = open_parsed_db(db_path)
    try:
        return upsert_parsed_workspace(conn, parsed_workspace)
    finally:
        conn.close()


def load_parsed_workspace(
    db_path: Path,
    workspace_id: str,
    agent_name: str,
) -> Optional[ParsedWorkspace]:
    """Load one parsed workspace from the shared database."""
    conn = open_parsed_db(db_path)
    try:
        return get_parsed_workspace(conn, workspace_id, agent_name)
    finally:
        conn.close()