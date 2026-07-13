"""Low-level Claude Code parser."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from src.extract.base import LowLevelWorkspaceParser
from src.extract.models import (
    AttachmentRef,
    CommandEnvelope,
    ContentBlockRecord,
    ParsedSession,
    ParsedWorkspace,
    ParserIssue,
    SessionEventRecord,
    SessionLinkRecord,
    TokenUsage,
    ToolCallRecord,
    ToolResultRecord,
    WorkspaceDescriptor,
)
from src.extract.utils import as_dict, flatten_text_content, normalize_session_title, parse_timestamp_ms, timestamp_ms_to_iso
from src.pipeline.extraction.adapter import adapt_parsed_workspace
from src.shared.io.paths import normalize_path
from src.shared.logging.logger import get_logger
from src.shared.models.workspace import WorkspaceActivity

logger = get_logger(__name__)

_SYSTEM_EVENT_TYPES = {
    "summary",
    "progress",
    "queue-operation",
    "file-history-snapshot",
    "system",
}

_TOOL_RESULT_KINDS = {
    "tool_result",
    "web_search_tool_result",
    "web_fetch_tool_result",
    "code_execution_tool_result",
    "bash_code_execution_tool_result",
    "text_editor_code_execution_tool_result",
    "tool_search_tool_result",
    "mcp_tool_result",
}


class ClaudeCodeLowLevelParser(LowLevelWorkspaceParser):
    """Source-of-truth parser for Claude Code workspace session files."""

    AGENT_NAME = "claude_code"
    _TITLE_NOISE_MARKERS = ("<local-command-caveat>",)
    _HOUSEKEEPING_COMMANDS = {"clear"}

    def __init__(
        self,
        claude_dir: Path | None = None,
        claude_dirs: list[Path] | None = None,
    ) -> None:
        configured_roots = [Path(path) for path in (claude_dirs or []) if path]
        if claude_dir:
            configured_roots.insert(0, Path(claude_dir))
        if not configured_roots:
            configured_roots = [Path.home() / ".claude"]
        self._configured_roots = configured_roots
        self._claude_dir = configured_roots[0]

    def scan_workspaces(self) -> list[WorkspaceDescriptor]:
        descriptors: list[WorkspaceDescriptor] = []
        seen: set[str] = set()
        seen_source_roots: set[str] = set()

        for claude_root in self._iter_claude_roots():
            history_file = claude_root / "history.jsonl"
            projects_dir = claude_root / "projects"
            if not projects_dir.exists():
                continue

            if history_file.exists():
                checked_history_ids: set[str] = set()
                checked_history_roots: set[str] = set()
                try:
                    with open(history_file, "r", encoding="utf-8", errors="replace") as handle:
                        for raw_line in handle:
                            raw_line = raw_line.strip()
                            if not raw_line:
                                continue
                            try:
                                entry = json.loads(raw_line)
                            except json.JSONDecodeError:
                                continue
                            project_path = entry.get("project")
                            if not isinstance(project_path, str) or not project_path.strip():
                                continue
                            workspace_id = self._encode_project_path(project_path)
                            source_root = projects_dir / workspace_id
                            normalized_source_root = str(source_root).replace("\\", "/").lower()
                            if (
                                workspace_id in seen
                                or workspace_id in checked_history_ids
                                or normalized_source_root in seen_source_roots
                                or normalized_source_root in checked_history_roots
                            ):
                                continue
                            checked_history_ids.add(workspace_id)
                            checked_history_roots.add(normalized_source_root)
                            if not source_root.exists():
                                continue
                            root_session_files = self._iter_root_session_files(source_root)
                            if not root_session_files:
                                continue
                            seen.add(workspace_id)
                            seen_source_roots.add(normalized_source_root)
                            descriptors.append(
                                WorkspaceDescriptor(
                                    workspace_id=workspace_id,
                                    agent_name=self.AGENT_NAME,
                                    workspace_name=Path(project_path).name,
                                    workspace_folder=project_path,
                                    source_root=str(source_root),
                                    metadata={
                                        "project_path": project_path,
                                        "session_count": len(root_session_files),
                                        "claude_root": str(claude_root),
                                    },
                                )
                            )
                except OSError as exc:
                    logger.warning("Failed to read Claude history file %s: %s", history_file, exc)

            try:
                for source_root in projects_dir.iterdir():
                    if not source_root.is_dir():
                        continue
                    workspace_id = source_root.name
                    if workspace_id in seen:
                        continue
                    root_session_files = self._iter_root_session_files(source_root)
                    if not root_session_files:
                        continue
                    normalized_source_root = str(source_root).replace("\\", "/").lower()
                    if normalized_source_root in seen_source_roots:
                        continue
                    project_path = self._infer_project_path_from_session_files(root_session_files, claude_root, workspace_id)
                    seen.add(workspace_id)
                    seen_source_roots.add(normalized_source_root)
                    descriptors.append(
                        WorkspaceDescriptor(
                            workspace_id=workspace_id,
                            agent_name=self.AGENT_NAME,
                            workspace_name=self._workspace_name_from_path(project_path, workspace_id),
                            workspace_folder=project_path,
                            source_root=str(source_root),
                            metadata={
                                "project_path": project_path,
                                "session_count": len(root_session_files),
                                "claude_root": str(claude_root),
                                "discovered_from": "projects-dir",
                            },
                        )
                    )
            except OSError as exc:
                logger.warning("Failed to inspect Claude projects dir %s: %s", projects_dir, exc)

        descriptors.sort(key=lambda item: item.workspace_name.lower())
        return descriptors

    def parse_workspace(self, workspace_id: str) -> ParsedWorkspace:
        descriptor = next((item for item in self.scan_workspaces() if item.workspace_id == workspace_id), None)
        if descriptor is None:
            source_root = self._find_workspace_root(workspace_id)
            descriptor = WorkspaceDescriptor(
                workspace_id=workspace_id,
                agent_name=self.AGENT_NAME,
                workspace_name=workspace_id,
                workspace_folder=workspace_id,
                source_root=str(source_root),
            )

        source_root = Path(descriptor.source_root)
        sessions: list[ParsedSession] = []
        issues: list[ParserIssue] = []

        if not source_root.exists():
            issues.append(
                ParserIssue(
                    level="warning",
                    code="workspace_missing",
                    message=f"Claude workspace root not found: {source_root}",
                )
            )
            return ParsedWorkspace(descriptor=descriptor, sessions=[], issues=issues)

        session_files = self._iter_session_files(source_root)
        child_to_parent: dict[str, str] = {}

        for session_file in session_files:
            session = self._parse_session_file(session_file, descriptor)
            sessions.append(session)
            for link in session.links:
                if link.relationship_type == "subagent" and link.target_session_id:
                    child_to_parent.setdefault(link.target_session_id, session.session_id)

        for session in sessions:
            if not session.parent_session_id:
                parent_session_id = child_to_parent.get(session.session_id, "")
                if parent_session_id:
                    session.parent_session_id = parent_session_id
                    if not session.relationship_type:
                        session.relationship_type = "subagent"

        sessions.sort(key=lambda item: (item.started_at_ms or 0, item.session_id))
        return ParsedWorkspace(
            descriptor=descriptor,
            sessions=sessions,
            issues=issues,
            metadata={"source_root": str(source_root)},
        )

    def get_workspace_activity(self, descriptor: WorkspaceDescriptor) -> WorkspaceActivity:
        source_root = Path(descriptor.source_root)
        sessions: list[ParsedSession] = []
        for session_file in self._iter_root_session_files(source_root):
            sessions.append(self._parse_session_file(session_file, descriptor))

        parsed_workspace = ParsedWorkspace(
            descriptor=descriptor,
            sessions=sessions,
            metadata={"source_root": str(source_root)},
        )
        adapted_workspace = adapt_parsed_workspace(parsed_workspace)
        visible_session_ids: list[str] = []
        seen: set[str] = set()
        for turn in adapted_workspace.turns:
            if not turn.session_id or turn.session_id in seen:
                continue
            seen.add(turn.session_id)
            visible_session_ids.append(turn.session_id)
        return WorkspaceActivity(
            session_count=len(visible_session_ids),
            turn_count=adapted_workspace.turn_count,
            session_ids=visible_session_ids,
        )

    @staticmethod
    def _encode_project_path(project_path: str) -> str:
        return re.sub(r"[:/\\.]", "-", project_path)

    @staticmethod
    def _iter_root_session_files(source_root: Path) -> list[Path]:
        candidates: list[Path] = []
        try:
            with os.scandir(source_root) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    suffix = Path(entry.name).suffix.lower()
                    if suffix in {".jsonl", ".json", ".claude"}:
                        candidates.append(Path(entry.path))
        except OSError:
            return []
        return sorted(candidates)

    @staticmethod
    def _iter_session_files(source_root: Path) -> list[Path]:
        candidates: set[Path] = set()
        for pattern in ("*.jsonl", "*.json", "*.claude"):
            candidates.update(source_root.rglob(pattern))
        return sorted(path for path in candidates if path.is_file())

    def _iter_claude_roots(self) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()

        def add_root(path: Path) -> None:
            normalized = str(path).lower()
            if normalized in seen:
                return
            seen.add(normalized)
            roots.append(path)

        for root in self._configured_roots:
            add_root(root)

        if platform.system() == "Windows":
            for distro in self._list_wsl_distros():
                home_root = Path(rf"\\wsl.localhost\{distro}\home")
                if not home_root.exists():
                    continue
                try:
                    for user_dir in home_root.iterdir():
                        claude_dir = user_dir / ".claude"
                        if (claude_dir / "projects").exists():
                            add_root(claude_dir)
                except OSError as exc:
                    logger.warning("Failed to inspect WSL Claude root %s: %s", home_root, exc)

        return roots

    @staticmethod
    @lru_cache(maxsize=1)
    def _list_wsl_distros() -> list[str]:
        try:
            result = subprocess.run(
                ["wsl.exe", "-l", "-q"],
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            logger.warning("Failed to list WSL distros: %s", exc)
            return []

        if result.returncode != 0:
            return []

        stdout = result.stdout or b""
        if b"\x00" in stdout:
            decoded = stdout.decode("utf-16le", errors="ignore")
        else:
            decoded = stdout.decode("utf-8", errors="replace")

        return [line.strip() for line in decoded.splitlines() if line.strip()]

    def _find_workspace_root(self, workspace_id: str) -> Path:
        for claude_root in self._iter_claude_roots():
            source_root = claude_root / "projects" / workspace_id
            if source_root.exists():
                return source_root
        return self._claude_dir / "projects" / workspace_id

    @staticmethod
    def _workspace_name_from_path(project_path: str, fallback: str) -> str:
        stripped = project_path.rstrip("/")
        if not stripped:
            return fallback
        if "/" in stripped:
            return stripped.split("/")[-1] or fallback
        return Path(stripped).name or fallback

    @staticmethod
    def _decode_project_path_fragment(workspace_id: str) -> str:
        if re.match(r"^[A-Za-z]--", workspace_id):
            drive = workspace_id[0].lower()
            rest = workspace_id[3:]
            return f"{drive}:/{rest.replace('-', '/')}"
        if workspace_id.startswith("-"):
            return workspace_id.replace("-", "/")
        return workspace_id

    def _infer_project_path(self, workspace_id: str, claude_root: Path) -> str:
        decoded = self._decode_project_path_fragment(workspace_id)
        normalized_root = str(claude_root).replace("\\", "/")
        wsl_match = re.match(r"^//wsl\.localhost/([^/]+)/home/([^/]+)/\.claude$", normalized_root, flags=re.IGNORECASE)
        if wsl_match and decoded.startswith("/"):
            distro = wsl_match.group(1)
            return f"vscode-remote://wsl+{distro}{decoded}"
        return decoded

    def _infer_project_path_from_session_files(
        self,
        session_files: list[Path],
        claude_root: Path,
        workspace_id: str,
    ) -> str:
        for session_file in session_files:
            project_path = self._extract_project_path_from_session_file(session_file)
            if project_path:
                return project_path
        return self._infer_project_path(workspace_id, claude_root)

    @staticmethod
    def _extract_project_path_from_session_file(session_file: Path) -> str:
        suffix = session_file.suffix.lower()
        if suffix == ".jsonl":
            try:
                with open(session_file, "r", encoding="utf-8", errors="replace") as handle:
                    for index, raw_line in enumerate(handle):
                        if index >= 32:
                            break
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            event = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        for key in ("cwd", "project", "workspace", "workspacePath"):
                            value = event.get(key)
                            if isinstance(value, str) and value.strip():
                                return normalize_path(value.strip())
            except OSError:
                return ""
            return ""

        try:
            with open(session_file, "r", encoding="utf-8", errors="replace") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return ""

        if isinstance(payload, dict):
            for key in ("cwd", "project", "workspace", "workspacePath"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return normalize_path(value.strip())
        return ""

    @staticmethod
    def _derive_session_id(file_path: Path, raw_events: list[dict[str, Any]], source_root: Path) -> str:
        relative_parts: tuple[str, ...] = ()
        try:
            relative_path = file_path.relative_to(source_root).with_suffix("")
            relative_text = str(relative_path)
            relative_parts = relative_path.parts
        except ValueError:
            relative_text = file_path.stem

        if len(relative_parts) > 1:
            if file_path.stem.startswith("agent-"):
                return file_path.stem
            safe_text = relative_text.replace("\\", "__").replace("/", "__")
            safe_text = re.sub(r"[^A-Za-z0-9_-]+", "_", safe_text).strip("_")
            return safe_text or file_path.stem

        for event in raw_events:
            if not isinstance(event, dict):
                continue
            for key in ("sessionId", "cliSessionId", "id"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            message_payload = event.get("message")
            if isinstance(message_payload, dict):
                for key in ("sessionId", "cliSessionId", "id"):
                    value = message_payload.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()

        safe_text = relative_text.replace("\\", "__").replace("/", "__")
        safe_text = re.sub(r"[^A-Za-z0-9_-]+", "_", safe_text).strip("_")
        return safe_text or file_path.stem

    @staticmethod
    def _infer_parent_session_id_from_path(file_path: Path, source_root: Path) -> str:
        try:
            relative_parts = file_path.relative_to(source_root).parts
        except ValueError:
            relative_parts = file_path.parts

        if "subagents" in relative_parts:
            subagents_index = relative_parts.index("subagents")
            if subagents_index > 0:
                return relative_parts[subagents_index - 1]
        return ""

    def _parse_session_file(self, file_path: Path, descriptor: WorkspaceDescriptor) -> ParsedSession:
        suffix = file_path.suffix.lower()
        issues: list[ParserIssue] = []
        links: list[SessionLinkRecord] = []
        source_root = Path(descriptor.source_root)
        metadata = {
            "source": self.AGENT_NAME,
            "file_suffix": suffix,
            "relative_path": self._relative_path_safe(file_path, source_root),
        }

        if suffix == ".jsonl":
            raw_events, raw_issues = self._read_jsonl_events(file_path)
            issues.extend(raw_issues)
            session_id = self._derive_session_id(file_path, raw_events, source_root)
            subagent_map = self._extract_subagent_map(raw_events)
            metadata.update(self._extract_session_metadata(raw_events))
            parent_session_id = self._detect_parent_session_id(raw_events, session_id) or ""
            if not parent_session_id and "subagents" in file_path.parts:
                parent_session_id = self._infer_parent_session_id_from_path(file_path, source_root)
            events: list[SessionEventRecord] = []
            for index, raw_event in enumerate(raw_events):
                event = self._parse_event(raw_event, index, subagent_map)
                events.append(event)
                for tool_call in event.tool_calls:
                    if tool_call.spawned_session_id:
                        links.append(
                            SessionLinkRecord(
                                target_session_id=tool_call.spawned_session_id,
                                relationship_type="subagent",
                                trigger_event_index=index,
                                trigger_tool_call_id=tool_call.call_id,
                                extra={"tool_name": tool_call.name},
                            )
                        )
            title = normalize_session_title(self._derive_title(events, session_id), fallback=session_id)
            started_at_ms, ended_at_ms = self._bounds_from_events(events)
            relationship_type = "subagent" if session_id.startswith("agent-") and parent_session_id else ""
            return ParsedSession(
                session_id=session_id,
                agent_name=self.AGENT_NAME,
                workspace_id=descriptor.workspace_id,
                workspace_name=descriptor.workspace_name,
                workspace_folder=descriptor.workspace_folder,
                title=title,
                source_path=str(file_path),
                started_at_ms=started_at_ms,
                ended_at_ms=ended_at_ms,
                parent_session_id=parent_session_id,
                relationship_type=relationship_type,
                events=events,
                links=links,
                metadata=metadata,
                issues=issues,
            )

        payload, payload_issues = self._read_json_document(file_path)
        issues.extend(payload_issues)
        events = self._parse_json_sidecar_events(payload)
        raw_payloads = [payload] if isinstance(payload, dict) else []
        session_id = self._derive_session_id(file_path, raw_payloads, source_root)
        title = normalize_session_title(self._derive_title(events, session_id), fallback=session_id)
        started_at_ms, ended_at_ms = self._bounds_from_events(events)
        return ParsedSession(
            session_id=session_id,
            agent_name=self.AGENT_NAME,
            workspace_id=descriptor.workspace_id,
            workspace_name=descriptor.workspace_name,
            workspace_folder=descriptor.workspace_folder,
            title=title,
            source_path=str(file_path),
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            events=events,
            metadata=metadata,
            issues=issues,
        )

    def _read_jsonl_events(self, file_path: Path) -> tuple[list[dict[str, Any]], list[ParserIssue]]:
        raw_events: list[dict[str, Any]] = []
        issues: list[ParserIssue] = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        parsed = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        issues.append(
                            ParserIssue(
                                level="warning",
                                code="invalid_jsonl_line",
                                message=f"Invalid JSONL in {file_path.name} line {line_number}: {exc}",
                            )
                        )
                        continue
                    if isinstance(parsed, dict):
                        raw_events.append(parsed)
        except OSError as exc:
            issues.append(
                ParserIssue(
                    level="error",
                    code="read_failed",
                    message=f"Cannot read {file_path}: {exc}",
                )
            )
        return raw_events, issues

    def _read_json_document(self, file_path: Path) -> tuple[dict[str, Any], list[ParserIssue]]:
        issues: list[ParserIssue] = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                ParserIssue(
                    level="error",
                    code="invalid_json_document",
                    message=f"Cannot load {file_path}: {exc}",
                )
            )
            return {}, issues
        return payload if isinstance(payload, dict) else {}, issues

    def _parse_json_sidecar_events(self, payload: dict[str, Any]) -> list[SessionEventRecord]:
        events: list[SessionEventRecord] = []
        messages = payload.get("messages")
        if isinstance(messages, list):
            for index, item in enumerate(messages):
                if not isinstance(item, dict):
                    continue
                event = self._parse_event(
                    {
                        "type": item.get("role") or item.get("type") or "message",
                        "timestamp": item.get("timestamp") or item.get("time"),
                        "message": {
                            "role": item.get("role") or item.get("type") or "",
                            "content": item.get("content") or item.get("text") or "",
                        },
                    },
                    index,
                    {},
                )
                event.raw = as_dict(item)
                events.append(event)
            return events

        if payload:
            title = str(payload.get("title") or "").strip()
            cwd = str(payload.get("cwd") or "").strip()
            model = str(payload.get("model") or "").strip()
            parts = [
                "Claude Code session metadata",
                f"Title: {title}" if title else "",
                f"Workspace: {cwd}" if cwd else "",
                f"Model: {model}" if model else "",
            ]
            event = SessionEventRecord(
                index=0,
                event_type="metadata",
                role="system",
                timestamp_ms=parse_timestamp_ms(payload.get("lastActivityAt") or payload.get("createdAt")),
                timestamp_iso=timestamp_ms_to_iso(parse_timestamp_ms(payload.get("lastActivityAt") or payload.get("createdAt"))),
                text="\n".join(part for part in parts if part),
                raw=payload,
            )
            events.append(event)
        return events

    def _parse_event(
        self,
        raw_event: dict[str, Any],
        index: int,
        subagent_map: dict[str, str],
    ) -> SessionEventRecord:
        event_type = str(raw_event.get("type") or "")
        message_payload = as_dict(raw_event.get("message"))
        role = self._event_role(raw_event, message_payload, event_type)
        timestamp_ms = parse_timestamp_ms(raw_event.get("timestamp") or raw_event.get("createdAt"))
        timestamp_iso = timestamp_ms_to_iso(timestamp_ms)
        model_id = str(
            message_payload.get("model")
            or raw_event.get("model")
            or raw_event.get("modelId")
            or raw_event.get("model_id")
            or raw_event.get("newModel")
            or ""
        )
        request_id = str(
            raw_event.get("requestId")
            or raw_event.get("request_id")
            or raw_event.get("sessionId")
            or raw_event.get("cliSessionId")
            or ""
        )
        message_id = str(raw_event.get("uuid") or raw_event.get("id") or message_payload.get("id") or "")
        content_value = message_payload.get("content") if isinstance(message_payload, dict) else raw_event.get("content")

        content_blocks = self._parse_content_blocks(content_value, subagent_map)
        text = self._event_text(raw_event, message_payload, content_value, content_blocks)
        thinking_text = "\n".join(block.text for block in content_blocks if block.kind in {"thinking", "redacted_thinking"})
        command = self._extract_command(text)
        token_usage = self._extract_token_usage(message_payload, raw_event)
        attachments = self._extract_attachments(raw_event, message_payload)
        tool_calls = [
            ToolCallRecord.from_dict(block.extra["tool_call"])
            for block in content_blocks
            if "tool_call" in block.extra
        ]
        tool_results = [
            ToolResultRecord.from_dict(block.extra["tool_result"])
            for block in content_blocks
            if "tool_result" in block.extra
        ]
        file_paths = self._extract_event_file_paths(raw_event, content_blocks, attachments)

        extra = self._extract_event_extra(raw_event, message_payload, event_type)

        return SessionEventRecord(
            index=index,
            event_type=event_type,
            role=role,
            timestamp_ms=timestamp_ms,
            timestamp_iso=timestamp_iso,
            message_id=message_id,
            request_id=request_id,
            model_id=model_id,
            text=text,
            thinking_text=thinking_text,
            token_usage=token_usage,
            command=command,
            content_blocks=content_blocks,
            tool_calls=tool_calls,
            tool_results=tool_results,
            attachments=attachments,
            file_paths=file_paths,
            raw=raw_event,
            extra=extra,
        )

    def _parse_content_blocks(
        self,
        content_value: Any,
        subagent_map: dict[str, str],
    ) -> list[ContentBlockRecord]:
        if isinstance(content_value, str):
            return [ContentBlockRecord(index=0, kind="text", text=content_value, raw={"text": content_value})]
        if not isinstance(content_value, list):
            return []

        blocks: list[ContentBlockRecord] = []
        for index, block in enumerate(content_value):
            if not isinstance(block, dict):
                continue
            block_dict = as_dict(block)
            kind = str(block_dict.get("type") or "")
            text = ""
            extra: dict[str, Any] = {}
            data = {key: value for key, value in block_dict.items() if key != "type"}

            if kind == "text":
                text = str(block_dict.get("text") or "")
            elif kind == "thinking":
                text = str(block_dict.get("thinking") or "")
            elif kind == "redacted_thinking":
                text = "[Redacted thinking]"
            elif kind == "image":
                title = str(block_dict.get("title") or "").strip()
                text = f"[Image: {title}]" if title else "[Image]"
            elif kind == "document":
                title = str(block_dict.get("title") or "").strip()
                text = f"[Document: {title}]" if title else "[Document]"
            elif kind == "search_result":
                title = str(block_dict.get("title") or block_dict.get("url") or "").strip()
                text = f"[Search result: {title}]" if title else "[Search result]"
            elif kind.endswith("tool_use"):
                tool_call = self._build_tool_call(block_dict, kind, subagent_map)
                extra["tool_call"] = tool_call.to_dict()
                text = self._summarize_tool_call(tool_call)
            elif kind in _TOOL_RESULT_KINDS or kind.endswith("tool_result"):
                tool_result = self._build_tool_result(block_dict, kind)
                extra["tool_result"] = tool_result.to_dict()
                text = tool_result.text
            else:
                text = flatten_text_content(block_dict) or f"[{kind or 'unknown'}]"

            blocks.append(
                ContentBlockRecord(
                    index=index,
                    kind=kind or "unknown",
                    text=text,
                    data=data,
                    raw=block_dict,
                    extra=extra,
                )
            )
        return blocks

    def _build_tool_call(
        self,
        block: dict[str, Any],
        kind: str,
        subagent_map: dict[str, str],
    ) -> ToolCallRecord:
        call_id = str(block.get("id") or block.get("tool_use_id") or "")
        if kind == "mcp_tool_use":
            server_name = str(block.get("server_name") or "")
            tool_name = str(block.get("tool_name") or "")
            name = f"{server_name}.{tool_name}".strip(".")
            arguments = as_dict(block.get("input"))
        else:
            name = str(block.get("name") or block.get("tool_name") or kind)
            arguments = as_dict(block.get("input"))

        file_paths = sorted(self._extract_file_paths(arguments))
        arguments_text = json.dumps(arguments, ensure_ascii=True, sort_keys=True) if arguments else ""
        return ToolCallRecord(
            call_id=call_id,
            name=name,
            kind=kind,
            arguments=arguments,
            arguments_text=arguments_text,
            file_paths=file_paths,
            spawned_session_id=subagent_map.get(call_id),
            raw=block,
            extra={
                "server_name": block.get("server_name"),
                "tool_name": block.get("tool_name"),
            },
        )

    def _build_tool_result(self, block: dict[str, Any], kind: str) -> ToolResultRecord:
        structured_content = as_dict(block.get("content"))
        text = self._summarize_tool_result(block, kind)
        is_error = self._extract_is_error(block)
        status = self._infer_tool_result_status(block, is_error)
        return ToolResultRecord(
            tool_call_id=str(block.get("tool_use_id") or block.get("id") or ""),
            kind=kind,
            text=text,
            structured_content=structured_content,
            is_error=is_error,
            status=status,
            raw=block,
            extra={
                "result_kind": kind,
            },
        )

    def _extract_attachments(
        self,
        raw_event: dict[str, Any],
        message_payload: dict[str, Any],
    ) -> list[AttachmentRef]:
        attachments: list[AttachmentRef] = []
        for container in (raw_event, message_payload):
            for key in ("attachment_refs", "attachments"):
                value = container.get(key)
                if not isinstance(value, list):
                    continue
                for item in value:
                    if isinstance(item, str):
                        attachments.append(AttachmentRef(kind=key, path=normalize_path(item), raw={"value": item}))
                    elif isinstance(item, dict):
                        path = str(item.get("path") or item.get("file_path") or "")
                        title = str(item.get("title") or item.get("name") or "")
                        media_type = str(item.get("media_type") or item.get("mime_type") or "")
                        attachments.append(
                            AttachmentRef(
                                kind=key,
                                path=normalize_path(path) if path else "",
                                title=title,
                                media_type=media_type,
                                raw=item,
                            )
                        )
        return attachments

    def _extract_token_usage(self, message_payload: dict[str, Any], raw_event: dict[str, Any]) -> Optional[TokenUsage]:
        usage = as_dict(message_payload.get("usage"))
        if not usage and isinstance(raw_event.get("usage"), dict):
            usage = as_dict(raw_event.get("usage"))
        if not usage:
            return None
        return TokenUsage(
            input_tokens=self._coerce_int(usage.get("input_tokens")),
            output_tokens=self._coerce_int(usage.get("output_tokens")),
            cache_read_input_tokens=self._coerce_int(usage.get("cache_read_input_tokens")),
            cache_creation_input_tokens=self._coerce_int(usage.get("cache_creation_input_tokens")),
            service_tier=str(usage.get("service_tier") or "") or None,
            extra={key: value for key, value in usage.items() if key not in {
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "service_tier",
            }},
        )

    @staticmethod
    def _event_role(
        raw_event: dict[str, Any],
        message_payload: dict[str, Any],
        event_type: str,
    ) -> str:
        explicit_role = str(message_payload.get("role") or raw_event.get("role") or "").strip()
        if explicit_role in {"user", "assistant", "system"}:
            return explicit_role
        if event_type in {"user", "assistant", "system"}:
            return event_type
        if event_type in _SYSTEM_EVENT_TYPES:
            return "system"
        return ""

    def _event_text(
        self,
        raw_event: dict[str, Any],
        message_payload: dict[str, Any],
        content_value: Any,
        content_blocks: list[ContentBlockRecord],
    ) -> str:
        event_type = str(raw_event.get("type") or "")
        if event_type == "summary":
            return str(raw_event.get("summary") or "").strip()
        if event_type == "progress":
            data = as_dict(raw_event.get("data"))
            return (
                str(data.get("message") or data.get("status") or data.get("type") or raw_event.get("message") or "")
            ).strip()
        if event_type == "queue-operation":
            operation = str(raw_event.get("operation") or "queue").strip()
            target = str(raw_event.get("tool_use_id") or raw_event.get("toolUseId") or "").strip()
            return f"Queue operation: {operation}{f' ({target})' if target else ''}".strip()
        if event_type == "file-history-snapshot":
            files = self._extract_named_paths(raw_event.get("files"))
            return f"File history snapshot ({len(files)} file{'s' if len(files) != 1 else ''})"
        if event_type == "system":
            return flatten_text_content(content_value) or str(raw_event.get("text") or raw_event.get("message") or "")

        text = flatten_text_content(content_value)
        if text:
            return text
        if content_blocks:
            summarized = [block.text for block in content_blocks if block.text]
            if summarized:
                return "\n".join(summarized)
        return str(raw_event.get("text") or message_payload.get("text") or "").strip()

    def _extract_event_extra(
        self,
        raw_event: dict[str, Any],
        message_payload: dict[str, Any],
        event_type: str,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        metadata_keys = {
            "uuid": "uuid",
            "parentUuid": "parent_uuid",
            "parentSessionId": "parent_session_id",
            "sessionId": "session_id",
            "cliSessionId": "cli_session_id",
            "gitBranch": "git_branch",
            "permissionMode": "permission_mode",
            "cwd": "cwd",
        }
        for source in (raw_event, message_payload):
            for source_key, target_key in metadata_keys.items():
                value = source.get(source_key) if isinstance(source, dict) else None
                if value not in (None, "") and target_key not in extra:
                    extra[target_key] = value

        if raw_event.get("isMeta") is not None:
            extra["is_meta"] = raw_event.get("isMeta")
        if raw_event.get("isCompactSummary") is not None:
            extra["is_compact_summary"] = raw_event.get("isCompactSummary")
        if event_type == "summary" and raw_event.get("summary") is not None:
            extra["summary"] = raw_event.get("summary")
        if event_type == "progress" and isinstance(raw_event.get("data"), dict):
            extra["progress"] = raw_event["data"]
        if event_type == "queue-operation":
            extra["queue_operation"] = {
                "operation": raw_event.get("operation"),
                "tool_use_id": raw_event.get("tool_use_id") or raw_event.get("toolUseId"),
                "source_session_id": raw_event.get("sourceSessionId") or raw_event.get("sessionId"),
            }
        if event_type == "file-history-snapshot":
            extra["snapshot_file_count"] = len(self._extract_named_paths(raw_event.get("files")))
        return extra

    @staticmethod
    def _summarize_tool_call(tool_call: ToolCallRecord) -> str:
        if tool_call.kind == "server_tool_use":
            prefix = "[Server] "
        elif tool_call.kind == "mcp_tool_use":
            prefix = "[MCP] "
        else:
            prefix = ""
        if tool_call.arguments_text:
            return f"{prefix}{tool_call.name}({tool_call.arguments_text})"
        return f"{prefix}{tool_call.name}"

    @staticmethod
    def _summarize_tool_result(block: dict[str, Any], kind: str) -> str:
        content = block.get("content")
        prefix = "[Error] " if ClaudeCodeLowLevelParser._extract_is_error(block) is True else ""

        if kind == "web_search_tool_result" and isinstance(content, list):
            titles: list[str] = []
            for item in content[:5]:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get("url")
                if isinstance(title, str) and title:
                    titles.append(title)
            summary = ", ".join(titles) if titles else "results"
            return f"{prefix}[Web search: {summary}]"

        if kind == "web_fetch_tool_result":
            if isinstance(content, dict):
                url = content.get("url")
                if isinstance(url, str) and url:
                    return f"{prefix}[Web fetch: {url}]"
            return f"{prefix}[Web fetch result]"

        if kind in {"code_execution_tool_result", "bash_code_execution_tool_result"}:
            stdout, stderr, exit_code = ClaudeCodeLowLevelParser._extract_execution_fields(content)
            lines: list[str] = []
            if stdout:
                lines.append(stdout)
            if stderr:
                lines.append(f"[stderr] {stderr}")
            if exit_code is not None:
                lines.append(f"[exit_code] {exit_code}")
            summary = "\n".join(lines) if lines else f"[{kind}]"
            return f"{prefix}{summary}"

        if kind == "text_editor_code_execution_tool_result":
            operation = "unknown"
            path = ""
            if isinstance(content, dict):
                operation = str(content.get("operation") or operation)
                path = str(content.get("path") or "")
            return f"{prefix}[File {operation}{f': {path}' if path else ''}]"

        if kind == "tool_search_tool_result":
            return f"{prefix}[Tool search result]"

        if kind == "mcp_tool_result":
            if isinstance(content, dict):
                text = content.get("text")
                if isinstance(text, str) and text:
                    return f"{prefix}{text}"
            flattened = flatten_text_content(content)
            return f"{prefix}{flattened or '[MCP result]'}"

        if kind == "tool_result":
            flattened = flatten_text_content(content)
            return f"{prefix}{flattened or '[Tool result]'}"

        flattened = flatten_text_content(content)
        return f"{prefix}{flattened or f'[{kind}]'}"

    @staticmethod
    def _extract_is_error(block: dict[str, Any]) -> Optional[bool]:
        for key in ("is_error", "isError", "error"):
            value = block.get(key)
            if isinstance(value, bool):
                return value
        content = block.get("content")
        if isinstance(content, dict):
            for key in ("is_error", "isError", "error"):
                value = content.get(key)
                if isinstance(value, bool):
                    return value
        return None

    @staticmethod
    def _infer_tool_result_status(block: dict[str, Any], is_error: Optional[bool]) -> str:
        if is_error is True:
            return "error"
        if is_error is False:
            return "success"

        for key in ("success",):
            value = block.get(key)
            if isinstance(value, bool):
                return "success" if value else "error"

        content = block.get("content")
        if isinstance(content, dict):
            for key in ("success",):
                value = content.get(key)
                if isinstance(value, bool):
                    return "success" if value else "error"
            for key in ("exit_code", "exitCode"):
                value = content.get(key)
                if isinstance(value, int):
                    return "success" if value == 0 else "error"

        return "unknown"

    @staticmethod
    def _extract_execution_fields(content: Any) -> tuple[str, str, int | None]:
        if not isinstance(content, dict):
            return "", "", None
        stdout = str(content.get("stdout") or "").strip()
        stderr = str(content.get("stderr") or "").strip()
        exit_code = content.get("exit_code") if isinstance(content.get("exit_code"), int) else None
        if exit_code is None and isinstance(content.get("exitCode"), int):
            exit_code = int(content["exitCode"])
        return stdout, stderr, exit_code

    @staticmethod
    def _extract_command(text: str) -> Optional[CommandEnvelope]:
        if "<command-name>" not in text:
            return None
        name_match = re.search(r"<command-name>\s*([^<]+?)\s*</command-name>", text, flags=re.DOTALL)
        if not name_match:
            return None
        args_match = re.search(r"<command-args>\s*([^<]+?)\s*</command-args>", text, flags=re.DOTALL)
        command_name = name_match.group(1).strip().lstrip("/")
        arguments_text = args_match.group(1).strip() if args_match else ""
        normalized_text = f"/{command_name}"
        if arguments_text:
            normalized_text = f"{normalized_text} {arguments_text}"
        return CommandEnvelope(
            name=command_name,
            arguments_text=arguments_text,
            normalized_text=normalized_text,
            raw_text=text,
        )

    @staticmethod
    def _extract_subagent_map(raw_events: list[dict[str, Any]]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for event in raw_events:
            event_type = event.get("type")
            if event_type == "queue-operation" and event.get("operation") == "enqueue":
                tool_use_id = str(
                    event.get("tool_use_id")
                    or event.get("toolUseId")
                    or event.get("parentToolUseID")
                    or event.get("parentToolUseId")
                    or ""
                )
                payload = json.dumps(event, ensure_ascii=True)
                task_match = re.search(r'"task[_-]?id"\s*:\s*"([^"]+)"', payload, flags=re.IGNORECASE)
                if not task_match:
                    task_match = re.search(r"agent-([A-Za-z0-9_-]+)", payload)
                if tool_use_id and task_match:
                    target = task_match.group(1)
                    mapping[tool_use_id] = target if target.startswith("agent-") else f"agent-{target}"
            elif event_type == "progress":
                data = as_dict(event.get("data"))
                if data.get("type") != "agent_progress":
                    continue
                tool_use_id = str(
                    event.get("parentToolUseID")
                    or event.get("parentToolUseId")
                    or data.get("parentToolUseID")
                    or data.get("parentToolUseId")
                    or ""
                )
                agent_id = str(data.get("agentId") or "")
                if tool_use_id and agent_id:
                    mapping[tool_use_id] = agent_id if agent_id.startswith("agent-") else f"agent-{agent_id}"
        return mapping

    @staticmethod
    def _extract_session_metadata(raw_events: list[dict[str, Any]]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        key_map = {
            "entrypoint": "entrypoint",
            "userType": "user_type",
            "isSidechain": "is_sidechain",
            "cwd": "cwd",
            "version": "version",
        }
        for event in raw_events:
            for source_key, target_key in key_map.items():
                value = event.get(source_key)
                if value not in (None, "") and target_key not in metadata:
                    metadata[target_key] = value

            if all(target_key in metadata for target_key in ("entrypoint", "user_type", "cwd")):
                break

        if metadata.get("entrypoint") == "sdk-cli":
            metadata["session_origin_label"] = "sdk-cli"
            metadata["is_headless_session"] = True
        return metadata

    @staticmethod
    def _detect_parent_session_id(raw_events: list[dict[str, Any]], session_id: str) -> Optional[str]:
        for event in raw_events:
            parent = event.get("parentSessionId")
            if isinstance(parent, str) and parent and parent != session_id:
                return parent
            if event.get("type") == "queue-operation":
                candidate = event.get("sourceSessionId") or event.get("sessionId")
                if isinstance(candidate, str) and candidate and candidate != session_id:
                    return candidate
        return None

    @classmethod
    def _derive_title(cls, events: list[SessionEventRecord], fallback: str) -> str:
        fallback_title = ""
        for event in events:
            if event.role != "user":
                continue

            preferred_title = cls._preferred_title_from_event(event)
            if preferred_title:
                return normalize_session_title(preferred_title, fallback=fallback)

            if not fallback_title:
                fallback_title = cls._fallback_title_from_event(event)

        if fallback_title:
            return normalize_session_title(fallback_title, fallback=fallback)
        return normalize_session_title(fallback)

    @classmethod
    def _preferred_title_from_event(cls, event: SessionEventRecord) -> str:
        text = event.text.strip()
        if not text:
            return ""
        lowered = text.lower()
        if any(marker in lowered for marker in cls._TITLE_NOISE_MARKERS):
            return ""
        if event.extra.get("is_meta") is True:
            return ""
        if event.command is not None:
            command_name = (event.command.name or "").strip().lower().lstrip("/")
            if command_name in cls._HOUSEKEEPING_COMMANDS:
                return ""
            if event.command.normalized_text:
                return event.command.normalized_text.strip()
        return text.splitlines()[0].strip()

    @classmethod
    def _fallback_title_from_event(cls, event: SessionEventRecord) -> str:
        if event.command is not None and event.command.normalized_text:
            return event.command.normalized_text.strip()
        text = event.text.strip()
        if not text:
            return ""
        lowered = text.lower()
        if any(marker in lowered for marker in cls._TITLE_NOISE_MARKERS):
            return ""
        return text.splitlines()[0].strip()

    @staticmethod
    def _bounds_from_events(events: list[SessionEventRecord]) -> tuple[int | None, int | None]:
        timestamps = [event.timestamp_ms for event in events if event.timestamp_ms is not None]
        if not timestamps:
            return None, None
        return min(timestamps), max(timestamps)

    @staticmethod
    def _extract_file_paths(arguments: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        stack: list[Any] = [arguments]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in {"file_path", "path", "file"} and isinstance(value, str) and value.strip():
                        paths.add(normalize_path(value))
                    else:
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
        return paths

    @staticmethod
    def _extract_file_paths_from_blocks(blocks: list[ContentBlockRecord]) -> set[str]:
        paths: set[str] = set()
        for block in blocks:
            tool_call = block.extra.get("tool_call")
            if isinstance(tool_call, dict):
                for path in tool_call.get("file_paths", []):
                    if isinstance(path, str) and path:
                        paths.add(path)
        return paths

    def _extract_event_file_paths(
        self,
        raw_event: dict[str, Any],
        content_blocks: list[ContentBlockRecord],
        attachments: list[AttachmentRef],
    ) -> list[str]:
        paths = {
            *self._extract_file_paths_from_blocks(content_blocks),
            *(attachment.path for attachment in attachments if attachment.path),
            *self._extract_named_paths(raw_event.get("files")),
        }
        return sorted(path for path in paths if path)

    @staticmethod
    def _extract_named_paths(value: Any) -> set[str]:
        paths: set[str] = set()
        if not isinstance(value, list):
            return paths
        for item in value:
            if isinstance(item, str) and item.strip():
                paths.add(normalize_path(item))
            elif isinstance(item, dict):
                candidate = item.get("path") or item.get("file_path") or item.get("uri")
                if isinstance(candidate, str) and candidate.strip():
                    paths.add(normalize_path(candidate))
        return paths

    @staticmethod
    def _relative_path_safe(path: Path, root: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return path.name

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
