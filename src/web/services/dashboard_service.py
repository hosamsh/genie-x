from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException

from src.shared.database import db_schema
from src.shared.io.run_dir import get_db_path
from src.shared.logging.logger import get_logger
from src.web.data_providers.extraction_provider import ExtractionDataProvider
from src.web.data_providers.system_provider import SystemDataProvider
from src.web.dashboards.loader import DataSourceType, get_dashboard_loader
from src.web.shared_state import get_shared_run_dir

logger = get_logger(__name__)


_WORKSPACE_DASHBOARD_PROVIDER_BY_ID = {
    "extraction": ExtractionDataProvider,
}


def _call_provider_function(
    provider: object,
    function_name: str,
    *,
    kwargs: dict | None = None,
):
    func = getattr(provider, function_name, None)
    if not callable(func):
        raise ValueError(f"Unknown provider function: {function_name}")

    if kwargs:
        return func(**kwargs)
    return func()


def list_dashboards_payload() -> dict:
    loader = get_dashboard_loader()
    dashboards = loader.load_all_dashboards()
    return {
        "dashboards": [
            {
                "id": d.dashboard.id,
                "name": d.dashboard.name,
                "description": d.dashboard.description,
                "icon": d.dashboard.icon,
            }
            for d in dashboards
        ]
    }


