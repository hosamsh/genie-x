"""Database storage for extraction results.

Handles persisting extracted workspace data to the database.
"""

import time
from pathlib import Path

from typing import cast, List

from src.shared.database.db_extract import upsert_workspace_info, upsert_metrics, upsert_turns, delete_workspace_extraction
from src.shared.database.db_schema import init_shared_db
from src.shared.code.loc_counter import count_loc_safe
from src.shared.logging.logger import get_logger
from src.shared.models.turn import EnrichedTurn
from src.shared.models.workspace import WorkspaceExtractionResult
from src.shared.models.workspace import ExtractedWorkspace
from src.shared.config.config_loader import get_config
from src.shared.search.search_indexer import generate_embeddings
from .workspace_discovery import find_workspace

logger = get_logger(__name__)


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
    
    # Get workspace metadata from extraction result
    workspace_id = extraction_result.workspace_id
    
    # Find workspace info for name and folder
    ws_storage = find_workspace(workspace_id)
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
    
    conn = init_shared_db(db_path, verbose=False)
    try:
        # Handle force refresh
        if force_refresh:
            logger.progress("[REFRESH] Deleting existing extraction data...")
            deleted = delete_workspace_extraction(conn, workspace_id)
            for table, count in deleted.items():
                if count > 0:
                    logger.progress(f"   Deleted {count:,} rows from {table}")
            logger.progress("")
        
        # Persist turns (combined_turns view will auto-generate from this)
        inserted_count = 0
        min_turn_id = None
        max_turn_id = None
        if extraction_result.turns:
            # Get turn ID range before insertion to track newly inserted turns
            cursor = conn.cursor()
            cursor.execute("SELECT COALESCE(MAX(id), 0) FROM turns")
            min_turn_id = cursor.fetchone()[0] + 1
            
            inserted_count = upsert_turns(conn, cast(List[EnrichedTurn], extraction_result.turns))
            
            cursor.execute("SELECT COALESCE(MAX(id), 0) FROM turns")
            max_turn_id = cursor.fetchone()[0]
        
        # Persist code metrics (used by combined_turns view for code_edits)
        if extraction_result.code_metrics:
            metrics_count = upsert_metrics(conn, extraction_result.code_metrics)
            logger.progress(f"[OK] Inserted {metrics_count} pre-extracted code metrics")
        
        conn.commit()
        
        # Auto-generate embeddings for newly inserted turns if enabled
        config = get_config()
        if config.search.auto_embed_on_extraction and inserted_count > 0 and min_turn_id and max_turn_id:
            try:
                generate_embeddings(
                    conn,
                    model_name=config.search.semantic_model,
                    batch_size=config.search.embedding_batch_size,
                    min_turn_id=min_turn_id,
                    max_turn_id=max_turn_id,
                    verbose=True,
                )
            except (ValueError, RuntimeError, OSError):
                # Error already logged by generate_embeddings, continue with extraction
                pass
        
        # Count combined turns from the view (auto-generated, no insertion needed)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM combined_turns WHERE workspace_id = ?",
            (workspace_id,)
        )
        combined_count = cursor.fetchone()[0]
        
        duration_ms = int(time.time() * 1000 - start_ms)
        logger.progress(
            f"[OK] Inserted {inserted_count} turns, {combined_count} combined exchanges "
            f"(auto-generated from view)"
        )
        
        # Count lines of code
        total_code_loc, total_doc_loc = 0, 0
        if inserted_count > 0:
            logger.progress(f"Counting lines of code in: {folder}...")
            total_code_loc, total_doc_loc = count_loc_safe(folder)
            logger.progress(f"[OK] Code LOC: {total_code_loc:,}, Doc LOC: {total_doc_loc:,}")
        
        # Update workspace metadata
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
        conn.commit()
        
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
