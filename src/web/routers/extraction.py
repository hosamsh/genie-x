"""
Extraction Endpoints - API endpoints for workspace extraction operations.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.shared.workspace_discovery import find_workspace
from src.web.services.extraction_service import (
    create_bulk_extraction_run,
    create_extraction_run,
    execute_bulk_streaming_extraction,
    execute_streaming_extraction,
)
from src.web.shared_state import get_all_workspace_metadata, get_workspace_status
router = APIRouter(tags=["extraction"])


class BulkExtractRequest(BaseModel):
    workspace_ids: list[str]
    refresh: bool = False


@router.post("/api/browse/workspace/{workspace_id}/extract-stream")
async def extract_workspace_streaming(
    workspace_id: str, deleteExisting: bool = False, sync: bool = False
):
    """Start streaming extraction for a workspace."""
    ws = find_workspace(workspace_id)
    if not ws:
        # Check if workspace exists in database only
        all_metadata = get_all_workspace_metadata()
        metadata = all_metadata.get(workspace_id)
        
        if not metadata or not metadata.db_available:
            raise HTTPException(status_code=404, detail="Workspace not found")

        if deleteExisting or sync:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete and/or sync extraction: workspace source is no longer available on disk",
            )

        return {
            "status": "already_extracted",
            "workspace_id": workspace_id,
            "streaming": False,
            "message": "Workspace data loaded from database (source no longer available)",
        }

    # Check if already extracted
    if not deleteExisting and not sync:
        all_extracted = True
        for agent in ws.agents:
            status = get_workspace_status(workspace_id, agent)
            if not status or not status.is_extracted:
                all_extracted = False
                break

        if all_extracted:
            return {
                "status": "already_extracted",
                "workspace_id": workspace_id,
                "streaming": False,
                "message": "Workspace already extracted",
            }

    run_id = create_extraction_run(workspace_id, ws.workspace_name)
    asyncio.create_task(
        execute_streaming_extraction(run_id, workspace_id, ws, deleteExisting)
    )

    return {
        "status": "started",
        "run_id": run_id,
        "workspace_id": workspace_id,
        "workspace_name": ws.workspace_name,
        "streaming": True,
    }


@router.post("/api/browse/bulk-extract-stream")
async def bulk_extract_streaming(request: BulkExtractRequest):
    """Start streaming bulk extraction for multiple workspaces."""
    if not request.workspace_ids:
        raise HTTPException(status_code=400, detail="No workspace IDs provided")

    run_id = create_bulk_extraction_run(request.workspace_ids)
    asyncio.create_task(
        execute_bulk_streaming_extraction(run_id, request.workspace_ids, request.refresh)
    )

    return {
        "status": "started",
        "run_id": run_id,
        "workspace_count": len(request.workspace_ids),
        "streaming": True,
    }

