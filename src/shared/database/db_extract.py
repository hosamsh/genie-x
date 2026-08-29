"""
Database functions for extraction operations.

Standalone functions for turn and workspace extraction storage.
"""

from __future__ import annotations

from collections import defaultdict
import json
import sqlite3
from typing import Any, Dict, List, Optional

from src.shared.logging.logger import get_logger
from src.shared.database.db_schema import ensure_turn_detail_tables, json_dumps_for_db, parse_json_field
from src.shared.models.code_metric import CodeMetric
from src.shared.models.turn import EnrichedTurn

logger = get_logger(__name__)


def _normalize_agent_name(agent_used: str) -> str:
    value = (agent_used or "").strip().lower()
    if value in {"copilot", "copilot_cli", "claude_code"}:
        return value
    return value


def sanitize_unicode(text: Optional[str]) -> Optional[str]:
    """Remove invalid Unicode surrogate characters that can't be encoded in UTF-8.
    
    Surrogates (U+D800-U+DFFF) are invalid in UTF-8 and cause encoding errors.
    This typically happens with malformed emoji or special characters.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return text
    try:
        # Try encoding - if it works, return as-is
        text.encode('utf-8')
        return text
    except UnicodeEncodeError:
        # Remove surrogates by encoding with 'ignore' error handler
        # which strips out unencodable characters
        return text.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')


def does_workspace_exist_in_db(conn: sqlite3.Connection, workspace_id: str) -> bool:
    """Return True if the workspace already has rows in the turns table."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM turns WHERE workspace_id = ?", (workspace_id,))
    count = cursor.fetchone()[0]
    return count > 0

def get_workspace_info_from_db(
    conn: sqlite3.Connection,
    workspace_id: str,
) -> Optional[Dict[str, Any]]:
    """Return workspace summary information from the turns table."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 
            workspace_name,
            workspace_folder,
            COUNT(DISTINCT session_id) as session_count,
            COUNT(*) as turn_count
        FROM turns
        WHERE workspace_id = ?
        GROUP BY workspace_id, workspace_name, workspace_folder
        """,
        (workspace_id,),
    )

    row = cursor.fetchone()
    if not row:
        return None

    return {
        "workspace_name": row[0],
        "workspace_folder": row[1],
        "session_count": row[2],
        "turn_count": row[3],
    }

def delete_workspace_extraction(conn: sqlite3.Connection, workspace_id: str) -> Dict[str, int]:
    """Delete all extraction data for a workspace.
    
    Removes turns, combined_turns, and code_metrics for the specified workspace.
    Uses explicit transaction with rollback to ensure atomicity.
    
    Args:
        conn: SQLite connection
        workspace_id: The workspace ID to delete data for
        
    Returns:
        Dict with counts of deleted rows per table
        
    Raises:
        sqlite3.Error: If deletion fails (transaction rolled back)
    """
    try:
        cursor = conn.cursor()
        deleted = {}

        cursor.execute("DELETE FROM turn_tool_calls WHERE workspace_id = ?", (workspace_id,))
        deleted["turn_tool_calls"] = cursor.rowcount

        cursor.execute("DELETE FROM turn_subagent_runs WHERE workspace_id = ?", (workspace_id,))
        deleted["turn_subagent_runs"] = cursor.rowcount
        
        # Delete from code_metrics first (references workspace_id)
        cursor.execute("DELETE FROM code_metrics WHERE workspace_id = ?", (workspace_id,))
        deleted["code_metrics"] = cursor.rowcount
        
        # Note: No need to delete from combined_turns - it's a VIEW that auto-updates
        # when turns table changes
        
        # Delete from turns (this will automatically update the combined_turns view)
        cursor.execute("DELETE FROM turns WHERE workspace_id = ?", (workspace_id,))
        deleted["turns"] = cursor.rowcount
        
        conn.commit()
        return deleted
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to delete workspace extraction for '{workspace_id}': {e}")
        raise

