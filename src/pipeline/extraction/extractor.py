"""Core workspace extraction logic.

Handles extracting workspace data from agents and enriching it.
"""

from typing import List, Optional, Union

from src.shared.logging.logger import get_logger
from src.shared.models.code_metric import CodeMetric
from src.shared.models.turn import Turn, EnrichedTurn
from src.shared.models.workspace import ExtractedWorkspace
from src.extract_plugins.agent_registry import get_extractor_class
from .turn_enrichment import enrich_turns
from .workspace_discovery import find_workspace

logger = get_logger(__name__)


def extract_workspace(workspace_id: str, agent_filter: Optional[str] = None) -> ExtractedWorkspace:
    """Extract workspace from agents, merge, and enrich turns.
    
    Args:
        workspace_id: The workspace ID to extract.
        agent_filter: Optional agent name to extract from (if None, extracts from all agents).
        
    Returns:
        ExtractedWorkspace with enriched turns and metrics.
    """
    workspace_info = find_workspace(workspace_id)
    if not workspace_info:
        raise ValueError(f"Workspace {workspace_id} not found in any registered agent")

    agents = workspace_info.agents
    
    if agent_filter:
        if agent_filter not in agents:
            raise ValueError(
                f"Workspace {workspace_id} not found in agent '{agent_filter}' "
                f"(available: {agents})"
            )
        agents = [agent_filter]
        logger.progress(f"  Filtering extraction to agent: {agent_filter}")

    all_base_turns: List[Union[Turn, EnrichedTurn]] = []
    all_code_metrics: List[CodeMetric] = []
    total_sessions = 0

    # Get agent-specific workspace IDs
    agent_workspace_ids = getattr(workspace_info, '_agent_workspace_ids', {})

    for agent_name in agents:
        ExtractorClass = get_extractor_class(agent_name)
        if ExtractorClass is None:
            continue
        extractor = None
        try:
            # Use agent-specific workspace ID if available, otherwise use the provided one
            agent_ws_id = agent_workspace_ids.get(agent_name, workspace_id)
            extractor = ExtractorClass.create(agent_ws_id)
            result = extractor.extract_sessions()
            if result.session_count > 0:
                logger.progress(
                    f"  [{agent_name.capitalize()}] Extracted {result.session_count} sessions, "
                    f"{result.turn_count} turns"
                )
            all_base_turns.extend(result.turns)
            all_code_metrics.extend(result.code_metrics)
            total_sessions += result.session_count
            
            if result.code_metrics:
                logger.progress(
                    f"  [{agent_name.capitalize()}] Found {len(result.code_metrics)} code metrics"
                )
        finally:
            if extractor:
                extractor.cleanup()

    if not all_base_turns:
        logger.warning(f"  No conversation data found for workspace {workspace_id}")
        logger.warning("            (workspace exists but sessions are empty)")
        return ExtractedWorkspace(
            turns=[],
            session_count=0,
            agent_name="+".join(agents) if agents else "unknown",
            workspace_id=workspace_id,
            code_metrics=[],
        )

    enriched_turns = enrich_turns(all_base_turns)

    agent_name = "+".join(agents) if agents else "unknown"
    return ExtractedWorkspace(
        turns=enriched_turns,
        session_count=total_sessions,
        agent_name=agent_name,
        workspace_id=workspace_id,
        code_metrics=all_code_metrics,
    )
