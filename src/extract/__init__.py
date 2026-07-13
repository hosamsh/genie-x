"""Low-level extraction package.

This package is the source-of-truth parser layer for raw agent session data.
It preserves event-level metadata so downstream code can build higher-level
views without re-reading agent-native storage.
"""

from .base import LowLevelWorkspaceParser
from .claude_code import ClaudeCodeLowLevelParser
from .codex import CodexLowLevelParser
from .copilot import CopilotLowLevelParser
from .copilot_cli import CopilotCliLowLevelParser
from .models import (
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

__all__ = [
    "AttachmentRef",
    "ClaudeCodeLowLevelParser",
    "CodexLowLevelParser",
    "CommandEnvelope",
    "ContentBlockRecord",
    "CopilotCliLowLevelParser",
    "CopilotLowLevelParser",
    "LowLevelWorkspaceParser",
    "ParsedSession",
    "ParsedWorkspace",
    "ParserIssue",
    "SessionEventRecord",
    "SessionLinkRecord",
    "TokenUsage",
    "ToolCallRecord",
    "ToolResultRecord",
    "WorkspaceDescriptor",
]