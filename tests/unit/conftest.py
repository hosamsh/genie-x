"""Pytest fixtures for genie-x tests.

This module provides test harness fixtures (Tier 0) including:
- T0-1: Temporary run directory fixture
- T0-2: Synthetic workspace fixtures  
- T0-3: CLI runner fixture
- T0-4: Web test client fixture
"""
import json
import os
import sqlite3
import subprocess
import sys
import yaml
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path for proper imports
# tests/unit/conftest.py -> parent.parent.parent = project root
_project_root = Path(__file__).parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import pytest


# Load environment variables from test.env if it exists
def _load_test_env():
    """Load environment variables from test.env file if present."""
    test_env_path = Path(__file__).parent / "test.env"
    if test_env_path.exists():
        with open(test_env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                # Parse KEY=VALUE format
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    # Only set if not already in environment
                    if key and value and key not in os.environ:
                        os.environ[key] = value


# Load test environment variables before any tests run
_load_test_env()


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests (require web server or heavy fixtures)"
    )


def get_test_db_path(run_dir: Path) -> Path:
    """Get the database path for a run directory in tests.
    
    Uses the centralized run_dir module to ensure tests use the
    same database filename as production code.
    
    Args:
        run_dir: Path to the run directory
        
    Returns:
        Path to the database file
    """
    from src.shared.io.run_dir import get_db_path
    return get_db_path(run_dir)


@pytest.fixture
def run_dir(tmp_path):
    """T0-1: Provide isolated run directory for each test.
    
    Returns:
        Path: Temporary run directory (auto-cleaned after test)
    """
    return tmp_path / "run"


@pytest.fixture
def copilot_workspace(tmp_path):
    """T0-2: Synthetic Copilot workspace fixture.
    
    Creates minimal valid Copilot workspace structure with:
    - workspace.json
    - chatSessions/<session_id>.json with searchable text
    - state.vscdb (SQLite) with session titles
    
    Returns:
        Dict with 'workspace_id', 'path', 'session_ids', 'workspace_folder'
    """
    workspace_id = "test-copilot-workspace-001"
    workspace_folder = str(tmp_path / "my-test-project")
    session_id = "session-abc-123"
    
    # Create workspace storage structure
    ws_path = tmp_path / "copilot_storage" / workspace_id
    ws_path.mkdir(parents=True)
    
    # Create workspace.json
    workspace_json = {
        "folder": f"file:///{workspace_folder.replace(chr(92), '/')}",
        "workspace": None
    }
    (ws_path / "workspace.json").write_text(json.dumps(workspace_json), encoding="utf-8")
    
    # Create chatSessions directory
    chat_dir = ws_path / "chatSessions"
    chat_dir.mkdir()
    
    # Create a session file with known searchable content
    session_data = {
        "version": 1,
        "requests": [
            {
                "requestId": "req-001",
                "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                "message": "Hello assistant, can you help with Python testing?",
                "modelId": "gpt-4",
                "response": [
                    {
                        "kind": "markdownContent",
                        "value": "Sure! I can help you with pytest testing."
                    }
                ]
            },
            {
                "requestId": "req-002",
                "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000) + 5000,
                "message": "Show me an example test function",
                "modelId": "gpt-4",
                "response": [
                    {
                        "kind": "markdownContent",
                        "value": "Here's a simple test:\n```python\ndef test_example():\n    assert 1 + 1 == 2\n```"
                    }
                ]
            }
        ]
    }
    (chat_dir / f"{session_id}.json").write_text(json.dumps(session_data), encoding="utf-8")
    
    # Create state.vscdb with session title
    db_path = ws_path / "state.vscdb"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    
    index_data = {
        "entries": {
            session_id: {
                "title": "Test Session Title"
            }
        }
    }
    conn.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        ("chat.ChatSessionStore.index", json.dumps(index_data))
    )
    conn.commit()
    conn.close()
    
    return {
        "workspace_id": workspace_id,
        "path": ws_path,
        "session_ids": [session_id],
        "workspace_folder": workspace_folder,
        "storage_root": tmp_path / "copilot_storage"
    }


