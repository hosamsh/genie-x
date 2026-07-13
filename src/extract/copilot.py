"""Low-level GitHub Copilot Chat parser."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any

from src.extract.base import LowLevelWorkspaceParser
from src.extract.models import (
    AttachmentRef,
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
from src.shared.io.paths import decode_file_uri, normalize_path
from src.shared.models.workspace import WorkspaceActivity


class CopilotLowLevelParser(LowLevelWorkspaceParser):
    """Source-of-truth parser for VS Code Copilot Chat session storage."""

    AGENT_NAME = "copilot"
    DISCOVERABLE_JSON_MARKERS = (
        '"message"',
        '"response"',
        '"turns"',
        '"messages"',
        '"variableData"',
    )

    def __init__(
        self,
        workspace_storage: Path | None = None,
        global_storage: Path | None = None,
    ) -> None:
        self._workspace_storage = Path(workspace_storage) if workspace_storage else self._default_workspace_storage()
        self._global_storage = Path(global_storage) if global_storage else self._default_global_storage()

    def scan_workspaces(self) -> list[WorkspaceDescriptor]:
        descriptors: list[WorkspaceDescriptor] = []

        if self._workspace_storage.exists():
            for folder in sorted(self._workspace_storage.iterdir()):
                if not folder.is_dir():
                    continue
                chat_dir = folder / "chatSessions"
                edit_sessions_dir = folder / "chatEditingSessions"
                session_files = self._resolve_session_files(chat_dir)
                if not session_files:
                    continue
                workspace_folder, workspace_name = self._load_workspace_identity(folder)
                descriptors.append(
                    WorkspaceDescriptor(
                        workspace_id=folder.name,
                        agent_name=self.AGENT_NAME,
                        workspace_name=workspace_name or folder.name,
                        workspace_folder=workspace_folder,
                        source_root=str(folder),
                        metadata={
                            "chat_sessions_dir": str(chat_dir),
                            "chat_editing_sessions_dir": str(edit_sessions_dir),
                            "session_count": len(session_files),
                        },
                    )
                )

        for subdir_name in ("emptyWindowChatSessions", "transferredChatSessions"):
            chat_dir = self._global_storage / subdir_name
            session_files = self._resolve_session_files(chat_dir)
            if not session_files:
                continue
            workspace_id = f"globalStorage/{subdir_name}"
            descriptors.append(
                WorkspaceDescriptor(
                    workspace_id=workspace_id,
                    agent_name=self.AGENT_NAME,
                    workspace_name="empty-window" if subdir_name == "emptyWindowChatSessions" else "transferred",
                    workspace_folder="",
                    source_root=str(self._global_storage),
                    metadata={
                        "chat_sessions_dir": str(chat_dir),
                        "chat_editing_sessions_dir": str(self._global_storage / "chatEditingSessions"),
                        "session_count": len(session_files),
                    },
                )
            )

        return descriptors

    def parse_workspace(self, workspace_id: str) -> ParsedWorkspace:
        descriptor = next((item for item in self.scan_workspaces() if item.workspace_id == workspace_id), None)
        if descriptor is None:
            descriptor = WorkspaceDescriptor(
                workspace_id=workspace_id,
                agent_name=self.AGENT_NAME,
                workspace_name=workspace_id,
                source_root=str(self._workspace_storage / workspace_id),
            )

        chat_sessions_dir = Path(descriptor.metadata.get("chat_sessions_dir") or Path(descriptor.source_root) / "chatSessions")
        chat_editing_sessions_dir = Path(
            descriptor.metadata.get("chat_editing_sessions_dir") or Path(descriptor.source_root) / "chatEditingSessions"
        )
        issues: list[ParserIssue] = []
        sessions: list[ParsedSession] = []
        edit_blocks_by_session = self._load_edit_session_blocks(chat_editing_sessions_dir, issues)

        for session_file in self._resolve_session_files(chat_sessions_dir):
            session_issues: list[ParserIssue] = []
            payload = self._load_session_payload(session_file, session_issues)
            issues.extend(session_issues)
            if not payload:
                continue
            parsed_sessions = self._parse_payload(payload, session_file, descriptor)
            for parsed_session in parsed_sessions:
                self._attach_edit_session_blocks(
                    parsed_session,
                    edit_blocks_by_session.get(parsed_session.session_id, {}),
                )
            sessions.extend(parsed_sessions)

        sessions.sort(key=lambda item: (item.started_at_ms or 0, item.session_id))
        return ParsedWorkspace(
            descriptor=descriptor,
            sessions=sessions,
            issues=issues,
            metadata={
                "chat_sessions_dir": str(chat_sessions_dir),
                "chat_editing_sessions_dir": str(chat_editing_sessions_dir),
            },
        )

    def get_workspace_activity(self, descriptor: WorkspaceDescriptor) -> WorkspaceActivity:
        chat_sessions_dir = Path(descriptor.metadata.get("chat_sessions_dir") or Path(descriptor.source_root) / "chatSessions")
        visible_session_ids: list[str] = []
        for session_file in self._resolve_session_files(chat_sessions_dir):
            if self._session_file_has_discoverable_activity(session_file):
                visible_session_ids.append(session_file.stem)
        return WorkspaceActivity(
            session_count=len(visible_session_ids),
            turn_count=len(visible_session_ids),
            session_ids=visible_session_ids,
        )

    @staticmethod
    def _default_workspace_storage() -> Path:
        system = platform.system()
        if system == "Windows":
            return Path(os.environ.get("APPDATA", "")) / "Code/User/workspaceStorage"
        if system == "Darwin":
            return Path.home() / "Library/Application Support/Code/User/workspaceStorage"
        return Path.home() / ".config/Code/User/workspaceStorage"

    @staticmethod
    def _default_global_storage() -> Path:
        system = platform.system()
        if system == "Windows":
            return Path(os.environ.get("APPDATA", "")) / "Code/User/globalStorage"
        if system == "Darwin":
            return Path.home() / "Library/Application Support/Code/User/globalStorage"
        return Path.home() / ".config/Code/User/globalStorage"

    @staticmethod
    def _resolve_session_files(chat_dir: Path) -> list[Path]:
        if not chat_dir.exists() or not chat_dir.is_dir():
            return []
        jsonl_by_stem = {path.stem: path for path in chat_dir.glob("*.jsonl")}
        json_by_stem = {path.stem: path for path in chat_dir.glob("*.json")}
        merged = {**json_by_stem, **jsonl_by_stem}
        return sorted(merged.values())

    def _session_file_has_discoverable_activity(self, session_file: Path) -> bool:
        if session_file.suffix == ".jsonl":
            return self._jsonl_session_file_has_discoverable_activity(session_file)
        return self._json_session_file_has_discoverable_activity(session_file)

    def _jsonl_session_file_has_discoverable_activity(self, session_file: Path) -> bool:
        try:
            with open(session_file, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        line = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(line, dict):
                        continue
                    if self._payload_has_discoverable_activity(line):
                        return True
                    value = line.get("v")
                    key_path = line.get("k")
                    if isinstance(value, dict) and self._payload_has_discoverable_activity(value):
                        return True
                    if isinstance(key_path, list) and key_path and key_path[0] in {"requests", "turns", "messages"}:
                        if self._payload_has_discoverable_activity({key_path[0]: value}):
                            return True
        except OSError:
            return False
        return False

    def _json_session_file_has_discoverable_activity(self, session_file: Path) -> bool:
        try:
            with open(session_file, "r", encoding="utf-8", errors="ignore") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        return False
                    if any(marker in chunk for marker in self.DISCOVERABLE_JSON_MARKERS):
                        return True
        except OSError:
            return False

    @classmethod
    def _payload_has_discoverable_activity(cls, payload: dict[str, Any] | list[Any]) -> bool:
        for conversation in cls._payload_conversations(payload):
            for request in as_list(conversation.get("requests")):
                if isinstance(request, dict) and cls._request_has_discoverable_activity(request):
                    return True

            for turn in as_list(conversation.get("turns")):
                if not isinstance(turn, dict):
                    continue
                request = turn.get("request")
                response = turn.get("response")
                if isinstance(request, dict) and cls._request_has_discoverable_activity(request):
                    return True
                if isinstance(response, dict) and flatten_text_content(response).strip():
                    return True

            for message in as_list(conversation.get("messages")):
                if isinstance(message, dict) and flatten_text_content(message).strip():
                    return True
        return False

    @staticmethod
    def _request_has_discoverable_activity(request: dict[str, Any]) -> bool:
        raw_message = request.get("message")
        if isinstance(raw_message, str) and raw_message.strip():
            return True
        if isinstance(raw_message, dict) and flatten_text_content(raw_message).strip():
            return True
        if as_list(request.get("response")):
            return True
        if request.get("result") not in (None, "", [], {}):
            return True
        if as_list(as_dict(request.get("variableData")).get("variables")):
            return True
        return False

    @staticmethod
    def _payload_conversations(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("conversations"), list):
            return [item for item in payload["conversations"] if isinstance(item, dict)]
        if isinstance(payload, dict):
            return [payload]
        return []

    @staticmethod
    def _load_workspace_identity(folder: Path) -> tuple[str, str]:
        workspace_folder = ""
        workspace_name = folder.name
        workspace_json = folder / "workspace.json"
        if workspace_json.exists():
            try:
                data = json.loads(workspace_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return workspace_folder, workspace_name
            uri = data.get("folder") or data.get("folderUri") or ""
            if uri:
                workspace_folder = decode_file_uri(uri)
                if workspace_folder:
                    workspace_name = Path(workspace_folder).name or workspace_name
        return workspace_folder, workspace_name

    def _load_session_payload(self, session_file: Path, issues: list[ParserIssue]) -> dict[str, Any] | list[Any]:
        try:
            if session_file.suffix == ".jsonl":
                return self._parse_jsonl_session(session_file)
            return json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                ParserIssue(
                    level="warning",
                    code="invalid_session_file",
                    message=f"Failed to parse {session_file}: {exc}",
                )
            )
            return {}

    def _load_edit_session_blocks(
        self,
        edit_sessions_dir: Path,
        issues: list[ParserIssue],
    ) -> dict[str, dict[str, list[ContentBlockRecord]]]:
        if not edit_sessions_dir.exists() or not edit_sessions_dir.is_dir():
            return {}

        blocks_by_session: dict[str, dict[str, list[ContentBlockRecord]]] = {}
        for session_dir in sorted(edit_sessions_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            state_file = session_dir / "state.json"
            if not state_file.exists():
                continue
            try:
                payload = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                issues.append(
                    ParserIssue(
                        level="warning",
                        code="invalid_edit_session_file",
                        message=f"Failed to parse {state_file}: {exc}",
                    )
                )
                continue

            session_blocks = self._build_edit_session_blocks(payload, session_dir)
            if session_blocks:
                blocks_by_session[session_dir.name] = session_blocks

        return blocks_by_session

    def _build_edit_session_blocks(
        self,
        payload: dict[str, Any],
        session_dir: Path,
    ) -> dict[str, list[ContentBlockRecord]]:
        timeline = as_dict(payload.get("timeline"))
        recent_snapshot = as_dict(payload.get("recentSnapshot"))
        contents_dir = session_dir / "contents"

        recent_hashes: dict[str, str] = {}
        for entry in as_list(recent_snapshot.get("entries")):
            entry_dict = as_dict(entry)
            resource = entry_dict.get("resource") or entry_dict.get("uri")
            current_hash = entry_dict.get("currentHash") or entry_dict.get("content")
            if isinstance(resource, str) and resource and isinstance(current_hash, str) and current_hash:
                recent_hashes[resource] = current_hash

        baselines_by_resource: dict[str, list[dict[str, Any]]] = {}
        for order, (baseline_key, baseline_payload) in enumerate(self._mapping_entries(timeline.get("fileBaselines"))):
            baseline_dict = as_dict(baseline_payload)
            baseline_key_text = baseline_key if isinstance(baseline_key, str) else ""
            resource = ""
            request_id = ""

            if baseline_key_text:
                resource, _, request_id = baseline_key_text.partition("::")

            if not resource:
                raw_resource = baseline_dict.get("resource") or baseline_dict.get("uri")
                resource = raw_resource if isinstance(raw_resource, str) else ""
            if not request_id:
                request_id = str(baseline_dict.get("requestId") or "")

            content_hash = baseline_dict.get("content")
            epoch = baseline_dict.get("epoch")
            epoch_value = int(epoch) if isinstance(epoch, (int, float)) else order

            if not resource or not request_id or not isinstance(content_hash, str) or not content_hash:
                continue

            baselines_by_resource.setdefault(resource, []).append(
                {
                    "request_id": request_id,
                    "content_hash": content_hash,
                    "epoch": epoch_value,
                    "order": order,
                }
            )

        blocks_by_request: dict[str, list[ContentBlockRecord]] = {}
        for resource, baselines in baselines_by_resource.items():
            baselines.sort(key=lambda item: (int(item["epoch"]), int(item["order"])))
            file_path = self._normalize_edit_resource_path(resource)
            if not file_path:
                continue

            for index, baseline in enumerate(baselines):
                after_hash = (
                    baselines[index + 1]["content_hash"]
                    if index + 1 < len(baselines)
                    else recent_hashes.get(resource, "")
                )
                if not isinstance(after_hash, str) or not after_hash:
                    continue

                before_text = self._read_edit_content(contents_dir, str(baseline["content_hash"]))
                after_text = self._read_edit_content(contents_dir, after_hash)
                if before_text is None or after_text is None or before_text == after_text:
                    continue

                request_id = str(baseline["request_id"])
                request_blocks = blocks_by_request.setdefault(request_id, [])
                raw_block = {
                    "uri": {"path": file_path},
                    "edits": [[{"text": after_text}]],
                    "beforeText": before_text,
                    "afterText": after_text,
                }
                request_blocks.append(
                    ContentBlockRecord(
                        index=len(request_blocks),
                        kind="textEditGroup",
                        text=after_text,
                        data={
                            "before_hash": str(baseline["content_hash"]),
                            "after_hash": after_hash,
                        },
                        raw=raw_block,
                        extra={
                            "text_edit_group": {
                                "uri": {"path": file_path},
                                "edits": [[{"text": after_text}]],
                                "before_text": before_text,
                                "after_text": after_text,
                                "before_hash": str(baseline["content_hash"]),
                                "after_hash": after_hash,
                            }
                        },
                    )
                )

        return blocks_by_request

    def _attach_edit_session_blocks(
        self,
        session: ParsedSession,
        session_blocks: dict[str, list[ContentBlockRecord]],
    ) -> None:
        if not session_blocks:
            return

        for event in session.events:
            if event.role != "assistant" or not event.request_id:
                continue

            blocks = session_blocks.pop(event.request_id, None)
            if not blocks:
                continue

            for block in blocks:
                event.content_blocks.append(
                    ContentBlockRecord(
                        index=len(event.content_blocks),
                        kind=block.kind,
                        text=block.text,
                        data=dict(block.data),
                        raw=dict(block.raw),
                        extra=dict(block.extra),
                    )
                )

            event.file_paths = sorted(
                {
                    *event.file_paths,
                    *self._extract_response_file_paths(event.content_blocks),
                }
            )
            event.text = self._render_blocks_text(event.content_blocks) or event.text

    @staticmethod
    def _mapping_entries(value: Any) -> list[tuple[Any, Any]]:
        if isinstance(value, dict):
            return list(value.items())
        entries: list[tuple[Any, Any]] = []
        for item in as_list(value):
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                entries.append((item[0], item[1]))
        return entries

    @staticmethod
    def _read_edit_content(contents_dir: Path, content_hash: str) -> str | None:
        if not content_hash:
            return None
        content_file = contents_dir / content_hash
        try:
            return content_file.read_text(encoding="utf-8")
        except OSError:
            return None

    @staticmethod
    def _normalize_edit_resource_path(resource: str) -> str:
        if not resource:
            return ""
        decoded = decode_file_uri(resource) if resource.startswith("file:") else resource
        return normalize_path(decoded) if decoded else ""

    def _parse_payload(
        self,
        payload: dict[str, Any] | list[Any],
        session_file: Path,
        descriptor: WorkspaceDescriptor,
    ) -> list[ParsedSession]:
        conversations = self._payload_conversations(payload)

        parsed_sessions: list[ParsedSession] = []
        single_conversation = len(conversations) == 1
        for index, conversation in enumerate(conversations):
            session_id = str(conversation.get("id") or (session_file.stem if single_conversation else f"{session_file.stem}-{index}"))
            title = normalize_session_title(conversation.get("customTitle") or conversation.get("title") or conversation.get("chatTitle") or "")
            workspace_folder = descriptor.workspace_folder or str(conversation.get("workspaceFolder") or conversation.get("workspace") or conversation.get("workspacePath") or "")
            events: list[SessionEventRecord] = []

            for request in as_list(conversation.get("requests")):
                if not isinstance(request, dict):
                    continue
                events.append(self._build_request_event(request, len(events)))
                if request.get("response"):
                    events.append(self._build_request_response_event(request, len(events)))

            for turn in as_list(conversation.get("turns")):
                if not isinstance(turn, dict):
                    continue
                request = turn.get("request") if isinstance(turn.get("request"), dict) else None
                response = turn.get("response") if isinstance(turn.get("response"), dict) else None
                if request is not None:
                    events.append(self._build_request_event(request, len(events)))
                if response is not None:
                    events.append(self._build_response_event(response, turn, len(events)))

            if not events and isinstance(conversation.get("messages"), list):
                for raw_message in conversation["messages"]:
                    if not isinstance(raw_message, dict):
                        continue
                    role = str(raw_message.get("role") or "assistant")
                    events.append(
                        SessionEventRecord(
                            index=len(events),
                            event_type=f"message.{role}",
                            role=role,
                            timestamp_ms=parse_timestamp_ms(raw_message.get("timestamp") or raw_message.get("createdAt")),
                            timestamp_iso=timestamp_ms_to_iso(parse_timestamp_ms(raw_message.get("timestamp") or raw_message.get("createdAt"))),
                            text=self._extract_message_text(raw_message),
                            content_blocks=self._build_response_blocks(raw_message.get("content") or raw_message.get("message") or raw_message.get("text")),
                            raw=raw_message,
                        )
                    )

            if not title:
                title = self._derive_title(events, session_id)
            title = normalize_session_title(title, fallback=session_id)
            if not self._events_have_meaningful_content(events):
                continue

            started_at_ms, ended_at_ms = self._bounds(events)
            parsed_sessions.append(
                ParsedSession(
                    session_id=session_id,
                    agent_name=self.AGENT_NAME,
                    workspace_id=descriptor.workspace_id,
                    workspace_name=descriptor.workspace_name,
                    workspace_folder=workspace_folder,
                    title=title,
                    source_path=str(session_file),
                    started_at_ms=started_at_ms,
                    ended_at_ms=ended_at_ms,
                    events=events,
                    metadata={"source": self.AGENT_NAME, "file_suffix": session_file.suffix},
                )
            )
        return parsed_sessions

    @staticmethod
    def _events_have_meaningful_content(events: list[SessionEventRecord]) -> bool:
        for event in events:
            if (event.text or event.thinking_text).strip():
                return True
            if event.tool_calls or event.tool_results or event.attachments or event.file_paths:
                return True
            if any((block.text or block.kind).strip() for block in event.content_blocks):
                return True
        return False

    def _build_request_event(self, request: dict[str, Any], index: int) -> SessionEventRecord:
        timestamp_ms = parse_timestamp_ms(request.get("timestamp") or request.get("createdAt"))
        content_blocks = self._build_request_blocks(request)
        attachments = self._extract_request_attachments(request)
        return SessionEventRecord(
            index=index,
            event_type="user.request",
            role="user",
            timestamp_ms=timestamp_ms,
            timestamp_iso=timestamp_ms_to_iso(timestamp_ms),
            request_id=str(request.get("requestId") or request.get("requestUUID") or request.get("clientRequestId") or ""),
            model_id=str(request.get("modelId") or request.get("model") or ""),
            text=self._extract_user_text(request),
            attachments=attachments,
            content_blocks=content_blocks,
            file_paths=self._extract_request_file_paths(request),
            raw=request,
            extra={
                "variable_data": as_dict(request.get("variableData")),
            },
        )

    def _build_response_event(self, response: dict[str, Any], turn: dict[str, Any], index: int) -> SessionEventRecord:
        timestamp_ms = parse_timestamp_ms(response.get("timestamp") or response.get("createdAt") or turn.get("timestamp"))
        content_value = response.get("message") if "message" in response else response
        blocks = self._build_response_blocks(content_value)
        tool_calls = [
            ToolCallRecord.from_dict(block.extra["tool_call"])
            for block in blocks
            if "tool_call" in block.extra
        ]
        tool_results = [
            ToolResultRecord.from_dict(block.extra["tool_result"])
            for block in blocks
            if "tool_result" in block.extra
        ]
        file_paths = sorted(
            {
                *self._extract_response_file_paths(blocks),
                *self._extract_edited_file_paths(turn),
            }
        )
        return SessionEventRecord(
            index=index,
            event_type="assistant.response",
            role="assistant",
            timestamp_ms=timestamp_ms,
            timestamp_iso=timestamp_ms_to_iso(timestamp_ms),
            request_id=str(turn.get("requestId") or response.get("requestId") or ""),
            model_id=str(response.get("modelId") or response.get("model") or turn.get("modelId") or ""),
            text=self._render_blocks_text(blocks) or self._extract_message_text(content_value),
            thinking_text=self._extract_thinking_text(blocks),
            content_blocks=blocks,
            tool_calls=tool_calls,
            tool_results=tool_results,
            file_paths=file_paths,
            raw=response,
            extra={"response_time_ms": self._extract_response_time(turn)},
        )

    def _build_request_response_event(self, request: dict[str, Any], index: int) -> SessionEventRecord:
        timestamp_ms = parse_timestamp_ms(request.get("timestamp") or request.get("createdAt"))
        content_value = request.get("response")
        blocks = self._build_response_blocks(content_value)
        tool_calls = [
            ToolCallRecord.from_dict(block.extra["tool_call"])
            for block in blocks
            if "tool_call" in block.extra
        ]
        tool_results = [
            ToolResultRecord.from_dict(block.extra["tool_result"])
            for block in blocks
            if "tool_result" in block.extra
        ]
        file_paths = sorted(
            {
                *self._extract_response_file_paths(blocks),
                *self._extract_edited_file_paths(request),
            }
        )
        return SessionEventRecord(
            index=index,
            event_type="assistant.response",
            role="assistant",
            timestamp_ms=timestamp_ms,
            timestamp_iso=timestamp_ms_to_iso(timestamp_ms),
            request_id=str(request.get("requestId") or request.get("requestUUID") or request.get("clientRequestId") or ""),
            model_id=str(request.get("modelId") or request.get("model") or ""),
            text=self._render_blocks_text(blocks) or flatten_text_content(content_value),
            thinking_text=self._extract_thinking_text(blocks),
            content_blocks=blocks,
            tool_calls=tool_calls,
            tool_results=tool_results,
            file_paths=file_paths,
            raw=request,
            extra={"response_time_ms": self._extract_response_time(request)},
        )

    def _build_response_blocks(self, content_value: Any) -> list[ContentBlockRecord]:
        if isinstance(content_value, str):
            return [ContentBlockRecord(index=0, kind="text", text=content_value, raw={"value": content_value})]
        if not isinstance(content_value, list):
            if isinstance(content_value, dict):
                text = self._extract_message_text(content_value)
                return [ContentBlockRecord(index=0, kind="message", text=text, raw=content_value)] if text else []
            return []

        blocks: list[ContentBlockRecord] = []
        for index, item in enumerate(content_value):
            if not isinstance(item, dict):
                continue
            item_dict = as_dict(item)
            kind = str(item_dict.get("kind") or item_dict.get("type") or "unknown")
            extra: dict[str, Any] = {}
            text = ""

            if kind == "thinking":
                value = item_dict.get("value")
                text = value if isinstance(value, str) else self._extract_message_text(item_dict)
                extra["thinking"] = {"value": value}
            elif kind == "inlineReference":
                reference = as_dict(item_dict.get("inlineReference"))
                resolved_name = self._extract_inline_reference_name(reference)
                text = f"`{resolved_name}`" if resolved_name else ""
                extra["inline_reference"] = {
                    "name": resolved_name,
                    "reference": reference,
                }
            elif kind == "codeblockUri":
                uri = as_dict(item_dict.get("codeblockUri"))
                path = uri.get("path") or uri.get("fsPath")
                text = normalize_path(path) if isinstance(path, str) and path else ""
                extra["codeblock_uri"] = uri
            elif kind == "textEditGroup":
                text = self._extract_text_edit_group_text(item_dict)
                extra["text_edit_group"] = {
                    "uri": as_dict(item_dict.get("uri")),
                    "edits": as_list(item_dict.get("edits")),
                }
            else:
                text = self._extract_message_text(item_dict)

            tool_call = self._extract_tool_call_from_item(item_dict, kind)
            if tool_call is not None:
                extra["tool_call"] = tool_call.to_dict()
                if not text:
                    text = tool_call.name
            tool_result = self._extract_tool_result_from_item(item_dict, kind)
            if tool_result is not None:
                extra["tool_result"] = tool_result.to_dict()
            blocks.append(
                ContentBlockRecord(
                    index=index,
                    kind=kind,
                    text=text,
                    data={str(key): value for key, value in item_dict.items() if key not in {"kind", "type"}},
                    raw=item_dict,
                    extra=extra,
                )
            )
        return blocks

    def _build_request_blocks(self, request: dict[str, Any]) -> list[ContentBlockRecord]:
        blocks: list[ContentBlockRecord] = []
        raw_message = request.get("message")
        message = as_dict(raw_message)
        text = raw_message if isinstance(raw_message, str) else message.get("text")
        if isinstance(text, str) and text.strip():
            blocks.append(
                ContentBlockRecord(
                    index=len(blocks),
                    kind="text",
                    text=text,
                    raw={"text": text},
                )
            )

        parts = as_list(message.get("parts"))
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_text = part.get("text")
            if isinstance(part_text, str) and part_text.strip():
                blocks.append(
                    ContentBlockRecord(
                        index=len(blocks),
                        kind="requestPart",
                        text=part_text,
                        data={key: value for key, value in part.items() if key != "text"},
                        raw=part,
                    )
                )

        variables = as_list(as_dict(request.get("variableData")).get("variables"))
        if isinstance(variables, list):
            for variable in variables:
                if not isinstance(variable, dict):
                    continue
                variable_dict = as_dict(variable)
                value = as_dict(variable_dict.get("value"))
                label = str(variable_dict.get("name") or value.get("path") or value.get("uri") or "").strip()
                blocks.append(
                    ContentBlockRecord(
                        index=len(blocks),
                        kind="variableRef",
                        text=label,
                        data={str(key): val for key, val in variable_dict.items() if key != "value"},
                        raw=variable_dict,
                    )
                )
        return blocks

    @staticmethod
    def _extract_tool_call_from_item(item: dict[str, Any], kind: str) -> ToolCallRecord | None:
        tool_id = str(item.get("toolCallId") or item.get("toolId") or "")
        tool_name = str(item.get("toolName") or item.get("toolId") or item.get("toolCallId") or "")
        if not tool_name:
            return None
        invocation_message = as_dict(item.get("invocationMessage"))
        file_paths = CopilotLowLevelParser._extract_tool_file_paths(invocation_message)
        return ToolCallRecord(
            call_id=tool_id,
            name=tool_name,
            kind=kind,
            arguments=invocation_message,
            arguments_text=json.dumps(invocation_message, ensure_ascii=True, sort_keys=True) if invocation_message else "",
            file_paths=sorted(set(file_paths)),
            raw=item,
        )

    def _extract_tool_result_from_item(self, item: dict[str, Any], kind: str) -> ToolResultRecord | None:
        tool_call_id = str(item.get("toolCallId") or item.get("toolId") or "")
        if not tool_call_id:
            return None

        structured_content: dict[str, Any] = {}
        for key in ("pastTenseMessage", "toolSpecificData", "generatedTitle", "isComplete", "isConfirmed", "source", "value", "message", "result", "content"):
            value = item.get(key)
            if value in (None, "", [], {}):
                continue
            structured_content[str(key)] = value

        text = (
            self._extract_message_text(item.get("pastTenseMessage"))
            or self._extract_message_text(item.get("value"))
            or self._extract_message_text(item.get("message"))
            or self._extract_message_text(item.get("result"))
            or self._extract_message_text(item.get("content"))
        )

        if not text and not structured_content:
            return None

        is_error_raw = item.get("isError")
        is_error = bool(is_error_raw) if isinstance(is_error_raw, bool) else None
        is_complete = item.get("isComplete")
        status = "error" if is_error else "success" if is_complete is True or bool(text) or bool(structured_content) else None

        return ToolResultRecord(
            tool_call_id=tool_call_id,
            kind=f"{kind}_result" if kind else "tool_result",
            text=text,
            structured_content=structured_content,
            is_error=is_error,
            status=status,
            raw=item,
        )

    @staticmethod
    def _extract_tool_file_paths(invocation_message: dict[str, Any]) -> list[str]:
        file_paths: list[str] = []

        uris = invocation_message.get("uris")
        if isinstance(uris, list):
            uri_entries = [as_dict(uri) for uri in uris]
        elif isinstance(uris, dict):
            uri_entries = [as_dict(uri) for uri in uris.values()]
        else:
            uri_entries = []

        for uri in uri_entries:
            path = uri.get("path") or uri.get("fsPath")
            if isinstance(path, str) and path:
                file_paths.append(normalize_path(path))

        for key in ("path", "file_path", "filePath"):
            path = invocation_message.get(key)
            if isinstance(path, str) and path:
                file_paths.append(normalize_path(path))

        return file_paths

    @staticmethod
    def _extract_user_text(request: dict[str, Any]) -> str:
        raw_message = request.get("message")
        if isinstance(raw_message, str) and raw_message.strip():
            return raw_message
        message = as_dict(raw_message)
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text
        parts = as_list(message.get("parts"))
        values = [str(part.get("text") or "") for part in parts if isinstance(part, dict) and part.get("text")]
        return "\n".join(values)

    def _extract_message_text(self, value: Any) -> str:
        if isinstance(value, dict):
            if isinstance(value.get("value"), str) and value["value"].strip():
                return value["value"]
            if isinstance(value.get("message"), str) and value["message"].strip():
                return value["message"]
            if isinstance(value.get("text"), str) and value["text"].strip():
                return value["text"]
            if value.get("kind") == "inlineReference":
                filename = self._extract_inline_reference_name(value.get("inlineReference"))
                return f"`{filename}`" if filename else ""
        return flatten_text_content(value)

    @staticmethod
    def _extract_text_edit_group_text(item: dict[str, Any]) -> str:
        edit_texts: list[str] = []
        edits = as_list(item.get("edits"))
        for edit_group in edits:
            if isinstance(edit_group, list):
                for edit in edit_group:
                    if isinstance(edit, dict) and isinstance(edit.get("text"), str) and edit["text"]:
                        edit_texts.append(edit["text"])
        return "\n".join(edit_texts)

    @staticmethod
    def _extract_inline_reference_name(reference: Any) -> str:
        if not isinstance(reference, dict):
            return ""
        name = reference.get("name")
        if isinstance(name, str) and name:
            return name
        path = reference.get("fsPath") or reference.get("path")
        if isinstance(path, str) and path:
            return Path(path).name
        location = as_dict(reference.get("location"))
        uri = as_dict(location.get("uri"))
        path = uri.get("fsPath") or uri.get("path")
        if isinstance(path, str) and path:
            return Path(path).name
        return ""

    @staticmethod
    def _extract_request_attachments(request: dict[str, Any]) -> list[AttachmentRef]:
        attachments: list[AttachmentRef] = []
        variables = as_list(as_dict(request.get("variableData")).get("variables"))
        if not isinstance(variables, list):
            return attachments
        for item in variables:
            if not isinstance(item, dict):
                continue
            item_dict = as_dict(item)
            kind = str(item_dict.get("kind") or "variable")
            value = as_dict(item_dict.get("value"))
            path = value.get("path") if isinstance(value.get("path"), str) else ""
            attachments.append(
                AttachmentRef(
                    kind=kind,
                    path=normalize_path(path) if path else "",
                    title=str(item_dict.get("name") or ""),
                    raw=item_dict,
                )
            )
        return attachments

    @staticmethod
    def _extract_request_file_paths(request: dict[str, Any]) -> list[str]:
        return sorted({attachment.path for attachment in CopilotLowLevelParser._extract_request_attachments(request) if attachment.path})

    @staticmethod
    def _extract_response_file_paths(blocks: list[ContentBlockRecord]) -> set[str]:
        paths: set[str] = set()
        for block in blocks:
            tool_call = block.extra.get("tool_call")
            if isinstance(tool_call, dict):
                for path in tool_call.get("file_paths", []):
                    if isinstance(path, str) and path:
                        paths.add(path)
            if block.kind == "textEditGroup":
                uri = as_dict(block.raw.get("uri"))
                path = uri.get("path") or uri.get("fsPath")
                if isinstance(path, str) and path:
                    paths.add(normalize_path(path))
            if block.kind == "codeblockUri":
                uri = as_dict(block.raw.get("codeblockUri"))
                path = uri.get("path") or uri.get("fsPath")
                if isinstance(path, str) and path:
                    paths.add(normalize_path(path))
        return paths

    @staticmethod
    def _extract_edited_file_paths(turn: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for event in as_list(turn.get("editedFileEvents")):
            if not isinstance(event, dict):
                continue
            uri = as_dict(event.get("uri"))
            path = uri.get("path") or uri.get("fsPath")
            if isinstance(path, str) and path:
                paths.add(normalize_path(path))
        return paths

    @staticmethod
    def _extract_thinking_text(blocks: list[ContentBlockRecord]) -> str:
        return "\n\n".join(block.text for block in blocks if block.kind == "thinking" and block.text)

    @staticmethod
    def _render_blocks_text(blocks: list[ContentBlockRecord]) -> str:
        visible_blocks = [
            block.text
            for block in blocks
            if block.text and block.kind not in {"codeblockUri"}
        ]
        return "".join(visible_blocks)

    @staticmethod
    def _extract_response_time(turn: dict[str, Any]) -> int | None:
        result = as_dict(turn.get("result"))
        timings = as_dict(result.get("timings"))
        total_elapsed = timings.get("totalElapsed")
        if isinstance(total_elapsed, (int, float)):
            return int(total_elapsed)
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

    @staticmethod
    def _parse_jsonl_session(path: Path) -> dict[str, Any]:
        lines: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    parsed = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    lines.append(parsed)
        if not lines:
            return {"requests": []}
        if "kind" in lines[0]:
            return CopilotLowLevelParser._reconstruct_delta_session(lines)
        session_meta: dict[str, Any] = {}
        requests: list[dict[str, Any]] = []
        for index, item in enumerate(lines):
            if index == 0 and "version" in item and "requestId" not in item:
                session_meta = item
            else:
                requests.append(item)
        return {**session_meta, "requests": requests}

    @staticmethod
    def _reconstruct_delta_session(lines: list[dict[str, Any]]) -> dict[str, Any]:
        state: dict[str, Any] = lines[0].get("v", {}) if lines[0].get("kind") == 0 else {}
        for operation in lines[1:]:
            kind = operation.get("kind")
            if kind not in (1, 2):
                continue
            key_path = operation.get("k", [])
            value = operation.get("v")
            if not key_path:
                continue
            CopilotLowLevelParser._apply_delta(state, key_path, value, kind)
        if "requests" not in state:
            state["requests"] = []
        return state

    @staticmethod
    def _apply_delta(state: dict[str, Any], key_path: list[Any], value: Any, kind: int) -> None:
        container: Any = state
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
                return

        final_key = key_path[-1]
        if kind == 1:
            if isinstance(final_key, int) and isinstance(container, list):
                while len(container) <= final_key:
                    container.append({})
                container[final_key] = value
            elif isinstance(container, dict):
                container[final_key] = value
        elif kind == 2:
            if isinstance(final_key, int) and isinstance(container, list):
                while len(container) <= final_key:
                    container.append([])
                target = container[final_key]
                if isinstance(target, list) and isinstance(value, list):
                    target.extend(value)
            elif isinstance(container, dict):
                if final_key not in container:
                    container[final_key] = []
                target = container[final_key]
                if isinstance(target, list) and isinstance(value, list):
                    target.extend(value)