def upsert_workspace_info(
    conn: sqlite3.Connection,
    workspace_id: str,
    workspace_name: str,
    workspace_folder: str,
    agent_used: str,
    extraction_duration_ms: int,
    session_count: int = 0,
    turn_count: int = 0,
    total_code_loc: int = 0,
    total_doc_loc: int = 0,
) -> None:
    """Insert or update workspace info after extraction.
    
    On insert: created_at and updated_at are set to current time.
    On update: only updated_at is modified (created_at preserved as first extraction time).
    
    Args:
        conn: SQLite connection
        workspace_id: Unique workspace identifier
        workspace_name: Human-readable workspace name
        workspace_folder: Path to workspace folder
        agent_used: Agent(s) used for extraction (for example 'copilot', 'claude_code', or 'copilot+claude_code')
        extraction_duration_ms: Time taken for extraction in milliseconds
        session_count: Number of sessions extracted
        turn_count: Number of turns extracted
        total_code_loc: Total lines of code in the workspace
        total_doc_loc: Total lines of documentation in the workspace
    """
    from datetime import datetime
    from src.shared.database.db_schema import ensure_workspace_info_table
    
    # Ensure table exists
    ensure_workspace_info_table(conn)
    
    try:
        # Check if LOC columns exist, add them if not (migration for existing tables)
        cursor = conn.cursor()
        
        now_iso = datetime.now().isoformat()
        
        # Check if workspace already exists
        cursor.execute("SELECT id FROM workspace_info WHERE workspace_id = ?", (workspace_id,))
        row = cursor.fetchone()
        
        if row:
            # Update existing record (preserves created_at as first extraction time)
            cursor.execute("""
                UPDATE workspace_info 
                SET workspace_name = ?,
                    workspace_folder = ?,
                    agent_used = ?,
                    extraction_duration_ms = ?,
                    session_count = ?,
                    turn_count = ?,
                    total_code_loc = ?,
                    total_doc_loc = ?,
                    updated_at = ?
                WHERE workspace_id = ?
            """, (
                workspace_name,
                workspace_folder,
                agent_used,
                extraction_duration_ms,
                session_count,
                turn_count,
                total_code_loc,
                total_doc_loc,
                now_iso,
                workspace_id,
            ))
        else:
            # Insert new record
            cursor.execute("""
                INSERT INTO workspace_info (
                    workspace_id, workspace_name, workspace_folder, agent_used,
                    extraction_duration_ms, session_count, turn_count, 
                    total_code_loc, total_doc_loc, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                workspace_id,
                workspace_name,
                workspace_folder,
                agent_used,
                extraction_duration_ms,
                session_count,
                turn_count,
                total_code_loc,
                total_doc_loc,
                now_iso,
                now_iso,
            ))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to upsert workspace info for '{workspace_id}': {e}")
        raise

def get_turns_by_session(conn: sqlite3.Connection, session_id: str) -> List[EnrichedTurn]:
    """Get all turns for a session, ordered by turn number."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            session_id, turn, role, text, original_text,
            workspace_id, workspace_name, workspace_folder, session_name,
            agent_used, model_id, request_id,
            timestamp_ms, timestamp_iso, ts,
            original_text_tokens, cleaned_text_tokens, code_tokens, tool_tokens, system_tokens, session_history_tokens,
            thinking_tokens, primary_language, languages, files, tools,
            merged_request_ids, thinking_text, thinking_duration_ms,
            responding_to_turn, response_time_ms,
            total_lines_added, total_lines_removed, total_nloc_change, weighted_complexity_change,
            parent_session_id, relationship_type
        FROM turns
        WHERE session_id = ?
        ORDER BY turn ASC
    """, (session_id,))

    turns = []
    for row in cursor.fetchall():
        turn = EnrichedTurn(
            session_id=row[0],
            turn=row[1],
            role=row[2],
            cleaned_text=row[3] or "",
            original_text=row[4] or "",
            workspace_id=row[5] or "",
            workspace_name=row[6] or "",
            workspace_folder=row[7] or "",
            session_name=row[8] or "",
            agent_used=row[9] or "",
            model_id=row[10] or "",
            request_id=row[11] or "",
            timestamp_ms=row[12],
            timestamp_iso=row[13],
            ts=row[14] or "",
            original_text_tokens=row[15] or 0,
            cleaned_text_tokens=row[16] or 0,
            code_tokens=row[17] or 0,
            tool_tokens=row[18] or 0,
            system_tokens=row[19] or 0,
            session_history_tokens=row[20] or 0,
            thinking_tokens=row[21] or 0,
            primary_language=row[22],
            languages=parse_json_field(row[23], []),
            files=parse_json_field(row[24], []),
            tools=parse_json_field(row[25], []),
            merged_request_ids=parse_json_field(row[26], []),
            thinking_text=row[27] or "",
            thinking_duration_ms=row[28] or 0,
            responding_to_turn=row[29],
            response_time_ms=row[30],
            total_lines_added=row[31],
            total_lines_removed=row[32],
            total_nloc_change=row[33],
            weighted_complexity_change=row[34],
            parent_session_id=row[35],
            relationship_type=row[36],
        )
        turns.append(turn)
    return turns

def get_session_ids_by_workspace(conn: sqlite3.Connection, workspace_id: str) -> List[str]:
    """Get all unique session IDs for a workspace, ordered by session start time."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT session_id, MIN(timestamp_ms) as start_time
        FROM turns
        WHERE workspace_id = ?
        GROUP BY session_id
        ORDER BY start_time ASC
    """, (workspace_id,))
    return [row[0] for row in cursor.fetchall()]

def upsert_turn(conn: sqlite3.Connection, turn: EnrichedTurn) -> None:
    """Insert or update a single turn (upsert to prevent duplicates)."""
    cursor = conn.cursor()
    data = {
        "session_id": turn.session_id,
        "turn": turn.turn,
        "role": turn.role,
        "text": sanitize_unicode(turn.cleaned_text),
        "original_text": sanitize_unicode(turn.original_text),
        "workspace_id": turn.workspace_id,
        "workspace_name": sanitize_unicode(turn.workspace_name),
        "workspace_folder": sanitize_unicode(turn.workspace_folder),
        "session_name": sanitize_unicode(turn.session_name),
        "agent_used": turn.agent_used,
        "model_id": turn.model_id,
        "request_id": turn.request_id,
        "timestamp_ms": turn.timestamp_ms,
        "timestamp_iso": turn.timestamp_iso,
        "ts": turn.ts,
        "original_text_tokens": turn.original_text_tokens,
        "cleaned_text_tokens": turn.cleaned_text_tokens,
        "code_tokens": turn.code_tokens,
        "tool_tokens": turn.tool_tokens,
        "system_tokens": turn.system_tokens,
        "session_history_tokens": turn.session_history_tokens,
        "thinking_tokens": turn.thinking_tokens,
        "primary_language": turn.primary_language,
        "languages": json_dumps_for_db(turn.languages),
        "files": json_dumps_for_db(turn.files),
        "tools": json_dumps_for_db(turn.tools),
        "merged_request_ids": json_dumps_for_db(turn.merged_request_ids),
        "thinking_text": sanitize_unicode(turn.thinking_text) if turn.thinking_text else None,
        "thinking_duration_ms": turn.thinking_duration_ms if turn.thinking_duration_ms else None,
        "responding_to_turn": turn.responding_to_turn,
        "response_time_ms": turn.response_time_ms,
        "total_lines_added": turn.total_lines_added,
        "total_lines_removed": turn.total_lines_removed,
        "total_nloc_change": turn.total_nloc_change,
        "weighted_complexity_change": turn.weighted_complexity_change,
        "parent_session_id": turn.parent_session_id,
        "relationship_type": turn.relationship_type,
    }
    columns = ", ".join(data.keys())
    placeholders = ", ".join(f":{k}" for k in data.keys())
    # Use INSERT OR REPLACE to handle duplicates (upsert on session_id + turn)
    cursor.execute(f"INSERT OR REPLACE INTO turns ({columns}) VALUES ({placeholders})", data)

def upsert_turns(conn: sqlite3.Connection, turns: List[EnrichedTurn]) -> int:
    """Insert multiple turns and their code_edits. Returns count of inserted turns."""
    from src.shared.models.turn import calculate_response_times, calculate_turn_metrics
    from src.shared.database.db_schema import ensure_turns_table

    if not turns:
        return 0

    # Ensure columns exist (applies forward migrations on old databases)
    ensure_turns_table(conn)

    # Calculate response times before insertion
    turns = calculate_response_times(turns)
    
    # Calculate aggregate code metrics for each turn
    for turn in turns:
        calculate_turn_metrics(turn)
    
    # Calculate session_history_tokens - cumulative tokens from previous turns
    # Group turns by session_id first
    turns_by_session: Dict[str, List[EnrichedTurn]] = {}
    for turn in turns:
        if turn.session_id not in turns_by_session:
            turns_by_session[turn.session_id] = []
        turns_by_session[turn.session_id].append(turn)
    
    # Sort each session's turns by turn index and calculate cumulative history
    for session_id, session_turns in turns_by_session.items():
        session_turns.sort(key=lambda t: t.turn)
        cumulative_tokens = 0
        for turn in session_turns:
            turn.session_history_tokens = cumulative_tokens
            # Add this turn's tokens to cumulative for next turn
            cumulative_tokens += turn.total_tokens
    
    metrics_to_insert: List[CodeMetric] = []
    # Aggregate edits per (request_id, file_path). The code_metrics table has a
    # UNIQUE(request_id, file_path) constraint, so multiple edits to one file in
    # a request must be combined (summing deltas) rather than overwriting each
    # other. This keeps code_metrics consistent with the per-turn rollup, which
    # sums every edit's delta.
    metrics_by_key: Dict[tuple, CodeMetric] = {}
    for turn in turns:
        upsert_turn(conn, turn)

        # Collect metrics from code edits
        if turn.code_edits:
            for edit in turn.code_edits:
                extra = edit.extra or {}
                delta = extra.get("delta_metrics") or {}
                key = (turn.request_id, edit.file_path)
                existing = metrics_by_key.get(key)
                if existing is None:
                    metric_record = CodeMetric(
                        request_id=turn.request_id,
                        session_id=turn.session_id,
                        file_path=edit.file_path,
                        workspace_id=turn.workspace_id,
                        agent_used=turn.agent_used,
                        model_id=turn.model_id,
                        before_metrics=extra.get("before_metrics"),
                        after_metrics=extra.get("after_metrics"),
                        delta_metrics=dict(delta),
                        code_before=edit.code_before,
                        code_after=edit.code_after,
                    )
                    metrics_by_key[key] = metric_record
                    metrics_to_insert.append(metric_record)
                else:
                    # Same file edited again within the request: sum the deltas
                    # and carry the latest after-state so the single stored row
                    # reflects the file's full change for this request.
                    merged = dict(existing.delta_metrics or {})
                    for field_name in ("nloc", "lines_added", "lines_removed", "token_count"):
                        merged[field_name] = (merged.get(field_name) or 0) + (delta.get(field_name) or 0)
                    cur_complexity = merged.get("cyclomatic_complexity") or 0
                    new_complexity = delta.get("cyclomatic_complexity") or 0
                    if abs(new_complexity) > abs(cur_complexity):
                        merged["cyclomatic_complexity"] = new_complexity
                    existing.delta_metrics = merged
                    if extra.get("after_metrics"):
                        existing.after_metrics = extra.get("after_metrics")
                    existing.code_after = edit.code_after

    conn.commit()
    
    # Insert collected metrics (best-effort - don't fail turn insertion if metrics fail)
    if metrics_to_insert:
        try:
            metrics_inserted = upsert_metrics(conn, metrics_to_insert)
            logger.debug(f"Inserted {metrics_inserted} code metric records")
        except Exception as exc:
            # Log warning but continue - code metrics are supplementary data
            logger.warning(f"Failed to insert code metrics for {len(metrics_to_insert)} edits: {exc}")
    
    return len(turns)


def replace_turn_detail_rows(conn: sqlite3.Connection, workspace_id: str, turns: List[EnrichedTurn]) -> Dict[str, int]:
    """Replace materialized turn detail rows for one workspace."""
    ensure_turn_detail_tables(conn)

    cursor = conn.cursor()
    cursor.execute("DELETE FROM turn_tool_calls WHERE workspace_id = ?", (workspace_id,))
    deleted_tool_calls = cursor.rowcount
    cursor.execute("DELETE FROM turn_subagent_runs WHERE workspace_id = ?", (workspace_id,))
    deleted_subagents = cursor.rowcount

    session_summaries = _build_session_summaries(turns)
    tool_rows = _build_turn_tool_rows(turns)
    subagent_rows = _build_turn_subagent_rows(turns, session_summaries)

    inserted_tool_calls = 0
    inserted_subagents = 0

    for row in tool_rows:
        cursor.execute(
            """
            INSERT OR REPLACE INTO turn_tool_calls (
                workspace_id, session_id, turn, tool_index, call_id, name, kind,
                arguments_json, arguments_text, file_paths_json, spawned_session_id,
                status, display_text, results_json, raw_call_json, extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                row["session_id"],
                row["turn"],
                row["tool_index"],
                row.get("call_id") or None,
                row.get("name") or None,
                row.get("kind") or None,
                json_dumps_for_db(row.get("arguments") or {}),
                row.get("arguments_text") or None,
                json_dumps_for_db(row.get("file_paths") or []),
                row.get("spawned_session_id") or None,
                row.get("status") or None,
                row.get("display_text") or None,
                json_dumps_for_db(row.get("results") or []),
                json_dumps_for_db(row.get("raw") or {}),
                json_dumps_for_db(row.get("extra") or {}),
            ),
        )
        inserted_tool_calls += 1

    for row in subagent_rows:
        cursor.execute(
            """
            INSERT OR REPLACE INTO turn_subagent_runs (
                workspace_id, session_id, turn, subagent_index, subagent_session_id,
                source_tool_call_id, source_tool_name, relationship_type, title,
                prompt_text, result_text, turn_count, total_lines_added,
                total_lines_removed, started_at_ms, ended_at_ms, extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                row["session_id"],
                row["turn"],
                row["subagent_index"],
                row["subagent_session_id"],
                row.get("source_tool_call_id") or None,
                row.get("source_tool_name") or None,
                row.get("relationship_type") or None,
                row.get("title") or None,
                row.get("prompt_text") or None,
                row.get("result_text") or None,
                row.get("turn_count") or 0,
                row.get("total_lines_added") or 0,
                row.get("total_lines_removed") or 0,
                row.get("started_at_ms"),
                row.get("ended_at_ms"),
                json_dumps_for_db(row.get("extra") or {}),
            ),
        )
        inserted_subagents += 1

    conn.commit()
    return {
        "deleted_tool_calls": deleted_tool_calls,
        "deleted_subagents": deleted_subagents,
        "tool_calls": inserted_tool_calls,
        "subagents": inserted_subagents,
    }


def _build_turn_tool_rows(turns: List[EnrichedTurn]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for turn in turns:
        extra = turn.extra or {}
        tool_calls = extra.get("tool_calls") or []
        tool_results = extra.get("tool_results") or []
        content_blocks = extra.get("content_blocks") or []

        results_by_call_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        orphan_results: List[Dict[str, Any]] = []
        for result in tool_results:
            if not isinstance(result, dict):
                continue
            tool_call_id = str(result.get("tool_call_id") or "")
            if tool_call_id:
                results_by_call_id[tool_call_id].append(result)
            else:
                orphan_results.append(result)

        display_text_by_call_id: Dict[str, str] = {}
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            extra_payload = block.get("extra") or {}
            if not isinstance(extra_payload, dict):
                continue
            tool_call = extra_payload.get("tool_call")
            if not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("call_id") or "")
            if call_id and call_id not in display_text_by_call_id:
                display_text_by_call_id[call_id] = str(block.get("text") or "")

        for tool_index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("call_id") or "")
            matched_results = results_by_call_id.get(call_id, []) if call_id else []
            if not matched_results and tool_index < len(orphan_results):
                matched_results = [orphan_results[tool_index]]
            rows.append(
                {
                    "session_id": turn.session_id,
                    "turn": turn.turn,
                    "tool_index": tool_index,
                    "call_id": call_id,
                    "name": tool_call.get("name") or "",
                    "kind": tool_call.get("kind") or "",
                    "arguments": tool_call.get("arguments") or {},
                    "arguments_text": tool_call.get("arguments_text") or "",
                    "file_paths": tool_call.get("file_paths") or [],
                    "spawned_session_id": tool_call.get("spawned_session_id") or None,
                    "status": tool_call.get("status") or _derive_tool_status(matched_results),
                    "display_text": display_text_by_call_id.get(call_id, "") or _summarize_tool_call_text(tool_call),
                    "results": matched_results,
                    "raw": tool_call.get("raw") or {},
                    "extra": tool_call.get("extra") or {},
                }
            )
    return rows


def _build_turn_subagent_rows(
    turns: List[EnrichedTurn],
    session_summaries: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    assistant_turns_by_session: Dict[str, List[EnrichedTurn]] = defaultdict(list)
    explicit_links: Dict[tuple[str, str], Dict[str, Any]] = {}

    for turn in turns:
        if turn.role == "assistant":
            assistant_turns_by_session[turn.session_id].append(turn)

        extra = turn.extra or {}
        tool_calls = [item for item in (extra.get("tool_calls") or []) if isinstance(item, dict)]
        source_links = [item for item in (extra.get("source_session_links") or []) if isinstance(item, dict)]

        for tool_call in tool_calls:
            subagent_session_id = tool_call.get("spawned_session_id")
            if isinstance(subagent_session_id, str) and subagent_session_id:
                explicit_links[(turn.session_id, subagent_session_id)] = {
                    "turn": turn,
                    "source_tool_call_id": tool_call.get("call_id") or "",
                    "source_tool_name": tool_call.get("name") or "",
                    "relationship_type": "subagent",
                }

        for link in source_links:
            if link.get("relationship_type") != "subagent":
                continue
            target_id = link.get("target_session_id")
            if not isinstance(target_id, str) or not target_id:
                continue
            explicit_links.setdefault(
                (turn.session_id, target_id),
                {
                    "turn": turn,
                    "source_tool_call_id": link.get("trigger_tool_call_id") or "",
                    "source_tool_name": (link.get("extra") or {}).get("tool_name") if isinstance(link.get("extra"), dict) else "",
                    "relationship_type": link.get("relationship_type") or "subagent",
                },
            )

    indexed_rows: Dict[tuple[str, int, str], Dict[str, Any]] = {}
    for child_session_id, summary in session_summaries.items():
        parent_session_id = summary.get("parent_session_id") or ""
        if not parent_session_id:
            continue

        link_info = explicit_links.get((parent_session_id, child_session_id))
        if link_info:
            parent_turn = link_info["turn"]
        else:
            parent_turn = _infer_parent_assistant_turn(
                assistant_turns_by_session.get(parent_session_id, []),
                summary.get("started_at_ms"),
            )
            if parent_turn is None:
                continue
            link_info = {
                "turn": parent_turn,
                "source_tool_call_id": "",
                "source_tool_name": "",
                "relationship_type": summary.get("relationship_type") or "subagent",
            }

        key = (parent_turn.session_id, parent_turn.turn, child_session_id)
        indexed_rows[key] = {
            "session_id": parent_turn.session_id,
            "turn": parent_turn.turn,
            "subagent_session_id": child_session_id,
            "source_tool_call_id": link_info.get("source_tool_call_id") or "",
            "source_tool_name": link_info.get("source_tool_name") or "",
            "relationship_type": link_info.get("relationship_type") or summary.get("relationship_type") or "subagent",
            "title": summary.get("title") or child_session_id,
            "prompt_text": summary.get("prompt_text") or "",
            "result_text": summary.get("result_text") or "",
            "turn_count": summary.get("turn_count") or 0,
            "total_lines_added": summary.get("total_lines_added") or 0,
            "total_lines_removed": summary.get("total_lines_removed") or 0,
            "started_at_ms": summary.get("started_at_ms"),
            "ended_at_ms": summary.get("ended_at_ms"),
            "extra": {
                "agent_used": summary.get("agent_used"),
                "model_ids": summary.get("model_ids", []),
            },
        }

    grouped_by_turn: Dict[tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
    for row in indexed_rows.values():
        grouped_by_turn[(row["session_id"], row["turn"])] .append(row)

    for group_rows in grouped_by_turn.values():
        for subagent_index, row in enumerate(sorted(group_rows, key=lambda item: (item.get("started_at_ms") or 0, item["subagent_session_id"]))):
            row["subagent_index"] = subagent_index
            rows.append(row)

    rows.sort(key=lambda item: (item["session_id"], item["turn"], item["subagent_index"]))
    return rows


def _build_session_summaries(turns: List[EnrichedTurn]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[EnrichedTurn]] = defaultdict(list)
    for turn in turns:
        grouped[turn.session_id].append(turn)

    summaries: Dict[str, Dict[str, Any]] = {}
    for session_id, session_turns in grouped.items():
        sorted_turns = sorted(session_turns, key=lambda item: item.turn)
        title = next((item.session_name for item in sorted_turns if item.session_name), session_id)
        prompt_text = next((item.original_text for item in sorted_turns if item.role == "user" and item.original_text), "")
        assistant_texts = [item.original_text for item in sorted_turns if item.role == "assistant" and item.original_text]
        summaries[session_id] = {
            "title": title,
            "prompt_text": prompt_text,
            "result_text": assistant_texts[-1] if assistant_texts else "",
            "turn_count": len(sorted_turns),
            "total_lines_added": sum(int(item.total_lines_added or 0) for item in sorted_turns),
            "total_lines_removed": sum(int(item.total_lines_removed or 0) for item in sorted_turns),
            "started_at_ms": min((item.timestamp_ms for item in sorted_turns if item.timestamp_ms is not None), default=None),
            "ended_at_ms": max((item.timestamp_ms for item in sorted_turns if item.timestamp_ms is not None), default=None),
            "agent_used": next((item.agent_used for item in sorted_turns if item.agent_used), ""),
            "model_ids": [item.model_id for item in sorted_turns if item.model_id],
            "parent_session_id": next((item.parent_session_id for item in sorted_turns if item.parent_session_id), ""),
            "relationship_type": next((item.relationship_type for item in sorted_turns if item.relationship_type), ""),
        }
    return summaries


def _infer_parent_assistant_turn(
    assistant_turns: List[EnrichedTurn],
    child_started_at_ms: Optional[int],
) -> Optional[EnrichedTurn]:
    if not assistant_turns:
        return None

    sorted_turns = sorted(assistant_turns, key=lambda item: (item.timestamp_ms or 0, item.turn))
    if child_started_at_ms is None:
        return sorted_turns[-1]

    eligible = [turn for turn in sorted_turns if turn.timestamp_ms is not None and turn.timestamp_ms <= child_started_at_ms]
    if eligible:
        return eligible[-1]
    return sorted_turns[-1]


def _derive_tool_status(results: List[Dict[str, Any]]) -> str:
    for result in results:
        status = result.get("status")
        if isinstance(status, str) and status and status != "unknown":
            return status
    for result in results:
        is_error = result.get("is_error")
        if is_error is True:
            return "error"
    if results:
        return "success"
    return "unknown"


def _summarize_tool_call_text(tool_call: Dict[str, Any]) -> str:
    name = str(tool_call.get("name") or "tool")
    arguments_text = str(tool_call.get("arguments_text") or "")
    return f"{name}({arguments_text})" if arguments_text else name

def count_turns_by_workspace(conn: sqlite3.Connection, workspace_id: str) -> int:
    """Get turn count for a workspace."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM turns WHERE workspace_id = ?",
        (workspace_id,)
    )
    return cursor.fetchone()[0]

