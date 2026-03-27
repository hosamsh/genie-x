"""Cursor Chat Data Extractor."""
from __future__ import annotations

import json
import os
import platform
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.shared.models.turn import Turn
from src.shared.io.paths import normalize_path, decode_file_uri, is_contained_in
from src.shared.logging.logger import get_logger

from .bubbles import BubbleData, parse_bubble, parse_timestamp
from .turns import WorkspaceMeta, TurnBuilder

logger = get_logger(__name__)


def _get_cursor_user_dir() -> Path:
    """Get Cursor User directory for current platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", "")) / "Cursor/User"
    elif system == "Darwin":
        return Path.home() / "Library/Application Support/Cursor/User"
    else:
        return Path.home() / ".config/Cursor/User"


def get_workspace_storage() -> Path:
    """Get Cursor workspace storage path for current platform."""
    return _get_cursor_user_dir() / "workspaceStorage"


def get_global_storage() -> Path:
    """Get Cursor global storage path for current platform."""
    return _get_cursor_user_dir() / "globalStorage"


def _get_global_db_path() -> Path:
    """Get the global state.vscdb path."""
    return get_global_storage() / "state.vscdb"


# =============================================================================
# Database Query Functions
# =============================================================================

def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if a table exists in the database."""
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return cursor.fetchone() is not None
    except sqlite3.Error:
        return False


def _query_composer_data(conn: sqlite3.Connection, composer_id: str) -> Optional[Dict[str, Any]]:
    """Query composerData from cursorDiskKV table."""
    try:
        cursor = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{composer_id}",)
        )
        row = cursor.fetchone()
        if row and row[0]:
            value = row[0]
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="ignore")
            return json.loads(value)
    except (sqlite3.Error, json.JSONDecodeError) as e:
        logger.debug(f"Error querying composer {composer_id}: {e}")
    return None


def _query_bubble_data(conn: sqlite3.Connection, composer_id: str, bubble_id: str) -> Optional[Dict[str, Any]]:
    """Query bubble data from cursorDiskKV table."""
    try:
        cursor = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"bubbleId:{composer_id}:{bubble_id}",)
        )
        row = cursor.fetchone()
        if row and row[0]:
            value = row[0]
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="ignore")
            return json.loads(value)
    except (sqlite3.Error, json.JSONDecodeError) as e:
        logger.debug(f"Error querying bubble {bubble_id}: {e}")
    return None


def _query_inline_diffs(conn: sqlite3.Connection, composer_id: str) -> Dict[str, Dict[str, Any]]:
    """Query all inlineDiffUndoRedo entries for a composer.
    
    Returns a dict mapping codeblock_id -> {before_lines, after_lines, file_path}
    """
    result = {}
    try:
        cursor = conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'inlineDiffUndoRedo%'"
        )
        for row in cursor:
            key, value = row
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="ignore")
            try:
                data = json.loads(value)
                metadata = data.get("composerMetadata", {})
                if metadata.get("composerId") == composer_id:
                    codeblock_id = metadata.get("codeblockId", "")
                    if codeblock_id:
                        uri = data.get("uri", {})
                        file_path = uri.get("fsPath", "") or uri.get("path", "")
                        result[codeblock_id] = {
                            "before_lines": data.get("originalTextLines", []),
                            "after_lines": data.get("newTextLines", []),
                            "file_path": normalize_path(file_path),
                        }
            except json.JSONDecodeError:
                continue
    except sqlite3.Error as e:
        logger.debug(f"Error querying inline diffs: {e}")
    return result


# =============================================================================
# Workspace Discovery
# =============================================================================