@pytest.fixture
def copilot_workspace_with_edits(tmp_path):
    """T0-2 Extended: Synthetic Copilot workspace with chatEditingSessions for code metrics tests.
    
    Creates minimal valid Copilot workspace structure with:
    - workspace.json
    - chatSessions/<session_id>.json with searchable text (2+ exchanges for response_time_ms)
    - chatEditingSessions/<session_id>/state.json with file edits
    - state.vscdb (SQLite) with session titles
    
    Feature completeness checklist:
    | Feature aspect | Fixture provides | Test verifies |
    |----------------|------------------|---------------|
    | Chat messages  | ✅ chatSessions/*.json | ✅ turns exist |
    | Code edits     | ✅ chatEditingSessions/<session>/ | ✅ code_metrics rows |
    | Session titles | ✅ state.vscdb with index | ✅ session_name populated |
    | Multi-turn     | ✅ 2+ exchanges | ✅ response_time_ms calculated |
    
    Returns:
        Dict with 'workspace_id', 'path', 'session_ids', 'workspace_folder', 'storage_root'
    """
    workspace_id = "test-copilot-edits-workspace-001"
    workspace_folder = str(tmp_path / "my-edits-project")
    session_id = "edit-session-123"
    request_id_1 = "req-edit-001"
    request_id_2 = "req-edit-002"
    
    # Create workspace storage structure
    ws_path = tmp_path / "copilot_storage" / workspace_id
    ws_path.mkdir(parents=True)
    
    # Create workspace.json
    workspace_json = {
        "folder": f"file:///{workspace_folder.replace(chr(92), '/')}",
        "workspace": None
    }
    (ws_path / "workspace.json").write_text(json.dumps(workspace_json), encoding="utf-8")
    
    # Create chatSessions directory
    chat_dir = ws_path / "chatSessions"
    chat_dir.mkdir()
    
    # Create session file with multiple exchanges (for response_time_ms calculation)
    base_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    session_data = {
        "version": 1,
        "requests": [
            {
                "requestId": request_id_1,
                "timestamp": base_ts,
                "message": "Create a Python file with a function",
                "modelId": "gpt-4o",
                "response": [
                    {
                        "kind": "markdownContent",
                        "value": "I'll create a new Python file:\n```python\ndef hello():\n    return 'Hello World'\n```"
                    }
                ]
            },
            {
                "requestId": request_id_2,
                "timestamp": base_ts + 10000,  # 10 seconds later for response_time_ms calc
                "message": "Add a test function to the file",
                "modelId": "gpt-4o",
                "response": [
                    {
                        "kind": "markdownContent",
                        "value": "I'll add a test function:\n```python\ndef test_hello():\n    assert hello() == 'Hello World'\n```"
                    }
                ]
            }
        ]
    }
    (chat_dir / f"{session_id}.json").write_text(json.dumps(session_data), encoding="utf-8")
    
    # Create chatEditingSessions directory with code edits
    edits_dir = ws_path / "chatEditingSessions" / session_id
    edits_dir.mkdir(parents=True)
    contents_dir = edits_dir / "contents"
    contents_dir.mkdir()
    
    # Create before/after content files (content is stored by hash)
    before_content = ""  # Empty file initially
    after_content_1 = "def hello():\n    return 'Hello World'\n"
    after_content_2 = "def hello():\n    return 'Hello World'\n\ndef test_hello():\n    assert hello() == 'Hello World'\n"
    
    # Simple hash for empty file prefix
    empty_hash = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    after_hash_1 = "abc123hash1"
    after_hash_2 = "def456hash2"
    
    (contents_dir / empty_hash).write_text(before_content, encoding="utf-8")
    (contents_dir / after_hash_1).write_text(after_content_1, encoding="utf-8")
    (contents_dir / after_hash_2).write_text(after_content_2, encoding="utf-8")
    
    # Create state.json with file baselines
    file_uri = f"file:///{workspace_folder.replace(chr(92), '/')}/hello.py"
    state_data = {
        "initialFileContents": [
            [file_uri, empty_hash]
        ],
        "timeline": {
            "fileBaselines": [
                [f"{file_uri}::{request_id_1}", {"requestId": request_id_1, "epoch": 1, "content": empty_hash}],
                [f"{file_uri}::{request_id_2}", {"requestId": request_id_2, "epoch": 2, "content": after_hash_1}]
            ]
        },
        "recentSnapshot": {
            "entries": [
                {"resource": file_uri, "currentHash": after_hash_2}
            ]
        }
    }
    (edits_dir / "state.json").write_text(json.dumps(state_data), encoding="utf-8")
    
    # Create state.vscdb with session title
    db_path = ws_path / "state.vscdb"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    
    index_data = {
        "entries": {
            session_id: {
                "title": "Code Editing Session"
            }
        }
    }
    conn.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        ("chat.ChatSessionStore.index", json.dumps(index_data))
    )
    conn.commit()
    conn.close()
    
    return {
        "workspace_id": workspace_id,
        "path": ws_path,
        "session_ids": [session_id],
        "workspace_folder": workspace_folder,
        "storage_root": tmp_path / "copilot_storage"
    }


