"""Unit tests for the GitHub Copilot CLI extractor plugin.

Covers pure helpers, session-file discovery, event parsing, workspace
grouping, and the public extractor interface – all without real filesystem
access to ``~/.copilot``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from src.extract_plugins.copilot_cli.extractor import (
    AGENT_NAME,
    SessionMeta,
    CopilotCliExtractor,
    _extract_files,
    _get_field,
    _make_workspace_id,
    _ms_to_iso,
    _parse_timestamp,
    parse_session_events,
    peek_session_meta,
    scan_session_files,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, events: List[dict]) -> Path:
    """Write *events* as a JSONL file at *path* and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(evt) + "\n")
    return path


def _session_start(
    session_id: str = "sess-1",
    workspace_folder: str = "/home/user/project",
    workspace_name: str = "project",
    model: str = "gpt-4o",
    timestamp: int = 1_700_000_000_000,
) -> dict:
    return {
        "type": "session.start",
        "sessionId": session_id,
        "workspaceFolder": workspace_folder,
        "workspaceName": workspace_name,
        "model": model,
        "timestamp": timestamp,
    }


def _user_msg(
    text: str = "Hello",
    message_id: str = "u1",
    timestamp: int = 1_700_000_001_000,
    files: list | None = None,
) -> dict:
    evt: dict = {"type": "user.message", "text": text, "messageId": message_id, "timestamp": timestamp}
    if files is not None:
        evt["files"] = files
    return evt


def _assistant_msg(
    text: str = "Hi there",
    message_id: str = "a1",
    timestamp: int = 1_700_000_002_000,
    model: str | None = None,
) -> dict:
    evt: dict = {"type": "assistant.message", "text": text, "messageId": message_id, "timestamp": timestamp}
    if model:
        evt["model"] = model
    return evt


def _tool_done() -> dict:
    return {"type": "tool.execution_complete", "toolId": "t1"}


def _model_change(model: str = "claude-3.5-sonnet", timestamp: int = 1_700_000_003_000) -> dict:
    return {"type": "session.model_change", "model": model, "timestamp": timestamp}


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------

class TestMakeWorkspaceId:
    def test_same_folder_gives_same_id(self):
        assert _make_workspace_id("/home/user/proj") == _make_workspace_id("/home/user/proj")

    def test_different_folders_give_different_ids(self):
        assert _make_workspace_id("/home/user/a") != _make_workspace_id("/home/user/b")

    def test_returns_16_hex_chars(self):
        ws_id = _make_workspace_id("/some/path")
        assert len(ws_id) == 16
        int(ws_id, 16)  # must be valid hex

    def test_backslash_normalised(self):
        # Windows vs POSIX path should produce the same ID after normalisation
        assert _make_workspace_id("C:\\Users\\dev\\proj") == _make_workspace_id("c:/Users/dev/proj")


class TestGetField:
    def test_returns_first_found(self):
        assert _get_field({"sessionId": "abc"}, "sessionId", "id") == "abc"

    def test_fallback_to_second_key(self):
        assert _get_field({"id": "xyz"}, "sessionId", "id") == "xyz"

    def test_returns_none_when_missing(self):
        assert _get_field({}, "sessionId", "id") is None

    def test_coerces_to_str(self):
        assert _get_field({"model": 42}, "model") == "42"


class TestParseTimestamp:
    def test_milliseconds_passthrough(self):
        assert _parse_timestamp(1_700_000_000_000) == 1_700_000_000_000

    def test_seconds_converted_to_ms(self):
        assert _parse_timestamp(1_700_000_000) == 1_700_000_000_000

    def test_none_input(self):
        assert _parse_timestamp(None) is None

    def test_string_digits(self):
        assert _parse_timestamp("1700000000000") == 1_700_000_000_000

    def test_non_numeric_string(self):
        assert _parse_timestamp("not-a-timestamp") is None


class TestMsToIso:
    def test_valid_timestamp(self):
        iso = _ms_to_iso(1_700_000_000_000)
        assert iso.startswith("2023-")
        assert "T" in iso

    def test_none_gives_empty_string(self):
        assert _ms_to_iso(None) == ""


