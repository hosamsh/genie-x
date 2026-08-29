"""
Workspace Endpoints - API endpoints for workspace listing and status.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from src.shared.workspace_discovery import find_workspace
from src.shared.io.paths import is_home_or_root_dir
from src.shared.logging.logger import get_logger
from src.web.shared_state import (
    connect_db,
    get_all_workspace_metadata,
    get_database_workspaces,
    get_workspace_status,
)
from src.web.utils.perf_timer import PerfTimer

logger = get_logger(__name__)
router = APIRouter(tags=["workspaces"])


@router.get("/api/browse/workspaces")
async def get_browse_workspaces(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    include_live: bool = Query(True),
    agent: str | None = Query(None),
    sort: str = Query("name", pattern="^(name|modified)$"),
):
    """List available workspaces with extraction/analysis status.

    Returns a merged list of:
    1. Live workspaces from extractors (source_available=True)
    2. Database-only workspaces that may no longer exist on disk (source_available=False)
    Optionally filters to workspaces discovered for a specific agent.
    """
    perf = PerfTimer("GET /api/browse/workspaces")
    try:
        if include_live:
            all_metadata = get_all_workspace_metadata()
            perf.checkpoint("get_all_workspace_metadata")
            merged_workspaces = [ws.to_api_dict() for ws in all_metadata.values()]
        else:
            db_workspaces = get_database_workspaces()
            perf.checkpoint("get_database_workspaces")
            merged_workspaces = [
                {
                    "workspace_id": data["workspace_id"],
                    "workspace_name": data["workspace_name"],
                    "workspace_folder": data["workspace_folder"],
                    "agents": data["agents"],
                    "session_count": data["session_count"],
                    "turn_count": data["turn_count"],
                    "status": {
                        agent: {"extracted_at": data["last_timestamp"], "run_dir": None}
                        for agent in data["agents"]
                    },
                    "source_available": None,
                    "first_timestamp": data["first_timestamp"],
                    "last_timestamp": data["last_timestamp"],
                }
                for data in db_workspaces.values()
                if not is_home_or_root_dir(data["workspace_folder"])
            ]

        normalized_agent = agent.strip().casefold() if agent else None
        if normalized_agent:
            merged_workspaces = [
                workspace
                for workspace in merged_workspaces
                if any(
                    (workspace_agent or "").casefold() == normalized_agent
                    for workspace_agent in workspace.get("agents", [])
                )
            ]
            perf.checkpoint("filter_agent")

        if sort == "modified":
            # Most recently active first; workspaces without a timestamp go last.
            merged_workspaces.sort(
                key=lambda x: (x.get("last_timestamp") or "", x["workspace_name"].lower()),
                reverse=True,
            )
        else:
            merged_workspaces.sort(key=lambda x: x["workspace_name"].lower())
        perf.checkpoint("sort_workspaces")

        # Paginate results
        total_count = len(merged_workspaces)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_list = merged_workspaces[start_idx:end_idx]

        total_pages = (total_count + page_size - 1) // page_size
        perf.done()

        return {
            "workspaces": paginated_list,
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
        }
    except Exception as e:
        logger.error(f"Error listing browse workspaces: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/browse/workspace/{workspace_id}/status")
async def get_workspace_status_all_agents(workspace_id: str):
    """Get extraction status for a workspace (all agents).

    Works for both live workspaces and database-only workspaces.
    """
    db_workspaces = get_database_workspaces()
    db_workspace = db_workspaces.get(workspace_id)
    if db_workspace:
        return {
            "workspace_id": workspace_id,
            "workspace_name": db_workspace["workspace_name"],
            "agents": db_workspace["agents"],
            "agents_status": {
                agent: {"extracted_at": db_workspace["last_timestamp"], "run_dir": None}
                for agent in db_workspace["agents"]
            },
            "is_extracted": db_workspace["turn_count"] > 0,
            "run_dir": None,
            "source_available": None,
        }

    conn = connect_db()
    try:
        cursor = conn.execute(
            """
            SELECT workspace_name, workspace_folder, GROUP_CONCAT(DISTINCT agent_used),
                   COUNT(DISTINCT session_id), COUNT(*), MIN(timestamp_iso), MAX(timestamp_iso)
            FROM turns
            WHERE workspace_id = ?
            GROUP BY workspace_id, workspace_name, workspace_folder
            LIMIT 1
            """,
            (workspace_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if row:
        agents = [agent for agent in (row[2] or "").split(",") if agent]
        return {
            "workspace_id": workspace_id,
            "workspace_name": row[0] or workspace_id,
            "workspace_folder": row[1] or "",
            "agents": agents,
            "agents_status": {
                agent: {"extracted_at": row[6], "run_dir": None}
                for agent in agents
            },
            "is_extracted": (row[4] or 0) > 0,
            "run_dir": None,
            "source_available": None,
        }

    # Fallback to live metadata for workspaces that are not in the DB yet.
    all_metadata = get_all_workspace_metadata()
    metadata = all_metadata.get(workspace_id)
    
    if not metadata:
        raise HTTPException(status_code=404, detail="Workspace not found")
    
    # Build agents status dict
    agents_status = {}
    for agent, status in metadata.agent_status.items():
        agents_status[agent] = {
            "extracted_at": status.extracted_at.isoformat() if status.extracted_at else None,
            "run_dir": status.run_dir,
        }
    
    return {
        "workspace_id": metadata.workspace_id,
        "workspace_name": metadata.workspace_name,
        "agents": metadata.agents,
        "agents_status": agents_status,
        "is_extracted": metadata.is_extracted,
        "run_dir": next((s.run_dir for s in metadata.agent_status.values() if s.run_dir), None),
        "source_available": metadata.source_available,
    }



@router.get("/api/browse/workspace/{workspace_id}/sync-status")
async def get_workspace_sync_status(workspace_id: str):
    """Check if workspace source has newer data than the extracted database."""
    from src.shared.workspace_discovery import get_workspace_latest_stats

    ws = find_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace source not available")

    agent_stats = get_workspace_latest_stats(workspace_id)

    total_source_sessions = 0
    total_source_turns = 0

    for _agent_name, stats in agent_stats.items():
        if stats:
            total_source_sessions += stats.session_count
            total_source_turns += stats.turn_count

    if total_source_sessions == 0:
        return {
            "workspace_id": workspace_id,
            "needs_sync": False,
            "source_sessions": 0,
            "source_turns": 0,
            "extracted_sessions": 0,
            "extracted_turns": 0,
            "new_sessions": 0,
            "new_turns": 0,
            "message": "No source data available for sync check",
        }

    total_extracted_sessions = 0
    total_extracted_turns = 0

    for agent in ws.agents:
        status = get_workspace_status(workspace_id, agent)
        if status:
            total_extracted_sessions += status.session_count
            total_extracted_turns += status.turn_count

    new_sessions = max(0, total_source_sessions - total_extracted_sessions)
    new_turns = max(0, total_source_turns - total_extracted_turns)

    needs_sync = (
        total_source_sessions > total_extracted_sessions
        or total_source_turns > total_extracted_turns
    )

    return {
        "workspace_id": workspace_id,
        "needs_sync": needs_sync,
        "source_sessions": total_source_sessions,
        "source_turns": total_source_turns,
        "extracted_sessions": total_extracted_sessions,
        "extracted_turns": total_extracted_turns,
        "new_sessions": new_sessions,
        "new_turns": new_turns,
        "message": (
            f"Found {new_sessions} new sessions, ~{new_turns} new turns"
            if needs_sync
            else "Workspace is up to date"
        ),
    }
