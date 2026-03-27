"""Unit tests for VSCode Copilot JSONL support and global session discovery."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


from src.extract_plugins.copilot.extractor import (
    WorkspaceMeta,
    _is_empty_session,
    _parse_jsonl_session,
    _reconstruct_delta_session,
    _resolve_session_files,
    discover_global_sessions,
    extract_session,
    extract_workspace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(request_id: str = "req-1", ts_offset: int = 0) -> dict:
    ts = int(datetime.now(timezone.utc).timestamp() * 1000) + ts_offset
    return {
        "requestId": request_id,
        "timestamp": ts,
        "message": {"text": f"User message for {request_id}"},
        "modelId": "gpt-4o",
        "response": [{"kind": "markdownContent", "value": f"Assistant reply for {request_id}"}],
    }


def _make_meta(tmp_path: Path, workspace_id: str = "ws-001") -> WorkspaceMeta:
    return WorkspaceMeta(
        workspace_id=workspace_id,
        workspace_name="test-project",
        workspace_folder=str(tmp_path / "project"),
        path=tmp_path / "storage" / workspace_id,
        titles={},
    )


# =============================================================================
# JSONL parsing
# =============================================================================

class TestParseJsonlSession:
    """_parse_jsonl_session converts JSONL lines into the standard session dict."""

    def test_basic_two_requests(self, tmp_path):
        req1 = _make_request("r1", 0)
        req2 = _make_request("r2", 5000)
        jsonl_path = tmp_path / "session.jsonl"
        jsonl_path.write_text(
            json.dumps(req1) + "\n" + json.dumps(req2) + "\n",
            encoding="utf-8",
        )
        result = _parse_jsonl_session(jsonl_path)
        assert "requests" in result
        assert len(result["requests"]) == 2
        assert result["requests"][0]["requestId"] == "r1"
        assert result["requests"][1]["requestId"] == "r2"

    def test_session_metadata_header(self, tmp_path):
        header = {"version": 1, "customTitle": "My Session"}
        req = _make_request("r1")
        jsonl_path = tmp_path / "session.jsonl"
        jsonl_path.write_text(
            json.dumps(header) + "\n" + json.dumps(req) + "\n",
            encoding="utf-8",
        )
        result = _parse_jsonl_session(jsonl_path)
        assert result.get("version") == 1
        assert result.get("customTitle") == "My Session"
        assert len(result["requests"]) == 1

    def test_empty_file_returns_empty_requests(self, tmp_path):
        jsonl_path = tmp_path / "empty.jsonl"
        jsonl_path.write_text("", encoding="utf-8")
        result = _parse_jsonl_session(jsonl_path)
        assert result.get("requests") == []

    def test_blank_lines_skipped(self, tmp_path):
        req = _make_request("r1")
        jsonl_path = tmp_path / "session.jsonl"
        jsonl_path.write_text(
            "\n\n" + json.dumps(req) + "\n\n",
            encoding="utf-8",
        )
        result = _parse_jsonl_session(jsonl_path)
        assert len(result["requests"]) == 1

    def test_malformed_lines_skipped(self, tmp_path):
        req = _make_request("r1")
        jsonl_path = tmp_path / "session.jsonl"
        jsonl_path.write_text(
            "not-json\n" + json.dumps(req) + "\n{broken",
            encoding="utf-8",
        )
        result = _parse_jsonl_session(jsonl_path)
        assert len(result["requests"]) == 1

    def test_nonexistent_file_returns_empty(self, tmp_path):
        result = _parse_jsonl_session(tmp_path / "ghost.jsonl")
        assert result == {}


# =============================================================================
# Delta format (VS Code state-store) reconstruction
# =============================================================================

def _delta_snapshot(initial_state: dict) -> dict:
    """Build a kind=0 initial snapshot line."""
    return {"kind": 0, "v": initial_state}


def _delta_set(key_path: list, value: object) -> dict:
    """Build a kind=1 set-at-path line."""
    return {"kind": 1, "k": key_path, "v": value}


def _delta_append(key_path: list, items: list) -> dict:
    """Build a kind=2 append-to-array line."""
    return {"kind": 2, "k": key_path, "v": items}


class TestReconstructDeltaSession:
    """_reconstruct_delta_session rebuilds session state from delta operations."""

    def test_single_request(self):
        lines = [
            _delta_snapshot({"version": 1, "requests": [], "sessionId": "s1"}),
            _delta_append(["requests"], [{
                "requestId": "r1",
                "message": {"text": "Hello"},
                "modelId": "gpt-4o",
                "response": [],
                "timestamp": 1700000000000,
            }]),
            _delta_append(["requests", 0, "response"], [
                {"kind": "markdownContent", "value": "Hi there!"},
            ]),
            _delta_set(["requests", 0, "result"], {"timings": {"totalElapsed": 500}}),
        ]
        result = _reconstruct_delta_session(lines)

        assert result["version"] == 1
        assert len(result["requests"]) == 1
        req = result["requests"][0]
        assert req["requestId"] == "r1"
        assert req["message"]["text"] == "Hello"
        assert req["modelId"] == "gpt-4o"
        assert len(req["response"]) == 1
        assert req["response"][0]["value"] == "Hi there!"
        assert req["result"]["timings"]["totalElapsed"] == 500

    def test_multiple_requests(self):
        lines = [
            _delta_snapshot({"requests": []}),
            _delta_append(["requests"], [{
                "requestId": "r1",
                "message": {"text": "First"},
                "response": [],
                "timestamp": 1700000000000,
            }]),
            _delta_append(["requests", 0, "response"], [
                {"kind": "markdownContent", "value": "Reply 1"},
            ]),
            _delta_append(["requests"], [{
                "requestId": "r2",
                "message": {"text": "Second"},
                "response": [],
                "timestamp": 1700000005000,
            }]),
            _delta_append(["requests", 1, "response"], [
                {"kind": "markdownContent", "value": "Reply 2"},
            ]),
        ]
        result = _reconstruct_delta_session(lines)

        assert len(result["requests"]) == 2
        assert result["requests"][0]["message"]["text"] == "First"
        assert result["requests"][1]["message"]["text"] == "Second"
        assert result["requests"][1]["response"][0]["value"] == "Reply 2"

    def test_initial_state_with_existing_requests(self):
        """Initial snapshot can already contain populated requests."""
        lines = [
            _delta_snapshot({"requests": [
                {"requestId": "r0", "message": {"text": "Pre-existing"}, "response": [
                    {"kind": "markdownContent", "value": "Old reply"},
                ]},
            ]}),
            _delta_append(["requests"], [{
                "requestId": "r1",
                "message": {"text": "New"},
                "response": [],
            }]),
            _delta_append(["requests", 1, "response"], [
                {"kind": "markdownContent", "value": "New reply"},
            ]),
        ]
        result = _reconstruct_delta_session(lines)

        assert len(result["requests"]) == 2
        assert result["requests"][0]["message"]["text"] == "Pre-existing"
        assert result["requests"][1]["message"]["text"] == "New"

    def test_set_overwrites_value(self):
        lines = [
            _delta_snapshot({"requests": [
                {"requestId": "r1", "modelId": "gpt-3.5", "response": []},
            ]}),
            _delta_set(["requests", 0, "modelId"], "gpt-4o"),
        ]
        result = _reconstruct_delta_session(lines)
        assert result["requests"][0]["modelId"] == "gpt-4o"

    def test_empty_snapshot_returns_empty_requests(self):
        lines = [_delta_snapshot({"version": 1, "requests": []})]
        result = _reconstruct_delta_session(lines)
        assert result["requests"] == []

    def test_missing_requests_key_added(self):
        lines = [_delta_snapshot({"version": 1})]
        result = _reconstruct_delta_session(lines)
        assert result["requests"] == []

    def test_multiple_response_appends(self):
        """Response items can be appended in multiple batches."""
        lines = [
            _delta_snapshot({"requests": [
                {"requestId": "r1", "message": {"text": "Q"}, "response": []},
            ]}),
            _delta_append(["requests", 0, "response"], [
                {"kind": "markdownContent", "value": "Part 1"},
            ]),
            _delta_append(["requests", 0, "response"], [
                {"kind": "markdownContent", "value": "Part 2"},
                {"kind": "markdownContent", "value": "Part 3"},
            ]),
        ]
        result = _reconstruct_delta_session(lines)
        resp = result["requests"][0]["response"]
        assert len(resp) == 3
        assert [r["value"] for r in resp] == ["Part 1", "Part 2", "Part 3"]


class TestParseJsonlDeltaFormat:
    """_parse_jsonl_session detects and handles delta-format JSONL files."""

    def test_delta_format_detected_and_parsed(self, tmp_path):
        lines = [
            _delta_snapshot({"version": 1, "requests": []}),
            _delta_append(["requests"], [{
                "requestId": "r1",
                "message": {"text": "Hello delta"},
                "modelId": "gpt-4o",
                "response": [],
                "timestamp": 1700000000000,
            }]),
            _delta_append(["requests", 0, "response"], [
                {"kind": "markdownContent", "value": "Delta reply"},
            ]),
        ]
        jsonl_path = tmp_path / "delta-session.jsonl"
        jsonl_path.write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n",
            encoding="utf-8",
        )
        result = _parse_jsonl_session(jsonl_path)
        assert len(result["requests"]) == 1
        assert result["requests"][0]["message"]["text"] == "Hello delta"
        assert result["requests"][0]["response"][0]["value"] == "Delta reply"

    def test_legacy_format_still_works(self, tmp_path):
        """Request-per-line format continues to work."""
        req = _make_request("r1")
        jsonl_path = tmp_path / "legacy.jsonl"
        jsonl_path.write_text(json.dumps(req) + "\n", encoding="utf-8")
        result = _parse_jsonl_session(jsonl_path)
        assert len(result["requests"]) == 1
        assert result["requests"][0]["requestId"] == "r1"


class TestExtractSessionDeltaJsonl:
    """extract_session produces correct Turn objects from delta-format .jsonl files."""

    def test_turns_from_delta_session(self, tmp_path):
        ws_path = tmp_path / "storage" / "ws-delta"
        ws_path.mkdir(parents=True)
        chat_dir = ws_path / "chatSessions"
        chat_dir.mkdir()

        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        lines = [
            _delta_snapshot({"version": 1, "requests": []}),
            _delta_append(["requests"], [{
                "requestId": "delta-req-1",
                "message": {"text": "What is Python?"},
                "modelId": "copilot/gpt-5-mini",
                "response": [],
                "timestamp": ts,
            }]),
            _delta_append(["requests", 0, "response"], [
                {"kind": "markdownContent", "value": "Python is a programming language."},
            ]),
            _delta_set(["requests", 0, "result"], {"timings": {"totalElapsed": 1200}}),
        ]

        session_id = "delta-test-session"
        session_file = chat_dir / f"{session_id}.jsonl"
        session_file.write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n",
            encoding="utf-8",
        )

        meta = WorkspaceMeta(
            workspace_id="ws-delta",
            workspace_name="test",
            workspace_folder="",
            path=ws_path,
            titles={},
        )
        turns = extract_session(session_file, meta)
        assert len(turns) == 2  # user + assistant

        user_turn = turns[0]
        assert user_turn.role == "user"
        assert user_turn.original_text == "What is Python?"
        assert user_turn.model_id == "copilot/gpt-5-mini"

        asst_turn = turns[1]
        assert asst_turn.role == "assistant"
        assert "Python is a programming language" in asst_turn.original_text
        assert asst_turn.extra.get("response_time_ms") == 1200
# =============================================================================
# _is_empty_session for JSONL
# =============================================================================

class TestIsEmptySessionJsonl:
    """_is_empty_session correctly classifies JSONL files."""

    def test_non_empty_jsonl(self, tmp_path):
        req = _make_request("r1")
        p = tmp_path / "s.jsonl"
        p.write_text(json.dumps(req) + "\n", encoding="utf-8")
        assert _is_empty_session(p) is False

    def test_empty_jsonl(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_text("", encoding="utf-8")
        assert _is_empty_session(p) is True

    def test_whitespace_only_jsonl(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_text("\n   \n\t\n", encoding="utf-8")
        assert _is_empty_session(p) is True


# =============================================================================
# _resolve_session_files – .jsonl preferred over .json
# =============================================================================

class TestResolveSessionFiles:
    """Verify that .jsonl is returned in preference to .json for the same stem."""

    def test_jsonl_preferred_over_json(self, tmp_path):
        stem = "abc-session"
        (tmp_path / f"{stem}.json").write_text("{}", encoding="utf-8")
        (tmp_path / f"{stem}.jsonl").write_text("{}", encoding="utf-8")

        files = _resolve_session_files(tmp_path)
        assert len(files) == 1
        assert files[0].suffix == ".jsonl"

    def test_json_only_returned(self, tmp_path):
        (tmp_path / "session.json").write_text("{}", encoding="utf-8")
        files = _resolve_session_files(tmp_path)
        assert len(files) == 1
        assert files[0].suffix == ".json"

    def test_jsonl_only_returned(self, tmp_path):
        (tmp_path / "session.jsonl").write_text("{}", encoding="utf-8")
        files = _resolve_session_files(tmp_path)
        assert len(files) == 1
        assert files[0].suffix == ".jsonl"

    def test_mixed_stems_returned(self, tmp_path):
        # s1 has both formats → jsonl wins; s2 only json; s3 only jsonl
        (tmp_path / "s1.json").write_text("{}", encoding="utf-8")
        (tmp_path / "s1.jsonl").write_text("{}", encoding="utf-8")
        (tmp_path / "s2.json").write_text("{}", encoding="utf-8")
        (tmp_path / "s3.jsonl").write_text("{}", encoding="utf-8")

        files = _resolve_session_files(tmp_path)
        stems = {f.stem: f.suffix for f in files}
        assert stems["s1"] == ".jsonl"
        assert stems["s2"] == ".json"
        assert stems["s3"] == ".jsonl"

    def test_empty_dir_returns_empty(self, tmp_path):
        assert _resolve_session_files(tmp_path) == []


# =============================================================================
# extract_session with JSONL input
# =============================================================================

class TestExtractSessionJsonl:
    """extract_session produces correct Turn objects from a .jsonl file."""

    def test_turns_extracted(self, tmp_path):
        ws_path = tmp_path / "storage" / "ws-jsonl"
        ws_path.mkdir(parents=True)
        chat_dir = ws_path / "chatSessions"
        chat_dir.mkdir()

        req = _make_request("req-jsonl-1")
        session_id = "jsonl-session-abc"
        session_file = chat_dir / f"{session_id}.jsonl"
        session_file.write_text(json.dumps(req) + "\n", encoding="utf-8")

        meta = WorkspaceMeta(
            workspace_id="ws-jsonl",
            workspace_name="test",
            workspace_folder="",
            path=ws_path,
            titles={},
        )
        turns = extract_session(session_file, meta)
        assert len(turns) == 2  # user + assistant
        user_turn = turns[0]
        assert user_turn.role == "user"
        assert user_turn.session_id == session_id
        assert "User message" in user_turn.original_text

    def test_empty_jsonl_returns_no_turns(self, tmp_path):
        ws_path = tmp_path / "storage" / "ws-empty"
        ws_path.mkdir(parents=True)
        chat_dir = ws_path / "chatSessions"
        chat_dir.mkdir()

        session_file = chat_dir / "empty-session.jsonl"
        session_file.write_text("", encoding="utf-8")

        meta = WorkspaceMeta(
            workspace_id="ws-empty",
            workspace_name="test",
            workspace_folder="",
            path=ws_path,
            titles={},
        )
        turns = extract_session(session_file, meta)
        assert turns == []


# =============================================================================
# discover_global_sessions
# =============================================================================

class TestDiscoverGlobalSessions:
    """discover_global_sessions finds emptyWindow and transferred sessions."""

    def _write_session(self, chat_dir: Path, session_id: str):
        chat_dir.mkdir(parents=True, exist_ok=True)
        req = _make_request(session_id)
        (chat_dir / f"{session_id}.jsonl").write_text(
            json.dumps(req) + "\n", encoding="utf-8"
        )

    def test_discovers_empty_window_sessions(self, tmp_path):
        global_storage = tmp_path / "globalStorage"
        chat_dir = global_storage / "emptyWindowChatSessions"
        self._write_session(chat_dir, "ew-session-001")

        metas = discover_global_sessions(base=global_storage)
        assert len(metas) == 1
        assert metas[0].workspace_id == "globalStorage/emptyWindowChatSessions"
        assert metas[0].chat_sessions_dir == chat_dir

    def test_discovers_transferred_sessions(self, tmp_path):
        global_storage = tmp_path / "globalStorage"
        chat_dir = global_storage / "transferredChatSessions"
        self._write_session(chat_dir, "tr-session-001")

        metas = discover_global_sessions(base=global_storage)
        assert len(metas) == 1
        assert metas[0].workspace_id == "globalStorage/transferredChatSessions"

    def test_discovers_both_subdirs(self, tmp_path):
        global_storage = tmp_path / "globalStorage"
        self._write_session(global_storage / "emptyWindowChatSessions", "ew-1")
        self._write_session(global_storage / "transferredChatSessions", "tr-1")

        metas = discover_global_sessions(base=global_storage)
        ids = {m.workspace_id for m in metas}
        assert "globalStorage/emptyWindowChatSessions" in ids
        assert "globalStorage/transferredChatSessions" in ids

    def test_skips_empty_subdirs(self, tmp_path):
        global_storage = tmp_path / "globalStorage"
        (global_storage / "emptyWindowChatSessions").mkdir(parents=True)

        metas = discover_global_sessions(base=global_storage)
        assert metas == []

    def test_nonexistent_base_returns_empty(self, tmp_path):
        metas = discover_global_sessions(base=tmp_path / "nonexistent")
        assert metas == []


# =============================================================================
# extract_workspace with chat_sessions_dir override (global sessions)
# =============================================================================

class TestExtractWorkspaceGlobal:
    """extract_workspace respects chat_sessions_dir from global sessions."""

    def test_extracts_from_global_chat_dir(self, tmp_path):
        global_storage = tmp_path / "globalStorage"
        chat_dir = global_storage / "emptyWindowChatSessions"
        chat_dir.mkdir(parents=True)

        req = _make_request("global-req-1")
        (chat_dir / "global-session-001.jsonl").write_text(
            json.dumps(req) + "\n", encoding="utf-8"
        )

        meta = WorkspaceMeta(
            workspace_id="globalStorage/emptyWindowChatSessions",
            workspace_name="emptyWindowChatSessions",
            workspace_folder="",
            path=global_storage,
            titles={},
            chat_sessions_dir=chat_dir,
        )
        turns = extract_workspace(meta)
        assert len(turns) == 2  # user + assistant
