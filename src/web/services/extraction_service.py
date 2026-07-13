"""
Extraction Service - Handles workspace extraction operations.

Contains the business logic for extracting workspaces, separated from
the HTTP endpoint handlers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.shared.logging.logger import get_logger
from src.shared.io.run_dir import get_db_path
from src.shared.text.text_shrinker import tokenize, STOPWORDS
from src.web.services.pipeline_run_tracker import get_pipeline_run_tracker
from src.web.services.sse_streaming import SSELogHandler, SSELogger
from src.web.shared_state import (
    get_shared_run_dir,
)


def generate_word_lists(
    conn: sqlite3.Connection,
    sql: str,
    params: Optional[Tuple[Any, ...]] = None,
    top_model_ids: Optional[List[str]] = None,
    min_word_length: int = 3,
    max_words_per_group: int = 500,
    exclude_patterns: Optional[List[str]] = None,
    max_chars_per_text: Optional[int] = None,
) -> Dict[str, Dict[str, List[List[Any]]]]:
    """Execute a query returning (role, model_id, text, thinking_text) rows
    and produce a standardized `word_lists` mapping used by dashboards.

    """
    params = params or ()
    compiled_patterns = []
    if exclude_patterns:
        for p in exclude_patterns:
            try:
                compiled_patterns.append(re.compile(p))
            except re.error:
                pass

    top_model_ids = top_model_ids or []
    top_model_set = set(top_model_ids)

    user_response = Counter()
    assistant_all_response = Counter()
    assistant_all_thinking = Counter()
    assistant_model_response: Dict[str, Counter] = {m: Counter() for m in top_model_ids}
    assistant_model_thinking: Dict[str, Counter] = {m: Counter() for m in top_model_ids}
    text_token_cache: Dict[str, List[str]] = {}

    def process_text(text: Optional[str], counters: List[Counter]):
        if not text:
            return
        if max_chars_per_text is not None and max_chars_per_text > 0 and len(text) > max_chars_per_text:
            text = text[:max_chars_per_text]
        valid_tokens = text_token_cache.get(text)
        if valid_tokens is None:
            tokens = tokenize(text)
            valid_tokens = []
            for token in tokens:
                if len(token) < min_word_length:
                    continue
                if token in STOPWORDS:
                    continue
                if token.isdigit():
                    continue
                if compiled_patterns and any(p.search(token) for p in compiled_patterns):
                    continue
                valid_tokens.append(token)
            if len(text_token_cache) < 10000:
                text_token_cache[text] = valid_tokens
        if not valid_tokens:
            return
        for c in counters:
            c.update(valid_tokens)

    try:
        cur = conn.execute(sql, params) if params else conn.execute(sql)
        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                role = row[0]
                model_id = row[1] or ""
                text = row[2]
                thinking_text = row[3]

                if role == "user":
                    process_text(text, [user_response])
                elif role == "assistant":
                    targets_resp = [assistant_all_response]
                    if model_id in top_model_set:
                        targets_resp.append(assistant_model_response[model_id])
                    process_text(text, targets_resp)

                    if thinking_text:
                        targets_think = [assistant_all_thinking]
                        if model_id in top_model_set:
                            targets_think.append(assistant_model_thinking[model_id])
                        process_text(thinking_text, targets_think)
    except sqlite3.OperationalError:
        # If table/columns missing, return empty structure
        pass

    word_lists: Dict[str, Dict[str, List[List[Any]]]] = {}
    word_lists["user"] = {
        "response": [[w, c] for w, c in user_response.most_common(max_words_per_group)],
        "thinking": [],
    }
    word_lists["assistant_all"] = {
        "response": [[w, c] for w, c in assistant_all_response.most_common(max_words_per_group)],
        "thinking": [[w, c] for w, c in assistant_all_thinking.most_common(max_words_per_group)],
    }

    for model_id in top_model_ids:
        word_lists[f"assistant_model::{model_id}"] = {
            "response": [[w, n] for w, n in assistant_model_response[model_id].most_common(max_words_per_group)],
            "thinking": [[w, n] for w, n in assistant_model_thinking[model_id].most_common(max_words_per_group)],
        }

    return word_lists

logger = get_logger(__name__)


def create_extraction_run(workspace_id: str, workspace_name: str) -> str:
    """Create a new extraction run and return its ID."""
    tracker = get_pipeline_run_tracker()
    run_id = f"extract_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    tracker.create_run(
        run_id,
        {
            "status": "pending",
            "started_at": datetime.now().isoformat(),
            "workspace_ids": [workspace_id],
            "action": "extract",
            "workspace_name": workspace_name,
        },
    )
    return run_id


def create_bulk_extraction_run(workspace_ids: list[str]) -> str:
    """Create a bulk extraction run and return its ID."""
    tracker = get_pipeline_run_tracker()
    run_id = f"bulk_extract_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    tracker.create_run(
        run_id,
        {
            "status": "pending",
            "started_at": datetime.now().isoformat(),
            "workspace_ids": workspace_ids,
            "action": "bulk_extract",
            "total_count": len(workspace_ids),
        },
    )
    return run_id


def extract_single_workspace_sync(workspace_id: str, ws: Any, refresh: bool) -> dict:
    """
    Synchronously extract a single workspace.
    
    This is designed to be called from asyncio.to_thread().
    
    Returns:
        dict with keys: success, total_sessions, total_turns, duration_ms, error
    """
    from src.pipeline.extraction.orchestrator import extract_single_workspace

    try:
        # Note: No need to manually clear data - extract_single_workspace
        # handles deletion internally when force_refresh=True is passed
        run_dir = get_shared_run_dir()
        db_path = get_db_path(Path(run_dir))

        result = extract_single_workspace(
            workspace_id=workspace_id, db_path=db_path, force_refresh=refresh
        )

        if result.success:
            
            return {
                "success": True,
                "total_sessions": result.session_count,
                "total_turns": result.turn_count,
                "duration_ms": result.duration_ms,
            }

        return {
            "success": False,
            "error": result.error or "Unknown extraction error",
        }

    except Exception as e:
        logger.error(f"Extraction failed for {workspace_id}: {e}")
        return {
            "success": False,
            "error": str(e),
        }


async def execute_streaming_extraction(
    run_id: str, workspace_id: str, ws: Any, refresh: bool
) -> None:
    """Execute extraction with streaming output to SSE queue."""
    sse = SSELogger(run_id)
    tracker = get_pipeline_run_tracker()
    sse_handler: Optional[logging.Handler] = None
    root_logger: Optional[logging.Logger] = None

    try:
        await sse.status("running")

        # Set up log forwarding to capture all pipeline logger output
        loop = asyncio.get_event_loop()
        sse_handler = SSELogHandler(sse.queue, loop)
        sse_handler.setFormatter(logging.Formatter("%(message)s"))

        root_logger = logging.getLogger("src")
        root_logger.addHandler(sse_handler)

        await sse.log(f"📂 Extracting workspace: {ws.workspace_name}")
        await sse.log(f"   Workspace ID: {workspace_id}")
        await sse.log(f"   Agents: {', '.join(ws.agents)}")
        await sse.log("=" * 50)

        await sse.log("🔍 Starting extraction from source files...")

        result = await asyncio.to_thread(
            extract_single_workspace_sync, workspace_id, ws, refresh
        )

        if result["success"]:
            await sse.log("✅ Extraction completed successfully!")
            await sse.log(f"   Total sessions: {result['total_sessions']}")
            await sse.log(f"   Total turns: {result['total_turns']}")
            await sse.log(f"   Duration: {result['duration_ms']}ms")

            tracker.update_run(
                run_id,
                completed_at=datetime.now().isoformat(),
                session_count=result["total_sessions"],
            )
            await sse.status("completed")
        else:
            raise Exception(result["error"])

    except Exception as e:
        logger.error(f"Streaming extraction {run_id} failed: {e}")
        traceback.print_exc()
        await sse.error(str(e))
        await sse.status("failed")

    finally:
        if sse_handler and root_logger:
            root_logger.removeHandler(sse_handler)
        await sse.done()


async def execute_bulk_streaming_extraction(
    run_id: str, workspace_ids: list[str], refresh: bool
) -> None:
    """Execute bulk extraction with streaming output."""
    from src.shared.workspace_discovery import find_workspace
    from src.web.shared_state import get_all_workspace_metadata, get_workspace_status

    sse = SSELogger(run_id)
    tracker = get_pipeline_run_tracker()
    sse_handler: Optional[logging.Handler] = None
    root_logger: Optional[logging.Logger] = None

    completed = 0
    failed = 0
    skipped = 0

    try:
        await sse.status("running")

        # Set up log forwarding to capture all pipeline logger output
        loop = asyncio.get_event_loop()
        sse_handler = SSELogHandler(sse.queue, loop)
        sse_handler.setFormatter(logging.Formatter("%(message)s"))

        root_logger = logging.getLogger("src")
        root_logger.addHandler(sse_handler)

        total = len(workspace_ids)
        await sse.log("📦 Bulk Extraction Starting")
        await sse.log(f"   Total workspaces: {total}")
        await sse.log(f"   Refresh mode: {'Yes' if refresh else 'No'}")
        await sse.log("=" * 60)

        for i, workspace_id in enumerate(workspace_ids):
            current = i + 1

            await sse.progress(current, total, completed, failed, skipped)

            ws = find_workspace(workspace_id)
            if not ws:
                # Check if workspace exists in database only
                all_metadata = get_all_workspace_metadata()
                metadata = all_metadata.get(workspace_id)
                
                if metadata and metadata.db_available:
                    await sse.log(
                        f"[{current}/{total}] ⏭️  {metadata.workspace_name} - Skipped (database only)"
                    )
                    skipped += 1
                else:
                    await sse.log(
                        f"[{current}/{total}] ❌ {workspace_id[:16]}... - Not found"
                    )
                    failed += 1
                continue

            await sse.log(f"[{current}/{total}] 📂 {ws.workspace_name}")

            if not refresh:
                all_extracted = True
                for agent in ws.agents:
                    status = get_workspace_status(workspace_id, agent)
                    if not status or not status.is_extracted:
                        all_extracted = False
                        break

                if all_extracted:
                    await sse.log("         ⏭️  Already extracted - skipping")
                    skipped += 1
                    continue

            await sse.log("         🔍 Extracting...")

            result = await asyncio.to_thread(
                extract_single_workspace_sync, workspace_id, ws, refresh
            )

            if result["success"]:
                await sse.log(
                    f"         ✓ {result['total_sessions']} sessions, {result['total_turns']} turns ({result['duration_ms']}ms)"
                )
                completed += 1
            else:
                await sse.log(f"         ❌ Failed: {result['error']}")
                failed += 1

        await sse.log("=" * 60)
        await sse.log("✅ Bulk Extraction Complete!")
        await sse.log(f"   ✓ Completed: {completed}")
        await sse.log(f"   ⏭️  Skipped: {skipped}")
        await sse.log(f"   ❌ Failed: {failed}")

        tracker.update_run(
            run_id,
            completed_at=datetime.now().isoformat(),
            completed=completed,
            failed=failed,
            skipped=skipped,
        )
        await sse.status("completed")

    except Exception as e:
        logger.error(f"Bulk extraction {run_id} failed: {e}")
        traceback.print_exc()
        await sse.error(str(e))
        await sse.status("failed")

    finally:
        if sse_handler and root_logger:
            root_logger.removeHandler(sse_handler)
        await sse.done()
