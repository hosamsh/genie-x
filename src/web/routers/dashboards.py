from __future__ import annotations

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from src.web.services.dashboard_service import (
    get_dashboard_data_payload,
    get_system_dashboard_chart_data_payload,
    get_system_dashboard_data_payload,
)

router = APIRouter()


@router.get("/api/browse/workspace/{workspace_id}/dashboards/{dashboard_id}")
async def get_dashboard_data(workspace_id: str, dashboard_id: str):
    try:
        return await run_in_threadpool(get_dashboard_data_payload, workspace_id, dashboard_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/system/dashboards/{dashboard_id}")
async def get_system_dashboard_data(dashboard_id: str):
    """Get data for system-level dashboards (not workspace-specific)."""
    try:
        return await run_in_threadpool(get_system_dashboard_data_payload, dashboard_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/system/dashboards/{dashboard_id}/charts/{chart_id}")
async def get_system_dashboard_chart_data(dashboard_id: str, chart_id: str):
    """Get one system-level dashboard chart, used for deferred expensive charts."""
    try:
        return await run_in_threadpool(get_system_dashboard_chart_data_payload, dashboard_id, chart_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
