"""Typed source-of-truth models for low-level parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.shared.models.dataclass_mixin import DataclassIO


@dataclass
class WorkspaceDescriptor(DataclassIO):
    """Minimal metadata needed to identify and load a workspace."""

    workspace_id: str
    agent_name: str
    workspace_name: str = ""
    workspace_folder: str = ""
    source_root: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParserIssue(DataclassIO):
    """Non-fatal parse issue."""

    level: str
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage(DataclassIO):
    """Provider token accounting preserved from source payloads."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None
    cache_creation_input_tokens: Optional[int] = None
    service_tier: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandEnvelope(DataclassIO):
    """Structured command trigger extracted from message text."""

    name: str
    arguments_text: str = ""
    normalized_text: str = ""
    raw_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttachmentRef(DataclassIO):
    """Attachment or external reference included in an event."""

    kind: str
    path: str = ""
    title: str = ""
    media_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallRecord(DataclassIO):
    """Structured tool invocation."""

    call_id: str = ""
    name: str = ""
    kind: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    arguments_text: str = ""
    file_paths: list[str] = field(default_factory=list)
    spawned_session_id: Optional[str] = None
    status: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultRecord(DataclassIO):
    """Structured tool result or completion record."""

    tool_call_id: str = ""
    kind: str = ""
    text: str = ""
    structured_content: dict[str, Any] = field(default_factory=dict)
    is_error: Optional[bool] = None
    status: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContentBlockRecord(DataclassIO):
    """Raw content block extracted from a provider event."""

    index: int
    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionLinkRecord(DataclassIO):
    """Cross-session link such as subagent or fork relationships."""

    target_session_id: str
    relationship_type: str
    trigger_event_index: Optional[int] = None
    trigger_tool_call_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionEventRecord(DataclassIO):
    """Single source event preserved with rich metadata."""

    index: int
    event_type: str
    role: str = ""
    timestamp_ms: Optional[int] = None
    timestamp_iso: str = ""
    message_id: str = ""
    request_id: str = ""
    model_id: str = ""
    text: str = ""
    thinking_text: str = ""
    token_usage: Optional[TokenUsage] = None
    command: Optional[CommandEnvelope] = None
    content_blocks: list[ContentBlockRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    tool_results: list[ToolResultRecord] = field(default_factory=list)
    attachments: list[AttachmentRef] = field(default_factory=list)
    file_paths: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedSession(DataclassIO):
    """Source-of-truth session representation."""

    session_id: str
    agent_name: str
    workspace_id: str
    workspace_name: str = ""
    workspace_folder: str = ""
    title: str = ""
    source_path: str = ""
    started_at_ms: Optional[int] = None
    ended_at_ms: Optional[int] = None
    parent_session_id: str = ""
    relationship_type: str = ""
    events: list[SessionEventRecord] = field(default_factory=list)
    links: list[SessionLinkRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    issues: list[ParserIssue] = field(default_factory=list)


@dataclass
class ParsedWorkspace(DataclassIO):
    """All parsed sessions for a workspace."""

    descriptor: WorkspaceDescriptor
    sessions: list[ParsedSession] = field(default_factory=list)
    issues: list[ParserIssue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
