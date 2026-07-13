"""Runtime helpers for integrating source parsers into the main extraction pipeline."""

from __future__ import annotations

import time

from src.extract.models import ParsedWorkspace
from src.extract.registry import build_parser, supports_agent
from src.pipeline.extraction.adapter import adapt_parsed_workspace
from src.shared.logging.logger import get_logger
from src.shared.models.workspace import ExtractedWorkspace

__all__ = ["supports_agent", "extract_workspace_from_source"]

logger = get_logger(__name__)


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def extract_workspace_from_source(agent_name: str, workspace_id: str) -> tuple[ParsedWorkspace, ExtractedWorkspace]:
    """Parse a workspace through the source parser layer and adapt it to Genie-X models."""
    perf_start = time.perf_counter()
    checkpoint = time.perf_counter()
    parser = _build_parser(agent_name)
    logger.info(
        f"[PERF] extract_workspace_from_source {agent_name}/{workspace_id} | build_parser: "
        f"{_elapsed_ms(checkpoint):.1f}ms"
    )
    checkpoint = time.perf_counter()
    parsed_workspace = parser.parse_workspace(workspace_id)
    raw_events = sum(len(session.events) for session in parsed_workspace.sessions)
    logger.info(
        f"[PERF] extract_workspace_from_source {agent_name}/{workspace_id} | parse_workspace: "
        f"{_elapsed_ms(checkpoint):.1f}ms ({len(parsed_workspace.sessions)} sessions, {raw_events} events)"
    )
    checkpoint = time.perf_counter()
    adapted_workspace = adapt_parsed_workspace(parsed_workspace)
    logger.info(
        f"[PERF] extract_workspace_from_source {agent_name}/{workspace_id} | adapt_parsed_workspace: "
        f"{_elapsed_ms(checkpoint):.1f}ms ({adapted_workspace.turn_count} turns)"
    )
    adapted_workspace.source_artifacts.setdefault("parsed_workspaces", []).append(parsed_workspace)
    logger.info(
        f"[PERF] extract_workspace_from_source {agent_name}/{workspace_id} | TOTAL: "
        f"{_elapsed_ms(perf_start):.1f}ms"
    )
    return parsed_workspace, adapted_workspace


def merge_parsed_workspaces(parsed_workspaces: list[ParsedWorkspace]) -> ParsedWorkspace:
    if not parsed_workspaces:
        raise ValueError("parsed_workspaces must not be empty")
    if len(parsed_workspaces) == 1:
        return parsed_workspaces[0]

    base = parsed_workspaces[0]
    sessions_by_id = {session.session_id: session for session in base.sessions}
    merged_issues = list(base.issues)
    merged_metadata = dict(base.metadata)
    source_roots = [base.descriptor.source_root]

    for parsed_workspace in parsed_workspaces[1:]:
        source_roots.append(parsed_workspace.descriptor.source_root)
        for session in parsed_workspace.sessions:
            sessions_by_id.setdefault(session.session_id, session)
        merged_issues.extend(parsed_workspace.issues)
        merged_metadata.update(parsed_workspace.metadata)

    merged_metadata["source_roots"] = source_roots
    return ParsedWorkspace(
        descriptor=base.descriptor,
        sessions=sorted(sessions_by_id.values(), key=lambda item: (item.started_at_ms or 0, item.session_id)),
        issues=merged_issues,
        metadata=merged_metadata,
    )


def _build_parser(agent_name: str):
    return build_parser(agent_name)
