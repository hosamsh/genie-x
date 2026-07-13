"""Database storage for extraction results.

Handles persisting extracted workspace data to the database.
"""

import time
from pathlib import Path

from typing import cast, List

from src.extract.models import ParsedWorkspace
from src.shared.database.db_parsed import delete_parsed_workspace, upsert_parsed_workspace
from src.shared.database.db_extract import upsert_workspace_info, upsert_metrics, upsert_turns, delete_workspace_extraction, replace_turn_detail_rows
from src.shared.database.db_schema import init_shared_db
from src.shared.code.loc_counter import count_loc_safe
from src.shared.logging.logger import get_logger
from src.shared.models.turn import EnrichedTurn
from src.shared.models.workspace import WorkspaceExtractionResult
from src.shared.models.workspace import ExtractedWorkspace
from src.shared.workspace_discovery import find_workspace
from src.web.shared_state import clear_workspace_metadata_caches
from src.web.services.dashboard_service import clear_system_dashboard_data_cache

logger = get_logger(__name__)


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def store_extraction_result(
    extraction_result: ExtractedWorkspace,
    db_path: Path,
    force_refresh: bool = False,
) -> WorkspaceExtractionResult:
    """Store extraction result to database.
    
    Args:
        extraction_result: ExtractedWorkspace from extract_workspace()
        db_path: Path to the database file
        force_refresh: Whether to delete existing data before storing
        
    Returns:
        WorkspaceExtractionResult with success status and metadata
    """
    start_ms = time.time() * 1000
    perf_start = time.perf_counter()
    
    # Get workspace metadata from extraction result
    workspace_id = extraction_result.workspace_id
    
    # Find workspace info for name and folder
    checkpoint = time.perf_counter()
    ws_storage = find_workspace(workspace_id)
    logger.info(f"[PERF] store_extraction_result {workspace_id} | find_workspace: {_elapsed_ms(checkpoint):.1f}ms")
    if not ws_storage:
        return WorkspaceExtractionResult(
            status="failed",
            workspace_id=workspace_id,
            workspace_name="",
            workspace_folder="",
            session_count=0,
            turn_count=0,
            combined_count=0,
            duration_ms=0,
            error="workspace_not_found",
        )
    
    name = ws_storage.workspace_name or "N/A"
    folder = ws_storage.workspace_folder or "N/A"

    for turn in extraction_result.turns:
        turn.workspace_id = workspace_id
        turn.workspace_name = name
        turn.workspace_folder = folder

    parsed_workspaces = extraction_result.source_artifacts.get("parsed_workspaces", []) if extraction_result.source_artifacts else []
    for parsed_workspace in parsed_workspaces:
        if not isinstance(parsed_workspace, ParsedWorkspace):
            continue
        parsed_workspace.descriptor.workspace_id = workspace_id
        parsed_workspace.descriptor.workspace_name = name
        parsed_workspace.descriptor.workspace_folder = folder
        for session in parsed_workspace.sessions:
            session.workspace_id = workspace_id
            session.workspace_name = name
            session.workspace_folder = folder
    
    checkpoint = time.perf_counter()
    conn = init_shared_db(db_path, verbose=False)
    logger.info(f"[PERF] store_extraction_result {workspace_id} | init_shared_db: {_elapsed_ms(checkpoint):.1f}ms")
    try:
        # Handle force refresh
        if force_refresh:
            checkpoint = time.perf_counter()
            logger.progress("[REFRESH] Deleting existing extraction data...")
            deleted = delete_workspace_extraction(conn, workspace_id)
            for table, count in deleted.items():
                if count > 0:
                    logger.progress(f"   Deleted {count:,} rows from {table}")

            for parsed_workspace in parsed_workspaces:
                if not isinstance(parsed_workspace, ParsedWorkspace):
                    continue
                delete_parsed_workspace(
                    conn,
                    parsed_workspace.descriptor.workspace_id,
                    parsed_workspace.descriptor.agent_name,
                )
            logger.progress("")
            logger.info(
                f"[PERF] store_extraction_result {workspace_id} | force_refresh_delete: "
                f"{_elapsed_ms(checkpoint):.1f}ms"
            )
        
        # Persist turns (combined_turns view will auto-generate from this)
        inserted_count = 0
        min_turn_id = None
        max_turn_id = None
        if extraction_result.turns:
            checkpoint = time.perf_counter()
            # Get turn ID range before insertion to track newly inserted turns
            cursor = conn.cursor()
            cursor.execute("SELECT COALESCE(MAX(id), 0) FROM turns")
            min_turn_id = cursor.fetchone()[0] + 1
            
            inserted_count = upsert_turns(conn, cast(List[EnrichedTurn], extraction_result.turns))
            
            cursor.execute("SELECT COALESCE(MAX(id), 0) FROM turns")
            max_turn_id = cursor.fetchone()[0]
            logger.info(
                f"[PERF] store_extraction_result {workspace_id} | upsert_turns: "
                f"{_elapsed_ms(checkpoint):.1f}ms ({inserted_count} turns)"
            )

        checkpoint = time.perf_counter()
        detail_counts = replace_turn_detail_rows(conn, workspace_id, cast(List[EnrichedTurn], extraction_result.turns))
        logger.info(
            f"[PERF] store_extraction_result {workspace_id} | replace_turn_detail_rows: "
            f"{_elapsed_ms(checkpoint):.1f}ms "
            f"({detail_counts.get('tool_calls', 0)} tool calls, {detail_counts.get('subagents', 0)} subagents)"
        )
        logger.debug(
            "Stored turn details for %s: %s tool runs, %s subagent refs",
            workspace_id,
            detail_counts.get("tool_calls", 0),
            detail_counts.get("subagents", 0),
        )
        
        # Persist code metrics (used by combined_turns view for code_edits)
        if extraction_result.code_metrics:
            checkpoint = time.perf_counter()
            metrics_count = upsert_metrics(conn, extraction_result.code_metrics)
            logger.progress(f"[OK] Inserted {metrics_count} pre-extracted code metrics")
            logger.info(
                f"[PERF] store_extraction_result {workspace_id} | upsert_pre_extracted_metrics: "
                f"{_elapsed_ms(checkpoint):.1f}ms ({metrics_count} metrics)"
            )

        if parsed_workspaces:
            checkpoint = time.perf_counter()
            raw_counts: list[dict[str, int]] = []
            for parsed_workspace in parsed_workspaces:
                if not isinstance(parsed_workspace, ParsedWorkspace):
                    continue
                raw_counts.append(upsert_parsed_workspace(conn, parsed_workspace))
            if raw_counts:
                total_raw_sessions = sum(item.get("sessions", 0) for item in raw_counts)
                total_raw_events = sum(item.get("events", 0) for item in raw_counts)
                logger.progress(
                    f"[OK] Stored {total_raw_sessions} parsed raw sessions, {total_raw_events} raw events"
                )
                logger.info(
                    f"[PERF] store_extraction_result {workspace_id} | upsert_parsed_workspaces: "
                    f"{_elapsed_ms(checkpoint):.1f}ms ({total_raw_sessions} sessions, {total_raw_events} events)"
                )
        
        checkpoint = time.perf_counter()
        conn.commit()
        logger.info(f"[PERF] store_extraction_result {workspace_id} | commit_extraction_rows: {_elapsed_ms(checkpoint):.1f}ms")
        
        # Count combined turns from the view (auto-generated, no insertion needed)
        checkpoint = time.perf_counter()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM combined_turns WHERE workspace_id = ?",
            (workspace_id,)
        )
        combined_count = cursor.fetchone()[0]
        logger.info(
            f"[PERF] store_extraction_result {workspace_id} | count_combined_turns: "
            f"{_elapsed_ms(checkpoint):.1f}ms ({combined_count} combined)"
        )
        
        duration_ms = int(time.time() * 1000 - start_ms)
        logger.progress(
            f"[OK] Inserted {inserted_count} turns, {combined_count} combined exchanges "
            f"(auto-generated from view)"
        )
        
        # Count lines of code
        total_code_loc, total_doc_loc = 0, 0
        if inserted_count > 0:
            logger.progress(f"Counting lines of code in: {folder}...")
            checkpoint = time.perf_counter()
            total_code_loc, total_doc_loc = count_loc_safe(folder)
            logger.info(
                f"[PERF] store_extraction_result {workspace_id} | count_loc_safe: "
                f"{_elapsed_ms(checkpoint):.1f}ms"
            )
            logger.progress(f"[OK] Code LOC: {total_code_loc:,}, Doc LOC: {total_doc_loc:,}")
        
        # Update workspace metadata
        checkpoint = time.perf_counter()
        upsert_workspace_info(
            conn=conn,
            workspace_id=workspace_id,
            workspace_name=name,
            workspace_folder=folder,
            agent_used=extraction_result.agent_name,
            extraction_duration_ms=duration_ms,
            session_count=extraction_result.session_count,
            turn_count=extraction_result.turn_count,
            total_code_loc=total_code_loc,
            total_doc_loc=total_doc_loc,
        )
        logger.info(f"[PERF] store_extraction_result {workspace_id} | upsert_workspace_info: {_elapsed_ms(checkpoint):.1f}ms")
        checkpoint = time.perf_counter()
        conn.commit()
        logger.info(f"[PERF] store_extraction_result {workspace_id} | commit_workspace_info: {_elapsed_ms(checkpoint):.1f}ms")
        checkpoint = time.perf_counter()
        clear_workspace_metadata_caches()
        clear_system_dashboard_data_cache()
        logger.info(f"[PERF] store_extraction_result {workspace_id} | clear_caches: {_elapsed_ms(checkpoint):.1f}ms")
        logger.info(f"[PERF] store_extraction_result {workspace_id} | TOTAL: {_elapsed_ms(perf_start):.1f}ms")
        
        return WorkspaceExtractionResult(
            status="success",
            workspace_id=workspace_id,
            workspace_name=name,
            workspace_folder=folder,
            session_count=extraction_result.session_count,
            turn_count=extraction_result.turn_count,
            combined_count=combined_count,
            duration_ms=duration_ms,
            total_code_loc=total_code_loc,
            total_doc_loc=total_doc_loc,
            embedding_min_turn_id=min_turn_id,
            embedding_max_turn_id=max_turn_id,
        )
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Storage failed: {e}")
        return WorkspaceExtractionResult(
            status="failed",
            workspace_id=workspace_id,
            workspace_name=name,
            workspace_folder=folder,
            session_count=0,
            turn_count=0,
            combined_count=0,
            duration_ms=int(time.time() * 1000 - start_ms),
            error=str(e),
        )
    finally:
        conn.close()
