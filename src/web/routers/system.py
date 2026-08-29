"""
System Overview API Router

Provides endpoints for system-wide metrics and overview data.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from src.shared.database import db_schema
from src.shared.io.run_dir import get_db_path
from src.shared.logging.logger import get_logger
from src.web.data_providers.system_provider import SystemDataProvider
from src.web.shared_state import get_shared_run_dir
from src.__version__ import __version__

logger = get_logger(__name__)
router = APIRouter()


@router.get("/api/version")
async def get_version():
    """Get application version."""
    return {"version": __version__}


@router.get("/api/system/stats")
async def get_system_stats():
    """Get basic system statistics."""
    try:
        run_dir = get_shared_run_dir()
        db_path = get_db_path(Path(run_dir))
        # connect_db creates and initializes an empty schema if the DB does
        # not yet exist, so a fresh install returns zeroed stats instead of 500.
        conn = db_schema.connect_db(db_path)
        
        try:
            provider = SystemDataProvider(conn)
            return provider.get_system_stats()
        finally:
            conn.close()
            
    except Exception as e:
        logger.exception("Failed to get system stats")
        raise HTTPException(status_code=500, detail=str(e)) from e


