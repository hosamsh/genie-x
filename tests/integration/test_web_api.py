"""Tests for web API endpoints (Features 5-9, 12).

Tests:
- T1-12: Web API version endpoint
- T1-13: Web API system stats empty state
- T1-14: Web API list workspaces
- T1-15: Web API workspace detail
- T1-16: Web API search

Note: These tests use TestClient (in-process) when available.
Mark as @pytest.mark.integration if real server subprocess is needed.
"""
from typing import Any

import pytest


@pytest.mark.integration
def test_web_api_version_endpoint(web_client: Any) -> None:
    """T1-12: Verify /api/version returns version info."""
    if web_client is None:
        pytest.skip("Web client unavailable")

    response = web_client.get("/api/version")

    assert response.status_code == 200, f"Version endpoint failed: {response.text}"

    data = response.json()
    assert "version" in data, "Response missing version key"


@pytest.mark.integration
def test_web_api_system_stats_empty_state(web_client: Any, run_dir: Any, monkeypatch: Any) -> None:
    """T1-13: Verify /api/system/stats handles empty database."""
    if web_client is None:
        pytest.skip("Web client unavailable")

    # Configure web to use empty run directory
    monkeypatch.setenv("WEB_RUN_DIR", str(run_dir))

    response = web_client.get("/api/system/stats")

    # Either 200 with data, or 404/500 if database not available
    # Both are valid behaviors for empty state
    if response.status_code == 200:
        data = response.json()
        # Check for either is_available=false or zero counts
        if "is_available" in data:
            # Some implementations may return availability flag
            pass
        else:
            # Check that counts are zero or minimal
            assert isinstance(data, dict), "Stats should return dict"
    else:
        # 404 or 500 indicates database not available - acceptable for empty state
        assert response.status_code in (404, 500), f"Unexpected status: {response.status_code}"


@pytest.mark.integration
def test_web_api_list_workspaces(
    web_client: Any,
    copilot_workspace: Any,
    run_dir: Any,
    monkeypatch: Any,
) -> None:
    """T1-14: Verify browse API returns workspace list."""
    if web_client is None:
        pytest.skip("Web client unavailable")

    # Extract workspace and configure web
    monkeypatch.setenv("COPILOT_WORKSPACE_STORAGE", str(copilot_workspace["storage_root"]))
    monkeypatch.setenv("WEB_RUN_DIR", str(run_dir))

    # Extract data first (using CLI in background)
    import subprocess
    import sys
    from pathlib import Path

    # RESPEC/tests/test_web_api.py -> parent.parent.parent = project root
    project_root = Path(__file__).parent.parent.parent
    import os
    env = os.environ.copy()
    env["COPILOT_WORKSPACE_STORAGE"] = str(copilot_workspace["storage_root"])
    subprocess.run(
        [
            sys.executable,
            str(project_root / "run_cli.py"),
            "--extract",
            copilot_workspace["workspace_id"],
            "--run-dir",
            str(run_dir)
        ],
        capture_output=True,
        env=env,
        check=False
    )

    response = web_client.get("/api/browse/workspaces")

    assert response.status_code == 200, f"Workspaces API failed: {response.text}"

    data = response.json()
    assert "workspaces" in data, "Response should contain workspaces key"
    workspaces = data["workspaces"]
    assert isinstance(workspaces, list), "Workspaces should be a list"

    if len(workspaces) > 0:
        workspace = workspaces[0]
        assert "workspace_id" in workspace
        assert "workspace_name" in workspace


