"""Fork-aware DAG helpers for Claude Code sessions."""
from __future__ import annotations

from dataclasses import dataclass

FORK_THRESHOLD = 3


@dataclass(frozen=True)
class DagEntry:
    uuid: str
    parent_uuid: str
    msg_type: str
    line_index: int
    message: dict
    timestamp_ms: int = 0


@dataclass(frozen=True)
class Branch:
    entry_indices: list[int]
    branch_uuid: str = ""


def build_dag(entries: list[DagEntry]) -> dict[int, list[int]]:
    """Return an index-based children map, or {} when the graph is unusable."""
    if not entries or any(not entry.uuid for entry in entries):
        return {}

    uuid_to_index = {entry.uuid: index for index, entry in enumerate(entries)}
    if len(uuid_to_index) != len(entries):
        return {}

    children_map = {index: [] for index in range(len(entries))}
    for index, entry in enumerate(entries):
        if not entry.parent_uuid:
            continue
        parent_index = uuid_to_index.get(entry.parent_uuid)
        if parent_index is None:
            return {}
        children_map[parent_index].append(index)

    for child_indices in children_map.values():
        child_indices.sort(key=lambda idx: entries[idx].line_index)
    return children_map


def _count_user_turns(
    start_index: int,
    children_map: dict[int, list[int]],
    entries: list[DagEntry],
) -> int:
    total = 1 if entries[start_index].msg_type == "user" else 0
    for child_index in children_map.get(start_index, []):
        total += _count_user_turns(child_index, children_map, entries)
    return total


def _walk(
    index: int,
    children_map: dict[int, list[int]],
    entries: list[DagEntry],
    prefix: list[int],
    branch_uuid: str,
) -> list[Branch]:
    current_path = [*prefix, index]
    child_indices = children_map.get(index, [])
    if not child_indices:
        return [Branch(entry_indices=current_path, branch_uuid=branch_uuid)]

    if len(child_indices) == 1:
        return _walk(child_indices[0], children_map, entries, current_path, branch_uuid)

    user_counts = [_count_user_turns(child_index, children_map, entries) for child_index in child_indices]
    if max(user_counts, default=0) <= FORK_THRESHOLD:
        return _walk(child_indices[-1], children_map, entries, current_path, branch_uuid)

    branches: list[Branch] = []
    for offset, child_index in enumerate(child_indices):
        child_branch_uuid = branch_uuid
        if offset > 0 and not child_branch_uuid:
            child_branch_uuid = entries[child_index].uuid
        branches.extend(_walk(child_index, children_map, entries, current_path, child_branch_uuid))
    return branches


def detect_forks(entries: list[DagEntry]) -> list[Branch]:
    """Split Claude DAG entries into extraction branches using the fork threshold."""
    if not entries:
        return []

    children_map = build_dag(entries)
    if not children_map:
        return [Branch(entry_indices=list(range(len(entries))))]

    uuid_to_index = {entry.uuid: index for index, entry in enumerate(entries)}
    root_indices = [
        index
        for index, entry in enumerate(entries)
        if not entry.parent_uuid or entry.parent_uuid not in uuid_to_index
    ]
    if len(root_indices) != 1:
        return [Branch(entry_indices=list(range(len(entries))))]

    return _walk(root_indices[0], children_map, entries, [], "")
