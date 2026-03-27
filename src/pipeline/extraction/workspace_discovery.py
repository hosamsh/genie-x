"""Workspace discovery and lookup across agents.

Handles finding, listing, and merging workspace information from different agents.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from src.shared.logging.logger import get_logger
from src.shared.models.workspace import WorkspaceInfo, WorkspaceActivity
from src.extract_plugins.agent_registry import get_extractor_class, list_registered_agents

logger = get_logger(__name__)

# Cache for workspace folders to avoid repeated scanning
_workspace_folders_cache: Optional[Set[str]] = None

# Cache for find_workspace results to avoid repeated agent scans during extraction
_find_workspace_cache: Dict[str, Optional[WorkspaceInfo]] = {}


def clear_find_workspace_cache() -> None:
    """Clear the find_workspace cache. Call when workspace state may have changed."""
    global _find_workspace_cache
    _find_workspace_cache = {}


def get_all_workspace_folders() -> Set[str]:
    """Get a set of all known workspace folder paths (normalized).
    
    Returns paths in lowercase POSIX format for consistent comparison.
    Results are cached to avoid repeated agent scans.
    """
    global _workspace_folders_cache
    
    if _workspace_folders_cache is not None:
        return _workspace_folders_cache
    
    folders: Set[str] = set()
    
    for agent_name in list_registered_agents():
        ExtractorClass = get_extractor_class(agent_name)
        if ExtractorClass is None:
            continue
        try:
            extractor = ExtractorClass.create("__scan__")
            workspaces = extractor.scan_workspaces()
            for ws in workspaces:
                if ws.workspace_folder:
                    # Normalize: lowercase POSIX path
                    normalized = ws.workspace_folder.replace("\\", "/").lower()
                    folders.add(normalized)
        except Exception:
            continue
    
    _workspace_folders_cache = folders
    return folders


def is_workspace_folder(path: str) -> bool:
    """Check if a given path is a known workspace folder.
    
    Args:
        path: Absolute path to check (can be Windows or POSIX format)
        
    Returns:
        True if the path is a registered workspace folder
    """
    if not path:
        return False
    
    normalized = path.replace("\\", "/").lower()
    all_folders = get_all_workspace_folders()
    return normalized in all_folders


def clear_workspace_folders_cache() -> None:
    """Clear the cached workspace folders. Call after workspace changes."""
    global _workspace_folders_cache
    _workspace_folders_cache = None


def list_all_workspaces() -> List[WorkspaceInfo]:
    """Get all workspaces from all registered agents, merged by workspace_id."""
    agent_workspaces: Dict[str, List[WorkspaceInfo]] = {}
    for agent_name in list_registered_agents():
        ExtractorClass = get_extractor_class(agent_name)
        if ExtractorClass is None:
            agent_workspaces[agent_name] = []
            continue
        try:
            extractor = ExtractorClass.create("__scan__")
            workspaces = extractor.scan_workspaces()
            agent_workspaces[agent_name] = workspaces
        except Exception:
            agent_workspaces[agent_name] = []
    
    all_workspaces = _merge_workspaces(agent_workspaces)
    all_workspaces.sort(key=lambda x: (x.workspace_name.lower() or x.workspace_id.lower()))
    return all_workspaces


def list_workspaces_by_page(page: int = 1, page_size: int = 50) -> Tuple[List[WorkspaceInfo], int]:
    """Get paginated workspaces. Returns (list, total_count)."""
    all_workspaces = list_all_workspaces()
    total_count = len(all_workspaces)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    return all_workspaces[start_idx:end_idx], total_count


def find_workspace(workspace_id: str) -> Optional[WorkspaceInfo]:
    """Find workspace by ID across all agents. Returns merged info if found.
    
    This searches for:
    1. Exact workspace_id match
    2. Workspaces with the same folder path (for cross-agent consolidation)
    
    Returns a WorkspaceInfo with an additional _agent_workspace_ids dict that maps
    agent_name -> original_workspace_id for use during extraction.
    
    Results are cached to avoid repeated agent scans during extraction pipelines.
    """
    # Check cache first
    if workspace_id in _find_workspace_cache:
        return _find_workspace_cache[workspace_id]
    
    matches: Dict[str, WorkspaceInfo] = {}
    agent_workspace_ids: Dict[str, str] = {}  # agent -> their workspace_id
    target_folder: Optional[str] = None
    
    # Single scan: collect all workspaces from all agents once
    all_agent_workspaces: Dict[str, List[WorkspaceInfo]] = {}
    for agent_name in list_registered_agents():
        ExtractorClass = get_extractor_class(agent_name)
        if ExtractorClass is None:
            all_agent_workspaces[agent_name] = []
            continue
        try:
            extractor = ExtractorClass.create("__scan__")
            all_agent_workspaces[agent_name] = extractor.scan_workspaces()
        except Exception:
            all_agent_workspaces[agent_name] = []
    
    # First pass: find exact ID matches
    for agent_name, workspaces in all_agent_workspaces.items():
        for ws in workspaces:
            if ws.workspace_id == workspace_id:
                matches[agent_name] = ws
                agent_workspace_ids[agent_name] = ws.workspace_id
                if ws.workspace_folder:
                    target_folder = ws.workspace_folder
    
    # Second pass: find other agents with same folder (using cached results)
    if matches and target_folder:
        normalized_target = target_folder.replace("\\", "/").lower()
        for agent_name, workspaces in all_agent_workspaces.items():
            if agent_name in matches:
                continue
            for ws in workspaces:
                if ws.workspace_folder:
                    if ws.workspace_folder.replace("\\", "/").lower() == normalized_target:
                        matches[agent_name] = ws
                        agent_workspace_ids[agent_name] = ws.workspace_id
                        break
    
    if not matches:
        _find_workspace_cache[workspace_id] = None
        return None
    
    # Build consolidated result
    first_match = list(matches.values())[0]
    agents = list(matches.keys())
    
    result = WorkspaceInfo(
        workspace_id=workspace_id,
        workspace_name=first_match.workspace_name,
        workspace_folder=first_match.workspace_folder,
        agents=agents,
        session_count=sum(ws.session_count for ws in matches.values()),
    )
    # Store the agent-specific IDs for extraction
    result._agent_workspace_ids = agent_workspace_ids  # type: ignore
    
    # Cache the result
    _find_workspace_cache[workspace_id] = result
    return result


def get_workspace_latest_stats(workspace_id: str) -> Dict[str, Optional[WorkspaceActivity]]:
    """Get latest activity stats from all agents without full extraction."""
    stats = {}
    for agent_name in list_registered_agents():
        ExtractorClass = get_extractor_class(agent_name)
        if ExtractorClass is None:
            stats[agent_name] = None
            continue
        try:
            extractor = ExtractorClass.create(workspace_id)
            stats[agent_name] = extractor.get_latest_activity()
        except Exception:
            stats[agent_name] = None
    return stats


def _merge_workspaces(agent_workspaces: Dict[str, List[WorkspaceInfo]]) -> List[WorkspaceInfo]:
    """Merge workspaces from multiple agents by workspace_id AND workspace_folder.
    
    This performs a two-level merge:
    1. First by workspace_id (for same-agent duplicates)
    2. Then by workspace_folder (for cross-agent consolidation)
    
    This allows different agents (copilot, cursor, claude_code) to share the same
    workspace entry when they point to the same folder, while preserving the ability
    to extract from specific agents using their original IDs.
    """
    # Step 1: Merge by workspace_id (handles duplicates within same source)
    by_id: Dict[str, Dict[str, Any]] = {}
    id_to_folder: Dict[str, str] = {}  # Track workspace_id -> workspace_folder mapping
    
    for agent_name, workspaces in agent_workspaces.items():
        for ws in workspaces:
            if ws.workspace_id not in by_id:
                by_id[ws.workspace_id] = {
                    'workspace_name': ws.workspace_name,
                    'workspace_folder': ws.workspace_folder,
                    'agents': [],
                    'session_count': 0,
                }
                id_to_folder[ws.workspace_id] = ws.workspace_folder
            
            by_id[ws.workspace_id]['agents'].append(agent_name)
            by_id[ws.workspace_id]['session_count'] += ws.session_count
    
    # Step 2: Merge by workspace_folder (consolidates across agents)
    by_folder: Dict[str, Dict[str, Any]] = {}
    
    for workspace_id, data in by_id.items():
        folder = data['workspace_folder']
        if not folder:  # Skip if no folder path
            # Keep as separate entry
            by_folder[workspace_id] = {
                'workspace_id': workspace_id,
                'workspace_name': data['workspace_name'],
                'workspace_folder': folder,
                'agents': data['agents'],
                'session_count': data['session_count'],
            }
            continue
        
        # Normalize folder for comparison
        normalized_folder = folder.replace("\\", "/").lower()
        
        if normalized_folder not in by_folder:
            # First workspace with this folder - use its ID
            by_folder[normalized_folder] = {
                'workspace_id': workspace_id,
                'workspace_name': data['workspace_name'],
                'workspace_folder': folder,
                'agents': data['agents'],
                'session_count': data['session_count'],
            }
        else:
            # Merge with existing folder entry
            by_folder[normalized_folder]['agents'].extend(data['agents'])
            by_folder[normalized_folder]['session_count'] += data['session_count']
            # Keep the shortest/simplest ID (prefer hash IDs over encoded paths)
            if len(workspace_id) < len(by_folder[normalized_folder]['workspace_id']):
                by_folder[normalized_folder]['workspace_id'] = workspace_id
    
    # Convert to WorkspaceInfo objects
    result = []
    for data in by_folder.values():
        # Deduplicate agents
        unique_agents = []
        seen = set()
        for agent in data['agents']:
            if agent not in seen:
                unique_agents.append(agent)
                seen.add(agent)
        
        result.append(WorkspaceInfo(
            workspace_id=data['workspace_id'],
            workspace_name=data['workspace_name'],
            workspace_folder=data['workspace_folder'],
            agents=unique_agents,
            session_count=data['session_count'],
        ))
    
    return result
