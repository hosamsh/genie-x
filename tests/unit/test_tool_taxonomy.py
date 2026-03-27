from __future__ import annotations

import pytest

from src.shared.code.tool_taxonomy import (
    CATEGORY_BASH,
    CATEGORY_EDIT,
    CATEGORY_GLOB,
    CATEGORY_GREP,
    CATEGORY_OTHER,
    CATEGORY_READ,
    CATEGORY_TASK,
    CATEGORY_TOOL,
    CATEGORY_WRITE,
    normalize_tool_category,
)


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("Read", CATEGORY_READ),
        ("Edit", CATEGORY_EDIT),
        ("Write", CATEGORY_WRITE),
        ("Bash", CATEGORY_BASH),
        ("Grep", CATEGORY_GREP),
        ("Glob", CATEGORY_GLOB),
        ("Task", CATEGORY_TASK),
        ("Skill", CATEGORY_TOOL),
        ("Shell", CATEGORY_BASH),
        ("StrReplace", CATEGORY_EDIT),
        ("LS", CATEGORY_GLOB),
        ("copilot_readFile", CATEGORY_READ),
        ("copilot_replaceString", CATEGORY_EDIT),
        ("copilot_createFile", CATEGORY_WRITE),
        ("copilot_runInTerminal", CATEGORY_BASH),
        ("copilot_searchFiles", CATEGORY_GREP),
        ("copilot_listDir", CATEGORY_GLOB),
        ("copilot_findTextInFiles", CATEGORY_GREP),
        ("runSubagent", CATEGORY_TASK),
        ("manage_todo_list", CATEGORY_TASK),
        ("mcp__filesystem__read_file", CATEGORY_READ),
        ("mcp__toolbox__task_runner", CATEGORY_TASK),
    ],
)
def test_normalize_tool_category_known_values(raw_name: str, expected: str) -> None:
    assert normalize_tool_category(raw_name) == expected


@pytest.mark.parametrize("raw_name", ["", "   ", "frobnicate", "copilot_unknownThing"])
def test_normalize_tool_category_unknown_values(raw_name: str) -> None:
    assert normalize_tool_category(raw_name) == CATEGORY_OTHER


def test_normalize_tool_category_is_case_insensitive() -> None:
    assert normalize_tool_category("COPILOT_READFILE") == CATEGORY_READ
