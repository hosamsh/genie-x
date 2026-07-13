"""Normalize raw tool names into a small cross-agent taxonomy."""
from __future__ import annotations

import re

CATEGORY_READ = "Read"
CATEGORY_EDIT = "Edit"
CATEGORY_WRITE = "Write"
CATEGORY_BASH = "Bash"
CATEGORY_GREP = "Grep"
CATEGORY_GLOB = "Glob"
CATEGORY_TASK = "Task"
CATEGORY_TOOL = "Tool"
CATEGORY_OTHER = "Other"

_EXACT_MAPPINGS: dict[str, str] = {
    # Claude Code
    "read": CATEGORY_READ,
    "edit": CATEGORY_EDIT,
    "multiedit": CATEGORY_EDIT,
    "write": CATEGORY_WRITE,
    "bash": CATEGORY_BASH,
    "grep": CATEGORY_GREP,
    "glob": CATEGORY_GLOB,
    "task": CATEGORY_TASK,
    "agent": CATEGORY_TASK,
    "skill": CATEGORY_TOOL,
    "todoread": CATEGORY_TOOL,
    "todowrite": CATEGORY_TOOL,
    "webfetch": CATEGORY_TOOL,
    "websearch": CATEGORY_TOOL,
    "ls": CATEGORY_GLOB,
    "notebookread": CATEGORY_READ,
    "notebookedit": CATEGORY_EDIT,
    # Generic aliases
    "shell": CATEGORY_BASH,
    "strreplace": CATEGORY_EDIT,
    # VS Code / Copilot
    "copilotreadfile": CATEGORY_READ,
    "copilotreplacestring": CATEGORY_EDIT,
    "copilotcreatefile": CATEGORY_WRITE,
    "copilotruninterminal": CATEGORY_BASH,
    "copilotsearchfiles": CATEGORY_GREP,
    "copilotfindtextinfiles": CATEGORY_GREP,
    "copilotlistdir": CATEGORY_GLOB,
    "runsubagent": CATEGORY_TASK,
    "managetodolist": CATEGORY_TASK,
}

_KEYWORD_MAPPINGS: list[tuple[tuple[str, ...], str]] = [
    (("read", "openfile", "loadfile"), CATEGORY_READ),
    (("edit", "replace", "patch", "diff"), CATEGORY_EDIT),
    (("write", "createfile"), CATEGORY_WRITE),
    (("bash", "shell", "terminal", "command"), CATEGORY_BASH),
    (("grep", "search", "findtext"), CATEGORY_GREP),
    (("glob", "listdir", "ls", "tree"), CATEGORY_GLOB),
    (("task", "subagent", "agent"), CATEGORY_TASK),
    (("skill", "tool", "todo"), CATEGORY_TOOL),
]


def _normalize_name(raw_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", raw_name.lower())


def normalize_tool_category(raw_name: str) -> str:
    """Return one of the canonical tool categories for *raw_name*."""
    if not raw_name or not raw_name.strip():
        return CATEGORY_OTHER

    normalized = _normalize_name(raw_name)
    if not normalized:
        return CATEGORY_OTHER

    if normalized.startswith("mcp") and "__" in raw_name:
        tail = raw_name.split("__")[-1]
        normalized = _normalize_name(tail)

    exact = _EXACT_MAPPINGS.get(normalized)
    if exact:
        return exact

    for keywords, category in _KEYWORD_MAPPINGS:
        if any(keyword in normalized for keyword in keywords):
            return category

    return CATEGORY_OTHER
