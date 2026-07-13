"""Reusable registry helpers for source parsers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypedDict, cast

from src.extract.base import LowLevelWorkspaceParser
from src.extract.claude_code import ClaudeCodeLowLevelParser
from src.extract.codex import CodexLowLevelParser
from src.extract.copilot import CopilotLowLevelParser
from src.extract.copilot_cli import CopilotCliLowLevelParser
from src.shared.config.config_loader import get_extract_config


_ParserFactory = Callable[..., LowLevelWorkspaceParser]


class AgentMetadata(TypedDict, total=False):
    """Static metadata for a supported agent."""

    name: str
    display_name: str
    icon: str
    color: str
    description: str

_PARSER_FACTORIES: dict[str, _ParserFactory] = {
    "claude_code": ClaudeCodeLowLevelParser,
    "codex": CodexLowLevelParser,
    "copilot": CopilotLowLevelParser,
    "copilot_cli": CopilotCliLowLevelParser,
}

_AGENT_METADATA: dict[str, AgentMetadata] = {
    "claude_code": {
        "name": "Claude Code",
        "display_name": "Claude Code",
        "icon": "icon.svg",
        "color": "bg-orange-900 text-orange-400",
        "description": "Anthropic Claude Code chat extraction",
    },
    "codex": {
        "name": "Codex",
        "display_name": "OpenAI Codex",
        "icon": "icon.png",
        "color": "bg-emerald-900 text-emerald-300",
        "description": "OpenAI Codex CLI rollout extraction",
    },
    "copilot": {
        "name": "Copilot",
        "display_name": "GitHub Copilot",
        "icon": "icon.png",
        "color": "bg-purple-900 text-purple-400",
        "description": "GitHub Copilot chat extraction",
    },
    "copilot_cli": {
        "name": "Copilot CLI",
        "display_name": "GitHub Copilot CLI",
        "icon": "icon.svg",
        "color": "bg-sky-900 text-sky-400",
        "description": "GitHub Copilot CLI agent session extraction",
    },
}

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def list_agents() -> list[str]:
    """List supported agents."""
    return sorted(_PARSER_FACTORIES)


def supports_agent(agent_name: str) -> bool:
    """Return True when the agent is implemented by the parser layer."""
    return agent_name in _PARSER_FACTORIES


def get_agent_metadata(agent_name: str) -> AgentMetadata | None:
    """Return metadata for a supported agent."""
    metadata = _AGENT_METADATA.get(agent_name)
    if metadata is None:
        return None
    return cast(AgentMetadata, dict(metadata))


def get_all_agent_metadata() -> dict[str, AgentMetadata]:
    """Return metadata for all supported agents."""
    return {
        agent_name: cast(AgentMetadata, dict(metadata))
        for agent_name, metadata in _AGENT_METADATA.items()
    }


def get_agent_icon_path(agent_name: str) -> Path | None:
    """Return the icon path for a supported agent when available."""
    metadata = _AGENT_METADATA.get(agent_name)
    if metadata is None:
        return None

    icon_name = metadata.get("icon")
    if not icon_name:
        return None

    icon_path = _ASSETS_DIR / agent_name / icon_name
    if icon_path.exists():
        return icon_path
    return None


def build_parser(agent_name: str) -> LowLevelWorkspaceParser:
    """Build a configured source parser for *agent_name*."""
    extract_config = get_extract_config(agent_name)
    parser_factory = _PARSER_FACTORIES[agent_name]

    if agent_name == "copilot":
        workspace_storage = _path_or_none(extract_config.get("workspace_storage"))
        global_storage = _path_or_none(extract_config.get("global_storage"))
        return parser_factory(workspace_storage=workspace_storage, global_storage=global_storage)

    if agent_name == "claude_code":
        claude_dir = _path_or_none(extract_config.get("claude_dir"))
        claude_dirs = _paths_or_none(extract_config.get("storage_roots"))
        return parser_factory(claude_dir=claude_dir, claude_dirs=claude_dirs)

    if agent_name == "codex":
        codex_home = _path_or_none(extract_config.get("codex_home"))
        codex_homes = _paths_or_none(extract_config.get("storage_roots"))
        return parser_factory(codex_home=codex_home, codex_homes=codex_homes)

    if agent_name == "copilot_cli":
        session_state_dir = _path_or_none(
            extract_config.get("session_state_dir") or extract_config.get("workspace_storage")
        )
        session_state_dirs = _paths_or_none(extract_config.get("storage_roots"))
        return parser_factory(base_dir=session_state_dir, base_dirs=session_state_dirs)

    return parser_factory()


def _path_or_none(value: Any) -> Path | None:
    if not value:
        return None
    return Path(str(value))


def _paths_or_none(value: Any) -> list[Path] | None:
    if not value:
        return None
    if isinstance(value, list):
        paths = [Path(str(item)) for item in value if item]
        return paths or None
    return [Path(str(value))]