def upsert_metrics(conn: sqlite3.Connection, metrics_list: List[CodeMetric]) -> int:
    """Insert or update metrics in the code_metrics table (upsert to prevent duplicates)."""
    cursor = conn.cursor()
    inserted = 0

    for metric in metrics_list:
        # Extract nested metrics (support both dict and CodeMetric)
        if isinstance(metric, CodeMetric):
            delta_metrics = metric.delta_metrics or {}
        else:
            # Backward compatibility with dict (shouldn't happen after refactor)
            delta_metrics = metric.get("delta_metrics") or {}
        
        data = {
            "request_id": metric.request_id if isinstance(metric, CodeMetric) else metric.get("request_id"),
            "session_id": metric.session_id if isinstance(metric, CodeMetric) else metric.get("session_id"),
            "file_path": metric.file_path if isinstance(metric, CodeMetric) else metric.get("file_path"),
            "workspace_id": metric.workspace_id if isinstance(metric, CodeMetric) else metric.get("workspace_id"),
            "agent_used": metric.agent_used if isinstance(metric, CodeMetric) else metric.get("agent_used"),
            "model_id": metric.model_id if isinstance(metric, CodeMetric) else metric.get("model_id"),
            "delta_nloc": (
                metric.delta_nloc
                if isinstance(metric, CodeMetric) and metric.delta_nloc is not None
                else delta_metrics.get("nloc")
            ),
            "delta_complexity": (
                metric.delta_complexity
                if isinstance(metric, CodeMetric) and metric.delta_complexity is not None
                else delta_metrics.get("cyclomatic_complexity")
            ),
            "lines_added": (
                metric.lines_added
                if isinstance(metric, CodeMetric) and metric.lines_added is not None
                else delta_metrics.get("lines_added")
            ),
            "lines_removed": (
                metric.lines_removed
                if isinstance(metric, CodeMetric) and metric.lines_removed is not None
                else delta_metrics.get("lines_removed")
            ),
            "before_metrics": json_dumps_for_db((metric.before_metrics if isinstance(metric, CodeMetric) else metric.get("before_metrics")) or {}),
            "after_metrics": json_dumps_for_db((metric.after_metrics if isinstance(metric, CodeMetric) else metric.get("after_metrics")) or {}),
            "delta_metrics": json_dumps_for_db((metric.delta_metrics if isinstance(metric, CodeMetric) else delta_metrics) or {}),
            "code_before": sanitize_unicode(metric.code_before if isinstance(metric, CodeMetric) else metric.get("code_before")),
            "code_after": sanitize_unicode(metric.code_after if isinstance(metric, CodeMetric) else metric.get("code_after")),
        }
        columns = ", ".join(data.keys())
        placeholders = ", ".join(f":{k}" for k in data.keys())
        # Use INSERT OR REPLACE to handle duplicates (upsert on request_id + file_path)
        cursor.execute(f"INSERT OR REPLACE INTO code_metrics ({columns}) VALUES ({placeholders})", data)
        inserted += 1
        
    conn.commit()
    return inserted