def discover_workspaces(
    workspace_storage: Optional[Path] = None,
    global_db_path: Optional[Path] = None
) -> List[WorkspaceMeta]:
    """Discover all workspaces with extractable Cursor chat sessions.
    
    Steps:
    1. Scan workspace directories in workspaceStorage
    2. For each workspace, read composer.composerData from ItemTable
    3. Validate each composer ID has extractable data
    4. Return only workspaces with validated sessions
    """
    workspace_storage = workspace_storage or get_workspace_storage()
    global_db_path = global_db_path or _get_global_db_path()
    
    if not workspace_storage.exists():
        logger.debug(f"Workspace storage not found: {workspace_storage}")
        return []
    
    # Open global database once for all validation (optional - some data lives here)
    global_conn = None
    if global_db_path.exists():
        try:
            global_conn = sqlite3.connect(str(global_db_path))
        except sqlite3.Error as e:
            logger.debug(f"Could not open Cursor global database (may be locked): {e}")
    
    workspaces = []
    
    try:
        for folder in workspace_storage.iterdir():
            if not folder.is_dir():
                continue

            # Containment guard: reject symlinks that escape workspace_storage
            if not is_contained_in(folder, workspace_storage):
                logger.warning("Workspace folder escapes storage root, skipping: %s", folder)
                continue
            
            workspace_db = folder / "state.vscdb"
            if not workspace_db.exists():
                continue

            try:
                meta = _load_workspace_meta(folder, workspace_db, global_conn)
                if meta and meta.composer_ids:
                    workspaces.append(meta)
            except Exception as e:
                logger.debug(f"Error loading workspace {folder.name}: {e}")
                continue
    finally:
        if global_conn:
            global_conn.close()
    
    return workspaces


def _load_workspace_meta(
    folder: Path, 
    workspace_db: Path,
    global_conn: Optional[sqlite3.Connection]
) -> Optional[WorkspaceMeta]:
    """Load workspace metadata and validate composer IDs."""
    workspace_id = folder.name
    workspace_name = workspace_id
    workspace_folder = ""
    
    # Parse workspace.json for folder path
    ws_json = folder / "workspace.json"
    if ws_json.exists():
        try:
            data = json.loads(ws_json.read_text(encoding="utf-8"))
            uri = data.get("folder") or data.get("folderUri", "")
            if uri:
                workspace_folder = decode_file_uri(uri)
                if workspace_folder:
                    workspace_name = Path(workspace_folder).name or workspace_folder
        except (json.JSONDecodeError, OSError):
            pass
    
    # Get composer list from workspace's ItemTable
    composer_ids = []
    workspace_conn = None
    try:
        workspace_conn = sqlite3.connect(str(workspace_db))
        cursor = workspace_conn.execute(
            "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
        )
        row = cursor.fetchone()
        if row and row[0]:
            value = row[0]
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="ignore")
            data = json.loads(value)
            all_composers = data.get("allComposers", [])
            
            # Validate each composer has extractable data
            for composer_info in all_composers:
                composer_id = composer_info.get("composerId") if isinstance(composer_info, dict) else None
                if composer_id and _validate_composer_has_data(
                    composer_id, global_conn, workspace_conn
                ):
                    composer_ids.append(composer_id)
    except (sqlite3.Error, json.JSONDecodeError) as e:
        logger.debug(f"Error reading composers from workspace {workspace_id}: {e}")
    finally:
        if workspace_conn:
            workspace_conn.close()
    
    if not composer_ids:
        return None
    
    return WorkspaceMeta(
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        workspace_folder=workspace_folder,
        path=folder,
        composer_ids=composer_ids,
    )


def _validate_composer_has_data(
    composer_id: str,
    global_conn: Optional[sqlite3.Connection],
    workspace_conn: sqlite3.Connection
) -> bool:
    """Validate that a composer has actual extractable content."""
    # Try global database first
    if global_conn:
        data = _query_composer_data(global_conn, composer_id)
        if data:
            has_content = (
                len(data.get("fullConversationHeadersOnly", [])) > 0 or
                len(data.get("conversation", [])) > 0
            )
            if has_content:
                return True
    
    # Fallback: check workspace database
    if _table_exists(workspace_conn, "cursorDiskKV"):
        data = _query_composer_data(workspace_conn, composer_id)
        if data:
            has_content = (
                len(data.get("fullConversationHeadersOnly", [])) > 0 or
                len(data.get("conversation", [])) > 0
            )
            if has_content:
                return True
    
    return False


# =============================================================================
# Session Extraction
# =============================================================================

