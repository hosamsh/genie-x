"""Extraction pipeline orchestrator.

Main entry points for workspace extraction with storage and reporting.
"""

import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.shared.database.db_schema import init_shared_db
from src.shared.io.run_dir import get_db_path
from src.shared.logging.logger import get_logger
from src.shared.models.workspace import WorkspaceExtractionResult
from src.shared.workspace_discovery import (
    find_workspace,
    prime_workspace_scan_cache,
    clear_workspace_scan_cache,
)
from .embedding_postprocess import run_embedding_postprocess
from .extractor import extract_workspace as _extract_workspace_data
from .storage import store_extraction_result

logger = get_logger(__name__)


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def init_run_directory(run_dir: Optional[str] = None) -> Path:
    """Initialize a run directory with database.
    
    Args:
        run_dir: Optional user-specified directory path. If None, creates a
                 timestamped directory under data/runs/.
    
    Returns:
        Path to the initialized run directory.
    """
    if run_dir:
        path = Path(run_dir)
        is_new = not path.exists()
    else:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = Path("data/runs") / timestamp
        is_new = True

    # Create the run directory
    path.mkdir(parents=True, exist_ok=True)

    db_path = get_db_path(path)
    if not db_path.exists():
        conn = init_shared_db(db_path, verbose=False)
        conn.close()

    if is_new:
        logger.progress(f"[OK] Created run directory: {path}")
    else:
        logger.progress(f"[OK] Using existing run directory: {path}")
    logger.progress(f"   [DB] Database: {db_path.name}\n")

    return path


def extract_single_workspace(
    workspace_id: str,
    db_path: Path,
    force_refresh: bool = False,
    agent_filter: Optional[str] = None,
) -> WorkspaceExtractionResult:
    """Extract workspace data and store to database.
    
    Convenience function that combines extraction + storage in one call.
    
    Args:
        workspace_id: The workspace ID to extract.
        db_path: Path to the database file.
        force_refresh: If True, delete existing data before storing.
        agent_filter: Optional agent name to extract from only.
        
    Returns:
        WorkspaceExtractionResult with extraction status and metadata.
    """
    timer = time.time()
    perf_start = time.perf_counter()
    duration_minutes = lambda: round((time.time() - timer) / 60, 2)

    logger.banner(f"Extracting Workspace: {workspace_id}")

    checkpoint = time.perf_counter()
    ws_storage = find_workspace(workspace_id)
    logger.info(f"[PERF] extract_single_workspace {workspace_id} | find_workspace: {_elapsed_ms(checkpoint):.1f}ms")
    if not ws_storage:
        logger.error(f"Workspace not found: {workspace_id}")
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

    agent_name = ", ".join(ws_storage.agents) if ws_storage.agents else "unknown"
    logger.progress(f"Found workspace in: {agent_name}")

    try:
        checkpoint = time.perf_counter()
        extraction_result = _extract_workspace_data(workspace_id, agent_filter=agent_filter)
        logger.info(
            f"[PERF] extract_single_workspace {workspace_id} | extract_workspace: {_elapsed_ms(checkpoint):.1f}ms"
        )
        logger.progress(
            f"[OK] Extracted {extraction_result.session_count} sessions, "
            f"{extraction_result.turn_count} turns"
        )

        checkpoint = time.perf_counter()
        storage_result = store_extraction_result(extraction_result, db_path, force_refresh)
        logger.info(
            f"[PERF] extract_single_workspace {workspace_id} | store_extraction_result: {_elapsed_ms(checkpoint):.1f}ms"
        )
        if not storage_result.success:
            raise RuntimeError(f"Storage failed: {storage_result.error}")

        checkpoint = time.perf_counter()
        run_embedding_postprocess(storage_result, db_path)
        logger.info(
            f"[PERF] extract_single_workspace {workspace_id} | embedding_postprocess: "
            f"{_elapsed_ms(checkpoint):.1f}ms"
        )

        _print_workspace_banner(
            workspace_id,
            storage_result.workspace_name,
            storage_result.workspace_folder,
            storage_result.session_count,
            storage_result.turn_count,
        )

        logger.info(f"[PERF] extract_single_workspace {workspace_id} | TOTAL: {_elapsed_ms(perf_start):.1f}ms")
        return storage_result

    except Exception as exc:
        logger.exception(f"Extract and store failed for {workspace_id}: {exc}")
        return WorkspaceExtractionResult(
            status="failed",
            workspace_id=workspace_id,
            workspace_name=ws_storage.workspace_name or "N/A",
            workspace_folder=ws_storage.workspace_folder or "N/A",
            session_count=0,
            turn_count=0,
            combined_count=0,
            duration_ms=int(time.time() * 1000 - (timer * 1000)),
            error=str(exc),
        )


