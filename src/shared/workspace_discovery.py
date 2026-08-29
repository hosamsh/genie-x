"""Shared workspace discovery for supported parser agents."""

from __future__ import annotations

import platform
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path

from src.extract.registry import build_parser, list_agents
from src.extract.base import LowLevelWorkspaceParser
from src.extract.models import WorkspaceDescriptor
from src.pipeline.extraction.adapter import adapt_parsed_workspace
from src.shared.io.paths import resolve_workspace_path
from src.shared.io.paths import is_home_or_root_dir
from src.shared.logging.logger import get_logger
from src.shared.models.workspace import WorkspaceActivity, WorkspaceInfo

logger = get_logger(__name__)

_workspace_folders_cache: Optional[Set[str]] = None
_find_workspace_cache: Dict[str, Optional[WorkspaceInfo]] = {}

# Opt-in cache of per-agent workspace scans. Disabled (None) by default so the
# long-running web server keeps its existing, un-cached scan behaviour (no added
# staleness). A CLI batch (e.g. `--extract --all`) enables it via
# prime_workspace_scan_cache() so all workspaces share ONE scan instead of
# re-scanning every agent (~40s each) per workspace.
_agent_scan_cache: Optional[Dict[str, List[WorkspaceInfo]]] = None


def clear_find_workspace_cache() -> None:
    global _find_workspace_cache
    _find_workspace_cache = {}
    _workspace_identity_key.cache_clear()


def clear_workspace_folders_cache() -> None:
    global _workspace_folders_cache
    _workspace_folders_cache = None
    _workspace_identity_key.cache_clear()


def prime_workspace_scan_cache() -> None:
    """Enable and populate the per-agent scan cache for a batch operation.

    Only intended for short-lived CLI runs. Must be paired with
    clear_workspace_scan_cache() (use try/finally) so the process does not hold
    a stale snapshot afterwards.
    """
    global _agent_scan_cache
    _agent_scan_cache = {}
    for agent_name in _list_all_agents():
        _scan_agent_workspaces(agent_name)


def clear_workspace_scan_cache() -> None:
    """Disable the per-agent scan cache (restores default un-cached behaviour)."""
    global _agent_scan_cache
    _agent_scan_cache = None


def get_all_workspace_folders() -> Set[str]:
    global _workspace_folders_cache
    if _workspace_folders_cache is not None:
        return _workspace_folders_cache

    folders: Set[str] = set()
    for agent_name in _list_all_agents():
        for workspace in _scan_agent_workspaces(agent_name):
            if workspace.workspace_folder:
                folders.add(_normalize_folder(workspace.workspace_folder))

    _workspace_folders_cache = folders
    return folders


def _cache_workspace_folders(agent_workspaces: Dict[str, List[WorkspaceInfo]]) -> None:
    """Populate the folder cache from an already-completed workspace scan."""
    global _workspace_folders_cache
    folders: Set[str] = set()
    for workspaces in agent_workspaces.values():
        for workspace in workspaces:
            if workspace.workspace_folder:
                folders.add(_normalize_folder(workspace.workspace_folder))
    _workspace_folders_cache = folders


def is_workspace_folder(path: str) -> bool:
    if not path:
        return False
    return _normalize_folder(path) in get_all_workspace_folders()


def list_all_workspaces() -> List[WorkspaceInfo]:
    agent_names = _list_all_agents()
    agent_workspaces: Dict[str, List[WorkspaceInfo]] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(agent_names)))) as executor:
        future_to_agent = {
            executor.submit(_scan_agent_workspaces, agent_name): agent_name
            for agent_name in agent_names
        }
        for future in as_completed(future_to_agent):
            agent_name = future_to_agent[future]
            try:
                agent_workspaces[agent_name] = future.result()
            except Exception as exc:
                logger.warning("workspace scan failed for %s: %s", agent_name, exc)
                agent_workspaces[agent_name] = []
    _cache_workspace_folders(agent_workspaces)
    all_workspaces = _merge_workspaces(agent_workspaces)
    all_workspaces.sort(key=lambda item: (item.workspace_name.lower() or item.workspace_id.lower()))
    return all_workspaces


def list_workspaces_by_page(page: int = 1, page_size: int = 50) -> Tuple[List[WorkspaceInfo], int]:
    all_workspaces = list_all_workspaces()
    total_count = len(all_workspaces)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    return all_workspaces[start_idx:end_idx], total_count


