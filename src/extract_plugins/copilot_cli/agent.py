"""GitHub Copilot CLI workspace extractor – adapter to the agent framework.

Adapts :class:`~src.extract_plugins.copilot_cli.extractor.CopilotCliExtractor`
to the :class:`~src.extract_plugins.agent_extractor.AgentExtractor` interface.

Session data is read from ``~/.copilot/session-state/`` (auto-detected) or
from a path supplied via ``extract.copilot_cli.session_state_dir`` in
``config.yaml``.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from src.shared.logging.logger import get_logger
from src.extract_plugins.agent_extractor import AgentExtractor
from src.shared.models.workspace import WorkspaceInfo, WorkspaceActivity, ExtractedWorkspace

from .extractor import CopilotCliExtractor as _Impl

logger = get_logger(__name__)


class Copilot_CliExtractor(AgentExtractor):
    """Agent-framework adapter for GitHub Copilot CLI session extraction.

    The Copilot CLI stores chat sessions as event-driven JSONL files under
    ``~/.copilot/session-state/``.  Each line in a file is a JSON event with
    a ``type`` field (``session.start``, ``user.message``, ``assistant.message``,
    ``session.model_change``, ``tool.execution_complete``, …).
    """

    AGENT_NAME = "copilot_cli"

    def __init__(self, workspace_id: str) -> None:
        super().__init__(workspace_id)
        session_state_dir = self._resolve_session_state_dir()
        self._impl = _Impl(workspace_id=workspace_id, session_state_dir=session_state_dir)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, workspace_id: str, **kwargs: object) -> "Copilot_CliExtractor":
        """Factory method required by the agent framework."""
        return cls(workspace_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_session_state_dir(self) -> Optional[Path]:
        """Return configured path override, or ``None`` to use the default."""
        if self.config:
            raw = self.config.get("session_state_dir") or self.config.get("workspace_storage")
            if raw:
                return Path(raw)
        return None

    # ------------------------------------------------------------------
    # AgentExtractor interface
    # ------------------------------------------------------------------

    def scan_workspaces(self) -> List[WorkspaceInfo]:
        """Scan the session-state directory and return discovered workspaces."""
        return self._impl.scan_workspaces()

    def extract_sessions(self) -> ExtractedWorkspace:
        """Extract all turns for this workspace."""
        return self._impl.extract_sessions()

    def get_latest_activity(self) -> Optional[WorkspaceActivity]:
        """Return quick session/turn stats without full parsing."""
        return self._impl.get_latest_activity()

    def cleanup(self) -> None:
        """No persistent resources to release."""
        self._impl.cleanup()