class TestExtractFiles:
    def test_string_list(self):
        evt = {"files": ["/a/b.py", "/c/d.ts"]}
        result = _extract_files(evt)
        assert len(result) == 2

    def test_dict_list_with_path(self):
        evt = {"files": [{"path": "/a/file.py"}]}
        result = _extract_files(evt)
        assert result == ["/a/file.py"]

    def test_dict_list_with_uri(self):
        evt = {"files": [{"uri": "/a/file.py"}]}
        result = _extract_files(evt)
        assert result == ["/a/file.py"]

    def test_attachments_fallback(self):
        evt = {"attachments": ["/x/y.go"]}
        result = _extract_files(evt)
        assert result == ["/x/y.go"]

    def test_empty(self):
        assert _extract_files({}) == []


# ---------------------------------------------------------------------------
# scan_session_files
# ---------------------------------------------------------------------------

class TestScanSessionFiles:
    def test_finds_flat_jsonl(self, tmp_path):
        (tmp_path / "session-a.jsonl").write_text("{}\n")
        (tmp_path / "session-b.jsonl").write_text("{}\n")
        files = scan_session_files(tmp_path)
        names = {f.name for f in files}
        assert "session-a.jsonl" in names
        assert "session-b.jsonl" in names

    def test_finds_events_jsonl_in_subdir(self, tmp_path):
        subdir = tmp_path / "workspace-1"
        subdir.mkdir()
        (subdir / "events.jsonl").write_text("{}\n")
        files = scan_session_files(tmp_path)
        assert any(f.name == "events.jsonl" for f in files)

    def test_does_not_recurse_beyond_one_level(self, tmp_path):
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        (deep / "session.jsonl").write_text("{}\n")
        files = scan_session_files(tmp_path)
        assert not any("b" in str(f.parent) and f.name == "session.jsonl" for f in files)

    def test_missing_dir_returns_empty(self, tmp_path):
        assert scan_session_files(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# peek_session_meta
# ---------------------------------------------------------------------------

class TestPeekSessionMeta:
    def test_reads_session_start(self, tmp_path):
        path = _write_jsonl(
            tmp_path / "s1.jsonl",
            [_session_start("sid-1", "/home/user/proj", "proj", "gpt-4o", 1_700_000_000_000)],
        )
        meta = peek_session_meta(path)
        assert meta is not None
        assert meta.session_id == "sid-1"
        assert meta.workspace_name == "proj"
        assert meta.model == "gpt-4o"
        assert meta.started_at == 1_700_000_000_000
        assert meta.file_path == path

    def test_workspace_id_derived_from_folder(self, tmp_path):
        folder = "/home/user/my-project"
        path = _write_jsonl(tmp_path / "s.jsonl", [_session_start(workspace_folder=folder)])
        meta = peek_session_meta(path)
        assert meta is not None
        assert meta.workspace_id == _make_workspace_id(folder)

    def test_fallback_without_session_start(self, tmp_path):
        path = _write_jsonl(tmp_path / "orphan.jsonl", [_user_msg("hi")])
        meta = peek_session_meta(path)
        assert meta is not None
        # Fallback: session_id and workspace_id derived from file stem
        assert meta.session_id == "orphan"
        assert meta.file_path == path

    def test_events_jsonl_fallback_uses_parent_dir(self, tmp_path):
        subdir = tmp_path / "ws-abc"
        path = _write_jsonl(subdir / "events.jsonl", [_user_msg("hi")])
        meta = peek_session_meta(path)
        assert meta is not None
        assert meta.session_id == "ws-abc"
        assert meta.workspace_id == "ws-abc"

    def test_invalid_json_lines_skipped(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text("not-json\n" + json.dumps(_session_start("s99")) + "\n")
        meta = peek_session_meta(path)
        assert meta is not None
        assert meta.session_id == "s99"

    def test_empty_file_returns_fallback(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        meta = peek_session_meta(path)
        assert meta is not None
        assert meta.session_id == "empty"


# ---------------------------------------------------------------------------
# parse_session_events
# ---------------------------------------------------------------------------

class TestParseSessionEvents:
    def _meta(self, session_id: str = "s1", model: str = "gpt-4o") -> SessionMeta:
        folder = "/home/user/proj"
        return SessionMeta(
            session_id=session_id,
            workspace_id=_make_workspace_id(folder),
            workspace_name="proj",
            workspace_folder=folder,
            session_name="",
            model=model,
            started_at=None,
            file_path=Path("/fake/path.jsonl"),
        )

    def test_user_turn_created(self, tmp_path):
        path = _write_jsonl(tmp_path / "s.jsonl", [_user_msg("hello")])
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 1
        assert turns[0].role == "user"
        assert turns[0].original_text == "hello"

    def test_assistant_turn_created(self, tmp_path):
        path = _write_jsonl(tmp_path / "s.jsonl", [_assistant_msg("world")])
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 1
        assert turns[0].role == "assistant"
        assert turns[0].original_text == "world"

    def test_tool_execution_complete_skipped(self, tmp_path):
        path = _write_jsonl(tmp_path / "s.jsonl", [_tool_done(), _user_msg("hi")])
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 1  # tool event not counted
        assert turns[0].role == "user"

    def test_model_change_applies_to_later_assistant(self, tmp_path):
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _user_msg("q1"),
                _assistant_msg("a1"),                    # model: gpt-4o (initial)
                _model_change("claude-3.5-sonnet"),
                _user_msg("q2", message_id="u2"),
                _assistant_msg("a2", message_id="a2"),  # model: claude-3.5-sonnet
            ],
        )
        meta = self._meta(model="gpt-4o")
        turns = parse_session_events(path, meta)
        assert len(turns) == 4

        first_assistant = turns[1]
        assert first_assistant.model_id == "gpt-4o"

        second_assistant = turns[3]
        assert second_assistant.model_id == "claude-3.5-sonnet"

    def test_per_turn_model_overrides_tracked(self, tmp_path):
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [_assistant_msg("resp", model="o1-mini")],
        )
        meta = self._meta(model="gpt-4o")
        turns = parse_session_events(path, meta)
        assert turns[0].model_id == "o1-mini"

    def test_turn_index_increments(self, tmp_path):
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [_user_msg("q"), _assistant_msg("a"), _user_msg("q2", message_id="u2")],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert [t.turn for t in turns] == [0, 1, 2]

    def test_agent_used_field(self, tmp_path):
        path = _write_jsonl(tmp_path / "s.jsonl", [_user_msg("x")])
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert turns[0].agent_used == AGENT_NAME

    def test_files_attached_to_user_turn(self, tmp_path):
        path = _write_jsonl(tmp_path / "s.jsonl", [_user_msg("q", files=["/a/b.py"])])
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert "/a/b.py" in turns[0].files

    def test_timestamp_converted(self, tmp_path):
        ts_ms = 1_700_000_001_000
        path = _write_jsonl(tmp_path / "s.jsonl", [_user_msg(timestamp=ts_ms)])
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert turns[0].timestamp_ms == ts_ms
        assert turns[0].timestamp_iso is not None
        assert turns[0].timestamp_iso.startswith("2023-")

    def test_session_start_updates_model(self, tmp_path):
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _session_start(model="o1"),
                _assistant_msg("resp"),
            ],
        )
        # meta carries an older model; session.start in file should override it
        meta = self._meta(model="gpt-4")
        turns = parse_session_events(path, meta)
        assert turns[0].model_id == "o1"

    def test_empty_file_returns_no_turns(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        meta = self._meta()
        assert parse_session_events(path, meta) == []

    def test_invalid_json_lines_tolerated(self, tmp_path):
        path = tmp_path / "mixed.jsonl"
        path.write_text("not-json\n" + json.dumps(_user_msg("ok")) + "\n")
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 1


# ---------------------------------------------------------------------------
# CopilotCliExtractor
# ---------------------------------------------------------------------------

class TestCopilotCliExtractor:
    """Tests for the core extractor class using a temporary session-state dir."""

    def _make_extractor(self, session_state_dir: Path, workspace_id: str) -> CopilotCliExtractor:
        return CopilotCliExtractor(workspace_id=workspace_id, session_state_dir=session_state_dir)

    def _write_session(
        self,
        base: Path,
        filename: str,
        events: List[dict],
    ) -> Path:
        return _write_jsonl(base / filename, events)

    # ---- scan_workspaces ------------------------------------------------

    def test_scan_workspaces_empty_dir(self, tmp_path):
        ext = self._make_extractor(tmp_path, "any")
        assert ext.scan_workspaces() == []

    def test_scan_workspaces_returns_workspace_info(self, tmp_path):
        self._write_session(
            tmp_path,
            "sess.jsonl",
            [_session_start("s1", "/home/user/proj", "proj")],
        )
        ws_id = _make_workspace_id("/home/user/proj")
        ext = self._make_extractor(tmp_path, ws_id)
        workspaces = ext.scan_workspaces()
        assert len(workspaces) == 1
        ws = workspaces[0]
        assert ws.workspace_id == ws_id
        assert ws.workspace_name == "proj"
        assert AGENT_NAME in ws.agents
        assert ws.session_count == 1

    def test_scan_workspaces_groups_sessions_by_workspace(self, tmp_path):
        folder_a = "/home/user/alpha"
        folder_b = "/home/user/beta"
        # Two sessions in workspace A, one in B
        self._write_session(tmp_path, "a1.jsonl", [_session_start("s1", folder_a)])
        self._write_session(tmp_path, "a2.jsonl", [_session_start("s2", folder_a)])
        self._write_session(tmp_path, "b1.jsonl", [_session_start("s3", folder_b)])

        ext = self._make_extractor(tmp_path, "any")
        workspaces = ext.scan_workspaces()
        assert len(workspaces) == 2
        counts = {ws.workspace_id: ws.session_count for ws in workspaces}
        assert counts[_make_workspace_id(folder_a)] == 2
        assert counts[_make_workspace_id(folder_b)] == 1

    def test_scan_workspaces_finds_events_jsonl_pattern(self, tmp_path):
        subdir = tmp_path / "my-workspace"
        _write_jsonl(subdir / "events.jsonl", [_session_start("s1", "/home/p")])
        ext = self._make_extractor(tmp_path, "any")
        workspaces = ext.scan_workspaces()
        assert len(workspaces) == 1

    # ---- extract_sessions -----------------------------------------------

    def test_extract_sessions_returns_extracted_workspace(self, tmp_path):
        folder = "/home/user/proj"
        ws_id = _make_workspace_id(folder)
        self._write_session(
            tmp_path,
            "sess.jsonl",
            [
                _session_start("s1", folder),
                _user_msg("question"),
                _assistant_msg("answer"),
            ],
        )
        ext = self._make_extractor(tmp_path, ws_id)
        result = ext.extract_sessions()
        assert result.agent_name == AGENT_NAME
        assert result.workspace_id == ws_id
        assert result.session_count == 1
        assert result.turn_count == 2

    def test_extract_sessions_only_returns_this_workspace(self, tmp_path):
        folder_a = "/home/user/alpha"
        folder_b = "/home/user/beta"
        self._write_session(tmp_path, "a.jsonl", [_session_start("s1", folder_a), _user_msg("qa")])
        self._write_session(tmp_path, "b.jsonl", [_session_start("s2", folder_b), _user_msg("qb")])

        ws_id = _make_workspace_id(folder_a)
        ext = self._make_extractor(tmp_path, ws_id)
        result = ext.extract_sessions()
        assert result.turn_count == 1
        assert result.turns[0].original_text == "qa"

    def test_extract_sessions_empty_workspace(self, tmp_path):
        ext = self._make_extractor(tmp_path, "nonexistent-id")
        result = ext.extract_sessions()
        assert result.turn_count == 0
        assert result.session_count == 0

    def test_extract_sessions_model_tracking(self, tmp_path):
        folder = "/home/user/proj"
        ws_id = _make_workspace_id(folder)
        self._write_session(
            tmp_path,
            "sess.jsonl",
            [
                _session_start("s1", folder, model="gpt-4o"),
                _user_msg("q"),
                _assistant_msg("a"),
                _model_change("claude-3.5-sonnet"),
                _user_msg("q2", message_id="u2"),
                _assistant_msg("a2", message_id="a2"),
            ],
        )
        ext = self._make_extractor(tmp_path, ws_id)
        result = ext.extract_sessions()
        turns = result.turns
        assert turns[1].model_id == "gpt-4o"    # first assistant
        assert turns[3].model_id == "claude-3.5-sonnet"  # after model change

    # ---- get_latest_activity --------------------------------------------

    def test_get_latest_activity_returns_stats(self, tmp_path):
        folder = "/home/user/proj"
        ws_id = _make_workspace_id(folder)
        self._write_session(
            tmp_path,
            "sess.jsonl",
            [
                _session_start("s1", folder),
                _user_msg("q"),
                _assistant_msg("a"),
                _tool_done(),
            ],
        )
        ext = self._make_extractor(tmp_path, ws_id)
        activity = ext.get_latest_activity()
        assert activity is not None
        assert activity.session_count == 1
        assert activity.turn_count == 2  # tool event excluded
        assert "s1" in activity.session_ids

    def test_get_latest_activity_none_when_no_sessions(self, tmp_path):
        ext = self._make_extractor(tmp_path, "ghost-id")
        assert ext.get_latest_activity() is None

    # ---- cleanup --------------------------------------------------------

    def test_cleanup_is_noop(self, tmp_path):
        ext = self._make_extractor(tmp_path, "any")
        ext.cleanup()  # Should not raise


# ---------------------------------------------------------------------------
# Agent registry integration (class name convention)
# ---------------------------------------------------------------------------

class TestRegistryConvention:
    def test_agent_module_exposes_correct_class(self):
        from src.extract_plugins.copilot_cli import agent as agent_mod
        cls = getattr(agent_mod, "Copilot_CliExtractor", None)
        assert cls is not None, "Copilot_CliExtractor must be defined in agent.py"

    def test_class_has_correct_agent_name(self):
        from src.extract_plugins.copilot_cli.agent import Copilot_CliExtractor
        assert Copilot_CliExtractor.AGENT_NAME == "copilot_cli"

    def test_class_name_matches_registry_convention(self):
        """Registry builds class name as agent_name.title() + 'Extractor'."""
        agent_name = "copilot_cli"
        expected = f"{agent_name.title()}Extractor"  # 'Copilot_CliExtractor'
        from src.extract_plugins.copilot_cli import agent as agent_mod
        assert hasattr(agent_mod, expected), (
            f"agent.py must expose a class named '{expected}' for the registry"
        )


# ---------------------------------------------------------------------------
# Nested event format (Copilot CLI agent actual format)
# ---------------------------------------------------------------------------

def _nested_session_start(
    session_id: str = "sess-1",
    cwd: str = "/home/user/project",
    repository: str = "user/project",
    model: str = "gpt-4o",
    timestamp: str = "2026-03-27T14:55:38.603Z",
) -> dict:
    return {
        "type": "session.start",
        "data": {
            "sessionId": session_id,
            "context": {
                "cwd": cwd,
                "gitRoot": cwd,
                "repository": repository,
            },
        },
        "id": "some-id",
        "timestamp": timestamp,
    }


def _nested_user_msg(
    text: str = "Hello",
    timestamp: str = "2026-03-27T14:58:24.186Z",
) -> dict:
    return {
        "type": "user.message",
        "data": {"content": text, "attachments": []},
        "id": "uid-1",
        "timestamp": timestamp,
    }


def _nested_assistant_msg(
    text: str = "Hi there",
    timestamp: str = "2026-03-27T14:59:00.000Z",
) -> dict:
    return {
        "type": "assistant.message",
        "data": {"messageId": "mid-1", "content": text},
        "id": "aid-1",
        "timestamp": timestamp,
    }


def _nested_model_change(
    new_model: str = "claude-3.5-sonnet",
    timestamp: str = "2026-03-27T15:00:00.000Z",
) -> dict:
    return {
        "type": "session.model_change",
        "data": {"previousModel": "gpt-4o", "newModel": new_model},
        "id": "mc-1",
        "timestamp": timestamp,
    }


class TestNestedGetField:
    """Tests for _get_field with nested data/context structure."""

    def test_finds_flat_key(self):
        assert _get_field({"sessionId": "abc"}, "sessionId") == "abc"

    def test_finds_key_in_data(self):
        event = {"data": {"sessionId": "abc"}}
        assert _get_field(event, "sessionId") == "abc"

    def test_finds_key_in_context(self):
        event = {"data": {"context": {"cwd": "/home/proj"}}}
        assert _get_field(event, "cwd") == "/home/proj"

    def test_flat_takes_precedence_over_nested(self):
        event = {"sessionId": "flat", "data": {"sessionId": "nested"}}
        assert _get_field(event, "sessionId") == "flat"


class TestParseTimestampIso:
    """Tests for ISO-8601 string timestamps."""

    def test_iso_string_parsed(self):
        result = _parse_timestamp("2026-03-27T14:55:38.603Z")
        assert result is not None
        assert result > 0

    def test_iso_string_gives_correct_ms(self):
        result = _parse_timestamp("2023-11-14T22:13:20.000Z")
        assert result == 1_700_000_000_000


class TestPeekSessionMetaNested:
    """Tests for peek_session_meta with nested event format."""

    def test_nested_session_start(self, tmp_path):
        path = _write_jsonl(
            tmp_path / "s1.jsonl",
            [_nested_session_start("sid-1", "/home/user/proj", "user/proj")],
        )
        meta = peek_session_meta(path)
        assert meta is not None
        assert meta.session_id == "sid-1"
        assert meta.workspace_folder == "/home/user/proj"
        assert meta.workspace_name == "proj"
        assert meta.started_at is not None

    def test_nested_workspace_id_from_cwd(self, tmp_path):
        cwd = "/home/user/my-project"
        path = _write_jsonl(tmp_path / "s.jsonl", [_nested_session_start(cwd=cwd)])
        meta = peek_session_meta(path)
        assert meta is not None
        assert meta.workspace_id == _make_workspace_id(cwd)

    def test_nested_events_jsonl_pattern(self, tmp_path):
        subdir = tmp_path / "eed03988-72fa-4bde-ad6a-e93d73f90756"
        path = _write_jsonl(
            subdir / "events.jsonl",
            [_nested_session_start("sess", "/code/project", "org/project")],
        )
        meta = peek_session_meta(path)
        assert meta is not None
        assert meta.workspace_folder == "/code/project"
        assert meta.workspace_name == "project"

    def test_workspace_yaml_fallback(self, tmp_path):
        """When session.start has no workspace info, fallback reads workspace.yaml."""
        subdir = tmp_path / "abc-session"
        _write_jsonl(subdir / "events.jsonl", [_nested_user_msg("hi")])
        yaml_path = subdir / "workspace.yaml"
        yaml_path.write_text("cwd: /code/projects/myproj\nrepository: user/myproj\nsummary: Fix bug\n")
        meta = peek_session_meta(subdir / "events.jsonl")
        assert meta is not None
        assert meta.workspace_folder == "/code/projects/myproj"
        assert meta.workspace_name == "myproj"
        assert meta.session_name == "Fix bug"


class TestParseNestedEvents:
    """Tests for parse_session_events with nested event format."""

    def _meta(self, session_id: str = "s1", model: str = "gpt-4o") -> SessionMeta:
        folder = "/home/user/proj"
        return SessionMeta(
            session_id=session_id,
            workspace_id=_make_workspace_id(folder),
            workspace_name="proj",
            workspace_folder=folder,
            session_name="",
            model=model,
            started_at=None,
            file_path=Path("/fake/path.jsonl"),
        )

    def test_nested_user_message(self, tmp_path):
        path = _write_jsonl(tmp_path / "s.jsonl", [_nested_user_msg("hello nested")])
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 1
        assert turns[0].role == "user"
        assert turns[0].original_text == "hello nested"

    def test_nested_assistant_message(self, tmp_path):
        path = _write_jsonl(tmp_path / "s.jsonl", [_nested_assistant_msg("response")])
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 1
        assert turns[0].role == "assistant"
        assert turns[0].original_text == "response"

    def test_nested_model_change_tracked(self, tmp_path):
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _nested_user_msg("q1"),
                _nested_assistant_msg("a1"),
                _nested_model_change("claude-3.5-sonnet"),
                _nested_user_msg("q2"),
                _nested_assistant_msg("a2"),
            ],
        )
        meta = self._meta(model="gpt-4o")
        turns = parse_session_events(path, meta)
        assert turns[1].model_id == "gpt-4o"
        assert turns[3].model_id == "claude-3.5-sonnet"

    def test_nested_iso_timestamp(self, tmp_path):
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [_nested_user_msg("q", timestamp="2023-11-14T22:13:20.000Z")],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert turns[0].timestamp_ms == 1_700_000_000_000
        assert turns[0].timestamp_iso is not None
        assert turns[0].timestamp_iso.startswith("2023-")