async def extract_workspaces(
    workspace_ids: List[str],
    run_dir: str,
    force_refresh: bool = False,
    agent_filter: Optional[str] = None,
) -> Dict[str, WorkspaceExtractionResult]:
    """Extract and store multiple workspaces.
    
    Args:
        workspace_ids: List of workspace IDs to extract.
        run_dir: Run directory path (string). Will be initialized if needed.
        force_refresh: If True, delete existing data before storing.
        agent_filter: Optional agent name to extract from only.
        
    Returns:
        Dict mapping workspace_id to WorkspaceExtractionResult.
    """
    # Initialize run directory and database
    perf_start = time.perf_counter()
    checkpoint = time.perf_counter()
    run_path = init_run_directory(run_dir)
    logger.info(f"[PERF] extract_workspaces | init_run_directory: {_elapsed_ms(checkpoint):.1f}ms")
    
    stats: Dict[str, WorkspaceExtractionResult] = {}
    successful = skipped = failed = 0
    pipeline_start = datetime.now()
    db_path = get_db_path(run_path)

    # Enable a shared per-agent scan cache for multi-workspace batches so we
    # scan each agent once instead of re-scanning for every workspace.
    use_scan_cache = len(workspace_ids) > 1
    if use_scan_cache:
        prime_workspace_scan_cache()
    try:
        for i, workspace_id in enumerate(workspace_ids, 1):
            logger.banner(f"Starting to process workspace {i}/{len(workspace_ids)}: {workspace_id}")
            start_time = time.time()
            workspace_perf_start = time.perf_counter()
            duration = lambda: round((time.time() - start_time) / 60, 2)

            # Call the single-workspace function
            result = extract_single_workspace(
                workspace_id=workspace_id,
                db_path=db_path,
                force_refresh=force_refresh,
                agent_filter=agent_filter,
            )

            # Check status and categorize
            if result.status == "failed":
                failed += 1
                logger.error(f"[FAILED] {workspace_id}: {result.error or 'Unknown error'}")
            elif result.session_count == 0 or result.turn_count == 0:
                logger.progress(f"[SKIP] Workspace {workspace_id} has no conversation data")
                result.status = "skipped"
                result.reason = "no_sessions"
                skipped += 1
            else:
                successful += 1

            stats[workspace_id] = result
            logger.info(
                f"[PERF] extract_workspaces | workspace {workspace_id}: {_elapsed_ms(workspace_perf_start):.1f}ms"
            )
    finally:
        if use_scan_cache:
            clear_workspace_scan_cache()

    pipeline_time = (datetime.now() - pipeline_start).total_seconds()
    logger.info(f"[PERF] extract_workspaces | TOTAL: {_elapsed_ms(perf_start):.1f}ms")
    _print_summary(run_path, workspace_ids, successful, skipped, failed, pipeline_time)
    return stats


def _print_summary(
    run_dir: Path,
    workspace_ids: List[str],
    successful: int,
    skipped: int,
    failed: int,
    pipeline_time: float,
) -> None:
    """Print a summary of the pipeline run."""
    logger.banner("Pipeline Complete")
    logger.progress(f"Run directory: {run_dir}")
    logger.progress(f"Workspaces processed: {len(workspace_ids)}")
    logger.progress(f"  Successful: {successful}")
    logger.progress(f"  Skipped: {skipped}")
    logger.progress(f"  Failed: {failed}")
    logger.progress(f"Total time: {pipeline_time / 60:.2f} minutes")


def _print_workspace_banner(
    workspace_id: str,
    name: str,
    folder: str,
    session_count: int,
    turn_count: int,
) -> None:
    """Print workspace information banner."""
    logger.banner(f"Processing Workspace: {workspace_id}")
    logger.progress(f"Workspace Name: {name}")
    logger.progress(f"Workspace Folder: {folder}")
    logger.progress(f"Sessions: {session_count}")
    logger.progress(f"Total Turns: {turn_count:,}\n")
