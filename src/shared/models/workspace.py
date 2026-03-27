"""Workspace and extraction models.

Models for workspace metadata and extraction operation results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Union

from .dataclass_mixin import DataclassIO
from .turn import Turn, EnrichedTurn
from .code_metric import CodeMetric


@dataclass
class WorkspaceInfo(DataclassIO):
    """Information about a workspace (from source and/or database).
    
    Core fields are populated by extractors when scanning source storage.
    Optional fields are populated when merging with database status.
    """
    # Core fields (always present)
    workspace_id: str
    workspace_name: str = ""
    workspace_folder: str = ""
    agents: List[str] = field(default_factory=list)
    session_count: int = 0
    
    # Optional: Source/database availability (for web API)
    source_available: bool = True   # Exists on disk (default True for backwards compat)
    db_available: bool = False      # Has data in database
    
    # Optional: Database-only fields
    turn_count: int = 0
    is_extracted: bool = False
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None
    
    # Optional: Per-agent status (for multi-agent workspaces)
    agent_status: Dict[str, "AgentStatus"] = field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkspaceInfo":
        """Create from dictionary (e.g., from scan functions)."""
        agents = data.get("agents", [])
        if not agents and data.get("agent"):
            agents = [data["agent"]]
        return cls(
            workspace_id=data.get("workspace_id", ""),
            workspace_name=data.get("workspace_name", ""),
            workspace_folder=data.get("workspace_folder", ""),
            agents=agents,
            session_count=data.get("session_count", 0),
        )
    

    
    def to_api_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        status_info = {}
        for agent, status in self.agent_status.items():
            status_info[agent] = {
                "extracted_at": status.extracted_at.isoformat() if status.extracted_at else None,
                "run_dir": status.run_dir,
            }
        
        return {
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "workspace_folder": self.workspace_folder,
            "agents": self.agents,
            "session_count": self.session_count,
            "turn_count": self.turn_count,
            "status": status_info,
            "source_available": self.source_available,
        }


@dataclass
class WorkspaceActivity:
    """Quick stats from source files (without full extraction).
    
    Used to detect if a workspace has new data since last extraction.
    Much faster than full extraction - just counts sessions/turns.
    """
    session_count: int
    turn_count: int
    session_ids: List[str]


@dataclass
class AgentStatus:
    """Status for a specific agent in a workspace."""
    agent: str
    is_extracted: bool = False
    session_count: int = 0
    turn_count: int = 0
    extracted_at: Optional[datetime] = None
    run_dir: Optional[str] = None
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None


@dataclass
class ExtractedWorkspace:
    """Result of extracting a workspace from an agent.
    
    Contains Turn objects (enriched or raw) and optional code metrics.
    This is returned by both individual agent extractors and the
    workspace_operations.extract_workspace() orchestrator.
    """
    turns: Sequence[Union[Turn, EnrichedTurn]]
    session_count: int
    agent_name: str
    workspace_id: str = ""
    code_metrics: List[CodeMetric] = field(default_factory=list)
    
    @property
    def turn_count(self) -> int:
        return len(self.turns)


@dataclass
class WorkspaceExtractionResult:
    """Result of processing a workspace through extraction and storage."""

    status: str  # "success", "skipped", or "failed"
    workspace_id: str
    workspace_name: str
    workspace_folder: str
    session_count: int
    turn_count: int
    duration_ms: int
    combined_count: int = 0
    total_code_loc: int = 0
    total_doc_loc: int = 0
    reason: Optional[str] = None  # For skipped
    error: Optional[str] = None  # For failed

    @property
    def success(self) -> bool:
        """Compatibility property for success check."""
        return self.status == "success"

    @property
    def duration_minutes(self) -> float:
        """Duration in minutes."""
        return round(self.duration_ms / 60000, 2)

    def to_dict(self) -> dict:
        """Convert to dict for serialization, excluding None values."""
        return {
            k: v
            for k, v in {
                "status": self.status,
                "workspace_id": self.workspace_id,
                "workspace_name": self.workspace_name,
                "workspace_folder": self.workspace_folder,
                "session_count": self.session_count,
                "turn_count": self.turn_count,
                "combined_count": self.combined_count,
                "duration_minutes": self.duration_minutes,
                "total_code_loc": self.total_code_loc,
                "total_doc_loc": self.total_doc_loc,
                "reason": self.reason,
                "error": self.error,
            }.items()
            if v is not None
        }