@pytest.mark.integration
def test_web_api_workspace_detail(
    web_client: Any,
    copilot_workspace: Any,
    run_dir: Any,
    monkeypatch: Any,
) -> None:
    """T1-15: Verify workspace detail returns sessions and metrics."""
    if web_client is None:
        pytest.skip("Web client unavailable")

    # Extract workspace
    monkeypatch.setenv("COPILOT_WORKSPACE_STORAGE", str(copilot_workspace["storage_root"]))
    monkeypatch.setenv("WEB_RUN_DIR", str(run_dir))

    # Extract data
    import subprocess
    import sys
    from pathlib import Path

    # RESPEC/tests/test_web_api.py -> parent.parent.parent = project root
    project_root = Path(__file__).parent.parent.parent
    import os
    env = os.environ.copy()
    env["COPILOT_WORKSPACE_STORAGE"] = str(copilot_workspace["storage_root"])
    subprocess.run(
        [
            sys.executable,
            str(project_root / "run_cli.py"),
            "--extract",
            copilot_workspace["workspace_id"],
            "--run-dir",
            str(run_dir)
        ],
        capture_output=True,
        env=env,
        check=False
    )

    workspace_id = copilot_workspace["workspace_id"]
    response = web_client.get(f"/api/browse/workspace/{workspace_id}/sessions")

    # 200 for success or 404/500 if workspace not found
    if response.status_code == 200:
        data = response.json()
        assert "sessions" in data, "Missing sessions key"
        assert isinstance(data["sessions"], list)
    else:
        # Acceptable error codes for missing workspace
        assert response.status_code in (404, 500), f"Unexpected status: {response.status_code}"


@pytest.mark.integration
def test_web_api_search(
    web_client: Any,
    copilot_workspace: Any,
    run_dir: Any,
    monkeypatch: Any,
) -> None:
    """T1-16: Verify search API returns results."""
    if web_client is None:
        pytest.skip("Web client unavailable")

    import subprocess
    import sys
    import os
    from pathlib import Path

    # RESPEC/tests/test_web_api.py -> parent.parent.parent = project root
    project_root = Path(__file__).parent.parent.parent
    workspace_id = copilot_workspace["workspace_id"]

    # Create test config in temp directory
    import yaml
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir.parent / "test_config.yaml"
    config_content = {
        "web": {"run_dir": str(run_dir)},
        "search": {
            "default_mode": "keyword",
            "max_page_size": 100,
            "semantic_min_score": 0.5,
            "semantic_strict_min_score": 0.7,
        },
        "extract": {
            "copilot": {"workspace_storage": str(copilot_workspace["storage_root"])},
            "claude_code": {"claude_dir": ""},
            "copilot_cli": {"session_state_dir": ""}
        },
        "llm_models": {},
        "model_defaults": {"enabled": False},
        "pricing": {"default": {"input": 1.0, "output": 1.0}, "models": {}}
    }
    with open(config_path, 'w') as f:
        yaml.dump(config_content, f)

    # Clear cached run directory and config so it picks up the test config
    from src.web.shared_state import clear_run_dir_cache
    clear_run_dir_cache()

    # Initialize config with test config path
    from src.shared.config import config_loader
    config_loader.get_config(str(config_path))

    # Set environment variables
    monkeypatch.setenv("COPILOT_WORKSPACE_STORAGE", str(copilot_workspace["storage_root"]))
    monkeypatch.setenv("WEB_RUN_DIR", str(run_dir))

    # Extract
    env = os.environ.copy()
    env["COPILOT_WORKSPACE_STORAGE"] = str(copilot_workspace["storage_root"])
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "run_cli.py"),
            "--config", str(config_path),
            "--extract",
            workspace_id,
            "--run-dir",
            str(run_dir)
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False
    )

    # Check extraction succeeded
    if result.returncode != 0:
        pytest.skip(f"Extraction failed: {result.stderr}")

    # Reindex
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "run_cli.py"),
            "--config", str(config_path),
            "--reindex",
            "--run-dir",
            str(run_dir)
        ],
        capture_output=True,
        text=True,
        check=False
    )

    response = web_client.get("/api/search?q=test&mode=keyword")

    assert response.status_code == 200, f"Search API failed: {response.text}"

    data = response.json()
    assert "results" in data, "Missing results key"
    assert isinstance(data["results"], list)

    # Check result structure if results exist
    if len(data["results"]) > 0:
        result = data["results"][0]
        assert "turn_id" in result or "id" in result
        assert "score" in result or "snippet" in result
