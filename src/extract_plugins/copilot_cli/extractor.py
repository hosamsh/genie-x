"""GitHub Copilot CLI session extractor – core implementation.

Discovers and parses event-driven JSONL session files written by the
GitHub Copilot CLI agent to ``~/.copilot/session-state/``.

Two file patterns are supported:
  - ``<session-state-dir>/*.jsonl``          – flat per-session files
  - ``<session-state-dir>/<name>/events.jsonl`` – one directory per session

Each file contains one JSON object per line.  Only these event types are
consumed; all others are silently skipped:

  ``session.start``         – session / workspace metadata + initial model
  ``user.message``          – user turn
  ``assistant.message``     – assistant turn (model may differ per turn)
  ``session.model_change``  – updates the active model for later turns
  ``tool.execution_complete`` – deliberately skipped
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from src.shared.models.turn import Turn
from src.shared.models.workspace import WorkspaceInfo, WorkspaceActivity, ExtractedWorkspace
from src.shared.io.paths import normalize_path, is_valid_session_id
from src.shared.logging.logger import get_logger

logger = get_logger(__name__)

AGENT_NAME = "copilot_cli"

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_session_state_dir() -> Path:
    """Default Copilot CLI session-state directory (all platforms)."""
    return Path.home() / ".copilot" / "session-state"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SessionMeta:
    """Lightweight metadata extracted from a session's ``session.start`` event."""

    session_id: str
    workspace_id: str
    workspace_name: str
    workspace_folder: str
    session_name: str
    model: str
    started_at: Optional[int]   # Unix ms; None if absent
    file_path: Path


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------

def _make_workspace_id(workspace_folder: str) -> str:
    """Derive a stable, filesystem-safe workspace ID from the folder path."""
    normalized = normalize_path(workspace_folder).rstrip("/")
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:16]


def _read_workspace_yaml(file_path: Path) -> Dict[str, Any]:
    """Read ``workspace.yaml`` adjacent to *file_path* (or in its parent).

    Returns an empty dict on any failure.
    """
    candidates = []
    if file_path.name == "events.jsonl":
        candidates.append(file_path.parent / "workspace.yaml")
    else:
        candidates.append(file_path.parent / file_path.stem / "workspace.yaml")
        candidates.append(file_path.with_suffix(".yaml"))
    for path in candidates:
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                    return data if isinstance(data, dict) else {}
            except Exception:
                continue
    return {}


def _get_field(event: Dict, *keys: str) -> Optional[str]:
    """Return the first non-``None`` value found among *keys* in *event*.

    Also searches inside ``event["data"]`` and ``event["data"]["context"]``
    to handle the nested event structure used by the Copilot CLI agent.
    """
    raw_data = event.get("data")
    data: Dict = raw_data if isinstance(raw_data, dict) else {}
    raw_ctx = data.get("context")
    context: Dict = raw_ctx if isinstance(raw_ctx, dict) else {}
    for key in keys:
        for source in (event, data, context):
            val = source.get(key)
            if val is not None:
                return str(val)
    return None


def _parse_timestamp(ts: Union[int, float, str, None]) -> Optional[int]:
    """Coerce *ts* to a Unix millisecond integer.

    Handles numeric timestamps (seconds or milliseconds) and ISO-8601 strings.
    """
    if ts is None:
        return None
    # Try ISO-8601 string first
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            pass
    try:
        val = int(ts)
        # Heuristic: values below 1e12 are almost certainly in seconds
        return val * 1000 if val < 10_000_000_000 else val
    except (TypeError, ValueError):
        return None


def _ms_to_iso(ts_ms: Optional[int]) -> str:
    """Convert a Unix-ms timestamp to an ISO-8601 string, or ``""`` on failure."""
    if ts_ms is None:
        return ""
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return dt.isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Session file discovery
# ---------------------------------------------------------------------------

def scan_session_files(base: Path) -> List[Path]:
    """Return all JSONL session files found under *base*.

    Searches for:
    - ``<base>/*.jsonl``             (flat pattern)
    - ``<base>/*/events.jsonl``      (subdirectory pattern)
    """
    if not base.is_dir():
        return []
    files: List[Path] = []
    for f in sorted(base.glob("*.jsonl")):
        files.append(f)
    for f in sorted(base.glob("*/events.jsonl")):
        files.append(f)
    return files


# ---------------------------------------------------------------------------
# Session metadata peek
# ---------------------------------------------------------------------------

