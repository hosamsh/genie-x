"""
Run the full pipeline for all sessions in a workspace.

This is the CLI entry point. Core functionality lives in src/pipeline/.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.shared.logging.logger import get_logger, setup_logging
from src.shared.config.config_loader import load_env, get_config
from src.shared.io.run_dir import require_db_path
from src.pipeline.extraction.workspace_discovery import (
    list_all_workspaces,
    list_workspaces_by_page,
    find_workspace,
)
from src.pipeline.extraction.orchestrator import extract_workspaces
from src.__version__ import __version__

# Initialize logging at module level
setup_logging()
logger = get_logger("run_cli")

EXAMPLES = """\
Examples:
  python run_cli.py --list
  python run_cli.py --extract abc123-def456-ghi789 --run-dir data/runs/my-run
  python run_cli.py --extract --all --run-dir data/runs/all-workspaces
  python run_cli.py --extract abc123 def456 --run-dir data/runs/multi
  python run_cli.py --extract --all --agent cursor --run-dir data/cursor-runs
  python run_cli.py --search "koko nutty" --run-dir data/runs/my-run
  python run_cli.py --search "koko nutty" --search-mode semantic --assistant-only --run-dir data/runs/my-run --json
  python run_cli.py --reindex --run-dir data/runs/my-run