def extract_session(
    composer_id: str,
    workspace_meta: WorkspaceMeta,
    global_conn: Optional[sqlite3.Connection],
    workspace_conn: Optional[sqlite3.Connection],
) -> List[Turn]:
    """Extract all turns from a single composer session."""
    # Get composer data (try global first, then workspace)
    composer_data = None
    conn_to_use = None
    
    if global_conn:
        composer_data = _query_composer_data(global_conn, composer_id)
        if composer_data:
            conn_to_use = global_conn
    
    if not composer_data and workspace_conn:
        if _table_exists(workspace_conn, "cursorDiskKV"):
            composer_data = _query_composer_data(workspace_conn, composer_id)
            if composer_data:
                conn_to_use = workspace_conn
    
    if not composer_data or not conn_to_use:
        logger.debug(f"No data found for composer {composer_id}")
        return []
    
    # Detect schema (A = new format, B = legacy)
    headers_only = composer_data.get("fullConversationHeadersOnly", [])
    inline_conversation = composer_data.get("conversation", [])
    
    if headers_only and len(headers_only) > 0:
        # Schema A: New format - fetch bubbles separately
        bubbles = _extract_bubbles_schema_a(conn_to_use, composer_id, headers_only)
    elif inline_conversation and len(inline_conversation) > 0:
        # Schema B: Legacy format - bubbles inline
        bubbles = _extract_bubbles_schema_b(inline_conversation)
    else:
        logger.debug(f"No conversation data in composer {composer_id}")
        return []
    
    if not bubbles:
        return []
    
    # Extract additional data for code edits
    inline_diffs = _query_inline_diffs(conn_to_use, composer_id)
    
    original_file_states = {}
    for uri, state_data in composer_data.get("originalFileStates", {}).items():
        if isinstance(state_data, dict) and "content" in state_data:
            original_file_states[uri] = state_data["content"]
    
    # Get session metadata
    session_name = composer_data.get("name", "") or ""
    session_timestamp = composer_data.get("createdAt") or composer_data.get("lastUpdatedAt")
    session_ts_ms, _ = parse_timestamp(session_timestamp)
    
    # Extract model info from usageData or modelConfig for propagation
    usage_data = composer_data.get("usageData", {})
    model_config = composer_data.get("modelConfig", {})
    
    # If single model in usageData, propagate to all assistant bubbles without model_info
    if len(usage_data) == 1:
        default_model = list(usage_data.keys())[0]
        for bubble in bubbles:
            if bubble.type == 2 and not bubble.model_info:  # assistant
                bubble.model_info = default_model
    elif not usage_data and model_config.get("modelName"):
        # Fallback to modelConfig
        default_model = model_config["modelName"]
        for bubble in bubbles:
            if bubble.type == 2 and not bubble.model_info:
                bubble.model_info = default_model
    
    # Build turns with merging
    builder = TurnBuilder(
        session_id=composer_id,
        workspace_meta=workspace_meta,
        session_name=session_name,
        session_timestamp_ms=session_ts_ms,
        inline_diffs=inline_diffs,
        original_file_states=original_file_states,
    )
    
    return builder.build_turns(bubbles)


def _extract_bubbles_schema_a(
    conn: sqlite3.Connection, 
    composer_id: str, 
    headers: List[Dict[str, Any]]
) -> List[BubbleData]:
    """Extract bubbles using Schema A (fullConversationHeadersOnly)."""
    bubbles = []
    last_timestamp_ms = None
    last_timestamp_iso = None
    last_model_info = None
    
    for header in headers:
        bubble_id = header.get("bubbleId", "")
        if not bubble_id:
            continue
        
        bubble_data = _query_bubble_data(conn, composer_id, bubble_id)
        if not bubble_data:
            # Use header info if bubble not found
            bubble = BubbleData(
                bubble_id=bubble_id,
                type=header.get("type", 0),
            )
        else:
            bubble = parse_bubble(bubble_id, bubble_data)
        
        # Propagate timestamp if missing
        if not bubble.timestamp_ms:
            bubble.timestamp_ms = last_timestamp_ms
            bubble.timestamp_iso = last_timestamp_iso
        else:
            last_timestamp_ms = bubble.timestamp_ms
            last_timestamp_iso = bubble.timestamp_iso
        
        # Propagate model_info for assistant bubbles
        if bubble.type == 2:  # assistant
            if bubble.model_info:
                last_model_info = bubble.model_info
            elif last_model_info:
                bubble.model_info = last_model_info
        
        bubbles.append(bubble)
    
    return bubbles