# =============================================================================
# Query functions for extraction data (read operations)
# =============================================================================

def query_workspace_status(
    conn: sqlite3.Connection,
    workspace_ids: List[str],
    agent: str
) -> Optional[Dict[str, Any]]:
    """Get the status of a workspace for a specific agent.
    
    Extraction status: workspace has records in turns table
    
    Args:
        conn: SQLite connection
        workspace_ids: Related workspace IDs to aggregate
        agent: The agent type (for example copilot, claude_code, or copilot_cli)
        
    Returns:
        Dict with status info if workspace has any data, None otherwise
    """
    if not workspace_ids:
        return None

    placeholders = ", ".join("?" for _ in workspace_ids)
    cursor = conn.execute(
        f"""SELECT COUNT(DISTINCT ps.session_id)
           FROM parsed_workspaces pw
           JOIN parsed_sessions ps ON ps.workspace_row_id = pw.id
           WHERE pw.workspace_id IN ({placeholders}) AND pw.agent_name = ?""",
        (*workspace_ids, agent),
    )
    parsed_session_count = (cursor.fetchone() or [0])[0] or 0

    cursor = conn.execute(
        f"""SELECT COUNT(*), COUNT(DISTINCT session_id), 
                  MIN(timestamp_iso), MAX(timestamp_iso)
           FROM turns 
           WHERE workspace_id IN ({placeholders}) AND LOWER(agent_used) = ?""",
        (*workspace_ids, agent.lower())
    )
    row = cursor.fetchone()
    turn_count = row[0] or 0
    session_count = parsed_session_count or (row[1] or 0)
    first_ts = row[2]
    last_ts = row[3]
    
    is_extracted = turn_count > 0 or session_count > 0
    
    if not is_extracted:
        return None
    
    return {
        "workspace_id": workspace_ids[0],
        "agent": agent,
        "is_extracted": is_extracted,
        "session_count": session_count,
        "turn_count": turn_count,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
    }