def get_dashboard_config_payload(dashboard_id: str) -> dict:
    loader = get_dashboard_loader()
    config = loader.load_dashboard(dashboard_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Dashboard not found: {dashboard_id}")
    return config.to_dict()


def get_dashboard_data_payload(workspace_id: str, dashboard_id: str) -> dict:
    from src.web.shared_state import resolve_workspace_folder
    
    start_time = time.perf_counter()
    logger.info(f"[PERF] get_dashboard_data_payload | START workspace={workspace_id[:8]} dashboard={dashboard_id}")
    
    run_dir = get_shared_run_dir()
    db_path = get_db_path(Path(run_dir))
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Database not found: {db_path}")
    
    conn = db_schema.connect_db(db_path)
    logger.info(f"[PERF] get_dashboard_data_payload | get_sqlite_connection: {(time.perf_counter()-start_time)*1000:.1f}ms")

    try:
        # Resolve workspace_folder for cross-agent consolidation
        workspace_folder = resolve_workspace_folder(workspace_id)
        logger.info(f"[PERF] get_dashboard_data_payload | resolved workspace_folder: {workspace_folder}")
        
        loader = get_dashboard_loader()
        config = loader.load_dashboard(dashboard_id)
        if not config:
            raise HTTPException(status_code=404, detail=f"Dashboard not found: {dashboard_id}")

        provider_cls = _WORKSPACE_DASHBOARD_PROVIDER_BY_ID.get(dashboard_id)
        if not provider_cls:
            raise HTTPException(status_code=400, detail=f"Unsupported workspace dashboard: {dashboard_id}")

        # Pass workspace_folder to provider for cross-agent consolidated queries
        provider = provider_cls(conn, workspace_id, workspace_folder=workspace_folder)
        logger.info(
            f"[PERF] get_dashboard_data_payload | create_provider: {(time.perf_counter()-start_time)*1000:.1f}ms"
        )

        metrics_data = {}
        logger.info(f"[PERF] get_dashboard_data_payload | loading {len(config.metrics)} metrics, {len(config.charts)} charts")
        for metric in config.metrics:
            try:
                metric_start = time.perf_counter()
                if metric.data_source.function:
                    result = _call_provider_function(
                        provider,
                        metric.data_source.function,
                    )
                    if metric.data_source.field:
                        metrics_data[metric.id] = result.get(metric.data_source.field)
                    else:
                        metrics_data[metric.id] = result
                    # Extract subtitle_field if configured
                    if metric.subtitle_field and isinstance(result, dict):
                        metrics_data[metric.id + '_subtitle'] = result.get(metric.subtitle_field)
                logger.info(f"[PERF] get_dashboard_data_payload | metric {metric.id}: {(time.perf_counter()-metric_start)*1000:.1f}ms")
            except Exception as e:
                logger.warning(f"Error fetching metric {metric.id}: {e}")
                metrics_data[metric.id] = None
        
        logger.info(f"[PERF] get_dashboard_data_payload | all_metrics: {(time.perf_counter()-start_time)*1000:.1f}ms")

        charts_data = {}
        for chart in config.charts:
            try:
                chart_start = time.perf_counter()
                if chart.data_source.type == DataSourceType.FUNCTION:
                    if chart.data_source.function:
                        # Pass chart options to the provider function (functions should accept **kwargs)
                        func_kwargs = chart.options if chart.options else {}
                        charts_data[chart.id] = _call_provider_function(
                            provider,
                            chart.data_source.function,
                            kwargs=func_kwargs,
                        )

                elif chart.data_source.type == DataSourceType.TASK:
                    task_name = chart.id
                    distribution = _call_provider_function(
                        provider,
                        "get_task_value_distribution",
                        kwargs={
                            "task_name": task_name,
                            "display_options": chart.options if chart.options else {},
                        },
                    )
                    charts_data[chart.id] = distribution
                
                logger.info(f"[PERF] get_dashboard_data_payload | chart {chart.id}: {(time.perf_counter()-chart_start)*1000:.1f}ms")

            except Exception as e:
                logger.warning(f"Error fetching chart data for {chart.id}: {e}")
                charts_data[chart.id] = []
        
        logger.info(f"[PERF] get_dashboard_data_payload | TOTAL: {(time.perf_counter()-start_time)*1000:.1f}ms")

        return {
            "dashboard_id": dashboard_id,
            "workspace_id": workspace_id,
            "config": config.to_dict(),
            "is_available": True,
            "data": {
                "metrics": metrics_data,
                "charts": charts_data,
            },
        }

    finally:
        conn.close()


def reload_dashboards_payload() -> dict:
    loader = get_dashboard_loader()
    loader.clear_cache()
    return {"status": "ok", "message": "Dashboard configurations reloaded"}


def get_system_dashboard_data_payload(dashboard_id: str) -> dict:
    """
    Get dashboard data for system-level dashboards (not workspace-specific).
    Used for dashboards with is_system_level=true in their config.
    """
    start_time = time.perf_counter()
    logger.info(f"[PERF] get_system_dashboard_data_payload | START dashboard={dashboard_id}")
    
    run_dir = get_shared_run_dir()
    db_path = get_db_path(Path(run_dir))
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Database not found: {db_path}")
    
    conn = db_schema.connect_db(db_path)
    logger.info(f"[PERF] get_system_dashboard_data_payload | get_sqlite_connection: {(time.perf_counter()-start_time)*1000:.1f}ms")

    try:
        loader = get_dashboard_loader()
        config = loader.load_dashboard(dashboard_id)
        if not config:
            raise HTTPException(status_code=404, detail=f"Dashboard not found: {dashboard_id}")

        provider = SystemDataProvider(conn)
        logger.info(f"[PERF] get_system_dashboard_data_payload | create_provider: {(time.perf_counter()-start_time)*1000:.1f}ms")

        # Check if we have any extracted workspaces
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM workspaces")
            total_workspaces = cursor.fetchone()[0]
            if total_workspaces == 0:
                return {
                    "dashboard_id": dashboard_id,
                    "config": config.to_dict(),
                    "is_available": False,
                    "message": "No workspaces have been extracted yet.",
                }
        except sqlite3.OperationalError:
            pass  # Continue anyway if check fails

        metrics_data = {}
        logger.info(f"[PERF] get_system_dashboard_data_payload | loading {len(config.metrics)} metrics, {len(config.charts)} charts")
        for metric in config.metrics:
            try:
                metric_start = time.perf_counter()
                if metric.data_source.function:
                    result = _call_provider_function(
                        provider,
                        metric.data_source.function,
                    )
                    if metric.data_source.field:
                        metrics_data[metric.id] = result.get(metric.data_source.field)
                    else:
                        metrics_data[metric.id] = result
                    # Extract subtitle_field if configured
                    if metric.subtitle_field and isinstance(result, dict):
                        metrics_data[metric.id + '_subtitle'] = result.get(metric.subtitle_field)
                logger.info(f"[PERF] get_system_dashboard_data_payload | metric {metric.id}: {(time.perf_counter()-metric_start)*1000:.1f}ms")
            except Exception as e:
                logger.error(f"Failed to load metric {metric.id}: {e}")
                metrics_data[metric.id] = None

        charts_data = {}
        for chart in config.charts:
            try:
                chart_start = time.perf_counter()
                if chart.data_source.function:
                    # Pass chart options to the provider function (functions should accept **kwargs)
                    func_kwargs = chart.options if chart.options else {}
                    charts_data[chart.id] = _call_provider_function(
                        provider,
                        chart.data_source.function,
                        kwargs=func_kwargs,
                    )
                logger.info(f"[PERF] get_system_dashboard_data_payload | chart {chart.id}: {(time.perf_counter()-chart_start)*1000:.1f}ms")
            except Exception as e:
                logger.error(f"Failed to load chart {chart.id}: {e}")
                charts_data[chart.id] = []

        logger.info(f"[PERF] get_system_dashboard_data_payload | TOTAL: {(time.perf_counter()-start_time)*1000:.1f}ms")

        return {
            "dashboard_id": dashboard_id,
            "config": config.to_dict(),
            "is_available": True,
            "data": {
                "metrics": metrics_data,
                "charts": charts_data,
            },
        }

    finally:
        conn.close()
