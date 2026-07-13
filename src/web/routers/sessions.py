"""
Session Endpoints - API endpoints for session and turn browsing.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.web.shared_state import (
    get_all_workspace_metadata,
    get_sessions_for_workspace_by_folder,
    get_turns_for_session,
)
from src.web.utils.perf_timer import PerfTimer
router = APIRouter(tags=["sessions"])


def _is_bootstrap_only_root_session(session: dict) -> bool:
    if session.get("parent_session_id"):
        return False

    title = str(session.get("session_name") or "").strip().lower()
    turn_count = int(session.get("turn_count") or 0)
    if turn_count > 1:
        return False

    if title.startswith("<local-command-caveat>"):
        return True
    return title == "/clear"


@router.get("/api/browse/workspace/{workspace_id}/sessions")
async def get_workspace_sessions(workspace_id: str):
    """Get all sessions for a workspace across all agents.
    
    Sessions are consolidated by workspace_folder, so workspaces that share
    the same folder (e.g., copilot + claude_code on the same project) will
    have their sessions combined in this view.
    """
    perf = PerfTimer(f"GET /api/browse/workspace/{workspace_id[:8]}/sessions")
    
    # Get unified workspace metadata
    all_metadata = get_all_workspace_metadata()
    metadata = all_metadata.get(workspace_id)
    perf.checkpoint("get_all_workspace_metadata")
    
    if not metadata:
        raise HTTPException(status_code=404, detail="Workspace not found")
    
    agents = metadata.agents
    # Use folder-based query for cross-agent consolidation
    # Pass 'all' to get sessions from all agents sharing the same folder
    related_workspace_ids = metadata._related_workspace_ids or [workspace_id]
    all_sessions = get_sessions_for_workspace_by_folder(
        workspace_id,
        'all',
        related_workspace_ids=related_workspace_ids,
        workspace_folder=metadata.workspace_folder,
    )
    perf.checkpoint("get_sessions_for_workspace_by_folder")
    
    # Add agent info to each session if not already present
    for s in all_sessions:
        if "agent" not in s and "agents" in s:
            # Use first agent if multiple
            s["agent"] = s["agents"][0] if s["agents"] else "unknown"
        if s.get("parent_session_id") == s.get("session_id"):
            s["parent_session_id"] = None

    children_by_parent: dict[str, list[dict]] = {}
    root_sessions: list[dict] = []
    for session in all_sessions:
        parent_session_id = session.get("parent_session_id")
        if parent_session_id and session.get("relationship_type") == "subagent":
            children_by_parent.setdefault(parent_session_id, []).append(session)
        elif _is_bootstrap_only_root_session(session):
            continue
        else:
            root_sessions.append(session)

    for session in root_sessions:
        children = children_by_parent.get(session["session_id"], [])
        session["subagent_count"] = len(children)
        if children:
            session["has_subagents"] = True

    root_sessions.sort(key=lambda s: s.get("last_timestamp") or s.get("first_timestamp") or "", reverse=True)
    perf.done()
    return {"sessions": root_sessions, "agents": agents}


@router.get("/api/browse/session/{session_id}/turns")
async def get_session_turns(session_id: str):
    """Get all turns for a session."""
    perf = PerfTimer(f"GET /api/browse/session/{session_id[:8]}/turns")
    turns = get_turns_for_session(session_id)
    perf.done()
    return {"turns": turns}
