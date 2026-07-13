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

_SYSTEM_DASHBOARD_CACHE_TTL_S = 300.0
_system_dashboard_cache: dict[str, dict] = {}
_WORKSPACE_DASHBOARD_CACHE_TTL_S = 300.0
_workspace_dashboard_cache: dict[str, dict] = {}


_WORKSPACE_DASHBOARD_PROVIDER_BY_ID = {
    "extraction": ExtractionDataProvider,
}


def clear_system_dashboard_data_cache() -> None:
    _system_dashboard_cache.clear()
    _workspace_dashboard_cache.clear()


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


def _is_deferred_chart(chart: object) -> bool:
    options = getattr(chart, "options", None) or {}
    return bool(options.get("defer_initial_load"))


def _deferred_chart_payload() -> dict:
    return {"deferred": True, "loading": True}


def _load_chart_data(provider: object, chart: object):
    if chart.data_source.type == DataSourceType.FUNCTION:
        if chart.data_source.function:
            func_kwargs = chart.options if chart.options else {}
            return _call_provider_function(
                provider,
                chart.data_source.function,
                kwargs=func_kwargs,
            )
        return []

    if chart.data_source.type == DataSourceType.TASK:
        return _call_provider_function(
            provider,
            "get_task_value_distribution",
            kwargs={
                "task_name": chart.id,
                "display_options": chart.options if chart.options else {},
            },
        )

    return []


def _resolve_workspace_scope(conn: sqlite3.Connection, workspace_id: str) -> tuple[str, list[str]]:
    cursor = conn.execute(
        """
        SELECT workspace_folder
        FROM turns
        WHERE workspace_id = ?
          AND workspace_folder IS NOT NULL
          AND workspace_folder != ''
        LIMIT 1
        """,
        (workspace_id,),
    )
    row = cursor.fetchone()
    workspace_folder = row[0] if row and row[0] else ""

    related_ids = {workspace_id}
    if workspace_folder:
        cursor = conn.execute(
            """
            SELECT DISTINCT workspace_id
            FROM workspace_info
            WHERE workspace_folder = ?
              AND workspace_id IS NOT NULL
              AND workspace_id != ''
            """,
            (workspace_folder,),
        )
        related_ids.update(row[0] for row in cursor.fetchall() if row and row[0])

    return workspace_folder or workspace_id, sorted(related_ids)


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
    start_time = time.perf_counter()
    logger.info(f"[PERF] get_dashboard_data_payload | START workspace={workspace_id[:8]} dashboard={dashboard_id}")
    
    run_dir = get_shared_run_dir()
    db_path = get_db_path(Path(run_dir))
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Database not found: {db_path}")
    
    conn = db_schema.connect_db(db_path)
    logger.info(f"[PERF] get_dashboard_data_payload | get_sqlite_connection: {(time.perf_counter()-start_time)*1000:.1f}ms")

    try:
        max_turn_id = 0
        try:
            cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM turns WHERE workspace_id = ?", (workspace_id,))
            max_turn_id = int(cursor.fetchone()[0] or 0)
        except sqlite3.OperationalError:
            max_turn_id = 0

        cache_key = f"{db_path}|{workspace_id}|{dashboard_id}|turns_max_id={max_turn_id}"
        cached = _workspace_dashboard_cache.get(cache_key)
        if cached and (time.monotonic() - float(cached["ts"])) < _WORKSPACE_DASHBOARD_CACHE_TTL_S:
            logger.info(
                f"[PERF] get_dashboard_data_payload | cache_hit workspace={workspace_id[:8]} dashboard={dashboard_id} turns_max_id={max_turn_id}"
            )
            return cached["payload"]

        workspace_folder, related_workspace_ids = _resolve_workspace_scope(conn, workspace_id)
        logger.info(f"[PERF] get_dashboard_data_payload | resolved workspace_folder: {workspace_folder}")
        
        loader = get_dashboard_loader()
        config = loader.load_dashboard(dashboard_id)
        if not config:
            raise HTTPException(status_code=404, detail=f"Dashboard not found: {dashboard_id}")

        provider_cls = _WORKSPACE_DASHBOARD_PROVIDER_BY_ID.get(dashboard_id)
        if not provider_cls:
            raise HTTPException(status_code=400, detail=f"Unsupported workspace dashboard: {dashboard_id}")

        # Pass workspace_folder to provider for cross-agent consolidated queries
        provider = provider_cls(
            conn,
            workspace_id,
            workspace_folder=workspace_folder,
            related_workspace_ids=related_workspace_ids,
        )
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
                if _is_deferred_chart(chart):
                    charts_data[chart.id] = _deferred_chart_payload()
                else:
                    charts_data[chart.id] = _load_chart_data(provider, chart)
                
                logger.info(f"[PERF] get_dashboard_data_payload | chart {chart.id}: {(time.perf_counter()-chart_start)*1000:.1f}ms")

            except Exception as e:
                logger.warning(f"Error fetching chart data for {chart.id}: {e}")
                charts_data[chart.id] = []
        
        logger.info(f"[PERF] get_dashboard_data_payload | TOTAL: {(time.perf_counter()-start_time)*1000:.1f}ms")

        payload = {
            "dashboard_id": dashboard_id,
            "workspace_id": workspace_id,
            "config": config.to_dict(),
            "is_available": True,
            "data": {
                "metrics": metrics_data,
                "charts": charts_data,
            },
        }

        _workspace_dashboard_cache[cache_key] = {
            "ts": time.monotonic(),
            "payload": payload,
        }

        return payload

    finally:
        conn.close()


