"""Low-level Codex parser."""

from __future__ import annotations

import json
import platform
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from src.extract.base import LowLevelWorkspaceParser
from src.extract.models import (
    ContentBlockRecord,
    ParsedSession,
    ParsedWorkspace,
    ParserIssue,
    SessionEventRecord,
    SessionLinkRecord,
    ToolCallRecord,
    ToolResultRecord,
    WorkspaceDescriptor,
)
from src.extract.utils import as_dict, as_list, flatten_text_content, normalize_session_title, parse_timestamp_ms, timestamp_ms_to_iso
from src.shared.io.paths import normalize_path
from src.shared.logging.logger import get_logger
from src.shared.models.workspace import WorkspaceActivity

logger = get_logger(__name__)


class CodexLowLevelParser(LowLevelWorkspaceParser):
    """Source-of-truth parser for local Codex rollout files."""

    AGENT_NAME = "codex"

    def __init__(self, codex_home: Path | None = None, codex_homes: list[Path] | None = None) -> None:
        configured_roots = [Path(path) for path in (codex_homes or []) if path]
        if codex_home:
            configured_roots.insert(0, Path(codex_home))
        self._auto_discover_wsl = not configured_roots
        if not configured_roots:
            configured_roots = [Path.home() / ".codex"]
        self._configured_roots = configured_roots
        self._codex_home = configured_roots[0]
        self._sessions_root = self._codex_home / "sessions"
        self._session_index_path = self._codex_home / "session_index.jsonl"

    def scan_workspaces(self) -> list[WorkspaceDescriptor]:
        grouped: dict[str, WorkspaceDescriptor] = {}
        for codex_home in self._iter_codex_roots():
            sessions_root = codex_home / "sessions"
            thread_names = self._load_thread_names(codex_home / "session_index.jsonl")

            for rollout_file in self._scan_rollout_files(sessions_root):
                meta = self._peek_meta(rollout_file)
                workspace_folder = meta.get("workspace_folder") or ""
                session_id = meta.get("session_id") or rollout_file.stem
                workspace_id = self._make_workspace_id(workspace_folder or session_id)
                workspace_name = self._workspace_name_from_path(workspace_folder, workspace_id)
                session_title = thread_names.get(session_id) or meta.get("title") or ""

                if workspace_id not in grouped:
                    grouped[workspace_id] = WorkspaceDescriptor(
                        workspace_id=workspace_id,
                        agent_name=self.AGENT_NAME,
                        workspace_name=workspace_name,
                        workspace_folder=workspace_folder,
                        source_root=str(sessions_root),
                        metadata={
                            "session_files": [str(rollout_file)],
                            "session_count": 1,
                            "codex_home": str(codex_home),
                            "thread_titles": {session_id: session_title} if session_title else {},
                        },
                    )
                else:
                    descriptor = grouped[workspace_id]
                    descriptor.metadata.setdefault("session_files", []).append(str(rollout_file))
                    descriptor.metadata["session_count"] = int(descriptor.metadata.get("session_count", 0)) + 1
                    if session_title:
                        descriptor.metadata.setdefault("thread_titles", {})[session_id] = session_title

        return sorted(grouped.values(), key=lambda item: item.workspace_name.lower())

    def _iter_codex_roots(self) -> list[Path]:
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

        if self._auto_discover_wsl and platform.system() == "Windows":
            for root in self._iter_wsl_codex_roots():
                add_root(root)

        return roots

    def _iter_wsl_codex_roots(self) -> list[Path]:
        roots: list[Path] = []
        for distro in self._list_wsl_distros():
            home_root = Path(rf"\\wsl.localhost\{distro}\home")
            if not home_root.exists():
                continue
            try:
                for user_dir in home_root.iterdir():
                    codex_dir = user_dir / ".codex"
                    if (codex_dir / "sessions").exists():
                        roots.append(codex_dir)
            except OSError as exc:
                logger.warning("Failed to inspect WSL Codex root %s: %s", home_root, exc)
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
            logger.warning("Failed to list WSL distros for Codex discovery: %s", exc)
            return []

        if result.returncode != 0:
            return []

        stdout = result.stdout or b""
        if b"\x00" in stdout:
            decoded = stdout.decode("utf-16le", errors="ignore")
        else:
            decoded = stdout.decode("utf-8", errors="replace")

        return [line.strip() for line in decoded.splitlines() if line.strip()]

    def parse_workspace(self, workspace_id: str) -> ParsedWorkspace:
        descriptor = next((item for item in self.scan_workspaces() if item.workspace_id == workspace_id), None)
        if descriptor is None:
            descriptor = WorkspaceDescriptor(
                workspace_id=workspace_id,
                agent_name=self.AGENT_NAME,
                workspace_name=workspace_id,
                source_root=str(self._sessions_root),
            )

        sessions: list[ParsedSession] = []
        issues: list[ParserIssue] = []
        session_files = [Path(path) for path in descriptor.metadata.get("session_files", [])]
        thread_titles = as_dict(descriptor.metadata.get("thread_titles"))

        for rollout_file in session_files:
            parsed_session, session_issues = self._parse_session_file(rollout_file, descriptor, thread_titles)
            if parsed_session is not None:
                sessions.append(parsed_session)
            issues.extend(session_issues)

        sessions.sort(key=lambda item: (item.started_at_ms or 0, item.session_id))
        return ParsedWorkspace(
            descriptor=descriptor,
            sessions=sessions,
            issues=issues,
            metadata={"codex_home": str(self._codex_home)},
        )

    def get_workspace_activity(self, descriptor: WorkspaceDescriptor) -> WorkspaceActivity:
        visible_session_ids: list[str] = []
        thread_titles = as_dict(descriptor.metadata.get("thread_titles"))
        for rollout_file in [Path(path) for path in descriptor.metadata.get("session_files", [])]:
            parsed_session, _ = self._parse_session_file(rollout_file, descriptor, thread_titles)
            if parsed_session is not None:
                visible_session_ids.append(parsed_session.session_id)
        return WorkspaceActivity(
            session_count=len(visible_session_ids),
            turn_count=len(visible_session_ids),
            session_ids=visible_session_ids,
        )

    @staticmethod
    def _scan_rollout_files(sessions_root: Path) -> list[Path]:
        if not sessions_root.exists() or not sessions_root.is_dir():
            return []
        return sorted(path for path in sessions_root.rglob("rollout-*.jsonl") if path.is_file())

    def _load_thread_names(self, session_index_path: Path) -> dict[str, str]:
        names: dict[str, str] = {}
        if not session_index_path.exists():
            return names
        try:
            with open(session_index_path, "r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    thread_id = payload.get("id")
                    thread_name = payload.get("thread_name")
                    if isinstance(thread_id, str) and thread_id.strip() and isinstance(thread_name, str) and thread_name.strip():
                        names[thread_id.strip()] = thread_name.strip()
        except OSError:
            return names
        return names

    def _peek_meta(self, rollout_file: Path) -> dict[str, Any]:
        session_id = ""
        workspace_folder = ""
        title = ""
        try:
            with open(rollout_file, "r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle):
                    if line_number >= 64:
                        break
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        rollout_line = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rollout_line, dict):
                        continue
                    item_type = str(rollout_line.get("type") or "")
                    payload = as_dict(rollout_line.get("payload"))
                    if item_type == "session_meta":
                        session_id = str(payload.get("session_id") or payload.get("id") or session_id).strip()
                        workspace_folder = normalize_path(str(payload.get("cwd") or workspace_folder).strip())
                    elif item_type == "event_msg":
                        event_type = str(payload.get("type") or "")
                        if event_type == "user_message" and not title:
                            title = self._extract_rollout_preview(payload)
                    if session_id and workspace_folder and title:
                        break
        except OSError:
            pass
        return {
            "session_id": session_id,
            "workspace_folder": workspace_folder,
            "title": title,
        }

    def _parse_session_file(
        self,
        rollout_file: Path,
        descriptor: WorkspaceDescriptor,
        thread_titles: dict[str, Any],
    ) -> tuple[ParsedSession | None, list[ParserIssue]]:
        issues: list[ParserIssue] = []
        events: list[SessionEventRecord] = []
        links: list[SessionLinkRecord] = []
        session_id = ""
        title = ""
        workspace_folder = descriptor.workspace_folder
        metadata: dict[str, Any] = {
            "source": self.AGENT_NAME,
            "relative_path": self._relative_path_safe(rollout_file, self._sessions_root),
            "file_suffix": rollout_file.suffix.lower(),
        }

        pending_tool_calls: dict[str, ToolCallRecord] = {}
        pending_spawn_begins: dict[str, dict[str, Any]] = {}
        event_index = 0

        try:
            with open(rollout_file, "r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        rollout_line = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        issues.append(
                            ParserIssue(
                                level="warning",
                                code="invalid_codex_jsonl_line",
                                message=f"Invalid Codex JSONL in {rollout_file.name} line {line_number}: {exc}",
                            )
                        )
                        continue
                    if not isinstance(rollout_line, dict):
                        continue

                    item_type = str(rollout_line.get("type") or "")
                    payload = as_dict(rollout_line.get("payload"))
                    timestamp_ms = parse_timestamp_ms(rollout_line.get("timestamp"))
                    timestamp_iso = timestamp_ms_to_iso(timestamp_ms)

                    if item_type == "session_meta":
                        session_id = str(payload.get("session_id") or payload.get("id") or session_id).strip()
                        workspace_folder = normalize_path(str(payload.get("cwd") or workspace_folder or "").strip())
                        metadata["originator"] = payload.get("originator")
                        metadata["cli_version"] = payload.get("cli_version")
                        metadata["model_provider"] = payload.get("model_provider")
                        metadata["history_mode"] = payload.get("history_mode")
                        metadata["source_name"] = payload.get("source")
                        git_payload = as_dict(rollout_line.get("git"))
                        if git_payload:
                            metadata["git"] = git_payload
                        continue

                    if item_type != "event_msg":
                        continue

                    parsed_event, new_links, pending_tool_calls, pending_spawn_begins = self._parse_rollout_event(
                        payload,
                        event_index,
                        timestamp_ms,
                        timestamp_iso,
                        pending_tool_calls,
                        pending_spawn_begins,
                    )
                    if parsed_event is not None:
                        events.append(parsed_event)
                        event_index += 1
                    links.extend(new_links)
                    if parsed_event is not None and parsed_event.role == "user" and not title:
                        title = self._extract_rollout_preview(payload)
        except OSError as exc:
            issues.append(ParserIssue(level="error", code="read_failed", message=f"Cannot read {rollout_file}: {exc}"))
            return None, issues

        if not session_id:
            session_id = rollout_file.stem
        title = normalize_session_title(thread_titles.get(session_id) or title or self._derive_title(events, session_id), fallback=session_id)
        started_at_ms, ended_at_ms = self._bounds(events)

        if not self._parsed_events_have_meaningful_content(events):
            return None, issues

        session = ParsedSession(
            session_id=session_id,
            agent_name=self.AGENT_NAME,
            workspace_id=descriptor.workspace_id,
            workspace_name=descriptor.workspace_name,
            workspace_folder=workspace_folder,
            title=title,
            source_path=str(rollout_file),
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            events=events,
            links=links,
            metadata=metadata,
            issues=issues,
        )
        return session, issues

    @staticmethod
    def _parsed_events_have_meaningful_content(events: list[SessionEventRecord]) -> bool:
        for event in events:
            if event.role in {"user", "assistant"}:
                if (event.text or event.thinking_text).strip():
                    return True
                if event.tool_calls or event.tool_results or event.attachments or event.file_paths:
                    return True
                if any(block.text.strip() for block in event.content_blocks):
                    return True
        return False

    def _parse_rollout_event(
        self,
        payload: dict[str, Any],
        index: int,
        timestamp_ms: Optional[int],
        timestamp_iso: str,
        pending_tool_calls: dict[str, ToolCallRecord],
        pending_spawn_begins: dict[str, dict[str, Any]],
    ) -> tuple[SessionEventRecord | None, list[SessionLinkRecord], dict[str, ToolCallRecord], dict[str, dict[str, Any]]]:
        event_type = str(payload.get("type") or "")
        links: list[SessionLinkRecord] = []

        if event_type == "user_message":
            message = str(payload.get("message") or "")
            return (
                SessionEventRecord(
                    index=index,
                    event_type=event_type,
                    role="user",
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    message_id=str(payload.get("client_id") or ""),
                    text=message,
                    raw=payload,
                    extra={
                        "images": as_list(payload.get("images")),
                        "local_images": [str(item) for item in as_list(payload.get("local_images"))],
                    },
                ),
                links,
                pending_tool_calls,
                pending_spawn_begins,
            )

        if event_type == "agent_message":
            return (
                SessionEventRecord(
                    index=index,
                    event_type=event_type,
                    role="assistant",
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    text=str(payload.get("message") or ""),
                    raw=payload,
                    extra={"phase": payload.get("phase")},
                ),
                links,
                pending_tool_calls,
                pending_spawn_begins,
            )

        if event_type == "agent_reasoning":
            return (
                SessionEventRecord(
                    index=index,
                    event_type=event_type,
                    role="assistant",
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    thinking_text=str(payload.get("text") or ""),
                    raw=payload,
                ),
                links,
                pending_tool_calls,
                pending_spawn_begins,
            )

        if event_type in {"exec_command_begin", "mcp_tool_call_begin", "dynamic_tool_call_request"}:
            tool_call = self._build_tool_call(event_type, payload)
            pending_tool_calls[tool_call.call_id or f"{event_type}-{index}"] = tool_call
            return (
                SessionEventRecord(
                    index=index,
                    event_type=event_type,
                    role="assistant",
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    request_id=str(payload.get("turn_id") or payload.get("call_id") or ""),
                    text=self._summarize_tool_call(tool_call),
                    content_blocks=[
                        ContentBlockRecord(
                            index=0,
                            kind=tool_call.kind or event_type,
                            text=self._summarize_tool_call(tool_call),
                            raw=payload,
                            extra={"tool_call": tool_call.to_dict()},
                        )
                    ],
                    tool_calls=[tool_call],
                    file_paths=tool_call.file_paths,
                    raw=payload,
                ),
                links,
                pending_tool_calls,
                pending_spawn_begins,
            )

        if event_type in {"exec_command_end", "mcp_tool_call_end", "dynamic_tool_call_response"}:
            tool_result = self._build_tool_result(event_type, payload)
            tool_call = pending_tool_calls.get(tool_result.tool_call_id or "")
            text = tool_result.text
            file_paths = tool_call.file_paths if tool_call else []
            return (
                SessionEventRecord(
                    index=index,
                    event_type=event_type,
                    role="user",
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    request_id=str(payload.get("turn_id") or payload.get("call_id") or ""),
                    text=text,
                    content_blocks=[
                        ContentBlockRecord(
                            index=0,
                            kind=tool_result.kind or event_type,
                            text=text,
                            raw=payload,
                            extra={"tool_result": tool_result.to_dict()},
                        )
                    ],
                    tool_results=[tool_result],
                    file_paths=file_paths,
                    raw=payload,
                ),
                links,
                pending_tool_calls,
                pending_spawn_begins,
            )

        if event_type == "patch_apply_end":
            tool_call = self._build_patch_apply_tool_call(payload)
            tool_result = self._build_patch_apply_tool_result(payload)
            text = tool_result.text
            return (
                SessionEventRecord(
                    index=index,
                    event_type=event_type,
                    role="assistant",
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    request_id=str(payload.get("turn_id") or payload.get("call_id") or ""),
                    text=text,
                    content_blocks=[
                        ContentBlockRecord(
                            index=0,
                            kind=event_type,
                            text=text,
                            raw=payload,
                            extra={
                                "tool_call": tool_call.to_dict(),
                                "tool_result": tool_result.to_dict(),
                                "patch_apply": {
                                    "changes": as_dict(payload.get("changes")),
                                    "stdout": str(payload.get("stdout") or ""),
                                    "stderr": str(payload.get("stderr") or ""),
                                    "success": payload.get("success"),
                                },
                            },
                        )
                    ],
                    tool_calls=[tool_call],
                    tool_results=[tool_result],
                    file_paths=tool_call.file_paths,
                    raw=payload,
                ),
                links,
                pending_tool_calls,
                pending_spawn_begins,
            )

        if event_type == "collab_agent_spawn_begin":
            call_id = str(payload.get("call_id") or "")
            pending_spawn_begins[call_id] = payload
            return (
                SessionEventRecord(
                    index=index,
                    event_type=event_type,
                    role="assistant",
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    text=str(payload.get("prompt") or ""),
                    raw=payload,
                    extra={"collab_spawn_begin": payload},
                ),
                links,
                pending_tool_calls,
                pending_spawn_begins,
            )

        if event_type == "collab_agent_spawn_end":
            call_id = str(payload.get("call_id") or "")
            begin_payload = pending_spawn_begins.get(call_id, {})
            subagent_session_id = str(payload.get("new_thread_id") or "")
            tool_call = ToolCallRecord(
                call_id=call_id,
                name="Agent",
                kind="collab_agent_spawn",
                arguments={
                    "prompt": payload.get("prompt") or begin_payload.get("prompt") or "",
                    "model": payload.get("model") or begin_payload.get("model"),
                },
                arguments_text=json.dumps(
                    {
                        "prompt": payload.get("prompt") or begin_payload.get("prompt") or "",
                        "model": payload.get("model") or begin_payload.get("model"),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                spawned_session_id=subagent_session_id or None,
                status=str(payload.get("status") or "") or None,
                raw=payload,
            )
            if subagent_session_id:
                links.append(
                    SessionLinkRecord(
                        target_session_id=subagent_session_id,
                        relationship_type="subagent",
                        trigger_event_index=index,
                        trigger_tool_call_id=call_id,
                        extra={"tool_name": "Agent"},
                    )
                )
            return (
                SessionEventRecord(
                    index=index,
                    event_type=event_type,
                    role="assistant",
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    text=self._summarize_tool_call(tool_call),
                    content_blocks=[
                        ContentBlockRecord(
                            index=0,
                            kind="collab_agent_spawn",
                            text=self._summarize_tool_call(tool_call),
                            raw=payload,
                            extra={"tool_call": tool_call.to_dict()},
                        )
                    ],
                    tool_calls=[tool_call],
                    raw=payload,
                ),
                links,
                pending_tool_calls,
                pending_spawn_begins,
            )

        if event_type == "sub_agent_activity":
            links.append(
                SessionLinkRecord(
                    target_session_id=str(payload.get("agent_thread_id") or ""),
                    relationship_type="subagent",
                    trigger_event_index=index,
                    extra={"kind": payload.get("kind")},
                )
            )
            return (
                SessionEventRecord(
                    index=index,
                    event_type=event_type,
                    role="system",
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    text=f"Subagent activity: {payload.get('kind') or ''}".strip(),
                    raw=payload,
                ),
                links,
                pending_tool_calls,
                pending_spawn_begins,
            )

        return (
            SessionEventRecord(
                index=index,
                event_type=event_type,
                role="system",
                timestamp_ms=timestamp_ms,
                timestamp_iso=timestamp_iso,
                text=self._system_event_text(event_type, payload),
                raw=payload,
            ),
            links,
            pending_tool_calls,
            pending_spawn_begins,
        )

    @staticmethod
    def _build_tool_call(event_type: str, payload: dict[str, Any]) -> ToolCallRecord:
        if event_type == "exec_command_begin":
            command = as_list(payload.get("command"))
            arguments = {
                "command": [str(item) for item in command],
                "cwd": payload.get("cwd"),
                "parsed_cmd": payload.get("parsed_cmd"),
                "source": payload.get("source"),
            }
            arguments_text = json.dumps(arguments, ensure_ascii=True, sort_keys=True)
            file_paths = [normalize_path(str(payload.get("cwd")))] if payload.get("cwd") else []
            return ToolCallRecord(
                call_id=str(payload.get("call_id") or ""),
                name="Bash",
                kind=event_type,
                arguments=arguments,
                arguments_text=arguments_text,
                file_paths=file_paths,
                raw=payload,
            )

        if event_type == "mcp_tool_call_begin":
            invocation = as_dict(payload.get("invocation"))
            server = str(invocation.get("server") or "")
            tool = str(invocation.get("tool") or "")
            name = f"{server}.{tool}".strip(".") or "MCP"
            arguments = invocation.get("arguments") if isinstance(invocation.get("arguments"), dict) else as_dict(invocation.get("arguments"))
            arguments_text = json.dumps(arguments, ensure_ascii=True, sort_keys=True) if arguments else ""
            return ToolCallRecord(
                call_id=str(payload.get("call_id") or ""),
                name=name,
                kind=event_type,
                arguments=arguments,
                arguments_text=arguments_text,
                raw=payload,
            )

        arguments = {
            "turn_id": payload.get("turn_id"),
            "namespace": payload.get("namespace"),
        }
        return ToolCallRecord(
            call_id=str(payload.get("call_id") or ""),
            name=str(payload.get("name") or payload.get("tool") or "DynamicTool"),
            kind=event_type,
            arguments=arguments,
            arguments_text=json.dumps(arguments, ensure_ascii=True, sort_keys=True),
            raw=payload,
        )

    @staticmethod
    def _build_tool_result(event_type: str, payload: dict[str, Any]) -> ToolResultRecord:
        if event_type == "exec_command_end":
            stdout = str(payload.get("stdout") or "")
            stderr = str(payload.get("stderr") or "")
            formatted_output = str(payload.get("formatted_output") or "")
            text = formatted_output or stdout or stderr or "(Command completed with no output)"
            exit_code = payload.get("exit_code")
            status = str(payload.get("status") or "") or ("success" if exit_code == 0 else "error")
            return ToolResultRecord(
                tool_call_id=str(payload.get("call_id") or ""),
                kind=event_type,
                text=text,
                structured_content={
                    "stdout": stdout,
                    "stderr": stderr,
                    "formatted_output": formatted_output,
                    "exit_code": exit_code,
                    "duration": payload.get("duration"),
                },
                is_error=(status == "error"),
                status=status,
                raw=payload,
            )

        if event_type == "mcp_tool_call_end":
            result = payload.get("result")
            text = flatten_text_content(result) or json.dumps(result, ensure_ascii=True) if result is not None else ""
            if not text:
                text = "(MCP tool completed with no output)"
            is_error = isinstance(result, str)
            return ToolResultRecord(
                tool_call_id=str(payload.get("call_id") or ""),
                kind=event_type,
                text=text,
                structured_content=as_dict(result),
                is_error=is_error,
                status="error" if is_error else "success",
                raw=payload,
            )

        result = payload.get("result")
        text = flatten_text_content(result) or (json.dumps(result, ensure_ascii=True) if result is not None else "")
        if not text:
            text = "(Tool completed with no output)"
        return ToolResultRecord(
            tool_call_id=str(payload.get("call_id") or ""),
            kind=event_type,
            text=text,
            structured_content=as_dict(result),
            status="success",
            raw=payload,
        )

    @staticmethod
    def _build_patch_apply_tool_call(payload: dict[str, Any]) -> ToolCallRecord:
        changes = as_dict(payload.get("changes"))
        file_paths = [normalize_path(path) for path in changes.keys() if isinstance(path, str) and path]
        arguments = {
            "change_count": len(file_paths),
            "file_paths": file_paths,
            "success": bool(payload.get("success")),
        }
        return ToolCallRecord(
            call_id=str(payload.get("call_id") or ""),
            name="ApplyPatch",
            kind="patch_apply_end",
            arguments=arguments,
            arguments_text=json.dumps(arguments, ensure_ascii=True, sort_keys=True),
            file_paths=file_paths,
            status="success" if payload.get("success") else "error",
            raw=payload,
        )

    @staticmethod
    def _build_patch_apply_tool_result(payload: dict[str, Any]) -> ToolResultRecord:
        stdout = str(payload.get("stdout") or "")
        stderr = str(payload.get("stderr") or "")
        success = bool(payload.get("success"))
        text = stdout or stderr or "(Patch applied with no output)"
        return ToolResultRecord(
            tool_call_id=str(payload.get("call_id") or ""),
            kind="patch_apply_end",
            text=text,
            structured_content={
                "stdout": stdout,
                "stderr": stderr,
                "success": success,
                "changes": as_dict(payload.get("changes")),
                "status": payload.get("status"),
            },
            is_error=not success,
            status="success" if success else "error",
            raw=payload,
        )

    @staticmethod
    def _summarize_tool_call(tool_call: ToolCallRecord) -> str:
        if tool_call.arguments_text:
            return f"{tool_call.name}({tool_call.arguments_text})"
        return tool_call.name

    @staticmethod
    def _system_event_text(event_type: str, payload: dict[str, Any]) -> str:
        if event_type == "turn_diff":
            return "Turn diff"
        if event_type == "thread_goal_updated":
            goal = as_dict(payload.get("goal"))
            objective = str(goal.get("objective") or "").strip()
            return objective or "Thread goal updated"
        if event_type == "turn_started":
            return "Turn started"
        if event_type == "turn_complete":
            return "Turn complete"
        if event_type == "session_configured":
            return "Session configured"
        return event_type.replace("_", " ").strip()

    @staticmethod
    def _extract_rollout_preview(payload: dict[str, Any]) -> str:
        event_type = str(payload.get("type") or "")
        if event_type == "user_message":
            return str(payload.get("message") or "").strip()
        if event_type == "thread_goal_updated":
            goal = as_dict(payload.get("goal"))
            return str(goal.get("objective") or "").strip()
        return ""

    @staticmethod
    def _workspace_name_from_path(workspace_folder: str, fallback: str) -> str:
        normalized = normalize_path(workspace_folder).rstrip("/")
        if not normalized:
            return fallback
        return Path(normalized).name or fallback

    @staticmethod
    def _make_workspace_id(workspace_folder: str) -> str:
        normalized = normalize_path(workspace_folder).rstrip("/")
        if not normalized:
            return "codex"
        return re.sub(r"[:/\\.]", "-", normalized)

    @staticmethod
    def _relative_path_safe(path: Path, root: Path) -> str:
        try:
            return str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            return path.name

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