def find_workspace(workspace_id: str) -> Optional[WorkspaceInfo]:
    if workspace_id in _find_workspace_cache:
        return _find_workspace_cache[workspace_id]

    all_agent_workspaces = {
        agent_name: _scan_agent_workspaces(agent_name)
        for agent_name in _list_all_agents()
    }
    _cache_workspace_folders(all_agent_workspaces)

    identity_key = ""
    seed_workspace: Optional[WorkspaceInfo] = None

    for agent_name, workspaces in all_agent_workspaces.items():
        for workspace in workspaces:
            if workspace.workspace_id == workspace_id:
                seed_workspace = workspace
                identity_key = get_workspace_identity_key(workspace)
                break
        if identity_key:
            break

    if not identity_key:
        merged = _merge_workspaces(all_agent_workspaces)
        for workspace in merged:
            if workspace.workspace_id == workspace_id or workspace_id in workspace._related_workspace_ids:
                seed_workspace = workspace
                identity_key = get_workspace_identity_key(workspace)
                break

    matches: Dict[str, WorkspaceInfo] = {}
    agent_workspace_ids: Dict[str, List[str]] = {}
    related_workspace_ids: Set[str] = set()
    total_session_count = 0

    if identity_key:
        for agent_name, workspaces in all_agent_workspaces.items():
            for workspace in workspaces:
                if get_workspace_identity_key(workspace) == identity_key:
                    matches.setdefault(agent_name, workspace)
                    agent_workspace_ids.setdefault(agent_name, []).append(workspace.workspace_id)
                    related_workspace_ids.add(workspace.workspace_id)
                    total_session_count += workspace.session_count

    if not matches and seed_workspace is not None:
        matches[seed_workspace.agents[0] if seed_workspace.agents else "unknown"] = seed_workspace
        if seed_workspace.workspace_id:
            related_workspace_ids.add(seed_workspace.workspace_id)
            total_session_count = seed_workspace.session_count
            agent_workspace_ids[seed_workspace.agents[0] if seed_workspace.agents else "unknown"] = [seed_workspace.workspace_id]

    if not matches:
        _find_workspace_cache[workspace_id] = None
        return None

    first_match = seed_workspace or next(iter(matches.values()))
    result = WorkspaceInfo(
        workspace_id=workspace_id,
        workspace_name=first_match.workspace_name,
        workspace_folder=first_match.workspace_folder,
        agents=list(matches.keys()),
        session_count=total_session_count,
    )
    result._agent_workspace_ids = agent_workspace_ids
    result._related_workspace_ids = sorted(related_workspace_ids)
    _find_workspace_cache[workspace_id] = result
    return result


def get_workspace_latest_stats(workspace_id: str) -> Dict[str, Optional[WorkspaceActivity]]:
    stats: Dict[str, Optional[WorkspaceActivity]] = {}
    workspace = find_workspace(workspace_id)
    if workspace is None:
        return stats

    agent_workspace_ids = getattr(workspace, "_agent_workspace_ids", {})
    for agent_name in workspace.agents:
        workspace_ids = agent_workspace_ids.get(agent_name, [workspace_id])
        if isinstance(workspace_ids, str):
            workspace_ids = [workspace_ids]
        aggregated: Optional[WorkspaceActivity] = None
        for agent_workspace_id in workspace_ids:
            activity = _get_agent_latest_stats(agent_name, agent_workspace_id)
            if activity is None:
                continue
            if aggregated is None:
                aggregated = WorkspaceActivity(
                    session_count=activity.session_count,
                    turn_count=activity.turn_count,
                    session_ids=list(activity.session_ids),
                )
            else:
                aggregated.session_count += activity.session_count
                aggregated.turn_count += activity.turn_count
                aggregated.session_ids.extend(activity.session_ids)
        if aggregated is not None:
            seen_ids: set[str] = set()
            aggregated.session_ids = [session_id for session_id in aggregated.session_ids if not (session_id in seen_ids or seen_ids.add(session_id))]
        stats[agent_name] = aggregated
    return stats


def _list_all_agents() -> List[str]:
    return list_agents()