def _extract_bubbles_schema_b(conversation: List[Dict[str, Any]]) -> List[BubbleData]:
    """Extract bubbles using Schema B (inline conversation)."""
    bubbles = []
    last_timestamp_ms = None
    last_timestamp_iso = None
    last_model_info = None
    
    for item in conversation:
        bubble_id = item.get("bubbleId", "")
        if not bubble_id:
            continue
        
        bubble = parse_bubble(bubble_id, item)
        
        # Propagate timestamp if missing
        if not bubble.timestamp_ms:
            bubble.timestamp_ms = last_timestamp_ms
            bubble.timestamp_iso = last_timestamp_iso
        else:
            last_timestamp_ms = bubble.timestamp_ms
            last_timestamp_iso = bubble.timestamp_iso
        
        # Propagate model_info for assistant bubbles
        if bubble.type == 2:  # assistant
            if bubble.model_info:
                last_model_info = bubble.model_info
            elif last_model_info:
                bubble.model_info = last_model_info
        
        bubbles.append(bubble)
    
    return bubbles


# =============================================================================
# Workspace Extraction
# =============================================================================

def extract_workspace(
    meta: WorkspaceMeta,
    global_db_path: Optional[Path] = None,
) -> Tuple[List[Turn], int]:
    """Extract all turns from a single workspace.
    
    Returns:
        Tuple of (turns, session_count)
    """
    global_db_path = global_db_path or _get_global_db_path()
    workspace_db = meta.path / "state.vscdb"
    
    global_conn = None
    workspace_conn = None
    
    try:
        if global_db_path.exists():
            global_conn = sqlite3.connect(str(global_db_path))
        
        if workspace_db.exists():
            workspace_conn = sqlite3.connect(str(workspace_db))
        
        all_turns: List[Turn] = []
        session_count = 0
        
        for composer_id in meta.composer_ids:
            try:
                turns = extract_session(
                    composer_id=composer_id,
                    workspace_meta=meta,
                    global_conn=global_conn,
                    workspace_conn=workspace_conn,
                )
                if turns:
                    all_turns.extend(turns)
                    session_count += 1
            except Exception as e:
                logger.warning(f"Failed to extract session {composer_id}: {e}")
                continue
        
        return all_turns, session_count
        
    finally:
        if global_conn:
            global_conn.close()
        if workspace_conn:
            workspace_conn.close()


# =============================================================================
# Utility Functions
# =============================================================================

def count_merged_turns(
    composer_id: str,
    global_conn: Optional[sqlite3.Connection],
    workspace_conn: Optional[sqlite3.Connection],
) -> int:
    """Count turns by simulating merge logic without full extraction."""
    # Get composer data
    composer_data = None
    
    if global_conn:
        composer_data = _query_composer_data(global_conn, composer_id)
    
    if not composer_data and workspace_conn:
        if _table_exists(workspace_conn, "cursorDiskKV"):
            composer_data = _query_composer_data(workspace_conn, composer_id)
    
    if not composer_data:
        return 0
    
    # Get message list
    headers = composer_data.get("fullConversationHeadersOnly", [])
    if not headers:
        headers = composer_data.get("conversation", [])
    
    # Count role transitions
    turn_count = 0
    last_role = None
    
    for msg in headers:
        msg_type = msg.get("type", 0)
        if msg_type == 1:
            role = "user"
        elif msg_type == 2:
            role = "assistant"
        else:
            continue
        
        if role != last_role:
            turn_count += 1
            last_role = role
    
    return turn_count


def get_workspace_activity(
    meta: WorkspaceMeta,
    global_db_path: Optional[Path] = None,
) -> Tuple[int, int, List[str]]:
    """Get quick stats for a workspace without full extraction.
    
    Returns:
        Tuple of (session_count, turn_count, session_ids)
    """
    global_db_path = global_db_path or _get_global_db_path()
    workspace_db = meta.path / "state.vscdb"
    
    global_conn = None
    workspace_conn = None
    
    try:
        if global_db_path.exists():
            global_conn = sqlite3.connect(str(global_db_path))
        
        if workspace_db.exists():
            workspace_conn = sqlite3.connect(str(workspace_db))
        
        session_ids = []
        turn_count = 0
        
        for composer_id in meta.composer_ids:
            try:
                count = count_merged_turns(composer_id, global_conn, workspace_conn)
                if count > 0:
                    session_ids.append(composer_id)
                    turn_count += count
            except Exception:
                continue
        
        return len(session_ids), turn_count, session_ids
        
    finally:
        if global_conn:
            global_conn.close()
        if workspace_conn:
            workspace_conn.close()
