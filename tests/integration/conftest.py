"""Shared test configuration and fixtures for integration tests."""

import json
import sqlite3
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yaml

import pytest

from src.shared.io.run_dir import get_db_path


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent.parent


def run_cli_command(args: list, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Run a CLI command and return the result."""
    if cwd is None:
        cwd = get_project_root()
    
    cmd = [sys.executable, "run_cli.py"] + args
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False
    )
    return result


@pytest.fixture
def test_run_dir():
    """Provide a test run directory."""
    run_dir = get_project_root() / "data" / "int-test"
    
    # Clean up before test
    if run_dir.exists():
        shutil.rmtree(run_dir)
    
    run_dir.mkdir(parents=True, exist_ok=True)
    
    yield run_dir
    
    # Keep the directory for inspection, but could clean up if needed
    # shutil.rmtree(run_dir)


def get_db_connection(run_dir: Path) -> sqlite3.Connection:
    """Get a connection to the test database."""
    db_path = get_db_path(run_dir)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def query_db(run_dir: Path, sql: str, params: tuple = ()) -> list:
    """Execute a query against the test database."""
    conn = get_db_connection(run_dir)
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        conn.close()


def query_db_single(run_dir: Path, sql: str, params: tuple = ()) -> Optional[Any]:
    """Execute a query and return a single result."""
    results = query_db(run_dir, sql, params)
    return results[0] if results else None


def count_table_rows(run_dir: Path, table: str, where: str = "") -> int:
    """Count rows in a table."""
    sql = f"SELECT COUNT(*) as count FROM {table}"
    if where:
        sql += f" WHERE {where}"
    result = query_db_single(run_dir, sql)
    return result[0] if result else 0


def clear_table(run_dir: Path, table: str) -> None:
    """Clear all rows from a table."""
    conn = get_db_connection(run_dir)
    try:
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()


def delete_db(run_dir: Path) -> None:
    """Delete the database file."""
    db_path = get_db_path(run_dir)
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Provide isolated run directory for each test."""
    return tmp_path / "run"


@pytest.fixture
def copilot_workspace(tmp_path: Path) -> Dict[str, Any]:
    """Synthetic Copilot workspace fixture."""
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
def make_test_config(tmp_path: Path) -> Callable[..., Path]:
    """Factory fixture to create per-test config files."""
    def _make_config(
        copilot_storage: Optional[Path] = None,
        claude_dir: Optional[Path] = None,
        copilot_cli_session_state_dir: Optional[Path] = None,
    ) -> Path:
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
def cli_runner() -> Callable[..., subprocess.CompletedProcess]:
    """CLI runner fixture for subprocess invocation."""
    def run_cli(*args: str, config_path: Path) -> subprocess.CompletedProcess:
        project_root = Path(__file__).parent.parent.parent
        cmd = [sys.executable, str(project_root / "run_cli.py"), "--config", str(config_path), *args]

        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            check=False
        )

    return run_cli


@pytest.fixture
def web_client() -> Any:
    """Web test client fixture (FastAPI TestClient if available)."""
    try:
        from fastapi.testclient import TestClient
        from src.web.app import create_app

        app = create_app()
        return TestClient(app)
    except ImportError:
        return None
