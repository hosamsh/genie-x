"""Core workspace extraction logic.

Handles extracting workspace data from agents and enriching it.
"""

import time
from typing import List, Optional, Union

from src.shared.logging.logger import get_logger
from src.shared.models.code_metric import CodeMetric
from src.shared.models.turn import Turn, EnrichedTurn
from src.shared.models.workspace import ExtractedWorkspace
from src.shared.workspace_discovery import find_workspace
from src.pipeline.extraction.runtime import extract_workspace_from_source, merge_parsed_workspaces, supports_agent
from .turn_enrichment import enrich_turns

logger = get_logger(__name__)


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def extract_workspace(workspace_id: str, agent_filter: Optional[str] = None) -> ExtractedWorkspace:
    """Extract workspace from agents, merge, and enrich turns.
    
    Args:
        workspace_id: The workspace ID to extract.
        agent_filter: Optional agent name to extract from (if None, extracts from all agents).
        
    Returns:
        ExtractedWorkspace with enriched turns and metrics.
    """
    perf_start = time.perf_counter()
    checkpoint = time.perf_counter()
    workspace_info = find_workspace(workspace_id)
    logger.info(f"[PERF] extract_workspace {workspace_id} | find_workspace: {_elapsed_ms(checkpoint):.1f}ms")
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

    unsupported_agents = [agent_name for agent_name in agents if not supports_agent(agent_name)]
    if unsupported_agents:
        raise ValueError(
            "Unsupported agents requested after parser cutover: "
            + ", ".join(sorted(unsupported_agents))
        )

    all_base_turns: List[Union[Turn, EnrichedTurn]] = []
    all_code_metrics: List[CodeMetric] = []
    parsed_workspaces: List[object] = []
    total_sessions = 0

    # Get agent-specific workspace IDs
    agent_workspace_ids = getattr(workspace_info, '_agent_workspace_ids', {})

    for agent_name in agents:
        source_workspace_ids = agent_workspace_ids.get(agent_name, [workspace_id])
        if isinstance(source_workspace_ids, str):
            source_workspace_ids = [source_workspace_ids]
        agent_parsed_workspaces = []
        agent_session_count = 0
        agent_turn_count = 0

        for agent_ws_id in source_workspace_ids:
            checkpoint = time.perf_counter()
            parsed_workspace, result = extract_workspace_from_source(agent_name, agent_ws_id)
            logger.info(
                f"[PERF] extract_workspace {workspace_id} | source {agent_name}/{agent_ws_id}: "
                f"{_elapsed_ms(checkpoint):.1f}ms ({result.session_count} sessions, {result.turn_count} turns)"
            )
            agent_parsed_workspaces.append(parsed_workspace)
            all_base_turns.extend(result.turns)
            all_code_metrics.extend(result.code_metrics)
            agent_session_count += result.session_count
            agent_turn_count += result.turn_count

        if agent_parsed_workspaces:
            parsed_workspaces.append(merge_parsed_workspaces(agent_parsed_workspaces))

        if agent_session_count > 0:
            logger.progress(
                f"  [{agent_name.capitalize()}] Extracted {agent_session_count} sessions, "
                f"{agent_turn_count} turns"
            )
        total_sessions += agent_session_count

    if not all_base_turns:
        logger.warning(f"  No conversation data found for workspace {workspace_id}")
        logger.warning("            (workspace exists but sessions are empty)")
        logger.info(f"[PERF] extract_workspace {workspace_id} | TOTAL: {_elapsed_ms(perf_start):.1f}ms")
        return ExtractedWorkspace(
            turns=[],
            session_count=0,
            agent_name="+".join(agents) if agents else "unknown",
            workspace_id=workspace_id,
            code_metrics=[],
        )

    checkpoint = time.perf_counter()
    enriched_turns = enrich_turns(all_base_turns)
    logger.info(
        f"[PERF] extract_workspace {workspace_id} | enrich_turns: "
        f"{_elapsed_ms(checkpoint):.1f}ms ({len(all_base_turns)} turns)"
    )

    agent_name = "+".join(agents) if agents else "unknown"
    logger.info(f"[PERF] extract_workspace {workspace_id} | TOTAL: {_elapsed_ms(perf_start):.1f}ms")
    return ExtractedWorkspace(
        turns=enriched_turns,
        session_count=total_sessions,
        agent_name=agent_name,
        workspace_id=workspace_id,
        code_metrics=all_code_metrics,
        source_artifacts={
            "parsed_workspaces": parsed_workspaces,
        } if parsed_workspaces else {},
    )
