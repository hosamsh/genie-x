"""
Shared state for the Browse Chats web feature.

Uses the same database as the CLI pipeline (genie.db in the run directory).
Extraction status is determined by presence of data in turns/combined_turns tables.


Run directory is configured via config.yaml under web.run_dir (default: data/web)
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from src.shared.logging.logger import get_logger
from src.shared.database import db_schema
from src.shared.database import db_extract
from src.shared.io.run_dir import get_db_path as _get_db_path

logger = get_logger(__name__)

# Default run directory - used if config doesn't specify one
DEFAULT_RUN_DIR = Path("data/web")

# Cache for run_dir to avoid repeated config loading
_cached_run_dir: Optional[Path] = None


def clear_run_dir_cache():
    """Clear the cached run directory (useful for testing/reloading)."""
    global _cached_run_dir
    _cached_run_dir = None
    logger.info("[WEB] Run directory cache cleared")


def get_run_dir() -> Path:
    """Get the run directory path from environment or config.yaml.
    
    Priority:
    1. WEB_RUN_DIR environment variable
    2. config.yaml: web.run_dir
    3. DEFAULT_RUN_DIR fallback
    """
    global _cached_run_dir
    
    if _cached_run_dir is not None:
        logger.debug(f"[WEB] Using cached run directory: {_cached_run_dir}")
        return _cached_run_dir
    
    # Check environment variable first (useful for testing)
    import os
    env_run_dir = os.environ.get("WEB_RUN_DIR")
    if env_run_dir:
        _cached_run_dir = Path(env_run_dir)
        logger.debug(f"[WEB] Using run directory from WEB_RUN_DIR env: {_cached_run_dir}")
        return _cached_run_dir
    
    try:
        from src.shared.config.config_loader import get_config
        config = get_config()
        
        # Access web.run_dir from config
        if hasattr(config, 'web') and hasattr(config.web, 'run_dir'):
            run_dir = config.web.run_dir
            if run_dir:
                _cached_run_dir = Path(run_dir)
                logger.info(f"[WEB] Loaded run directory from config.yaml: {_cached_run_dir}")
                return _cached_run_dir
    except Exception as e:
        logger.warning(f"Could not load web.run_dir from config: {e}")
    
    _cached_run_dir = DEFAULT_RUN_DIR
    logger.debug(f"[WEB] Using default run directory: {_cached_run_dir}")
    return _cached_run_dir


def get_db_path() -> Path:
    """Get the path to the pipeline database."""
    db_path = _get_db_path(get_run_dir())
    logger.debug(f"[WEB] Database path: {db_path} (exists: {db_path.exists()})")
    return db_path


@dataclass
class WorkspaceStatus:
    """Status of a workspace based on database contents."""
    workspace_id: str
    agent: str
    is_extracted: bool = False
    session_count: int = 0
    turn_count: int = 0
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None
    
    # Properties for web API serialization
    @property
    def extracted_at(self) -> Optional[datetime]:
        """Return first_timestamp as datetime for API responses."""
        if self.first_timestamp:
            try:
                return datetime.fromisoformat(self.first_timestamp)
            except (ValueError, TypeError):
                pass
        return None
    
    @property
    def run_dir(self) -> Optional[str]:
        """Return run directory path for API responses."""
        return str(get_run_dir()) if self.is_extracted else None


def connect_db() -> sqlite3.Connection:
    """Connect to the pipeline database.
    
    Creates the database and initializes schema if it doesn't exist.
    """
    db_path = get_db_path()
    return db_schema.connect_db(db_path)


def resolve_workspace_folder(workspace_id: str) -> Optional[str]:
    """Resolve a workspace_id to its workspace_folder.
    
    This enables cross-agent consolidation by querying using workspace_folder
    instead of workspace_id. When multiple agents (copilot, claude_code, cursor)
    work on the same folder, they may have different workspace_ids but the same
    workspace_folder.
    
    Args:
        workspace_id: The workspace ID to resolve
        
    Returns:
        The normalized workspace_folder path, or None if not found
    """
    conn = connect_db()
    try:
        # First try to find workspace_folder from turns table
        cursor = conn.execute(
            """SELECT workspace_folder FROM turns 
               WHERE workspace_id = ? AND workspace_folder IS NOT NULL AND workspace_folder != ''
               LIMIT 1""",
            (workspace_id,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
        
        # Fallback: try workspace_info table
        cursor = conn.execute(
            """SELECT workspace_folder FROM workspace_info 
               WHERE workspace_id = ? AND workspace_folder IS NOT NULL AND workspace_folder != ''
               LIMIT 1""",
            (workspace_id,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
        
        return None
    finally:
        conn.close()


def get_workspace_status(workspace_id: str, agent: str) -> Optional[WorkspaceStatus]:
    """Get the status of a workspace for a specific agent.
    
    Extraction status: workspace has records in turns table
    
    Args:
        workspace_id: The workspace ID
        agent: The agent type (copilot, cursor, etc.)
        
    Returns:
        WorkspaceStatus if workspace has any data, None otherwise
    """
    conn = connect_db()
    try:
        status_dict = db_extract.query_workspace_status(conn, workspace_id, agent)
        if not status_dict:
            return None
        
        return WorkspaceStatus(
            workspace_id=status_dict["workspace_id"],
            agent=status_dict["agent"],
            is_extracted=status_dict["is_extracted"],
            session_count=status_dict["session_count"],
            turn_count=status_dict["turn_count"],
            first_timestamp=status_dict["first_timestamp"],
            last_timestamp=status_dict["last_timestamp"],
        )
    finally:
        conn.close()


def get_all_workspace_statuses() -> Dict[str, Dict[str, WorkspaceStatus]]:
    """Get status for all workspaces in the database.
    
    Returns:
        Dict mapping workspace_id -> agent -> WorkspaceStatus
    """
    conn = connect_db()
    try:
        status_dicts = db_extract.query_all_workspace_statuses(conn)
        
        result: Dict[str, Dict[str, WorkspaceStatus]] = {}
        for workspace_id, agents in status_dicts.items():
            result[workspace_id] = {}
            for agent, status_dict in agents.items():
                result[workspace_id][agent] = WorkspaceStatus(
                    workspace_id=status_dict["workspace_id"],
                    agent=status_dict["agent"],
                    is_extracted=status_dict["is_extracted"],
                    session_count=status_dict["session_count"],
                    turn_count=status_dict["turn_count"],
                    first_timestamp=status_dict["first_timestamp"],
                    last_timestamp=status_dict["last_timestamp"],
                )
        
        return result
    finally:
        conn.close()


def get_database_workspaces() -> Dict[str, Dict[str, Any]]:
    """Get all workspaces that have data in the database.
    
    This returns workspace info derived from the turns table,
    which may include workspaces that no longer exist on disk.
    
    Returns:
        Dict mapping workspace_id -> workspace_dict (for internal use)
    """
    conn = connect_db()
    try:
        return db_extract.query_database_workspaces(conn)
    finally:
        conn.close()


def get_all_workspace_metadata() -> Dict[str, Any]:
    """Get all workspace metadata (unified model).
    
    This function returns WorkspaceInfo objects with enriched database fields.
    Workspaces are consolidated by workspace_folder to support cross-agent
    consolidation (e.g., copilot + claude_code on the same project folder).
    
    Returns:
        Dict mapping workspace_id -> WorkspaceInfo
    """
    from src.shared.models.workspace import WorkspaceInfo
    from src.pipeline.extraction.workspace_discovery import list_all_workspaces
    
    # Get live workspaces from disk
    live_workspaces = list_all_workspaces()
    live_map = {ws.workspace_id: ws for ws in live_workspaces}
    
    # Get database workspaces (raw dicts)
    db_workspaces_raw = get_database_workspaces()
    
    # Convert raw dicts to WorkspaceInfo objects for merging
    db_workspaces = {}
    for ws_id, data in db_workspaces_raw.items():
        db_workspaces[ws_id] = WorkspaceInfo(
            workspace_id=data["workspace_id"],
            workspace_name=data["workspace_name"],
            workspace_folder=data["workspace_folder"],
            agents=data["agents"],
            session_count=data["session_count"],
            turn_count=data["turn_count"],
            is_extracted=data["turn_count"] > 0,
            first_timestamp=data["first_timestamp"],
            last_timestamp=data["last_timestamp"],
            source_available=False,  # These are DB-only workspaces
            db_available=True,
        )
    
    # Get all statuses
    all_statuses = get_all_workspace_statuses()
    
    # First pass: Merge by workspace_id (existing logic)
    by_id: Dict[str, Any] = {}
    all_workspace_ids = set(live_map.keys()) | set(db_workspaces.keys())
    
    for ws_id in all_workspace_ids:
        live_info = live_map.get(ws_id)
        db_info = db_workspaces.get(ws_id)
        statuses = all_statuses.get(ws_id, {})
        
        # Merge live and DB info
        if live_info and db_info:
            # Workspace exists both on disk and in DB
            merged = WorkspaceInfo(
                workspace_id=ws_id,
                workspace_name=live_info.workspace_name or db_info.workspace_name,
                workspace_folder=live_info.workspace_folder or db_info.workspace_folder,
                agents=sorted(list(set(live_info.agents) | set(db_info.agents))),
                session_count=db_info.session_count,  # DB is source of truth
                turn_count=db_info.turn_count,
                is_extracted=db_info.turn_count > 0,
                first_timestamp=db_info.first_timestamp,
                last_timestamp=db_info.last_timestamp,
                source_available=True,
                db_available=True,
            )
        elif live_info:
            # Only on disk, not in DB
            # Check if there are statuses indicating extraction has happened
            has_extracted_status = any(s.is_extracted for s in statuses.values()) if statuses else False
            merged = WorkspaceInfo(
                workspace_id=ws_id,
                workspace_name=live_info.workspace_name,
                workspace_folder=live_info.workspace_folder,
                agents=live_info.agents,
                session_count=live_info.session_count,
                is_extracted=has_extracted_status,
                source_available=True,
                db_available=False,
            )
        elif db_info:
            # Only in DB
            merged = db_info
        else:
            # Should never happen given how all_workspace_ids is built
            continue
        
        # Add per-agent status if available
        if statuses:
            from src.shared.models.workspace import AgentStatus
            agent_status_dict = {}
            for agent, status in statuses.items():
                agent_status_dict[agent] = AgentStatus(
                    agent=status.agent,
                    is_extracted=status.is_extracted,
                    session_count=status.session_count,
                    turn_count=status.turn_count,
                    extracted_at=status.extracted_at,
                    run_dir=status.run_dir,
                    first_timestamp=status.first_timestamp,
                    last_timestamp=status.last_timestamp,
                )
            merged.agent_status = agent_status_dict
        
        by_id[ws_id] = merged
    
    # Second pass: Consolidate by workspace_folder for cross-agent unification
    # This ensures copilot + claude_code + cursor on the same folder show as ONE workspace
    by_folder: Dict[str, WorkspaceInfo] = {}
    folder_to_canonical_id: Dict[str, str] = {}
    
    for ws_id, ws_info in by_id.items():
        folder = ws_info.workspace_folder
        if not folder:
            # No folder - keep as separate entry (use ws_id as key)
            by_folder[ws_id] = ws_info
            continue
        
        # Normalize folder for comparison (URI-safe: don't use Path which mangles ://)
        normalized_folder = folder.replace("\\", "/").lower()
        
        if normalized_folder not in folder_to_canonical_id:
            # First workspace with this folder - use it as canonical
            folder_to_canonical_id[normalized_folder] = ws_id
            by_folder[ws_id] = ws_info
        else:
            # Merge with existing workspace that has the same folder
            canonical_id = folder_to_canonical_id[normalized_folder]
            existing = by_folder[canonical_id]
            
            # Merge agents
            merged_agents = sorted(list(set(existing.agents) | set(ws_info.agents)))
            
            # Merge agent_status
            merged_status = dict(existing.agent_status)
            merged_status.update(ws_info.agent_status)
            
            # Aggregate counts
            merged_session_count = existing.session_count + ws_info.session_count
            merged_turn_count = existing.turn_count + ws_info.turn_count
            
            # Pick earliest first_timestamp and latest last_timestamp
            first_ts = existing.first_timestamp
            if ws_info.first_timestamp:
                if not first_ts or ws_info.first_timestamp < first_ts:
                    first_ts = ws_info.first_timestamp
            
            last_ts = existing.last_timestamp
            if ws_info.last_timestamp:
                if not last_ts or ws_info.last_timestamp > last_ts:
                    last_ts = ws_info.last_timestamp
            
            # Update the canonical entry
            by_folder[canonical_id] = WorkspaceInfo(
                workspace_id=canonical_id,  # Keep the canonical ID
                workspace_name=existing.workspace_name or ws_info.workspace_name,
                workspace_folder=existing.workspace_folder or ws_info.workspace_folder,
                agents=merged_agents,
                session_count=merged_session_count,
                turn_count=merged_turn_count,
                is_extracted=existing.is_extracted or ws_info.is_extracted,
                first_timestamp=first_ts,
                last_timestamp=last_ts,
                source_available=existing.source_available or ws_info.source_available,
                db_available=existing.db_available or ws_info.db_available,
                agent_status=merged_status,
            )
    
    return by_folder


def get_sessions_for_workspace(workspace_id: str, agent: str) -> List[Dict[str, Any]]:
    """Get all sessions for a workspace.
    
    Derives session info from the turns table.
    
    Args:
        workspace_id: The workspace ID
        agent: The agent type
        
    Returns:
        List of session dicts
    """
    conn = connect_db()
    try:
        return db_extract.query_workspace_sessions(conn, workspace_id, agent)
    finally:
        conn.close()


def get_sessions_for_workspace_by_folder(workspace_id: str, agent: str) -> List[Dict[str, Any]]:
    """Get all sessions for a workspace, consolidated by workspace_folder.
    
    This resolves workspace_id to workspace_folder and queries all sessions
    across all agents that share the same folder. This enables cross-agent
    consolidation for workspaces used by multiple AI assistants.
    
    Args:
        workspace_id: The workspace ID (used to resolve workspace_folder)
        agent: The agent type filter (or 'all' for all agents)
        
    Returns:
        List of session dicts from all agents sharing the same folder
    """
    workspace_folder = resolve_workspace_folder(workspace_id)
    if not workspace_folder:
        # Fallback to workspace_id-based query if folder not found
        return get_sessions_for_workspace(workspace_id, agent)
    
    conn = connect_db()
    try:
        return db_extract.query_workspace_sessions_by_folder(conn, workspace_folder, agent)
    finally:
        conn.close()


def get_turns_for_session(session_id: str) -> List[Dict[str, Any]]:
    """Get all turns for a session.
    
    Args:
        session_id: The session ID
        
    Returns:
        List of turn dicts ordered by turn number
    """
    conn = connect_db()
    try:
        return db_extract.query_session_turns(conn, session_id)
    finally:
        conn.close()


def get_shared_run_dir() -> Path:
    """Get the path to the shared run directory.
    
    This is the same as get_run_dir() for compatibility.
    """
    return get_run_dir()
