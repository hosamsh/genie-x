"""Copilot Chat Data Extractor."""
from __future__ import annotations

import json
import os
import platform
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.shared.models.turn import Turn
from src.shared.io.paths import normalize_path, decode_file_uri, is_valid_session_id
from src.shared.logging.logger import get_logger

logger = get_logger(__name__)

from .edits import extract_edits


def get_workspace_storage() -> Path:
    """Get VS Code workspace storage path for current platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", "")) / "Code/User/workspaceStorage"
    elif system == "Darwin":
        return Path.home() / "Library/Application Support/Code/User/workspaceStorage"
    else:
        return Path.home() / ".config/Code/User/workspaceStorage"


def get_global_storage() -> Path:
    """Get VS Code global storage path for current platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", "")) / "Code/User/globalStorage"
    elif system == "Darwin":
        return Path.home() / "Library/Application Support/Code/User/globalStorage"
    else:
        return Path.home() / ".config/Code/User/globalStorage"


@dataclass
class WorkspaceMeta:
    """Workspace metadata."""
    workspace_id: str
    workspace_name: str
    workspace_folder: str
    path: Path
    titles: dict[str, str]  # session_id -> title
    # When set, overrides the default ``path / "chatSessions"`` lookup so that
    # global-storage sessions (emptyWindowChatSessions, transferredChatSessions)
    # can reuse the same extraction pipeline.
    chat_sessions_dir: Path | None = field(default=None)


def _resolve_session_files(chat_dir: Path) -> list[Path]:
    """Return all session files in *chat_dir*, preferring `.jsonl` over `.json` for the same stem.

    When both ``session.json`` and ``session.jsonl`` exist, only the `.jsonl`
    file is returned so callers never process the same session twice.
    """
    jsonl_by_stem = {p.stem: p for p in chat_dir.glob("*.jsonl")}
    json_by_stem  = {p.stem: p for p in chat_dir.glob("*.json")}
    # Merge: .jsonl wins on collision
    merged = {**json_by_stem, **jsonl_by_stem}
    return list(merged.values())


def discover_workspaces(base: Path | None = None) -> list[WorkspaceMeta]:
    """Discover all workspaces with chat sessions."""
    base = base or get_workspace_storage()
    workspaces = []
    
    if not base.exists():
        return []
        
    for folder in base.iterdir():
        if not folder.is_dir():
            continue
        chat_dir = folder / "chatSessions"
        if not chat_dir.exists():
            continue
        
        # Check for non-empty sessions (both .json and .jsonl)
        sessions = _resolve_session_files(chat_dir)
        if not sessions:
            continue
        
        has_content = any(not _is_empty_session(s) for s in sessions)
        if not has_content:
            continue
        
        # Load metadata
        meta = _load_workspace_meta(folder)
        workspaces.append(meta)
    
    return workspaces


def discover_global_sessions(base: Path | None = None) -> list[WorkspaceMeta]:
    """Discover Copilot chat sessions in VS Code global storage.

    Scans two well-known sub-directories of *globalStorage*:

    * ``emptyWindowChatSessions`` – sessions started without an open folder
    * ``transferredChatSessions`` – sessions migrated from workspace storage

    Each non-empty directory is surfaced as a synthetic :class:`WorkspaceMeta`
    with ``workspace_id = "globalStorage/<subdir_name>"``.

    Args:
        base: Override the default global storage root (used in tests).

    Returns:
        List of :class:`WorkspaceMeta` instances for discovered global sessions.
    """
    base = base or get_global_storage()
    workspaces = []

    for subdir_name in ("emptyWindowChatSessions", "transferredChatSessions"):
        chat_dir = base / subdir_name
        if not chat_dir.exists() or not chat_dir.is_dir():
            continue

        sessions = _resolve_session_files(chat_dir)
        if not sessions:
            continue

        has_content = any(not _is_empty_session(s) for s in sessions)
        if not has_content:
            continue

        workspace_id = f"globalStorage/{subdir_name}"
        workspace_name = "empty-window" if subdir_name == "emptyWindowChatSessions" else "transferred"
        workspaces.append(WorkspaceMeta(
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_folder="",
            path=base,
            titles={},
            chat_sessions_dir=chat_dir,
        ))

    return workspaces