def query_all_workspace_statuses(conn: sqlite3.Connection) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Get status for all workspaces in the database.
    
    Args:
        conn: SQLite connection
        
    Returns:
        Dict mapping workspace_id -> agent -> status_dict
    """
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    
    # Get all workspaces with turns
    cursor = conn.execute(
        """SELECT workspace_id, agent_used, 
                  COUNT(*) as turn_count,
                  COUNT(DISTINCT session_id) as session_count,
                  MIN(timestamp_iso) as first_ts,
                  MAX(timestamp_iso) as last_ts
           FROM turns 
           WHERE workspace_id IS NOT NULL
           GROUP BY workspace_id, agent_used"""
    )
    
    for row in cursor:
        workspace_id = row[0]
        agent_raw = row[1] or "unknown"
        # Normalize agent name
        agent = _normalize_agent_name(agent_raw)
        
        if workspace_id not in result:
            result[workspace_id] = {}
        
        result[workspace_id][agent] = {
            "workspace_id": workspace_id,
            "agent": agent,
            "is_extracted": True,
            "session_count": row[3] or 0,
            "turn_count": row[2] or 0,
            "first_timestamp": row[4],
            "last_timestamp": row[5],
        }
    
    return result


def query_database_workspaces(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """Get all workspaces that have data in the database.
    
    This returns workspace info derived from the turns table,
    which may include workspaces that no longer exist on disk.
    
    Args:
        conn: SQLite connection
        
    Returns:
        Dict mapping workspace_id -> workspace_info dict
    """
    result: Dict[str, Dict[str, Any]] = {}
    
    # Get workspace info from turns table
    cursor = conn.execute(
        """SELECT workspace_id, workspace_name, workspace_folder, agent_used,
                  COUNT(*) as turn_count,
                  COUNT(DISTINCT session_id) as session_count,
                  MIN(timestamp_iso) as first_ts,
                  MAX(timestamp_iso) as last_ts
           FROM turns 
           WHERE workspace_id IS NOT NULL
           GROUP BY workspace_id, workspace_name, workspace_folder, agent_used"""
    )
    
    # Group by workspace_id, collecting agents
    workspace_data: Dict[str, Dict[str, Any]] = {}
    for row in cursor:
        ws_id = row[0]
        ws_name = row[1] or ""
        ws_folder = row[2] or ""
        agent_raw = row[3] or "unknown"
        # Normalize agent name
        agent = _normalize_agent_name(agent_raw)
        turn_count = row[4] or 0
        session_count = row[5] or 0
        first_ts = row[6]
        last_ts = row[7]
        
        if ws_id not in workspace_data:
            workspace_data[ws_id] = {
                "workspace_name": ws_name,
                "workspace_folder": ws_folder,
                "agents": set(),
                "session_count": 0,
                "turn_count": 0,
                "first_timestamp": first_ts,
                "last_timestamp": last_ts,
            }
        
        workspace_data[ws_id]["agents"].add(agent)
        workspace_data[ws_id]["session_count"] += session_count
        workspace_data[ws_id]["turn_count"] += turn_count
        
        # Update timestamps
        if first_ts:
            existing_first = workspace_data[ws_id]["first_timestamp"]
            if not existing_first or first_ts < existing_first:
                workspace_data[ws_id]["first_timestamp"] = first_ts
        if last_ts:
            existing_last = workspace_data[ws_id]["last_timestamp"]
            if not existing_last or last_ts > existing_last:
                workspace_data[ws_id]["last_timestamp"] = last_ts
    
    # Build result
    for ws_id, data in workspace_data.items():
        result[ws_id] = {
            "workspace_id": ws_id,
            "workspace_name": data["workspace_name"],
            "workspace_folder": data["workspace_folder"],
            "agents": sorted(list(data["agents"])),
            "session_count": data["session_count"],
            "turn_count": data["turn_count"],
            "first_timestamp": data["first_timestamp"],
            "last_timestamp": data["last_timestamp"],
        }
    
    return result


def query_workspace_sessions(
    conn: sqlite3.Connection,
    workspace_id: str,
    agent: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get all sessions for a workspace.
    
    Derives session info from the turns table.
    
    Args:
        conn: SQLite connection
        workspace_id: The workspace ID
        agent: Optional agent type to filter by (if None or 'all', returns all agents)
        
    Returns:
        List of session dicts
    """
    # Build agent filter - if agent is empty or 'all', don't filter
    if agent and agent.lower() not in ('all', 'unknown', ''):
        agent_filter = f"%{agent.lower()}%"
        cursor = conn.execute(
            """SELECT session_id, session_name,
                      COUNT(*) as turn_count,
                      MIN(timestamp_iso) as first_timestamp,
                      MAX(timestamp_iso) as last_timestamp,
                      SUM(COALESCE(total_lines_added, 0)) as total_lines_added,
                      SUM(COALESCE(total_lines_removed, 0)) as total_lines_removed,
                      GROUP_CONCAT(DISTINCT primary_language) as languages
               FROM turns 
               WHERE workspace_id = ? AND LOWER(agent_used) LIKE ?
               GROUP BY session_id
               ORDER BY first_timestamp DESC""",
            (workspace_id, agent_filter)
        )
    else:
        cursor = conn.execute(
            """SELECT session_id, session_name,
                      COUNT(*) as turn_count,
                      MIN(timestamp_iso) as first_timestamp,
                      MAX(timestamp_iso) as last_timestamp,
                      SUM(COALESCE(total_lines_added, 0)) as total_lines_added,
                      SUM(COALESCE(total_lines_removed, 0)) as total_lines_removed,
                      GROUP_CONCAT(DISTINCT primary_language) as languages
               FROM turns 
               WHERE workspace_id = ?
               GROUP BY session_id
               ORDER BY first_timestamp DESC""",
            (workspace_id,)
        )
    
    sessions = []
    for row in cursor:
        # Parse languages from comma-separated string
        lang_str = row[7] or ""
        languages = [l.strip() for l in lang_str.split(",") if l.strip()]
        
        sessions.append({
            "session_id": row[0],
            "session_name": row[1] or (row[0][:8] if row[0] else "unknown"),
            "turn_count": row[2] or 0,
            "first_timestamp": row[3],
            "last_timestamp": row[4],
            "total_lines_added": row[5] or 0,
            "total_lines_removed": row[6] or 0,
            "languages": languages,
            "total_files_edited": 0,  # Would need to aggregate from turns
        })
    
    return sessions