# ---------------------------------------------------------------------------
# Turn merging – consecutive same-role messages
# ---------------------------------------------------------------------------


class TestTurnMerging:
    """Consecutive same-role messages must be merged into a single turn."""

    def _meta(self, session_id: str = "s1", model: str = "gpt-4o") -> SessionMeta:
        folder = "/home/user/proj"
        return SessionMeta(
            session_id=session_id,
            workspace_id=_make_workspace_id(folder),
            workspace_name="proj",
            workspace_folder=folder,
            session_name="",
            model=model,
            started_at=None,
            file_path=Path("/fake/path.jsonl"),
        )

    def test_consecutive_assistant_messages_merged(self, tmp_path):
        """Two back-to-back assistant messages should produce one turn."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _user_msg("question"),
                _assistant_msg("part 1"),
                _assistant_msg("part 2"),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[1].role == "assistant"
        assert "part 1" in turns[1].original_text
        assert "part 2" in turns[1].original_text

    def test_many_consecutive_assistant_messages_merged(self, tmp_path):
        """Multiple consecutive assistant messages collapse into one turn."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _user_msg("q"),
                _assistant_msg("a1"),
                _assistant_msg("a2"),
                _assistant_msg("a3"),
                _assistant_msg("a4"),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 2
        assert turns[1].role == "assistant"
        assert turns[1].original_text == "a1\n\na2\n\na3\n\na4"

    def test_consecutive_user_messages_merged(self, tmp_path):
        """Two back-to-back user messages should also be merged."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _user_msg("part 1"),
                _user_msg("part 2"),
                _assistant_msg("response"),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert "part 1" in turns[0].original_text
        assert "part 2" in turns[0].original_text
        assert turns[1].role == "assistant"

    def test_alternating_roles_not_merged(self, tmp_path):
        """Normal alternation should produce separate turns."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _user_msg("q1"),
                _assistant_msg("a1"),
                _user_msg("q2"),
                _assistant_msg("a2"),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 4
        assert [t.role for t in turns] == ["user", "assistant", "user", "assistant"]

    def test_merged_turn_indices_sequential(self, tmp_path):
        """Turn indices must be sequential after merging."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _user_msg("q"),
                _assistant_msg("a1"),
                _assistant_msg("a2"),
                _user_msg("q2"),
                _assistant_msg("a3"),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert [t.turn for t in turns] == [0, 1, 2, 3]
        assert [t.role for t in turns] == ["user", "assistant", "user", "assistant"]

    def test_merged_keeps_earliest_timestamp(self, tmp_path):
        """Merged turn should keep the earliest timestamp."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _assistant_msg("first", timestamp=1_700_000_005_000),
                _assistant_msg("second", timestamp=1_700_000_001_000),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 1
        assert turns[0].timestamp_ms == 1_700_000_001_000

    def test_merged_keeps_first_model(self, tmp_path):
        """Model ID from first message in merged group should be kept."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _assistant_msg("a1", model="gpt-4o"),
                _assistant_msg("a2", model="o1-mini"),
            ],
        )
        meta = self._meta(model="")
        turns = parse_session_events(path, meta)
        assert len(turns) == 1
        assert turns[0].model_id == "gpt-4o"

    def test_merged_user_files_combined(self, tmp_path):
        """Files from merged user messages should be combined."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _user_msg("q1", files=["/a/b.py"]),
                _user_msg("q2", files=["/c/d.py"]),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 1
        assert "/a/b.py" in turns[0].files
        assert "/c/d.py" in turns[0].files

    def test_nested_consecutive_assistant_merged(self, tmp_path):
        """Nested format consecutive assistant messages should also merge."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _nested_user_msg("question"),
                _nested_assistant_msg("reply part 1"),
                _nested_assistant_msg("reply part 2"),
                _nested_assistant_msg("reply part 3"),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[1].role == "assistant"
        assert turns[1].original_text == "reply part 1\n\nreply part 2\n\nreply part 3"

    def test_complex_merging_scenario(self, tmp_path):
        """Mixed roles with multiple consecutive groups."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _user_msg("u1"),
                _assistant_msg("a1"),
                _assistant_msg("a2"),
                _assistant_msg("a3"),
                _user_msg("u2"),
                _user_msg("u3"),
                _assistant_msg("a4"),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 4
        assert [t.role for t in turns] == ["user", "assistant", "user", "assistant"]
        assert turns[0].original_text == "u1"
        assert turns[1].original_text == "a1\n\na2\n\na3"
        assert turns[2].original_text == "u2\n\nu3"
        assert turns[3].original_text == "a4"

    def test_empty_text_in_consecutive_not_duplicated(self, tmp_path):
        """Empty-text messages in a consecutive group should not add blank lines."""
        path = _write_jsonl(
            tmp_path / "s.jsonl",
            [
                _assistant_msg("content"),
                _assistant_msg(""),
                _assistant_msg("more content"),
            ],
        )
        meta = self._meta()
        turns = parse_session_events(path, meta)
        assert len(turns) == 1
        assert turns[0].original_text == "content\n\nmore content"
