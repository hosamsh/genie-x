"""Base interfaces for low-level parsers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ParsedWorkspace, WorkspaceDescriptor


class LowLevelWorkspaceParser(ABC):
    """Parser interface for raw workspace extraction."""

    AGENT_NAME: str

    @abstractmethod
    def scan_workspaces(self) -> list[WorkspaceDescriptor]:
        """Return discoverable workspaces for the parser's agent."""

    @abstractmethod
    def parse_workspace(self, workspace_id: str) -> ParsedWorkspace:
        """Parse a workspace into a source-of-truth low-level representation."""

    def get_workspace_activity(self, descriptor: WorkspaceDescriptor):
        """Return visible workspace activity for discovery, or None to use generic parsing."""
        return None