def _normalize_folder(folder: str) -> str:
    """Normalize a workspace folder path for comparison.
    
    Handles both regular paths (C:\\code\\project) and URI-style paths
    (vscode-remote://wsl+ubuntu/home/...) without mangling the URI scheme.
    """
    if not folder:
        return ""
    # Replace backslashes with forward slashes and lowercase
    return folder.replace("\\", "/").lower()


def query_workspace_sessions_by_folder(
    conn: sqlite3.Connection,
    workspace_folder: str,
    workspace_ids: Optional[List[str]] = None,
    agent: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get all sessions for a workspace by folder path.
    
    This enables cross-agent consolidation by querying using workspace_folder
    instead of workspace_id. When multiple supported agents work on the same
    folder, they may have different workspace_ids but the same
    workspace_folder.
    
    Args:
        conn: SQLite connection
        workspace_folder: The workspace folder path (will be normalized)
        workspace_ids: Optional related workspace IDs to include even when folder strings differ
        agent: Optional agent type to filter by (if None or 'all', returns all agents)
        
    Returns:
        List of session dicts
    """
    # Normalize folder for comparison (URI-safe: don't use Path which mangles ://)
    normalized_folder = _normalize_folder(workspace_folder)
    
    id_filter_sql = ""
    params: List[Any] = [normalized_folder]
    if workspace_ids:
        placeholders = ", ".join("?" for _ in workspace_ids)
        id_filter_sql = f" OR workspace_id IN ({placeholders})"
        params.extend(workspace_ids)

    # Build agent filter - if agent is empty or 'all', don't filter
    if agent and agent.lower() not in ('all', 'unknown', ''):
        agent_filter = agent.lower()
        cursor = conn.execute(
            f"""SELECT session_id, session_name,
                      MIN(parent_session_id) as parent_session_id,
                      MIN(relationship_type) as relationship_type,
                      COUNT(*) as turn_count,
                      MIN(timestamp_iso) as first_timestamp,
                      MAX(timestamp_iso) as last_timestamp,
                      SUM(COALESCE(total_lines_added, 0)) as total_lines_added,
                      SUM(COALESCE(total_lines_removed, 0)) as total_lines_removed,
                      GROUP_CONCAT(DISTINCT primary_language) as languages,
                      GROUP_CONCAT(DISTINCT agent_used) as agents
               FROM turns 
               WHERE (LOWER(REPLACE(workspace_folder, '\\', '/')) = ?{id_filter_sql}) AND LOWER(agent_used) = ?
               GROUP BY session_id
               ORDER BY first_timestamp DESC""",
            (*params, agent_filter)
        )
    else:
        cursor = conn.execute(
            f"""SELECT session_id, session_name,
                      MIN(parent_session_id) as parent_session_id,
                      MIN(relationship_type) as relationship_type,
                      COUNT(*) as turn_count,
                      MIN(timestamp_iso) as first_timestamp,
                      MAX(timestamp_iso) as last_timestamp,
                      SUM(COALESCE(total_lines_added, 0)) as total_lines_added,
                      SUM(COALESCE(total_lines_removed, 0)) as total_lines_removed,
                      GROUP_CONCAT(DISTINCT primary_language) as languages,
                      GROUP_CONCAT(DISTINCT agent_used) as agents
               FROM turns 
               WHERE (LOWER(REPLACE(workspace_folder, '\\', '/')) = ?{id_filter_sql})
               GROUP BY session_id
               ORDER BY first_timestamp DESC""",
            tuple(params)
        )
    
    sessions = []
    for row in cursor:
        # Parse languages from comma-separated string
        lang_str = row[9] or ""
        languages = [l.strip() for l in lang_str.split(",") if l.strip()]
        
        # Parse agents from comma-separated string
        agents_str = row[10] or "" if len(row) > 10 else ""
        agents = [a.strip() for a in agents_str.split(",") if a.strip()]
        
        sessions.append({
            "session_id": row[0],
            "session_name": row[1] or (row[0][:8] if row[0] else "unknown"),
            "parent_session_id": row[2],
            "relationship_type": row[3],
            "turn_count": row[4] or 0,
            "first_timestamp": row[5],
            "last_timestamp": row[6],
            "total_lines_added": row[7] or 0,
            "total_lines_removed": row[8] or 0,
            "languages": languages,
            "agents": agents,
            "total_files_edited": 0,  # Would need to aggregate from turns
        })
    
    return sessions


def query_workspace_sessions_for_ids(
    conn: sqlite3.Connection,
    workspace_ids: List[str],
    agent: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not workspace_ids:
        return []

    placeholders = ", ".join("?" for _ in workspace_ids)
    params: List[Any] = list(workspace_ids)
    agent_sql = ""
    if agent and agent.lower() not in ("all", "unknown", ""):
        agent_sql = " AND LOWER(agent_used) = ?"
        params.append(agent.lower())

    cursor = conn.execute(
        f"""SELECT session_id, session_name,
                  MIN(parent_session_id) as parent_session_id,
                  MIN(relationship_type) as relationship_type,
                  COUNT(*) as turn_count,
                  MIN(timestamp_iso) as first_timestamp,
                  MAX(timestamp_iso) as last_timestamp,
                  SUM(COALESCE(total_lines_added, 0)) as total_lines_added,
                  SUM(COALESCE(total_lines_removed, 0)) as total_lines_removed,
                  GROUP_CONCAT(DISTINCT primary_language) as languages,
                  GROUP_CONCAT(DISTINCT agent_used) as agents
           FROM turns
           WHERE workspace_id IN ({placeholders}){agent_sql}
           GROUP BY session_id
           ORDER BY first_timestamp DESC""",
        tuple(params),
    )

    sessions: List[Dict[str, Any]] = []
    for row in cursor:
        lang_str = row[9] or ""
        languages = [l.strip() for l in lang_str.split(",") if l.strip()]
        agents_str = row[10] or ""
        agents = [a.strip() for a in agents_str.split(",") if a.strip()]
        sessions.append(
            {
                "session_id": row[0],
                "session_name": row[1] or (row[0][:8] if row[0] else "unknown"),
                "parent_session_id": row[2],
                "relationship_type": row[3],
                "turn_count": row[4] or 0,
                "first_timestamp": row[5],
                "last_timestamp": row[6],
                "total_lines_added": row[7] or 0,
                "total_lines_removed": row[8] or 0,
                "languages": languages,
                "agents": agents,
                "total_files_edited": 0,
            }
        )
    return sessions


def query_session_source_metadata(
    conn: sqlite3.Connection,
    workspace_ids: List[str],
    session_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    if not workspace_ids or not session_ids:
        return {}

    workspace_placeholders = ", ".join("?" for _ in workspace_ids)
    session_placeholders = ", ".join("?" for _ in session_ids)
    params: List[Any] = [*workspace_ids, *session_ids]

    cursor = conn.execute(
        f"""SELECT ps.session_id, ps.metadata_json, pe.event_index, pe.raw_json
           FROM parsed_sessions ps
           JOIN parsed_workspaces pw ON pw.id = ps.workspace_row_id
           LEFT JOIN parsed_events pe ON pe.session_row_id = ps.id AND pe.event_index < 8
           WHERE pw.workspace_id IN ({workspace_placeholders})
             AND ps.session_id IN ({session_placeholders})
           ORDER BY ps.session_id ASC, pe.event_index ASC""",
        tuple(params),
    )

    results: Dict[str, Dict[str, Any]] = {}
    for row in cursor:
        session_id = row[0]
        metadata = results.setdefault(session_id, {})

        if row[1]:
            parsed_metadata = parse_json_field(row[1], {})
            if isinstance(parsed_metadata, dict):
                for key in ("entrypoint", "user_type", "is_sidechain", "cwd", "version", "session_origin_label", "is_headless_session"):
                    value = parsed_metadata.get(key)
                    if value not in (None, "") and key not in metadata:
                        metadata[key] = value

        if row[3]:
            try:
                raw_event = json.loads(row[3]) if isinstance(row[3], str) else row[3]
            except (json.JSONDecodeError, TypeError):
                raw_event = {}
            if isinstance(raw_event, dict):
                if raw_event.get("entrypoint") and "entrypoint" not in metadata:
                    metadata["entrypoint"] = raw_event.get("entrypoint")
                if raw_event.get("userType") and "user_type" not in metadata:
                    metadata["user_type"] = raw_event.get("userType")
                if raw_event.get("isSidechain") is not None and "is_sidechain" not in metadata:
                    metadata["is_sidechain"] = raw_event.get("isSidechain")
                if raw_event.get("cwd") and "cwd" not in metadata:
                    metadata["cwd"] = raw_event.get("cwd")
                if raw_event.get("version") and "version" not in metadata:
                    metadata["version"] = raw_event.get("version")

        if metadata.get("entrypoint") == "sdk-cli":
            metadata.setdefault("session_origin_label", "sdk-cli")
            metadata.setdefault("is_headless_session", True)

    return results


def query_session_turns(conn: sqlite3.Connection, session_id: str) -> List[Dict[str, Any]]:
    """Get all turns for a session.
    
    Args:
        conn: SQLite connection
        session_id: The session ID
        
    Returns:
        List of turn dicts ordered by turn number
    """
    cursor = conn.execute(
        """SELECT turn, role, text, original_text, timestamp_iso, 
                  model_id, agent_used, session_name, files, tools,
                  total_lines_added, total_lines_removed, request_id
           FROM turns 
           WHERE session_id = ?
           ORDER BY turn ASC""",
        (session_id,)
    )

    tool_runs_by_turn = query_turn_tool_calls_for_session(conn, session_id)
    subagent_runs_by_turn = query_turn_subagent_runs_for_session(conn, session_id)
    code_edits_by_turn = query_turn_code_edits_for_session(conn, session_id)
    
    turns = []
    for row in cursor:
        # Parse JSON fields
        files = []
        tools = []
        try:
            if row[8]:
                files = json.loads(row[8]) if isinstance(row[8], str) else row[8]
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            if row[9]:
                tools = json.loads(row[9]) if isinstance(row[9], str) else row[9]
        except (json.JSONDecodeError, TypeError):
            pass
        
        turns.append({
            "turn": row[0],
            "role": row[1],
            "text": row[2],
            "original_text": row[3],
            "timestamp_iso": row[4],
            "model_id": row[5],
            "agent_used": row[6],
            "session_name": row[7],
            "files": files,
            "tools": tools,
            "lines_added": row[10] or 0,
            "lines_removed": row[11] or 0,
            "tool_runs": tool_runs_by_turn.get(row[0], []),
            "subagent_runs": subagent_runs_by_turn.get(row[0], []),
            "files_edited": len(code_edits_by_turn.get(row[0], [])),
            "code_edits": code_edits_by_turn.get(row[0], []),
        })

    return turns


def query_turn_tool_calls_for_session(
    conn: sqlite3.Connection,
    session_id: str,
) -> Dict[int, List[Dict[str, Any]]]:
    ensure_turn_detail_tables(conn)
    cursor = conn.execute(
        """
        SELECT turn, tool_index, call_id, name, kind, arguments_json, arguments_text,
               file_paths_json, spawned_session_id, status, display_text, results_json,
               raw_call_json, extra_json
        FROM turn_tool_calls
        WHERE session_id = ?
        ORDER BY turn ASC, tool_index ASC
        """,
        (session_id,),
    )

    results: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in cursor.fetchall():
        results[int(row[0])].append(
            {
                "tool_index": row[1],
                "call_id": row[2] or "",
                "name": row[3] or "",
                "kind": row[4] or "",
                "arguments": parse_json_field(row[5], {}),
                "arguments_text": row[6] or "",
                "file_paths": parse_json_field(row[7], []),
                "spawned_session_id": row[8],
                "status": row[9] or "",
                "display_text": row[10] or "",
                "results": parse_json_field(row[11], []),
                "raw_call": parse_json_field(row[12], {}),
                "extra": parse_json_field(row[13], {}),
            }
        )
    return dict(results)


def query_turn_code_edits_for_session(
    conn: sqlite3.Connection,
    session_id: str,
) -> Dict[int, List[Dict[str, Any]]]:
    cursor = conn.execute(
        """
        SELECT t.turn, cm.file_path, cm.lines_added, cm.lines_removed,
               cm.code_before, cm.code_after, cm.before_metrics,
               cm.after_metrics, cm.delta_metrics
        FROM turns t
        JOIN code_metrics cm
          ON cm.session_id = t.session_id
         AND cm.request_id = t.request_id
        WHERE t.session_id = ?
          AND COALESCE(t.request_id, '') != ''
          AND COALESCE(cm.request_id, '') != ''
        ORDER BY t.turn ASC, cm.file_path ASC
        """,
        (session_id,),
    )

    results: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in cursor.fetchall():
        results[int(row[0])].append(
            {
                "file_path": row[1] or "",
                "lines_added": row[2] or 0,
                "lines_removed": row[3] or 0,
                "code_before": row[4] or None,
                "code_after": row[5] or None,
                "before_metrics": parse_json_field(row[6], {}),
                "after_metrics": parse_json_field(row[7], {}),
                "delta_metrics": parse_json_field(row[8], {}),
            }
        )
    return dict(results)


def query_turn_subagent_runs_for_session(
    conn: sqlite3.Connection,
    session_id: str,
) -> Dict[int, List[Dict[str, Any]]]:
    ensure_turn_detail_tables(conn)
    cursor = conn.execute(
        """
        SELECT turn, subagent_index, subagent_session_id, source_tool_call_id,
               source_tool_name, relationship_type, title, prompt_text, result_text,
               turn_count, total_lines_added, total_lines_removed,
               started_at_ms, ended_at_ms, extra_json
        FROM turn_subagent_runs
        WHERE session_id = ?
        ORDER BY turn ASC, subagent_index ASC
        """,
        (session_id,),
    )

    results: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in cursor.fetchall():
        results[int(row[0])].append(
            {
                "subagent_index": row[1],
                "subagent_session_id": row[2],
                "source_tool_call_id": row[3] or "",
                "source_tool_name": row[4] or "",
                "relationship_type": row[5] or "",
                "title": row[6] or row[2],
                "prompt_text": row[7] or "",
                "result_text": row[8] or "",
                "turn_count": row[9] or 0,
                "total_lines_added": row[10] or 0,
                "total_lines_removed": row[11] or 0,
                "started_at_ms": row[12],
                "ended_at_ms": row[13],
                "extra": parse_json_field(row[14], {}),
            }
        )
    return dict(results)


# =============================================================================
# Session file meta (incremental parse support)
# =============================================================================

def get_session_file_meta(
    conn: sqlite3.Connection,
    session_id: str,
    agent: str,
) -> Optional[Dict[str, Any]]:
    """Return stored meta for a session file, or None if not recorded yet."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT file_path, file_size, last_offset, message_count, updated_at
        FROM session_file_meta
        WHERE session_id = ? AND agent = ?
        """,
        (session_id, agent),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "file_path": row[0] or "",
        "file_size": row[1] or 0,
        "last_offset": row[2] or 0,
        "message_count": row[3] or 0,
        "updated_at": row[4] or "",
    }


def upsert_session_file_meta(
    conn: sqlite3.Connection,
    session_id: str,
    agent: str,
    file_path: str,
    file_size: int,
    last_offset: int,
    message_count: int = 0,
) -> None:
    """Record or update the parse position for a session file."""
    from datetime import datetime

    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO session_file_meta
            (session_id, agent, file_path, file_size, last_offset, message_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, agent)
        DO UPDATE SET
            file_path = excluded.file_path,
            file_size = excluded.file_size,
            last_offset = excluded.last_offset,
            message_count = excluded.message_count,
            updated_at = excluded.updated_at
        """,
        (
            session_id,
            agent,
            file_path,
            file_size,
            last_offset,
            message_count,
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