def peek_session_meta(file_path: Path) -> Optional[SessionMeta]:
    """Read the first ``session.start`` event from *file_path* and return
    a :class:`SessionMeta`.

    Falls back to path-derived values when no ``session.start`` event is found
    (e.g. for session files that pre-date the event schema).
    Returns ``None`` if the derived session ID is invalid.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                evt_type = event.get("type", "")

                if evt_type == "session.start":
                    session_id = (
                        _get_field(event, "sessionId", "session_id", "id")
                        or file_path.stem
                    )
                    workspace_folder = normalize_path(
                        _get_field(event, "workspaceFolder", "workspace_folder", "folder", "cwd") or ""
                    )
                    workspace_name = (
                        _get_field(event, "workspaceName", "workspace_name", "name")
                        or (Path(workspace_folder).name if workspace_folder else "")
                    )
                    workspace_id = (
                        _make_workspace_id(workspace_folder)
                        if workspace_folder
                        else (
                            file_path.parent.name
                            if file_path.name == "events.jsonl"
                            else file_path.stem
                        )
                    )
                    model = _get_field(event, "model", "modelId", "model_id") or ""
                    ts = _parse_timestamp(
                        event.get("timestamp")
                        or _get_field(event, "startTime")
                    )
                    session_name = (
                        _get_field(event, "sessionName", "session_name", "title") or ""
                    )
                    return SessionMeta(
                        session_id=session_id,
                        workspace_id=workspace_id,
                        workspace_name=workspace_name,
                        workspace_folder=workspace_folder,
                        session_name=session_name,
                        model=model,
                        started_at=ts,
                        file_path=file_path,
                    )

                # Stop scanning if we reach message content before session.start
                if evt_type in ("user.message", "assistant.message"):
                    break

    except Exception as exc:
        logger.debug("Could not peek session meta from %s: %s", file_path, exc)

    # Fallback: derive from file path + workspace.yaml
    fallback_id = (
        file_path.parent.name if file_path.name == "events.jsonl" else file_path.stem
    )
    if not is_valid_session_id(fallback_id):
        logger.warning("Skipping session with invalid fallback ID: %s", file_path)
        return None

    ws_yaml = _read_workspace_yaml(file_path)
    workspace_folder = normalize_path(
        ws_yaml.get("cwd") or ws_yaml.get("git_root") or ""
    )
    workspace_name = (
        (Path(workspace_folder).name if workspace_folder else "")
        or ws_yaml.get("repository", "")
    )
    workspace_id = (
        _make_workspace_id(workspace_folder) if workspace_folder else fallback_id
    )

    return SessionMeta(
        session_id=fallback_id,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        workspace_folder=workspace_folder,
        session_name=ws_yaml.get("summary", ""),
        model="",
        started_at=None,
        file_path=file_path,
    )


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------

def parse_session_events(file_path: Path, meta: SessionMeta) -> List[Turn]:
    """Parse all events in *file_path* and return a list of :class:`Turn` objects.

    Consecutive messages with the same role are merged into a single turn so
    the output always alternates user / assistant.  Text from merged messages
    is joined with ``"\\n\\n"``.

    Model tracking:
      The model starts as ``meta.model`` (from ``session.start``).
      ``session.model_change`` events update the active model; the new value
      is applied to all subsequent assistant turns.
    """
    # Pass 1 – aggregate events, merging consecutive same-role messages
    aggregated: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    current_model = meta.model

    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("Skipping invalid JSON line in %s", file_path)
                    continue

                evt_type = event.get("type", "")

                if evt_type == "session.start":
                    m = _get_field(event, "model", "modelId", "model_id")
                    if m:
                        current_model = m
                    continue

                if evt_type == "session.model_change":
                    m = _get_field(event, "newModel", "model", "modelId", "model_id")
                    if m:
                        current_model = m
                    continue

                if evt_type == "tool.execution_complete":
                    continue

                if evt_type in ("user.message", "assistant.message"):
                    role = "user" if evt_type == "user.message" else "assistant"
                    ts = _parse_timestamp(event.get("timestamp"))
                    text = _get_field(event, "text", "content", "message") or ""
                    message_id = _get_field(event, "messageId", "message_id", "id") or ""
                    model_for_turn = (
                        _get_field(event, "model", "modelId", "model_id")
                        or current_model
                    )
                    files = _extract_files(event) if role == "user" else []

                    if current and current["role"] == role:
                        # Same role – merge into current accumulator
                        if text:
                            current["text_parts"].append(text)
                        current["files"].extend(files)
                        # Keep earliest timestamp
                        if ts is not None and (
                            current["ts_ms"] is None or ts < current["ts_ms"]
                        ):
                            current["ts_ms"] = ts
                        # Keep first non-empty identifiers
                        if model_for_turn and not current.get("model_id"):
                            current["model_id"] = model_for_turn
                        if message_id and not current.get("request_id"):
                            current["request_id"] = message_id
                    else:
                        # Role changed – finalize previous accumulator
                        if current:
                            aggregated.append(current)
                        current = {
                            "role": role,
                            "text_parts": [text] if text else [],
                            "ts_ms": ts,
                            "model_id": model_for_turn,
                            "request_id": message_id,
                            "files": files,
                        }
                    continue

    except Exception as exc:
        logger.error("Error parsing session file %s: %s", file_path, exc)

    # Flush last accumulator
    if current:
        aggregated.append(current)

    # Pass 2 – convert aggregated dicts to Turn objects
    turns: List[Turn] = []
    for turn_index, agg in enumerate(aggregated):
        text = "\n\n".join(agg["text_parts"]).strip()
        if not text:
            continue
        ts_ms = agg["ts_ms"]
        turns.append(
            Turn(
                session_id=meta.session_id,
                turn=turn_index,
                role=agg["role"],
                original_text=text,
                workspace_id=meta.workspace_id,
                workspace_name=meta.workspace_name,
                workspace_folder=meta.workspace_folder,
                session_name=meta.session_name,
                agent_used=AGENT_NAME,
                model_id=agg.get("model_id") or "",
                request_id=agg.get("request_id") or "",
                timestamp_ms=ts_ms,
                timestamp_iso=_ms_to_iso(ts_ms),
                ts=_ms_to_iso(ts_ms),
                files=agg.get("files") or [],
            )
        )

    return turns


def _extract_files(event: Dict) -> List[str]:
    """Extract file path references from a ``user.message`` event."""
    files: List[str] = []
    raw_data = event.get("data")
    data: Dict = raw_data if isinstance(raw_data, dict) else {}
    raw_files = (
        event.get("files") or event.get("attachments")
        or data.get("files") or data.get("attachments")
        or []
    )
    if not isinstance(raw_files, list):
        return files
    for item in raw_files:
        if isinstance(item, str):
            files.append(normalize_path(item))
        elif isinstance(item, dict):
            fp = _get_field(item, "path", "uri", "name")
            if fp:
                files.append(normalize_path(fp))
    return files


# ---------------------------------------------------------------------------
# Core extractor class (plain, no AgentExtractor dependency)
# ---------------------------------------------------------------------------

class CopilotCliExtractor:
    """GitHub Copilot CLI session extractor.

    Designed to be instantiated by :class:`Copilot_CliExtractor` in ``agent.py``
    which resolves the ``session_state_dir`` from configuration.

    Args:
        workspace_id:       Workspace identifier to filter sessions.
        session_state_dir:  Root directory to scan.  Defaults to
                            ``~/.copilot/session-state``.
    """

    def __init__(self, workspace_id: str, session_state_dir: Optional[Path] = None) -> None:
        self.workspace_id = workspace_id
        self.session_state_dir = session_state_dir or get_session_state_dir()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _all_sessions(self) -> List[SessionMeta]:
        files = scan_session_files(self.session_state_dir)
        return [m for m in (peek_session_meta(f) for f in files) if m is not None]

    def _workspace_sessions(self) -> List[SessionMeta]:
        return [s for s in self._all_sessions() if s.workspace_id == self.workspace_id]

    # ------------------------------------------------------------------
    # Public API (mirrors AgentExtractor interface)
    # ------------------------------------------------------------------

    def scan_workspaces(self) -> List[WorkspaceInfo]:
        """Discover all workspaces by grouping sessions by ``workspace_id``."""
        sessions = self._all_sessions()
        ws_map: Dict[str, List[SessionMeta]] = {}
        for s in sessions:
            ws_map.setdefault(s.workspace_id, []).append(s)

        workspaces: List[WorkspaceInfo] = []
        for ws_id, group in ws_map.items():
            first = group[0]
            workspaces.append(
                WorkspaceInfo(
                    workspace_id=ws_id,
                    workspace_name=first.workspace_name,
                    workspace_folder=first.workspace_folder,
                    agents=[AGENT_NAME],
                    session_count=len(group),
                    source_available=True,
                )
            )
        return workspaces

    def extract_sessions(self) -> ExtractedWorkspace:
        """Extract all turns for this :attr:`workspace_id`."""
        workspace_sessions = self._workspace_sessions()
        all_turns: List[Turn] = []
        seen_ids: set[str] = set()

        for meta in workspace_sessions:
            seen_ids.add(meta.session_id)
            all_turns.extend(parse_session_events(meta.file_path, meta))

        return ExtractedWorkspace(
            turns=all_turns,
            session_count=len(seen_ids),
            agent_name=AGENT_NAME,
            workspace_id=self.workspace_id,
        )

    def get_latest_activity(self) -> Optional[WorkspaceActivity]:
        """Return quick session/turn stats without full content parsing."""
        workspace_sessions = self._workspace_sessions()
        if not workspace_sessions:
            return None

        session_ids = [s.session_id for s in workspace_sessions]
        total_turns = 0
        for meta in workspace_sessions:
            try:
                with open(meta.file_path, "r", encoding="utf-8") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") in ("user.message", "assistant.message"):
                            total_turns += 1
            except Exception as exc:
                logger.debug("Could not count turns in %s: %s", meta.file_path, exc)

        return WorkspaceActivity(
            session_count=len(workspace_sessions),
            turn_count=total_turns,
            session_ids=session_ids,
        )

    def cleanup(self) -> None:
        """No persistent resources to release."""
