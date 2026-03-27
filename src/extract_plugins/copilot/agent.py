"""Copilot workspace extractor - adapts consolidated copilot implementation to agent framework."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from src.extract_plugins.agent_extractor import (
    AgentExtractor,
    ExtractedWorkspace,
    WorkspaceActivity,
)
from src.shared.models.workspace import WorkspaceInfo

# Import the consolidated implementation
from src.shared.io.paths import is_valid_session_id

from .extractor import (
    _resolve_session_files,
    discover_global_sessions,
    discover_workspaces,
    extract_workspace,
    get_global_storage,
    WorkspaceMeta,
)


class CopilotExtractor(AgentExtractor):
    """Copilot extractor using the clean consolidated copilot implementation.
    
    This adapter bridges the internal extractor to the agent framework interface.
    """
    
    AGENT_NAME = "copilot"
    
    def __init__(self, workspace_id: str, workspace_meta: Optional[WorkspaceMeta] = None):
        super().__init__(workspace_id)
        self._meta = workspace_meta
        self._workspace_cache: dict[str, WorkspaceMeta] = {}
    
    @classmethod
    def create(cls, workspace_id: str, **kwargs) -> "CopilotExtractor":
        """Factory method to create extractor instance."""
        return cls(workspace_id, workspace_meta=kwargs.get("workspace_meta"))
    
    def scan_workspaces(self) -> List[WorkspaceInfo]:
        """Scan for available workspaces with Copilot chat sessions."""
        # Get workspace_storage from config if available
        storage_path = None
        if self.config:
            ws_path = self.config.get('workspace_storage')
            if ws_path:
                storage_path = Path(ws_path)
        
        workspaces = discover_workspaces(base=storage_path) + discover_global_sessions(base=get_global_storage())
        
        result = []
        for meta in workspaces:
            # Cache for later use
            self._workspace_cache[meta.workspace_id] = meta
            
            # Count sessions
            chat_dir = meta.chat_sessions_dir if meta.chat_sessions_dir is not None else meta.path / "chatSessions"
            session_count = len(_resolve_session_files(chat_dir)) if chat_dir.exists() else 0
            
            result.append(WorkspaceInfo(
                workspace_id=meta.workspace_id,
                workspace_name=meta.workspace_name,
                workspace_folder=meta.workspace_folder,
                agents=[self.AGENT_NAME],
                session_count=session_count,
            ))
        
        return result
    
    def extract_sessions(self) -> ExtractedWorkspace:
        """Extract all turns from the workspace."""
        meta = self._get_workspace_meta()
        if not meta:
            return ExtractedWorkspace(
                turns=[], session_count=0, agent_name=self.AGENT_NAME,
                workspace_id=self.workspace_id, code_metrics=[]
            )
        
        # Extract using consolidated extractor
        # It already returns BaseTurn objects with edits integrated
        turns = extract_workspace(meta)
        
        session_ids = {turn.session_id for turn in turns}
        
        return ExtractedWorkspace(
            turns=turns,
            session_count=len(session_ids),
            agent_name=self.AGENT_NAME,
            workspace_id=self.workspace_id,
            code_metrics=[],
        )
    
    def get_latest_activity(self) -> Optional[WorkspaceActivity]:
        """Get quick stats from source files without full extraction."""
        meta = self._get_workspace_meta()
        if not meta:
            return None
        
        chat_dir = meta.chat_sessions_dir if meta.chat_sessions_dir is not None else meta.path / "chatSessions"
        if not chat_dir.exists():
            return None
        
        session_ids = []
        turn_count = 0
        
        for session_file in _resolve_session_files(chat_dir):
            sid = session_file.stem
            if not is_valid_session_id(sid):
                continue
            session_ids.append(sid)
            # Quick estimate: read file and count "role" occurrences
            try:
                content = session_file.read_text(encoding="utf-8")
                turn_count += content.count('"role"')
            except OSError:
                pass
        
        return WorkspaceActivity(
            session_count=len(session_ids),
            turn_count=turn_count,
            session_ids=session_ids,
        )
    
    def cleanup(self) -> None:
        """Cleanup resources (none needed for file-based extraction)."""
        pass
    
    def _get_workspace_meta(self) -> Optional[WorkspaceMeta]:
        """Get workspace metadata, discovering if needed."""
        if self._meta:
            return self._meta
        
        # Check cache
        if self.workspace_id in self._workspace_cache:
            return self._workspace_cache[self.workspace_id]
        
        # Get workspace_storage from config if available
        storage_path = None
        if self.config:
            ws_path = self.config.get('workspace_storage')
            if ws_path:
                storage_path = Path(ws_path)
        
        # Discover all workspaces and find ours
        for meta in discover_workspaces(base=storage_path) + discover_global_sessions(base=get_global_storage()):
            self._workspace_cache[meta.workspace_id] = meta
            if meta.workspace_id == self.workspace_id:
                self._meta = meta
                return meta
        
        return None
