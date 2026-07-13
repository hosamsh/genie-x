"""Agent metadata and icon serving endpoints."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from src.extract.registry import (
    get_all_agent_metadata,
    get_agent_icon_path,
    get_agent_metadata,
    list_agents,
)
from src.shared.logging.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _build_icon_url(agent_id: str) -> str:
    """Build a cache-busted icon URL for an agent."""
    base_url = f"/api/agents/{agent_id}/icon"
    icon_path = get_agent_icon_path(agent_id)
    if icon_path is None:
        return base_url

    try:
        version = icon_path.stat().st_mtime_ns
    except OSError:
        return base_url

    return f"{base_url}?v={version}"


@router.get("/")
async def get_agents():
    """Get list of all supported agents with their metadata."""
    try:
        agents = list_agents()
        metadata = get_all_agent_metadata()
        
        result = []
        for agent in agents:
            agent_data: dict[str, Any] = {
                "id": agent,
                "name": agent,
                "has_icon": get_agent_icon_path(agent) is not None,
                "icon_url": _build_icon_url(agent),
            }
            
            # Add metadata if available
            if agent in metadata:
                agent_data.update(dict(metadata[agent]))
            
            result.append(agent_data)
        
        return JSONResponse(content={"agents": result})
    except Exception as e:
        logger.error(f"Error listing agents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{agent_id}/metadata")
async def get_agent_metadata_endpoint(agent_id: str):
    """Get metadata for a specific agent."""
    metadata = get_agent_metadata(agent_id)
    
    if metadata is None:
        # Return minimal metadata even if file doesn't exist
        return JSONResponse(content={
            "id": agent_id,
            "name": agent_id.replace("_", " ").title(),
            "display_name": agent_id.replace("_", " ").title(),
            "color": "bg-surface-dark text-terminal-gray",
            "description": f"{agent_id} chat extraction",
            "has_icon": get_agent_icon_path(agent_id) is not None,
            "icon_url": _build_icon_url(agent_id),
        })
    
    result = dict(metadata)
    result["id"] = agent_id
    result["has_icon"] = get_agent_icon_path(agent_id) is not None
    result["icon_url"] = _build_icon_url(agent_id)
    
    return JSONResponse(content=result)


@router.get("/{agent_id}/icon")
async def get_agent_icon(agent_id: str):
    """Get icon file for a specific agent."""
    icon_path = get_agent_icon_path(agent_id)
    
    if icon_path is None:
        # Return default icon
        static_dir = Path(__file__).parent.parent / "static"
        default_icon = static_dir / "img/agent-logo.svg"
        
        if not default_icon.exists():
            raise HTTPException(status_code=404, detail="No icon available")
        
        return FileResponse(
            default_icon,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"}
        )
    
    # Determine media type based on extension
    media_type = "image/svg+xml" if icon_path.suffix == ".svg" else f"image/{icon_path.suffix[1:]}"
    
    return FileResponse(
        icon_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600"}
    )