def _scan_agent_workspaces(agent_name: str) -> List[WorkspaceInfo]:
    if _agent_scan_cache is not None and agent_name in _agent_scan_cache:
        return _agent_scan_cache[agent_name]
    start_time = time.perf_counter()
    try:
        parser = build_parser(agent_name)
        descriptors = parser.scan_workspaces()
        workspaces: list[WorkspaceInfo] = []
        for descriptor in descriptors:
            # Skip home directories and filesystem roots. CLI agents record the
            # directory they were launched from, which is often a home dir
            # rather than a real project.
            if is_home_or_root_dir(descriptor.workspace_folder):
                continue
            activity = _get_descriptor_discovery_stats(parser, descriptor)
            if activity is None or activity.session_count <= 0 or activity.turn_count <= 0:
                continue
            workspaces.append(
                WorkspaceInfo(
                    workspace_id=descriptor.workspace_id,
                    workspace_name=descriptor.workspace_name,
                    workspace_folder=descriptor.workspace_folder,
                    agents=[agent_name],
                    session_count=activity.session_count,
                    source_available=True,
                )
            )
        logger.info(
            f"[PERF] list_all_workspaces | scan {agent_name}: {(time.perf_counter()-start_time)*1000:.1f}ms ({len(workspaces)} workspaces)"
        )
        if _agent_scan_cache is not None:
            _agent_scan_cache[agent_name] = workspaces
        return workspaces
    except Exception as exc:
        logger.warning("workspace scan failed for %s: %s", agent_name, exc)
        if _agent_scan_cache is not None:
            _agent_scan_cache[agent_name] = []
        return []


def _get_agent_latest_stats(agent_name: str, workspace_id: str) -> Optional[WorkspaceActivity]:
    try:
        parser = build_parser(agent_name)
        descriptor = next((item for item in parser.scan_workspaces() if item.workspace_id == workspace_id), None)
        if descriptor is None:
            return None
        return _get_descriptor_latest_stats(parser, descriptor)
    except Exception as exc:
        logger.warning("activity scan failed for %s/%s: %s", agent_name, workspace_id, exc)
        return None


def _get_descriptor_discovery_stats(
    parser: LowLevelWorkspaceParser,
    descriptor: WorkspaceDescriptor,
) -> Optional[WorkspaceActivity]:
    parser_activity = parser.get_workspace_activity(descriptor)
    if parser_activity is not None:
        return parser_activity
    return _get_descriptor_latest_stats(parser, descriptor)


def _get_descriptor_latest_stats(
    parser: LowLevelWorkspaceParser,
    descriptor: WorkspaceDescriptor,
) -> Optional[WorkspaceActivity]:
    parsed_workspace = parser.parse_workspace(descriptor.workspace_id)
    adapted_workspace = adapt_parsed_workspace(parsed_workspace)
    visible_session_ids = _visible_session_ids_from_turns(adapted_workspace.turns)
    if not visible_session_ids:
        return WorkspaceActivity(session_count=0, turn_count=0, session_ids=[])
    return WorkspaceActivity(
        session_count=len(visible_session_ids),
        turn_count=adapted_workspace.turn_count,
        session_ids=visible_session_ids,
    )


def _visible_session_ids_from_turns(turns: list[Any]) -> list[str]:
    session_ids: list[str] = []
    seen: set[str] = set()
    for turn in turns:
        session_id = getattr(turn, "session_id", "")
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        session_ids.append(session_id)
    return session_ids


def _normalize_folder(path: str) -> str:
    return path.replace("\\", "/").lower()


def get_workspace_identity_key(workspace: WorkspaceInfo) -> str:
    return _workspace_identity_key(workspace.workspace_folder or "", workspace.workspace_id)


@lru_cache(maxsize=4096)
def _workspace_identity_key(folder: str, workspace_id: str) -> str:
    normalized_folder = _normalize_folder(folder)
    resolved_path, _ = resolve_workspace_path(folder)
    translated_linux = _translate_linux_path_from_wsl(resolved_path)
    if translated_linux:
        resolved_path = translated_linux
    normalized_resolved = _normalize_folder(resolved_path)
    if normalized_resolved:
        return f"folder:{normalized_resolved}"
    return f"folder:{normalized_folder}" if normalized_folder else f"id:{workspace_id}"