@pytest.fixture
def copilot_workspace_with_long_text(tmp_path):
    """Synthetic Copilot workspace with long/noisy text for TextShrinker tests (T1-7c).
    
    Creates a workspace with messages containing:
    - Repeated lines (for TextShrinker deduplication)
    - Long text (>500 chars to trigger shrinking)
    
    Returns:
        Dict with workspace info
    """
    workspace_id = "test-copilot-longtext-001"
    workspace_folder = str(tmp_path / "longtext-project")
    session_id = "longtext-session-001"
    
    # Create workspace storage structure
    ws_path = tmp_path / "copilot_storage" / workspace_id
    ws_path.mkdir(parents=True)
    
    # Create workspace.json
    workspace_json = {
        "folder": f"file:///{workspace_folder.replace(chr(92), '/')}",
        "workspace": None
    }
    (ws_path / "workspace.json").write_text(json.dumps(workspace_json), encoding="utf-8")
    
    # Create chatSessions directory
    chat_dir = ws_path / "chatSessions"
    chat_dir.mkdir()
    
    # Create long repetitive text (will trigger TextShrinker)
    repeated_line = "This is a repeated line that should be deduplicated. "
    long_repetitive_text = (repeated_line * 50)  # ~2500 chars of repetitive text
    
    session_data = {
        "version": 1,
        "requests": [
            {
                "requestId": "req-long-001",
                "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                "message": "Analyze this log output:\n" + long_repetitive_text,
                "modelId": "gpt-4",
                "response": [
                    {
                        "kind": "markdownContent",
                        "value": "I see the log has repetitive entries. " + long_repetitive_text
                    }
                ]
            }
        ]
    }
    (chat_dir / f"{session_id}.json").write_text(json.dumps(session_data), encoding="utf-8")
    
    # Create state.vscdb
    db_path = ws_path / "state.vscdb"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    index_data = {"entries": {session_id: {"title": "Long Text Session"}}}
    conn.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        ("chat.ChatSessionStore.index", json.dumps(index_data))
    )
    conn.commit()
    conn.close()
    
    return {
        "workspace_id": workspace_id,
        "path": ws_path,
        "session_ids": [session_id],
        "workspace_folder": workspace_folder,
        "storage_root": tmp_path / "copilot_storage",
        "expected_original_text": long_repetitive_text
    }

@pytest.fixture
def claude_workspace(tmp_path):
    """T0-2: Synthetic Claude Code workspace fixture.
    
    Creates minimal .claude directory with history.jsonl
    
    Returns:
        Dict with 'path', 'workspace_folder'
    """
    workspace_folder = tmp_path / "claude-project"
    workspace_folder.mkdir(parents=True)
    
    claude_dir = workspace_folder / ".claude"
    claude_dir.mkdir()
    
    # Create history.jsonl with messages
    session_id = "claude-session-001"
    messages = [
        {
            "role": "user",
            "content": "Hello Claude",
            "timestamp": datetime.now(timezone.utc).isoformat()
        },
        {
            "role": "assistant",
            "content": "Hello! How can I help?",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    ]
    
    history_path = claude_dir / f"{session_id}.jsonl"
    with open(history_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")
    
    return {
        "path": claude_dir,
        "workspace_folder": str(workspace_folder)
    }


@pytest.fixture
def make_test_config(tmp_path):
    """Factory fixture to create per-test config files.
    
    Returns:
        Callable that creates a config file with custom paths
    """
    def _make_config(
        copilot_storage=None,
        claude_dir=None,
        copilot_cli_session_state_dir=None,
    ) -> Path:
        """Create a test config file with specified paths.
        
        Args:
            copilot_storage: Path to copilot workspace storage
            claude_dir: Path to .claude directory
            copilot_cli_session_state_dir: Path to copilot CLI session-state dir
            
        Returns:
            Path to created config file
        """
        
        config = {
            "extract": {
                "copilot": {
                    "workspace_storage": str(copilot_storage) if copilot_storage else str(tmp_path / "empty_copilot")
                },
                "claude_code": {
                    "claude_dir": str(claude_dir) if claude_dir else str(tmp_path / "empty_claude")
                },
                "copilot_cli": {
                    "session_state_dir": str(copilot_cli_session_state_dir) if copilot_cli_session_state_dir else str(tmp_path / "empty_copilot_cli")
                }
            },
            "llm_models": {},
            "model_defaults": {"enabled": False},
            "pricing": {
                "default": {"input": 1.0, "output": 1.0},
                "models": {}
            }
        }
        
        config_path = tmp_path / "test_config.yaml"
        with open(config_path, 'w') as f:
            yaml.dump(config, f)
        
        return config_path
    
    return _make_config


@pytest.fixture
def cli_runner():
    """T0-3: CLI runner fixture for subprocess invocation.
    
    Returns:
        Callable that runs run_cli.py with given args and config_path
    """
    def run_cli(*args: str, config_path: Path) -> subprocess.CompletedProcess:
        """Run CLI with provided arguments.
        
        Args:
            *args: CLI arguments (without 'python run_cli.py')
            config_path: Path to test config file
            
        Returns:
            CompletedProcess with stdout, stderr, returncode
        """
        # tests/unit/conftest.py -> parent.parent.parent = project root
        project_root = Path(__file__).parent.parent.parent
        cmd = [sys.executable, str(project_root / "run_cli.py"), "--config", str(config_path), *args]
        
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_root)
        )
    
    return run_cli


@pytest.fixture
def web_client():
    """T0-4: Web test client fixture (placeholder for FastAPI TestClient).
    
    Note: This requires the web server dependencies to be available.
    Mark tests using this fixture with @pytest.mark.integration if needed.
    
    Returns:
        TestClient instance or None if dependencies unavailable
    """
    try:
        from fastapi.testclient import TestClient
        from src.web.app import create_app
        
        app = create_app()
        return TestClient(app)
    except ImportError:
        return None