def _is_empty_session(path: Path) -> bool:
    """Quick check if session has no requests (reads first 2 KB for .json, full file for .jsonl)."""
    try:
        if path.suffix == ".jsonl":
            return _is_empty_jsonl_session(path)
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(2048)
        return '"requests": []' in head or '"requests":[]' in head
    except:
        return True


def _is_empty_jsonl_session(path: Path) -> bool:
    """Return True when every line of a JSONL session file is blank."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    return False
        return True
    except (OSError, UnicodeDecodeError):
        return True


def _parse_jsonl_session(path: Path) -> dict:
    """Parse a JSONL session file into the standard session dict structure.

    Handles two formats:

    1. **VS Code state-store delta log** – detected when the first parsed
       line contains a ``kind`` key.  Operations: ``kind=0`` (initial
       snapshot), ``kind=1`` (set at key-path), ``kind=2`` (append to
       array at key-path).  The deltas are replayed to reconstruct the
       full session dict.

    2. **Legacy request-per-line** – each non-empty line is a JSON object
       representing either session metadata (``version`` present,
       ``requestId`` absent) or a request.

    Returns:
        ``{"requests": [...], ...}`` or ``{}`` on I/O error.
    """
    lines: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    lines.append(obj)
    except OSError:
        return {}

    if not lines:
        return {"requests": []}

    # Detect delta format: first line has a "kind" key
    if "kind" in lines[0]:
        return _reconstruct_delta_session(lines)

    # Legacy: request-per-line format
    session_meta: dict = {}
    requests: list[dict] = []
    for i, obj in enumerate(lines):
        if i == 0 and "version" in obj and "requestId" not in obj:
            session_meta = obj
        else:
            requests.append(obj)
    return {**session_meta, "requests": requests}


def _apply_delta(state: dict, key_path: list, value: object, kind: int) -> None:
    """Apply a single delta operation to *state* in-place.

    Args:
        state: The session dict being built.
        key_path: Array of keys/indices describing the target location.
        value: The value to set (kind=1) or list of items to append (kind=2).
        kind: 1 for set, 2 for append.
    """
    if not key_path:
        return

    # Navigate to the parent container
    container: object = state
    for key in key_path[:-1]:
        if isinstance(key, int) and isinstance(container, list):
            while len(container) <= key:
                container.append({})
            container = container[key]
        elif isinstance(container, dict):
            if key not in container:
                container[key] = {}
            container = container[key]
        else:
            return  # unreachable path – skip silently

    final_key = key_path[-1]

    if kind == 1:  # set
        if isinstance(final_key, int) and isinstance(container, list):
            while len(container) <= final_key:
                container.append({})
            container[final_key] = value
        elif isinstance(container, dict):
            container[final_key] = value

    elif kind == 2:  # append
        if isinstance(final_key, int) and isinstance(container, list):
            while len(container) <= final_key:
                container.append({})
            target = container[final_key]
            if isinstance(target, list) and isinstance(value, list):
                target.extend(value)
        elif isinstance(container, dict):
            if final_key not in container:
                container[final_key] = []
            target = container[final_key]
            if isinstance(target, list) and isinstance(value, list):
                target.extend(value)


def _reconstruct_delta_session(lines: list[dict]) -> dict:
    """Reconstruct a full session dict from VS Code state-store delta lines.

    The first line (``kind=0``) supplies the initial snapshot via its ``v``
    field.  Subsequent lines carry ``kind=1`` (set) or ``kind=2`` (append)
    operations, each with a ``k`` key-path and ``v`` value.
    """
    first = lines[0]
    state: dict = first.get("v", {}) if first.get("kind") == 0 else {}

    for op in lines[1:]:
        kind = op.get("kind")
        if kind not in (1, 2):
            continue
        key_path = op.get("k", [])
        value = op.get("v")
        if not key_path:
            continue
        _apply_delta(state, key_path, value, kind)

    # Ensure there is always a "requests" key
    if "requests" not in state:
        state["requests"] = []

    return state


def _load_workspace_meta(folder: Path) -> WorkspaceMeta:
    """Load workspace.json and session titles."""
    workspace_id = folder.name
    workspace_name = workspace_id
    workspace_folder = ""
    
    # Parse workspace.json
    ws_json = folder / "workspace.json"
    if ws_json.exists():
        try:
            data = json.loads(ws_json.read_text(encoding="utf-8"))
            uri = data.get("folder") or data.get("folderUri", "")
            if uri:
                workspace_folder = decode_file_uri(uri)
                workspace_name = Path(workspace_folder).name
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    
    # Load session titles from state.vscdb
    titles = _load_session_titles(folder / "state.vscdb")
    
    return WorkspaceMeta(
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        workspace_folder=workspace_folder,
        path=folder,
        titles=titles,
    )


def _load_session_titles(db_path: Path) -> dict[str, str]:
    """Query state.vscdb for session titles."""
    titles = {}
    if not db_path.exists():
        return titles
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            data = json.loads(row[0])
            for sid, info in data.get("entries", {}).items():
                if isinstance(info, dict) and "title" in info:
                    titles[sid] = info["title"]
    except (sqlite3.Error, json.JSONDecodeError, OSError):
        pass
    return titles


def extract_session(path: Path, meta: WorkspaceMeta) -> list[Turn]:
    """Extract all turns from a chat session file (.json or .jsonl)."""
    session_id = path.stem
    if not is_valid_session_id(session_id):
        logger.warning("Skipping session with invalid ID derived from filename: %s", path.name)
        return []
    if _is_empty_session(path):
        return []
    
    try:
        if path.suffix == ".jsonl":
            data = _parse_jsonl_session(path)
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    
    requests = data.get("requests", [])
    if not requests:
        return []
    
    # Get session name (priority: customTitle, db title, empty)
    session_name = data.get("customTitle", "") or meta.titles.get(session_id, "")
    
    turns = []
    file_mtime_ms = int(path.stat().st_mtime * 1000)
    
    for i, req in enumerate(requests):
        timestamp_ms = _parse_timestamp(req, file_mtime_ms)
        timestamp_iso = datetime.fromtimestamp(
            timestamp_ms / 1000, tz=timezone.utc
        ).isoformat()
        request_id = _find_field(req, ["requestId", "requestUUID", "clientRequestId", "conversationId", "sessionId"])
        model_id = _find_field(req, ["modelId", "model", "responseModel", "modelIdentifier"])
        
        # User turn
        user_text = _extract_user_text(req)
        user_files = _extract_user_files(req)
        turns.append(Turn(
            session_id=session_id,
            turn=i * 2,
            role="user",
            original_text=user_text,
            workspace_id=meta.workspace_id,
            workspace_name=meta.workspace_name,
            workspace_folder=meta.workspace_folder,
            session_name=session_name,
            agent_used="copilot",
            request_id=request_id,
            model_id=model_id,
            timestamp_ms=timestamp_ms,
            timestamp_iso=timestamp_iso,
            ts=str(timestamp_ms),
            files=user_files,
        ))
        
        # Assistant turn
        asst_text, tools, asst_files, thinking = _extract_assistant_response(req)
        response_time_ms = _extract_response_time(req)
        
        # Build extra dict with response time if available
        extra = {}
        if response_time_ms > 0:
            extra["response_time_ms"] = response_time_ms
        
        turns.append(Turn(
            session_id=session_id,
            turn=i * 2 + 1,
            role="assistant",
            original_text=asst_text,
            workspace_id=meta.workspace_id,
            workspace_name=meta.workspace_name,
            workspace_folder=meta.workspace_folder,
            session_name=session_name,
            agent_used="copilot",
            request_id=request_id,
            model_id=model_id,
            timestamp_ms=timestamp_ms,
            timestamp_iso=timestamp_iso,
            ts=str(timestamp_ms),
            files=asst_files,
            tools=tools,
            thinking_text=thinking,
            extra=extra,
        ))
    
    return turns


def _extract_response_time(req: dict) -> int:
    """Extract response time from result.timings.totalElapsed."""
    result = req.get("result", {})
    if isinstance(result, dict):
        timings = result.get("timings", {})
        if isinstance(timings, dict):
            total_elapsed = timings.get("totalElapsed")
            if isinstance(total_elapsed, (int, float)):
                return int(total_elapsed)
    return 0


def _parse_timestamp(req: dict, fallback_ms: int) -> int:
    """Extract timestamp with priority: timestamp > createdAt > fallback."""
    if "timestamp" in req:
        ts = req["timestamp"]
        if isinstance(ts, (int, float)):
            return int(ts)
        if isinstance(ts, str) and ts.isdigit():
            return int(ts)
    
    if "createdAt" in req:
        iso = req["createdAt"]
        try:
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            dt = datetime.fromisoformat(iso)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    
    return fallback_ms


def _find_field(obj: dict, field_names: list[str], visited: set | None = None) -> str:
    """Recursively search for the first matching field."""
    if visited is None:
        visited = set()
    
    obj_id = id(obj)
    if obj_id in visited:
        return ""
    visited.add(obj_id)
    
    # Check direct fields (case-insensitive)
    lower_names = [n.lower() for n in field_names]
    for key, val in obj.items():
        if key.lower() in lower_names and isinstance(val, (str, int)):
            return str(val)
    
    # Recurse into nested objects
    for val in obj.values():
        if isinstance(val, dict):
            result = _find_field(val, field_names, visited)
            if result:
                return result
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    result = _find_field(item, field_names, visited)
                    if result:
                        return result
    return ""


def _extract_user_text(req: dict) -> str:
    """Extract user message text."""
    msg = req.get("message", {})
    if isinstance(msg, dict):
        # Priority 1: message.text
        if msg.get("text"):
            return msg["text"]
        # Priority 2: message.parts[].text
        parts = msg.get("parts", [])
        if parts:
            return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return ""


def _extract_user_files(req: dict) -> list[str]:
    """Extract context files attached by user."""
    files = []
    variables = req.get("variableData", {}).get("variables", [])
    for v in variables:
        if v.get("kind") == "file":
            path = v.get("value", {}).get("path", "")
            if path:
                files.append(normalize_path(path))
    return sorted(set(files))


def _extract_filename_from_ref(ref: dict) -> str:
    """Extract just the filename or symbol name from an inlineReference.
    
    The reference can be:
    1. A URI dict with fsPath/path (file reference)
    2. A symbol reference with 'name' and 'location' (function/class reference)
    
    For symbol references, we prioritize the 'name' field (e.g., "normalize_shape(shape)")
    over extracting from the location URI.
    """
    if not isinstance(ref, dict):
        return ""
    
    # Check for symbol name first (symbol references like functions/classes)
    # These have a 'name' field and optionally a 'location'
    name = ref.get("name", "")
    if name:
        return name
    
    # Try to get path from fsPath or path field (file references)
    path = ref.get("fsPath") or ref.get("path") or ""
    
    # If it's a symbol reference with location but no name, try to get the file
    if not path and "location" in ref:
        loc = ref.get("location", {})
        uri = loc.get("uri", {})
        if isinstance(uri, dict):
            path = uri.get("fsPath") or uri.get("path") or ""
    
    if path:
        # Extract just the filename
        return Path(path).name
    
    return ""


def _extract_assistant_response(req: dict) -> tuple[str, list[str], list[str], str]:
    """Extract assistant response text, tools used, files referenced, and thinking content.
    
    Returns:
        Tuple of (text, tools, files, thinking)
        - text: Regular response text with inlineReferences resolved
        - tools: List of tool names used
        - files: List of file paths referenced  
        - thinking: Concatenated thinking content from reasoning models
    """
    response = req.get("response", [])
    if not isinstance(response, list):
        return "", [], [], ""
    
    text_parts = []
    thinking_parts = []
    tools = set()
    files = set()
    
    # Track code block context to preserve fences around textEditGroup
    in_code_block_context = False
    pending_code_fence = None
    
    for item in response:
        if not isinstance(item, dict):
            continue
        
        kind = item.get("kind", "")
        
        # Handle thinking blocks separately
        if kind == "thinking":
            val = item.get("value", "")
            if isinstance(val, str) and val.strip():
                thinking_parts.append(val.strip())
            continue
        
        # Handle inline references - extract filename and add to text
        if kind == "inlineReference":
            ref = item.get("inlineReference", {})
            filename = _extract_filename_from_ref(ref)
            if filename:
                text_parts.append(f"`{filename}`")
            continue
        
        # Track codeblockUri - indicates a code block is starting
        if kind == "codeblockUri":
            in_code_block_context = True
            # If we have a pending code fence (opening), add it now
            if pending_code_fence:
                text_parts.append(pending_code_fence)
                pending_code_fence = None
            continue
        
        # Files and code content from textEditGroup
        if kind == "textEditGroup":
            uri = item.get("uri", {})
            if uri.get("path"):
                files.add(normalize_path(uri["path"]))
            
            # If we have a pending code fence (opening), add it before the code
            if pending_code_fence:
                text_parts.append(pending_code_fence)
                pending_code_fence = None
            
            # Extract actual code from edits array
            edits = item.get("edits", [])
            edit_texts = []
            for edit_group in edits:
                if isinstance(edit_group, list):
                    for edit in edit_group:
                        if isinstance(edit, dict) and edit.get("text"):
                            edit_text = edit["text"]
                            # Ensure each edit starts with a newline if it doesn't already
                            if edit_text and not edit_text.startswith("\n"):
                                edit_text = "\n" + edit_text
                            edit_texts.append(edit_text)
            
            if edit_texts:
                # Add the code content to text_parts
                code_content = "\n".join(edit_texts)
                text_parts.append(code_content)
                # After adding code, we expect a closing fence
                in_code_block_context = True
            continue
        
        # Regular text (no kind or other kinds with value)
        val = item.get("value", "")
        if isinstance(val, str) and val.strip():
            stripped = val.strip()
            is_code_fence = stripped == "```" or (stripped.startswith("```") and len(stripped) <= 15 and "\n" not in stripped)
            
            if is_code_fence:
                if in_code_block_context:
                    # We're in a code block context, preserve this fence (closing)
                    text_parts.append(val)
                    in_code_block_context = False
                else:
                    # Not in code block context yet, save as pending (opening)
                    pending_code_fence = val
            else:
                # Not a code fence, add normally
                text_parts.append(val)
        
        # Tools
        for key in ["toolId", "toolName"]:
            if item.get(key):
                tools.add(item[key])
        
        # Files from invocationMessage.uris
        inv = item.get("invocationMessage")
        if isinstance(inv, dict):
            for uri in inv.get("uris", []):
                if isinstance(uri, dict) and uri.get("path"):
                    files.add(normalize_path(uri["path"]))
    
    # Files from editedFileEvents
    for event in req.get("editedFileEvents", []):
        uri = event.get("uri", {})
        if uri.get("path"):
            files.add(normalize_path(uri["path"]))
    
    # Join text parts - use empty string joiner to preserve original spacing
    # since each part already has its own spacing
    text = "".join(text_parts)
    
    # Join thinking parts with double newline
    thinking = "\n\n".join(thinking_parts)
    
    return text, sorted(tools), sorted(files), thinking


def extract_workspace(meta: WorkspaceMeta) -> list[Turn]:
    """Extract all data from a single workspace, matching edits to turns."""
    turns = []
    
    chat_dir = meta.chat_sessions_dir if meta.chat_sessions_dir is not None else meta.path / "chatSessions"
    edits_dir = meta.path / "chatEditingSessions"
    
    for session_file in _resolve_session_files(chat_dir):
        session_turns = extract_session(session_file, meta)
        
        # Check for corresponding edit session (only for regular workspaces)
        edit_folder = edits_dir / session_file.stem
        if edit_folder.exists():
            session_edits = extract_edits(edit_folder)
            
            # Match edits to turns by request_id
            for edit in session_edits:
                req_id = edit.extra.get("request_id")
                if req_id:
                    # Find assistant turn with this request_id
                    for turn in session_turns:
                        if turn.role == "assistant" and turn.request_id == req_id:
                            turn.code_edits.append(edit)
                            break
                            
        turns.extend(session_turns)
    
    return turns
