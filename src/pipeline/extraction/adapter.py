"""Convert generic parsed workspaces into Genie-X extraction models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.extract.models import (
    ParsedSession,
    ParsedWorkspace,
    SessionEventRecord,
    ToolCallRecord,
    ToolResultRecord,
)
from src.extract.utils import as_dict
from src.extract.storage import load_parsed_workspace
from src.shared.models.turn import CodeEdit, Turn
from src.shared.models.workspace import ExtractedWorkspace


_NON_CONVERSATION_COMMANDS = {"clear", "effort"}


@dataclass
class _TurnAccumulator:
    role: str
    event_indices: list[int] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    thinking_texts: list[str] = field(default_factory=list)
    timestamp_ms: Optional[int] = None
    timestamp_iso: str = ""
    request_ids: list[str] = field(default_factory=list)
    model_ids: list[str] = field(default_factory=list)
    file_paths: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    tool_results: list[ToolResultRecord] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    code_edits: list[CodeEdit] = field(default_factory=list)
    source_input_tokens: int = 0
    source_output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    service_tiers: list[str] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    extras: list[dict[str, Any]] = field(default_factory=list)


def adapt_parsed_workspace(parsed_workspace: ParsedWorkspace) -> ExtractedWorkspace:
    """Adapt a generic parsed workspace to Genie-X extraction models."""
    turns: list[Turn] = []
    visible_session_count = 0

    for session in parsed_workspace.sessions:
        session_turns = _adapt_session(session, parsed_workspace)
        if session_turns:
            visible_session_count += 1
            turns.extend(session_turns)

    turns.sort(key=lambda turn: (turn.timestamp_ms or 0, turn.session_id, turn.turn))
    return ExtractedWorkspace(
        turns=turns,
        session_count=visible_session_count,
        agent_name=parsed_workspace.descriptor.agent_name,
        workspace_id=parsed_workspace.descriptor.workspace_id,
        code_metrics=[],
    )


def load_and_adapt_workspace(
    db_path: Path,
    workspace_id: str,
    agent_name: str,
) -> ExtractedWorkspace:
    """Load a persisted parsed workspace and adapt it to Genie-X models."""
    parsed_workspace = load_parsed_workspace(db_path, workspace_id, agent_name)
    if parsed_workspace is None:
        return ExtractedWorkspace(
            turns=[],
            session_count=0,
            agent_name=agent_name,
            workspace_id=workspace_id,
            code_metrics=[],
        )
    return adapt_parsed_workspace(parsed_workspace)


def _adapt_session(session: ParsedSession, parsed_workspace: ParsedWorkspace) -> list[Turn]:
    conversation_events: list[SessionEventRecord] = []
    preamble_active = False
    seen_meaningful_user = False

    for event in session.events:
        if event.role not in {"user", "assistant"}:
            if _is_non_conversation_tool_result_event(event) and seen_meaningful_user and conversation_events and conversation_events[-1].role == "assistant":
                conversation_events.append(event)
            continue

        if event.role == "user":
            if _is_local_command_caveat_event(event):
                preamble_active = True
                continue
            if _is_local_command_stdout_event(event):
                if preamble_active:
                    continue
                if not _should_include_event(event):
                    continue
            if _is_non_conversation_command_event(event):
                if preamble_active:
                    continue
                continue
            if _is_pure_tool_result_user_event(event):
                if seen_meaningful_user and conversation_events and conversation_events[-1].role == "assistant":
                    conversation_events.append(event)
                continue
            if not _should_include_event(event):
                continue

            preamble_active = False
            seen_meaningful_user = True
            conversation_events.append(event)
            continue

        if not seen_meaningful_user:
            continue
        conversation_events.append(event)

    if not seen_meaningful_user or not conversation_events:
        return []

    turn_accumulators: list[_TurnAccumulator] = []
    current: Optional[_TurnAccumulator] = None

    for event in conversation_events:
        if current is not None and current.role == "assistant" and _is_assistant_tool_result_followup_event(event):
            _accumulate_tool_result_followup(current, event)
            continue
        if current is None or current.role != event.role:
            if current is not None:
                turn_accumulators.append(current)
            current = _TurnAccumulator(role=event.role)
        _accumulate_event(current, event)

    if current is not None:
        turn_accumulators.append(current)

    session_links = [link.to_dict() for link in session.links]
    session_metadata = dict(session.metadata)
    session_issues = [issue.to_dict() for issue in session.issues]
    workspace_issues = [issue.to_dict() for issue in parsed_workspace.issues]

    turns: list[Turn] = []
    for turn_index, accumulator in enumerate(turn_accumulators):
        text = "\n\n".join(part for part in accumulator.texts if part).strip()
        thinking_text = "\n\n".join(part for part in accumulator.thinking_texts if part).strip()
        merged_request_ids = _unique_preserve_order(accumulator.request_ids)
        request_id = merged_request_ids[0] if merged_request_ids else ""
        model_id = next((value for value in accumulator.model_ids if value), "")
        file_paths = sorted(_unique_preserve_order(accumulator.file_paths))
        tool_names = sorted(_unique_preserve_order(accumulator.tool_names))

        extra: dict[str, Any] = {
            "source_event_indices": list(accumulator.event_indices),
            "source_event_types": list(accumulator.event_types),
            "source_raw_events": list(accumulator.raw_events),
            "source_session_links": session_links,
            "source_session_metadata": session_metadata,
            "source_session_issues": session_issues,
            "source_workspace_issues": workspace_issues,
            "content_blocks": list(accumulator.content_blocks),
            "tool_calls": [tool_call.to_dict() for tool_call in accumulator.tool_calls],
            "tool_results": [tool_result.to_dict() for tool_result in accumulator.tool_results],
            "attachments": list(accumulator.attachments),
        }

        token_usage = _build_token_usage_summary(accumulator)
        if token_usage:
            extra["token_usage"] = token_usage
        if accumulator.source_input_tokens:
            extra["source_input_tokens"] = accumulator.source_input_tokens
        if accumulator.source_output_tokens:
            extra["source_output_tokens"] = accumulator.source_output_tokens

        spawned_session_ids = _unique_preserve_order(
            [tool_call.spawned_session_id for tool_call in accumulator.tool_calls if tool_call.spawned_session_id]
        )
        if spawned_session_ids:
            extra["subagent_session_ids"] = spawned_session_ids
            if len(spawned_session_ids) == 1:
                extra["subagent_session_id"] = spawned_session_ids[0]

        for extra_payload in accumulator.extras:
            for key, value in extra_payload.items():
                if key not in extra:
                    extra[key] = value

        turn = Turn(
            session_id=session.session_id,
            turn=turn_index,
            role=accumulator.role,
            original_text=text,
            thinking_text=thinking_text,
            workspace_id=parsed_workspace.descriptor.workspace_id,
            workspace_name=parsed_workspace.descriptor.workspace_name,
            workspace_folder=parsed_workspace.descriptor.workspace_folder,
            session_name=session.title or session.session_id,
            agent_used=parsed_workspace.descriptor.agent_name,
            model_id=model_id if accumulator.role == "assistant" else model_id,
            request_id=request_id,
            merged_request_ids=merged_request_ids,
            timestamp_ms=accumulator.timestamp_ms,
            timestamp_iso=accumulator.timestamp_iso,
            ts=str(accumulator.timestamp_ms) if accumulator.timestamp_ms is not None else "",
            files=file_paths,
            tools=tool_names,
            code_edits=list(accumulator.code_edits),
            parent_session_id=session.parent_session_id or None,
            relationship_type=session.relationship_type or None,
            extra=extra,
        )
        turns.append(turn)

    return turns


def _accumulate_event(accumulator: _TurnAccumulator, event: SessionEventRecord) -> None:
    accumulator.event_indices.append(event.index)
    accumulator.event_types.append(event.event_type)
    display_text = _event_display_text(event)
    if display_text:
        accumulator.texts.append(display_text)
    if event.thinking_text:
        accumulator.thinking_texts.append(event.thinking_text)
    if event.timestamp_ms is not None and (accumulator.timestamp_ms is None or event.timestamp_ms < accumulator.timestamp_ms):
        accumulator.timestamp_ms = event.timestamp_ms
        accumulator.timestamp_iso = event.timestamp_iso
    if event.request_id:
        accumulator.request_ids.append(event.request_id)
    if event.model_id:
        accumulator.model_ids.append(event.model_id)
    accumulator.file_paths.extend(event.file_paths)
    accumulator.tool_calls.extend(event.tool_calls)
    accumulator.tool_results.extend(event.tool_results)
    accumulator.tool_names.extend(tool_call.name for tool_call in event.tool_calls if tool_call.name)
    accumulator.attachments.extend([attachment.to_dict() for attachment in event.attachments])
    accumulator.content_blocks.extend([content_block.to_dict() for content_block in event.content_blocks])
    accumulator.raw_events.append(event.raw)
    accumulator.extras.append(dict(event.extra) if event.extra else {})

    token_usage = event.token_usage
    if token_usage is not None:
        accumulator.source_input_tokens += int(token_usage.input_tokens or 0)
        accumulator.source_output_tokens += int(token_usage.output_tokens or 0)
        accumulator.cache_read_input_tokens += int(token_usage.cache_read_input_tokens or 0)
        accumulator.cache_creation_input_tokens += int(token_usage.cache_creation_input_tokens or 0)
        if token_usage.service_tier:
            accumulator.service_tiers.append(token_usage.service_tier)

    accumulator.code_edits.extend(_derive_code_edits(event))


def _accumulate_tool_result_followup(accumulator: _TurnAccumulator, event: SessionEventRecord) -> None:
    accumulator.event_indices.append(event.index)
    accumulator.event_types.append(event.event_type)
    if event.request_id:
        accumulator.request_ids.append(event.request_id)
    if event.model_id:
        accumulator.model_ids.append(event.model_id)
    accumulator.file_paths.extend(event.file_paths)
    accumulator.tool_results.extend(event.tool_results)
    accumulator.attachments.extend([attachment.to_dict() for attachment in event.attachments])
    accumulator.raw_events.append(event.raw)
    accumulator.extras.append(dict(event.extra) if event.extra else {})

    token_usage = event.token_usage
    if token_usage is not None:
        accumulator.source_input_tokens += int(token_usage.input_tokens or 0)
        accumulator.source_output_tokens += int(token_usage.output_tokens or 0)
        accumulator.cache_read_input_tokens += int(token_usage.cache_read_input_tokens or 0)
        accumulator.cache_creation_input_tokens += int(token_usage.cache_creation_input_tokens or 0)
        if token_usage.service_tier:
            accumulator.service_tiers.append(token_usage.service_tier)


def _should_include_event(event: SessionEventRecord) -> bool:
    if event.role != "user":
        return True
    if not event.content_blocks:
        return True
    if _is_pure_tool_result_user_event(event):
        return False
    return True


def _is_local_command_caveat_event(event: SessionEventRecord) -> bool:
    text = (event.text or "").strip().lower()
    return event.extra.get("is_meta") is True and "<local-command-caveat>" in text


def _is_local_command_stdout_event(event: SessionEventRecord) -> bool:
    text = (event.text or "").strip().lower()
    return text.startswith("<local-command-stdout>") or text.startswith("<local-command-stderr>")


def _is_non_conversation_command_event(event: SessionEventRecord) -> bool:
    if event.command is None:
        return False
    command_name = (event.command.name or "").strip().lower().lstrip("/")
    return command_name in _NON_CONVERSATION_COMMANDS


def _is_pure_tool_result_user_event(event: SessionEventRecord) -> bool:
    if event.role != "user":
        return False
    if not event.content_blocks:
        return False
    block_kinds = {block.kind for block in event.content_blocks}
    return bool(block_kinds) and block_kinds.issubset(
        {
            "tool_result",
            "web_search_tool_result",
            "web_fetch_tool_result",
            "code_execution_tool_result",
            "bash_code_execution_tool_result",
            "text_editor_code_execution_tool_result",
            "tool_search_tool_result",
            "mcp_tool_result",
        }
    )


def _is_non_conversation_tool_result_event(event: SessionEventRecord) -> bool:
    if event.role in {"user", "assistant"}:
        return False
    return bool(event.tool_results)


def _is_assistant_tool_result_followup_event(event: SessionEventRecord) -> bool:
    return _is_pure_tool_result_user_event(event) or _is_non_conversation_tool_result_event(event)


def _event_display_text(event: SessionEventRecord) -> str:
    if event.role == "user" and event.command and event.command.normalized_text:
        return event.command.normalized_text
    if event.role == "user":
        return event.text
    if event.content_blocks:
        parts: list[str] = []
        for block in event.content_blocks:
            if block.kind == "thinking":
                continue
            if block.kind == "codeblockUri":
                continue
            tool_call_payload = block.extra.get("tool_call") if isinstance(block.extra, dict) else None
            if isinstance(tool_call_payload, dict):
                raw_value = block.raw.get("value") if isinstance(block.raw, dict) else None
                if isinstance(raw_value, str) and raw_value.strip():
                    parts.append(raw_value)
                continue
            if block.text:
                parts.append(block.text)
        if parts:
            return "".join(parts) if event.role == "assistant" else "\n\n".join(parts)
    return event.text


def _derive_code_edits(event: SessionEventRecord) -> list[CodeEdit]:
    code_edits: list[CodeEdit] = []

    for tool_call in event.tool_calls:
        normalized_name = tool_call.name.lower()
        if normalized_name == "write":
            file_path = _first_path(tool_call.file_paths, tool_call.arguments)
            if not file_path:
                continue
            code_edits.append(
                CodeEdit(
                    file_path=file_path,
                    language=_detect_language(file_path),
                    code_after=_string_arg(tool_call.arguments, "content"),
                    extra={
                        "tool": tool_call.name,
                        "tool_call": tool_call.to_dict(),
                    },
                )
            )
        elif normalized_name == "edit":
            file_path = _first_path(tool_call.file_paths, tool_call.arguments)
            if not file_path:
                continue
            old_string = _string_arg(tool_call.arguments, "old_string")
            new_string = _string_arg(tool_call.arguments, "new_string")
            code_edits.append(
                CodeEdit(
                    file_path=file_path,
                    language=_detect_language(file_path),
                    code_before=old_string,
                    code_after=new_string,
                    diff=_simple_diff(old_string, new_string),
                    extra={
                        "tool": tool_call.name,
                        "tool_call": tool_call.to_dict(),
                    },
                )
            )
        elif normalized_name == "applypatch":
            changes = as_dict(tool_call.raw.get("changes"))
            for raw_path, change_payload in changes.items():
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                change_dict = as_dict(change_payload)
                unified_diff = str(change_dict.get("unified_diff") or "")
                file_path = _normalize_path(raw_path)
                lines_added, lines_removed = _count_unified_diff_lines(unified_diff)
                code_edits.append(
                    CodeEdit(
                        file_path=file_path,
                        language=_detect_language(file_path),
                        diff=unified_diff or None,
                        extra={
                            "tool": tool_call.name,
                            "tool_call": tool_call.to_dict(),
                            "patch_change": change_dict,
                            "delta_metrics": {
                                "nloc": lines_added - lines_removed,
                                "lines_added": lines_added,
                                "lines_removed": lines_removed,
                                "cyclomatic_complexity": 0,
                                "token_count": 0,
                            },
                        },
                    )
                )

    for block in event.content_blocks:
        if block.kind != "textEditGroup":
            continue
        text_edit_group = block.extra.get("text_edit_group") if isinstance(block.extra, dict) else None
        if not isinstance(text_edit_group, dict):
            continue
        uri = text_edit_group.get("uri") if isinstance(text_edit_group.get("uri"), dict) else {}
        path = uri.get("path") or uri.get("fsPath")
        if not isinstance(path, str) or not path:
            continue
        file_path = _normalize_path(path)
        before_text = text_edit_group.get("before_text")
        after_text = text_edit_group.get("after_text")
        code_before = before_text if isinstance(before_text, str) else None
        code_after = after_text if isinstance(after_text, str) else block.text
        code_edits.append(
            CodeEdit(
                file_path=file_path,
                language=_detect_language(file_path),
                code_before=code_before,
                code_after=code_after,
                diff=_simple_diff(code_before or "", code_after) if code_after else None,
                extra={
                    "source_block": block.to_dict(),
                    "text_edit_group": text_edit_group,
                },
            )
        )

    return code_edits


def _build_token_usage_summary(accumulator: _TurnAccumulator) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if accumulator.source_input_tokens:
        summary["input_tokens"] = accumulator.source_input_tokens
    if accumulator.source_output_tokens:
        summary["output_tokens"] = accumulator.source_output_tokens
    if accumulator.cache_read_input_tokens:
        summary["cache_read_input_tokens"] = accumulator.cache_read_input_tokens
    if accumulator.cache_creation_input_tokens:
        summary["cache_creation_input_tokens"] = accumulator.cache_creation_input_tokens
    if accumulator.service_tiers:
        summary["service_tiers"] = _unique_preserve_order(accumulator.service_tiers)
    return summary


def _first_path(file_paths: list[str], arguments: dict[str, Any]) -> str:
    if file_paths:
        return file_paths[0]
    for key in ("file_path", "path", "file"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return _normalize_path(value)
    return ""


def _string_arg(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    return value if isinstance(value, str) else ""


def _count_unified_diff_lines(diff_text: str) -> tuple[int, int]:
    if not diff_text:
        return 0, 0

    lines_added = 0
    lines_removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            lines_added += 1
        elif line.startswith("-"):
            lines_removed += 1
    return lines_added, lines_removed


def _simple_diff(before: str, after: str) -> str:
    return f"--- old\n+++ new\n@@ @@\n-{before}\n+{after}"


def _detect_language(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    language_map = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescriptreact",
        ".jsx": "javascriptreact",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".md": "markdown",
        ".html": "html",
        ".css": "css",
        ".scss": "scss",
        ".sql": "sql",
        ".sh": "shellscript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".cs": "csharp",
        ".rb": "ruby",
    }
    return language_map.get(suffix, "text")


def _normalize_path(path: str) -> str:
    if not path:
        return ""
    normalized = path.replace("\\", "/")
    # Strip the leading slash before a Windows drive letter. VS Code file URIs
    # yield paths like "/c:/code/project"; we want "c:/code/project".
    if len(normalized) >= 3 and normalized[0] == "/" and normalized[2] == ":":
        normalized = normalized[1:]
    return normalized.lower()


def _unique_preserve_order(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result