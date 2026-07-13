"""Low-level GitHub Copilot CLI parser."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.extract.base import LowLevelWorkspaceParser
from src.extract.models import (
    ContentBlockRecord,
    ParsedSession,
    ParsedWorkspace,
    ParserIssue,
    SessionEventRecord,
    ToolCallRecord,
    ToolResultRecord,
    WorkspaceDescriptor,
)
from src.extract.utils import as_dict, as_list, flatten_text_content, normalize_session_title, parse_timestamp_ms, timestamp_ms_to_iso
from src.shared.io.paths import normalize_path


class CopilotCliLowLevelParser(LowLevelWorkspaceParser):
    """Source-of-truth parser for Copilot CLI session logs."""

    AGENT_NAME = "copilot_cli"

    def __init__(self, base_dir: Path | None = None, base_dirs: list[Path] | None = None) -> None:
        configured_roots = [Path(path) for path in (base_dirs or []) if path]
        if base_dir:
            configured_roots.insert(0, Path(base_dir))
        if not configured_roots:
            configured_roots = [Path.home() / ".copilot" / "session-state"]
        self._base_dirs = configured_roots
        self._base_dir = configured_roots[0]

    def scan_workspaces(self) -> list[WorkspaceDescriptor]:
        grouped: dict[str, WorkspaceDescriptor] = {}
        for base_dir in self._base_dirs:
            for session_file in self._scan_session_files(base_dir):
                meta = self._peek_meta(session_file)
                workspace_id = meta["workspace_id"]
                if workspace_id not in grouped:
                    grouped[workspace_id] = WorkspaceDescriptor(
                        workspace_id=workspace_id,
                        agent_name=self.AGENT_NAME,
                        workspace_name=meta["workspace_name"],
                        workspace_folder=meta["workspace_folder"],
                        source_root=str(base_dir),
                        metadata={"session_files": [str(session_file)], "session_count": 1},
                    )
                else:
                    grouped[workspace_id].metadata.setdefault("session_files", []).append(str(session_file))
                    grouped[workspace_id].metadata["session_count"] = int(grouped[workspace_id].metadata.get("session_count", 0)) + 1
        return sorted(grouped.values(), key=lambda item: item.workspace_name.lower())

    def parse_workspace(self, workspace_id: str) -> ParsedWorkspace:
        descriptor = next((item for item in self.scan_workspaces() if item.workspace_id == workspace_id), None)
        if descriptor is None:
            descriptor = WorkspaceDescriptor(
                workspace_id=workspace_id,
                agent_name=self.AGENT_NAME,
                workspace_name=workspace_id,
                source_root=str(self._base_dir),
            )

        session_files = [Path(path) for path in descriptor.metadata.get("session_files", [])]
        sessions: list[ParsedSession] = []
        issues: list[ParserIssue] = []

        for session_file in session_files:
            parsed_session, session_issues = self._parse_session_file(session_file, descriptor)
            if parsed_session is not None:
                sessions.append(parsed_session)
            issues.extend(session_issues)

        sessions.sort(key=lambda item: (item.started_at_ms or 0, item.session_id))
        return ParsedWorkspace(descriptor=descriptor, sessions=sessions, issues=issues)

    @staticmethod
    def _scan_session_files(base_dir: Path) -> list[Path]:
        if not base_dir.exists() or not base_dir.is_dir():
            return []
        files: list[Path] = []
        files.extend(sorted(base_dir.glob("*.jsonl")))
        files.extend(sorted(base_dir.glob("*/events.jsonl")))
        files.extend(sorted(base_dir.glob("../history-session-state/*.json")))
        return files

    def _peek_meta(self, session_file: Path) -> dict[str, str]:
        if session_file.suffix.lower() == ".json":
            return self._peek_meta_from_legacy_json(session_file)

        workspace_folder = ""
        workspace_name = ""
        session_id = session_file.parent.name if session_file.name == "events.jsonl" else session_file.stem
        try:
            with open(session_file, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    workspace_folder = normalize_path(self._event_field(event, "workspaceFolder", "workspace_folder", "folder", "cwd", "workingDirectory", "workspace", "workspacePath") or workspace_folder)
                    workspace_name = str(self._event_field(event, "workspaceName", "workspace_name", "name") or workspace_name)
                    if self._event_field(event, "sessionId", "session_id", "id"):
                        session_id = str(self._event_field(event, "sessionId", "session_id", "id"))
                    if workspace_folder:
                        break
        except OSError:
            pass
        if workspace_folder and not workspace_name:
            workspace_name = Path(workspace_folder).name
        workspace_id = self._make_workspace_id(workspace_folder) if workspace_folder else session_id
        return {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "workspace_name": workspace_name or workspace_id,
            "workspace_folder": workspace_folder,
        }

    def _peek_meta_from_legacy_json(self, session_file: Path) -> dict[str, str]:
        session_id = session_file.stem
        workspace_folder = ""
        workspace_name = ""
        try:
            payload = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}

        if isinstance(payload, dict):
            session_id = str(payload.get("session_id") or payload.get("sessionId") or payload.get("id") or session_id)
            workspace_folder = normalize_path(
                str(
                    payload.get("workspaceFolder")
                    or payload.get("workspace_folder")
                    or payload.get("cwd")
                    or payload.get("workingDirectory")
                    or payload.get("workspace")
                    or payload.get("workspacePath")
                    or ""
                )
            )
            workspace_name = str(payload.get("workspaceName") or payload.get("workspace_name") or "")

            if not workspace_folder:
                items = as_list(payload.get("events")) or as_list(payload.get("history"))
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_dict = as_dict(item)
                    workspace_folder = normalize_path(self._event_field(item_dict, "workspaceFolder", "workspace_folder", "folder", "cwd", "workingDirectory", "workspace", "workspacePath") or workspace_folder)
                    workspace_name = str(self._event_field(item_dict, "workspaceName", "workspace_name", "name") or workspace_name)
                    if workspace_folder:
                        break

        if workspace_folder and not workspace_name:
            workspace_name = Path(workspace_folder).name
        workspace_id = self._make_workspace_id(workspace_folder) if workspace_folder else session_id
        return {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "workspace_name": workspace_name or workspace_id,
            "workspace_folder": workspace_folder,
        }

    @staticmethod
    def _event_field(event: dict[str, Any], *keys: str) -> str:
        raw_data = event.get("data")
        data = as_dict(raw_data)
        raw_context = data.get("context")
        context = as_dict(raw_context)
        for key in keys:
            for source in (event, data, context):
                value = source.get(key)
                if value is not None:
                    return str(value)
        return ""

    @staticmethod
    def _make_workspace_id(workspace_folder: str) -> str:
        normalized = normalize_path(workspace_folder).rstrip("/")
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:16]

    def _parse_session_file(
        self,
        session_file: Path,
        descriptor: WorkspaceDescriptor,
    ) -> tuple[ParsedSession | None, list[ParserIssue]]:
        issues: list[ParserIssue] = []

        if session_file.suffix.lower() == ".json":
            return self._parse_legacy_json_session_file(session_file, descriptor)

        raw_events: list[dict[str, Any]] = []
        try:
            with open(session_file, "r", encoding="utf-8") as handle:
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
                                message=f"Invalid CLI event in {session_file.name} line {line_number}: {exc}",
                            )
                        )
                        continue
                    if isinstance(parsed, dict):
                        raw_events.append(parsed)
        except OSError as exc:
            issues.append(ParserIssue(level="error", code="read_failed", message=f"Cannot read {session_file}: {exc}"))
            return None, issues

        if not raw_events:
            return None, issues

        meta = self._peek_meta(session_file)
        events = [self._parse_event(event, index) for index, event in enumerate(raw_events)]
        title = normalize_session_title(self._derive_title(events, meta["session_id"]), fallback=meta["session_id"])
        started_at_ms, ended_at_ms = self._bounds(events)
        session = ParsedSession(
            session_id=meta["session_id"],
            agent_name=self.AGENT_NAME,
            workspace_id=descriptor.workspace_id,
            workspace_name=descriptor.workspace_name,
            workspace_folder=descriptor.workspace_folder,
            title=title,
            source_path=str(session_file),
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            events=events,
            metadata={"source": self.AGENT_NAME},
            issues=issues,
        )
        return session, issues

    def _parse_legacy_json_session_file(
        self,
        session_file: Path,
        descriptor: WorkspaceDescriptor,
    ) -> tuple[ParsedSession | None, list[ParserIssue]]:
        issues: list[ParserIssue] = []
        try:
            payload = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                ParserIssue(
                    level="warning",
                    code="invalid_legacy_json",
                    message=f"Failed to parse legacy Copilot CLI JSON {session_file}: {exc}",
                )
            )
            return None, issues

        if not isinstance(payload, dict):
            issues.append(
                ParserIssue(
                    level="warning",
                    code="invalid_legacy_json_shape",
                    message=f"Legacy Copilot CLI JSON must be an object: {session_file}",
                )
            )
            return None, issues

        meta = self._legacy_meta(payload, session_file)
        events = self._parse_legacy_json_payload(payload)
        if not events:
            return None, issues

        title = normalize_session_title(self._derive_title(events, meta["session_id"]), fallback=meta["session_id"])
        started_at_ms, ended_at_ms = self._bounds(events)
        session = ParsedSession(
            session_id=meta["session_id"],
            agent_name=self.AGENT_NAME,
            workspace_id=descriptor.workspace_id,
            workspace_name=descriptor.workspace_name,
            workspace_folder=descriptor.workspace_folder,
            title=title,
            source_path=str(session_file),
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            events=events,
            metadata={"source": self.AGENT_NAME, "storage_shape": "legacy-json"},
            issues=issues,
        )
        return session, issues

    def _parse_legacy_json_payload(self, payload: dict[str, Any]) -> list[SessionEventRecord]:
        if isinstance(payload.get("turns"), list) or isinstance(payload.get("messages"), list) or isinstance(payload.get("conversations"), list):
            return self._parse_conversation_style_payload(payload)

        event_items = payload.get("events") if isinstance(payload.get("events"), list) else None
        if event_items is None and isinstance(payload.get("history"), list):
            event_items = payload["history"]
        if event_items is None:
            return self._parse_conversation_style_payload(payload)

        events: list[SessionEventRecord] = []
        for item in event_items:
            if not isinstance(item, dict):
                continue
            parsed_event = self._parse_event(item, len(events))
            if parsed_event.role or parsed_event.text or parsed_event.tool_calls or parsed_event.tool_results:
                events.append(parsed_event)
        return events

    def _parse_conversation_style_payload(self, payload: dict[str, Any]) -> list[SessionEventRecord]:
        events: list[SessionEventRecord] = []

        turns = as_list(payload.get("turns"))
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_dict = as_dict(turn)
            request = as_dict(turn_dict.get("request")) or None
            response = as_dict(turn_dict.get("response")) or None
            if request is not None:
                events.append(self._legacy_message_to_event(request, "user", len(events)))
            if response is not None:
                events.append(self._legacy_message_to_event(response, "assistant", len(events)))

        if not events:
            for item in as_list(payload.get("messages")):
                if not isinstance(item, dict):
                    continue
                item_dict = as_dict(item)
                role = self._event_role(item_dict, item_dict, str(item_dict.get("type") or item_dict.get("role") or "")) or str(item_dict.get("role") or "assistant")
                events.append(self._legacy_message_to_event(item_dict, role, len(events)))

        if not events:
            for conversation in as_list(payload.get("conversations")):
                if not isinstance(conversation, dict):
                    continue
                nested_events = self._parse_conversation_style_payload(as_dict(conversation))
                events.extend(nested_events)

        return [event for event in events if event.role or event.text or event.tool_calls or event.tool_results]

    def _legacy_message_to_event(self, item: dict[str, Any], role: str, index: int) -> SessionEventRecord:
        timestamp_ms = parse_timestamp_ms(item.get("timestamp") or item.get("createdAt") or item.get("time"))
        event_type = str(item.get("type") or f"legacy.{role}")
        text = self._event_text(item, item, event_type)
        return SessionEventRecord(
            index=index,
            event_type=event_type,
            role=role,
            timestamp_ms=timestamp_ms,
            timestamp_iso=timestamp_ms_to_iso(timestamp_ms),
            message_id=str(item.get("messageId") or item.get("message_id") or item.get("id") or ""),
            request_id=str(item.get("requestId") or item.get("messageId") or item.get("id") or ""),
            model_id=str(item.get("model") or item.get("modelId") or item.get("model_id") or ""),
            text=text,
            content_blocks=[
                ContentBlockRecord(
                    index=0,
                    kind=event_type,
                    text=text,
                    data={key: value for key, value in item.items() if key not in {"type"}},
                    raw=item,
                )
            ] if text else [],
            raw=item,
        )

    @staticmethod
    def _legacy_meta(payload: dict[str, Any], session_file: Path) -> dict[str, str]:
        session_id = str(payload.get("session_id") or payload.get("sessionId") or payload.get("id") or session_file.stem)
        workspace_folder = normalize_path(
            str(
                payload.get("cwd")
                or payload.get("workingDirectory")
                or payload.get("workspace")
                or payload.get("workspacePath")
                or ""
            )
        )
        workspace_name = str(payload.get("workspaceName") or payload.get("workspace_name") or "")
        if workspace_folder and not workspace_name:
            workspace_name = Path(workspace_folder).name
        workspace_id = CopilotCliLowLevelParser._make_workspace_id(workspace_folder) if workspace_folder else session_id
        return {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "workspace_name": workspace_name or workspace_id,
            "workspace_folder": workspace_folder,
        }

    def _parse_event(self, event: dict[str, Any], index: int) -> SessionEventRecord:
        event_type = str(event.get("type") or "")
        payload: dict[str, Any] = as_dict(event.get("data")) or event
        role = self._event_role(event, payload, event_type)
        timestamp_ms = parse_timestamp_ms(event.get("timestamp") or payload.get("timestamp") or payload.get("createdAt") or payload.get("time") or event.get("createdAt") or event.get("time"))
        timestamp_iso = timestamp_ms_to_iso(timestamp_ms)
        text = self._event_text(event, payload, event_type)
        tool_calls: list[ToolCallRecord] = []
        tool_results: list[ToolResultRecord] = []

        if event_type == "tool.execution_complete":
            tool_result = ToolResultRecord(
                tool_call_id=str(payload.get("toolCallId") or payload.get("toolId") or event.get("toolCallId") or event.get("toolId") or ""),
                kind=event_type,
                text=flatten_text_content(payload.get("output") or payload.get("result") or payload.get("message") or payload.get("content") or event.get("output") or event.get("result") or event.get("message") or event.get("content")),
                structured_content={key: value for key, value in payload.items()},
                is_error=bool(payload.get("error")) if payload.get("error") is not None else bool(event.get("error")) if event.get("error") is not None else None,
                status=self._event_status(payload, event),
                raw=event,
            )
            tool_results.append(tool_result)

        if payload.get("toolId") or payload.get("toolName") or event.get("toolId") or event.get("toolName"):
            tool_calls.append(
                ToolCallRecord(
                    call_id=str(payload.get("toolId") or payload.get("toolCallId") or event.get("toolId") or event.get("toolCallId") or ""),
                    name=str(payload.get("toolName") or payload.get("toolId") or event.get("toolName") or event.get("toolId") or ""),
                    kind=event_type,
                    arguments=as_dict(payload.get("input")) or as_dict(event.get("input")),
                    arguments_text=json.dumps(payload.get("input") if isinstance(payload.get("input"), dict) else event.get("input"), ensure_ascii=True, sort_keys=True) if isinstance(payload.get("input"), dict) or isinstance(event.get("input"), dict) else "",
                    file_paths=self._extract_file_paths(payload, event),
                    raw=event,
                )
            )

        for tool_request in as_list(payload.get("toolRequests")):
                if not isinstance(tool_request, dict):
                    continue
                tool_request_dict = as_dict(tool_request)
                arguments_value = tool_request_dict.get("arguments")
                arguments = as_dict(arguments_value)
                tool_calls.append(
                    ToolCallRecord(
                        call_id=str(tool_request_dict.get("toolCallId") or tool_request_dict.get("toolId") or ""),
                        name=str(tool_request_dict.get("name") or tool_request_dict.get("toolName") or ""),
                        kind=str(tool_request_dict.get("type") or event_type),
                        arguments=arguments,
                        arguments_text=json.dumps(arguments_value, ensure_ascii=True, sort_keys=True) if isinstance(arguments_value, (dict, list, str, int, float, bool)) else "",
                        file_paths=self._extract_file_paths(tool_request_dict, payload, event),
                        raw=tool_request_dict,
                    )
                )

        content_blocks = [
            ContentBlockRecord(
                index=0,
                kind=event_type or "event",
                text=text,
                data={key: value for key, value in payload.items()},
                raw=event,
                extra={
                    **({"tool_call": tool_calls[0].to_dict()} if tool_calls else {}),
                    **({"tool_result": tool_results[0].to_dict()} if tool_results else {}),
                },
            )
        ] if text or tool_calls or tool_results else []

        return SessionEventRecord(
            index=index,
            event_type=event_type,
            role=role,
            timestamp_ms=timestamp_ms,
            timestamp_iso=timestamp_iso,
            message_id=str(payload.get("messageId") or payload.get("message_id") or event.get("messageId") or event.get("message_id") or event.get("id") or ""),
            request_id=str(payload.get("requestId") or payload.get("request_id") or payload.get("messageId") or event.get("requestId") or event.get("request_id") or event.get("messageId") or event.get("id") or ""),
            model_id=str(payload.get("model") or payload.get("modelId") or payload.get("model_id") or payload.get("newModel") or event.get("model") or event.get("modelId") or event.get("model_id") or event.get("newModel") or ""),
            text=text,
            content_blocks=content_blocks,
            tool_calls=tool_calls,
            tool_results=tool_results,
            file_paths=self._extract_file_paths(payload, event),
            raw=event,
        )

    @staticmethod
    def _event_role(event: dict[str, Any], payload: dict[str, Any], event_type: str) -> str:
        explicit_role = payload.get("role") if payload.get("role") is not None else event.get("role")
        if isinstance(explicit_role, str):
            lowered = explicit_role.lower()
            if lowered in {"user", "human"}:
                return "user"
            if lowered == "system":
                return "system"
            if lowered:
                return "assistant"
        lowered_type = event_type.lower()
        if lowered_type.startswith("system"):
            return "system"
        if "user" in lowered_type or lowered_type in {"userpromptsubmitted", "prompt"}:
            return "user"
        if "assistant" in lowered_type or lowered_type in {"assistantresponse", "response", "completion"}:
            return "assistant"
        return ""

    def _event_text(self, event: dict[str, Any], payload: dict[str, Any], event_type: str) -> str:
        for key in ("content", "transformedContent", "message", "text", "value"):
            text = flatten_text_content(payload.get(key))
            if text:
                return text
        for key in ("message", "content", "text", "value"):
            text = flatten_text_content(event.get(key))
            if text:
                return text
        if event_type.lower() in {"userpromptsubmitted", "prompt"}:
            text = flatten_text_content(payload.get("prompt") or payload.get("initialPrompt") or event.get("prompt") or event.get("initialPrompt"))
            if text:
                return text
        if event_type.lower() in {"assistantresponse", "response", "completion", "tool.execution_complete"}:
            text = flatten_text_content(payload.get("output") or payload.get("result") or event.get("output") or event.get("result"))
            if text:
                return text
        return ""

    @staticmethod
    def _extract_file_paths(*containers: dict[str, Any]) -> list[str]:
        files: set[str] = set()
        for container in containers:
            if not isinstance(container, dict):
                continue
            raw_files = container.get("files") or container.get("attachments") or []
            if isinstance(raw_files, list):
                for item in raw_files:
                    if isinstance(item, str) and item:
                        files.add(normalize_path(item))
                    elif isinstance(item, dict):
                        candidate = item.get("path") or item.get("uri") or item.get("name")
                        if isinstance(candidate, str) and candidate:
                            files.add(normalize_path(candidate))
            arguments = as_dict(container.get("arguments"))
            for key in ("path", "file_path", "file"):
                candidate = arguments.get(key)
                if isinstance(candidate, str) and candidate:
                    files.add(normalize_path(candidate))
        return sorted(files)

    @staticmethod
    def _event_status(payload: dict[str, Any], event: dict[str, Any]) -> str | None:
        if isinstance(payload.get("status"), str) and payload["status"]:
            return str(payload["status"])
        if isinstance(event.get("status"), str) and event["status"]:
            return str(event["status"])
        if payload.get("error") or event.get("error"):
            return "error"
        success = payload.get("success") if payload.get("success") is not None else event.get("success")
        if success is True:
            return "success"
        if success is False:
            return "error"
        exit_code = payload.get("exitCode") if payload.get("exitCode") is not None else event.get("exitCode")
        if isinstance(exit_code, int):
            return "success" if exit_code == 0 else "error"
        return None

    @staticmethod
    def _derive_title(events: list[SessionEventRecord], fallback: str) -> str:
        for event in events:
            if event.role == "user" and event.text.strip():
                return normalize_session_title(event.text, fallback=fallback)
        return normalize_session_title(fallback)

    @staticmethod
    def _bounds(events: list[SessionEventRecord]) -> tuple[int | None, int | None]:
        timestamps = [event.timestamp_ms for event in events if event.timestamp_ms is not None]
        if not timestamps:
            return None, None
        return min(timestamps), max(timestamps)