def _translate_wsl_linux_path(distro: str, linux_path: str) -> str:
    try:
        result = subprocess.run(
            ["wsl.exe", "-d", distro, "wslpath", "-w", linux_path],
            capture_output=True,
            check=False,
        )
    except OSError:
        return linux_path

    if result.returncode != 0:
        return linux_path

    stdout = result.stdout or b""
    if b"\x00" in stdout:
        decoded = stdout.decode("utf-16le", errors="ignore")
    else:
        decoded = stdout.decode("utf-8", errors="replace")

    translated = decoded.strip()
    return translated or linux_path


def _translate_linux_path_from_wsl(path_str: str) -> str:
    if platform.system() != "Windows":
        return ""
    if not path_str.startswith("/"):
        return ""

    mnt_match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path_str)
    if mnt_match:
        drive_letter = mnt_match.group(1).lower()
        rest_of_path = mnt_match.group(2)
        windows_path = f"{drive_letter}:/{rest_of_path}"
        try:
            if Path(windows_path).exists():
                return windows_path
        except (OSError, ValueError):
            pass

    windows_style = path_str.replace("/", "\\")
    for distro in _list_wsl_distros():
        translated = f"\\\\wsl.localhost\\{distro}{windows_style}"
        try:
            if Path(translated).exists():
                return translated
        except (OSError, ValueError):
            continue
    return ""


@lru_cache(maxsize=1)
def _list_wsl_distros() -> tuple[str, ...]:
    try:
        result = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, check=False)
    except OSError:
        return ()

    if result.returncode != 0:
        return ()

    stdout = result.stdout or b""
    if b"\x00" in stdout:
        decoded = stdout.decode("utf-16le", errors="ignore")
    else:
        decoded = stdout.decode("utf-8", errors="replace")
    return tuple(line.strip() for line in decoded.splitlines() if line.strip())


def _merge_workspaces(agent_workspaces: Dict[str, List[WorkspaceInfo]]) -> List[WorkspaceInfo]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for agent_name, workspaces in agent_workspaces.items():
        for workspace in workspaces:
            if workspace.workspace_id not in by_id:
                by_id[workspace.workspace_id] = {
                    "workspace_name": workspace.workspace_name,
                    "workspace_folder": workspace.workspace_folder,
                    "agents": [],
                    "session_count": 0,
                    "related_workspace_ids": {workspace.workspace_id},
                }

            by_id[workspace.workspace_id]["agents"].append(agent_name)
            by_id[workspace.workspace_id]["session_count"] += workspace.session_count

    by_key: Dict[str, Dict[str, Any]] = {}
    for workspace_id, data in by_id.items():
        probe = WorkspaceInfo(
            workspace_id=workspace_id,
            workspace_name=data["workspace_name"],
            workspace_folder=data["workspace_folder"],
            agents=list(data["agents"]),
            session_count=data["session_count"],
        )
        merge_key = get_workspace_identity_key(probe)
        if merge_key not in by_key:
            by_key[merge_key] = {
                "workspace_id": workspace_id,
                "workspace_name": data["workspace_name"],
                "workspace_folder": data["workspace_folder"],
                "agents": data["agents"],
                "session_count": data["session_count"],
                "related_workspace_ids": {workspace_id},
            }
        else:
            by_key[merge_key]["agents"].extend(data["agents"])
            by_key[merge_key]["session_count"] += data["session_count"]
            by_key[merge_key]["related_workspace_ids"].add(workspace_id)
            if len(workspace_id) < len(by_key[merge_key]["workspace_id"]):
                by_key[merge_key]["workspace_id"] = workspace_id
            if by_key[merge_key]["workspace_folder"].startswith("vscode-remote://") and data["workspace_folder"]:
                by_key[merge_key]["workspace_folder"] = data["workspace_folder"]

    merged: List[WorkspaceInfo] = []
    for data in by_key.values():
        unique_agents: List[str] = []
        seen: Set[str] = set()
        for agent in data["agents"]:
            if agent in seen:
                continue
            seen.add(agent)
            unique_agents.append(agent)

        workspace = WorkspaceInfo(
            workspace_id=data["workspace_id"],
            workspace_name=data["workspace_name"],
            workspace_folder=data["workspace_folder"],
            agents=unique_agents,
            session_count=data["session_count"],
        )
        workspace._related_workspace_ids = sorted(data["related_workspace_ids"])
        merged.append(workspace)

    return merged