"""


def _require_run_dir(args: argparse.Namespace, option: str) -> Path:
    """Validate that --run-dir is provided and return the path."""
    if not args.run_dir:
        logger.error(f"[ERROR] {option} requires --run-dir <run_folder>")
        sys.exit(1)
    return Path(args.run_dir)


# -------------------- CLI Argument Parsing --------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the full pipeline for workspace(s)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    
    # Version flag
    parser.add_argument(
        "--version", "-v", action="version",
        version=f"genie-x {__version__}",
        help="Show version and exit"
    )
    
    # Config
    parser.add_argument(
        "--config", type=str,
        help="Path to config file (default: config/config.yaml)"
    )
    
    # Main commands
    parser.add_argument(
        "--list-workspaces", "--list", nargs="*", metavar=("PAGE", "SIZE"),
        dest="list_workspaces",
        help="List available workspaces (optional: page number and page size)",
    )
    parser.add_argument("--json", action="store_true", help="Output in JSON format (for list/search)")
    
    parser.add_argument(
        "--extract", nargs="*", metavar="WORKSPACE_ID",
        help="Extract workspace(s). Provide workspace IDs or use with --all"
    )
    
    # Workspace selection
    parser.add_argument("--all", action="store_true", help="Process all workspaces (use with --extract)")
    parser.add_argument("--agent", type=str, help="Filter to specific agent (e.g., 'copilot', 'cursor')")
    
    # Run configuration
    parser.add_argument("--run-dir", type=str, help="Run directory (required for --extract)")
    parser.add_argument("--force-refresh", action="store_true", help="Force reprocessing of all turns")

    # Search
    parser.add_argument("--search", type=str, help="Search turns by keyword/semantic/hybrid")
    parser.add_argument(
        "--search-mode",
        type=str,
        choices=["hybrid", "keyword", "semantic"],
        help="Search mode (default from config)",
    )
    parser.add_argument("--user-only", action="store_true", help="Search user turns only")
    parser.add_argument("--assistant-only", action="store_true", help="Search assistant turns only")
    parser.add_argument("--page", type=int, default=1, help="Search results page number")
    parser.add_argument("--page-size", type=int, default=20, help="Search results page size")
    parser.add_argument("--min-score", type=float, help="Minimum semantic similarity score (0-1)")
    parser.add_argument("--strict", action="store_true", help="Use stricter semantic threshold")

    # Search index maintenance
    parser.add_argument("--reindex", action="store_true", help="Rebuild search indices (FTS + embeddings)")
    parser.add_argument("--embedding-model", type=str, help="Override embedding model for indexing")
    parser.add_argument("--embedding-batch-size", type=int, help="Override embedding batch size")
    
    return parser.parse_args(argv)


# -------------------- CLI Command Handlers --------------------

def _print_workspaces(workspaces, page: int, total_count: int, page_size: int, as_json: bool = False) -> None:
    """Print workspaces in a formatted table or as JSON."""
    import json
    
    if as_json:
        output = {
            "page": page,
            "total_count": total_count,
            "page_size": page_size,
            "workspaces": [
                {
                    "workspace_id": ws.workspace_id,
                    "name": ws.workspace_name,
                    "agents": ws.agents,
                    "session_count": ws.session_count,
                    "workspace_folder": ws.workspace_folder,
                }
                for ws in workspaces
            ]
        }
        print(json.dumps(output, indent=2))
        return

    logger.progress(f"\nAvailable workspaces (page {page}, showing {len(workspaces)} of {total_count}):")
    logger.progress("")
    logger.progress(f"{'ID':<40} {'Name':<30} {'Agent':<10} {'Sessions':<10}")
    logger.progress("-" * 90)

    for ws in workspaces:
        ws_id = ws.workspace_id
        name = (ws.workspace_name or "N/A")[:28]
        agent = ", ".join(ws.agents) if ws.agents else "unknown"
        sessions = ws.session_count
        logger.progress(f"{ws_id:<40} {name:<30} {agent:<10} {sessions:<10}")

    logger.progress("")
    if total_count > page_size:
        total_pages = (total_count + page_size - 1) // page_size
        logger.progress(f"Page {page} of {total_pages} (use --list-workspaces {page + 1} for next page)")
    logger.progress("")


def handle_list_workspaces(args: argparse.Namespace) -> None:
    """Handle --list-workspaces command. Exits after printing."""
    ws = args.list_workspaces
    page = int(ws[0]) if ws and ws[0].isdigit() else 1
    page_size = int(ws[1]) if len(ws) > 1 and ws[1].isdigit() else 50

    workspaces, total_count = list_workspaces_by_page(page, page_size)
    _print_workspaces(workspaces, page, total_count, page_size, as_json=args.json)
    sys.exit(0)


def _format_search_results(results: List[Dict[str, Any]], total_count: int, page: int, page_size: int) -> None:
    logger.progress(f"\nSearch results (page {page}, showing {len(results)} of {total_count}):")
    logger.progress("")
    for idx, row in enumerate(results, start=1 + (page - 1) * page_size):
        role = row.get("role") or "unknown"
        score = row.get("score", 0.0)
        text = (row.get("original_text") or "").replace("\n", " ").strip()
        snippet = text[:160] + ("..." if len(text) > 160 else "")
        logger.progress(f"{idx:>4}. [{role}] score={score:.3f} turn={row.get('turn')} session={row.get('session_id')}")
        logger.progress(f"      {snippet}")
    logger.progress("")


async def handle_search(args: argparse.Namespace) -> None:
    """Handle search command."""
    from src.shared.database import db_schema
    from src.shared.database import db_search

    run_path = _require_run_dir(args, "--search")
    db_path = require_db_path(run_path)

    if args.user_only and args.assistant_only:
        logger.error("[ERROR] Use only one of --user-only or --assistant-only")
        sys.exit(1)

    roles = None
    if args.user_only:
        roles = ["user"]
    elif args.assistant_only:
        roles = ["assistant"]

    conn = db_schema.connect_db(db_path)
    try:
        result = db_search.search_turns(
            conn,
            query=args.search,
            mode=args.search_mode,
            roles=roles,
            page=args.page,
            page_size=args.page_size,
            min_score=args.min_score,
            strict=args.strict,
        )
    except Exception as exc:
        logger.error(f"[ERROR] Search failed: {exc}")
        logger.progress("[TIP] Run --reindex to build FTS and embeddings for this database")
        sys.exit(1)
    finally:
        conn.close()

    if args.json:
        import json as json_module

        print(json_module.dumps(result, indent=2))
        return

    _format_search_results(
        result["results"],
        result["total_count"],
        result["page"],
        result["page_size"],
    )


async def handle_reindex(args: argparse.Namespace) -> None:
    """Handle search index rebuild."""
    from src.shared.database import db_schema
    from src.shared.search.search_indexer import generate_embeddings
    from src.shared.config.config_loader import get_config
    
    run_path = _require_run_dir(args, "--reindex")
    db_path = require_db_path(run_path)

    config = get_config().search
    model_name = args.embedding_model or config.semantic_model
    batch_size = args.embedding_batch_size or config.embedding_batch_size

    conn = db_schema.connect_db(db_path)
    try:
        db_schema.ensure_turns_fts_table(conn)
        db_schema.ensure_turn_embeddings_table(conn)
        db_schema.rebuild_turns_fts(conn)
        stats = generate_embeddings(
            conn,
            model_name=model_name,
            batch_size=batch_size,
            verbose=not args.json,
        )
    finally:
        conn.close()

    if args.json:
        import json as json_module

        print(json_module.dumps({"model": model_name, **stats}, indent=2))
        return

    logger.progress(f"\nSearch index rebuilt using model '{model_name}'")


async def handle_extract(args: argparse.Namespace) -> None:
    """Handle workspace extraction."""
    # Validate --run-dir is provided
    if not args.run_dir:
        logger.error("[ERROR] --extract requires --run-dir <run_folder>")
        sys.exit(1)
    
    agent_filter = args.agent
    
    # Collect workspace IDs and info
    if args.all:
        workspaces_info = list_all_workspaces()
        if agent_filter:
            workspaces_info = [w for w in workspaces_info if agent_filter in w.agents]
            logger.progress(f"[INFO] Filtered to {len(workspaces_info)} workspaces with agent '{agent_filter}'")
        workspace_ids = [w.workspace_id for w in workspaces_info]
        logger.progress(f"[INFO] Extracting all {len(workspace_ids)} workspaces from storage")
    elif args.extract:
        workspace_ids = args.extract
        workspaces_info = [ws for ws_id in workspace_ids if (ws := find_workspace(ws_id))]
    else:
        logger.error("[ERROR] --extract requires workspace IDs or --all")
        logger.progress("[TIP] Use --list to see available workspaces")
        sys.exit(1)

    if not workspace_ids:
        logger.error("[ERROR] No workspaces to process")
        logger.progress("[TIP] Use --list to see available workspaces")
        sys.exit(1)
    
    total_sessions = sum(ws.session_count for ws in workspaces_info) if workspaces_info else 0
    logger.progress(f"\n[INFO] Total workspaces: {len(workspace_ids)}, sessions: {total_sessions}")
    
    await extract_workspaces(
        workspace_ids,
        args.run_dir,
        force_refresh=args.force_refresh,
        agent_filter=agent_filter,
    )


# -------------------- Main Entry Point --------------------

async def main(argv: Optional[List[str]] = None) -> None:
    """Main CLI entry point."""
    args = parse_args(argv)
    
    # Load .env file at startup
    load_env()
    
    # Load config with optional override
    if args.config:
        get_config(args.config)
    
    # Handle --list-workspaces
    if args.list_workspaces is not None:
        handle_list_workspaces(args)
        return
    
    # Handle --extract
    if args.extract is not None:
        await handle_extract(args)
        return

    # Handle --reindex
    if args.reindex:
        await handle_reindex(args)
        return

    # Handle --search
    if args.search is not None:
        await handle_search(args)
        return
    
    # No command provided
    logger.error("[ERROR] No command specified")
    logger.progress("[TIP] Use --list to see workspaces, --extract to process them, or --search to find content")
    logger.progress("[TIP] Run with --help for more information")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