def reload_dashboards_payload() -> dict:
    loader = get_dashboard_loader()
    loader.clear_cache()
    clear_system_dashboard_data_cache()
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
        max_turn_id = 0
        try:
            cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM turns")
            max_turn_id = int(cursor.fetchone()[0] or 0)
        except sqlite3.OperationalError:
            max_turn_id = 0

        cache_key = f"{db_path}|{dashboard_id}|turns_max_id={max_turn_id}"
        cached = _system_dashboard_cache.get(cache_key)
        if cached and (time.monotonic() - float(cached["ts"])) < _SYSTEM_DASHBOARD_CACHE_TTL_S:
            logger.info(
                f"[PERF] get_system_dashboard_data_payload | cache_hit dashboard={dashboard_id} turns_max_id={max_turn_id}"
            )
            return cached["payload"]

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
                if _is_deferred_chart(chart):
                    charts_data[chart.id] = _deferred_chart_payload()
                else:
                    charts_data[chart.id] = _load_chart_data(provider, chart)
                logger.info(f"[PERF] get_system_dashboard_data_payload | chart {chart.id}: {(time.perf_counter()-chart_start)*1000:.1f}ms")
            except Exception as e:
                logger.error(f"Failed to load chart {chart.id}: {e}")
                charts_data[chart.id] = []

        logger.info(f"[PERF] get_system_dashboard_data_payload | TOTAL: {(time.perf_counter()-start_time)*1000:.1f}ms")

        payload = {
            "dashboard_id": dashboard_id,
            "config": config.to_dict(),
            "is_available": True,
            "data": {
                "metrics": metrics_data,
                "charts": charts_data,
            },
        }

        _system_dashboard_cache[cache_key] = {
            "ts": time.monotonic(),
            "payload": payload,
        }

        return payload

    finally:
        conn.close()


def get_system_dashboard_chart_data_payload(dashboard_id: str, chart_id: str) -> dict:
    """Get one system-level chart payload, used for deferred expensive charts."""
    start_time = time.perf_counter()
    logger.info(
        f"[PERF] get_system_dashboard_chart_data_payload | START dashboard={dashboard_id} chart={chart_id}"
    )

    run_dir = get_shared_run_dir()
    db_path = get_db_path(Path(run_dir))
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Database not found: {db_path}")

    conn = db_schema.connect_db(db_path)
    try:
        loader = get_dashboard_loader()
        config = loader.load_dashboard(dashboard_id)
        if not config:
            raise HTTPException(status_code=404, detail=f"Dashboard not found: {dashboard_id}")

        chart = next((c for c in config.charts if c.id == chart_id), None)
        if not chart:
            raise HTTPException(status_code=404, detail=f"Chart not found: {chart_id}")

        provider = SystemDataProvider(conn)
        data = _load_chart_data(provider, chart)
        logger.info(
            f"[PERF] get_system_dashboard_chart_data_payload | TOTAL: {(time.perf_counter()-start_time)*1000:.1f}ms"
        )
        return {
            "dashboard_id": dashboard_id,
            "chart_id": chart_id,
            "data": data,
        }
    finally:
        conn.close()
