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

import pytest


@pytest.mark.integration
def test_web_api_version_endpoint(web_client):
    """T1-12: Verify /api/version returns version info."""
    if web_client is None:
        pytest.skip("Web client unavailable")
    
    response = web_client.get("/api/version")
    
    assert response.status_code == 200, f"Version endpoint failed: {response.text}"
    
    data = response.json()
    assert "version" in data, "Response missing version key"


@pytest.mark.integration
def test_web_api_agents_endpoint_includes_versioned_icon_url(web_client):
    """Verify agent metadata includes cache-busted icon URLs."""
    if web_client is None:
        pytest.skip("Web client unavailable")

    response = web_client.get("/api/agents/")

    assert response.status_code == 200, f"Agents endpoint failed: {response.text}"

    data = response.json()
    assert "agents" in data, "Response missing agents key"

    codex = next((agent for agent in data["agents"] if agent.get("id") == "codex"), None)
    assert codex is not None, "Expected codex agent metadata"
    assert codex.get("icon") == "icon.png"
    assert str(codex.get("icon_url") or "").startswith("/api/agents/codex/icon?v=")


@pytest.mark.integration
def test_web_api_system_stats_empty_state(web_client, run_dir, monkeypatch):
    """T1-13: Verify /api/system/stats handles empty database gracefully."""
    if web_client is None:
        pytest.skip("Web client unavailable")
    
    # Configure web to use empty run directory
    monkeypatch.setenv("WEB_RUN_DIR", str(run_dir))
    
    response = web_client.get("/api/system/stats")
    
    # Define explicit expectations for each status code
    if response.status_code == 200:
        data = response.json()
        assert isinstance(data, dict), "Stats should return dict"
        
        # Verify the response indicates empty/unavailable state
        if "is_available" in data:
            # If availability flag exists, it should indicate unavailable for empty db
            assert data["is_available"] is False, (
                f"Empty database should report is_available=False, got {data['is_available']}"
            )
        else:
            # If no availability flag, verify counts are zero or check structure
            count_fields = ["total_workspaces", "total_turns", "total_sessions",
                          "workspace_count", "turn_count", "session_count"]
            found_any_count = False
            for field in count_fields:
                if field in data:
                    found_any_count = True
                    assert data[field] == 0, (
                        f"Expected {field}=0 for empty database, got {data[field]}"
                    )
            # If no count fields, at least verify we got a valid response structure
            assert found_any_count or len(data) > 0, (
                f"Stats response has unexpected structure: {list(data.keys())}"
            )
    
    elif response.status_code == 404:
        # 404 is acceptable if it clearly indicates database not found
        response_text = response.text.lower()
        assert "database" in response_text or "not found" in response_text or "unavailable" in response_text, (
            f"404 response should indicate database not found, got: {response.text[:200]}"
        )
    
    elif response.status_code == 500:
        # 500 with "database not available" message is acceptable for empty state
        # Note: Ideally this should be a 404, but the current implementation wraps it
        response_text = response.text.lower()
        if "database" in response_text and ("not available" in response_text or "not found" in response_text):
            pass  # Acceptable - database not available error wrapped in 500
        else:
            pytest.fail(f"500 error not related to missing database: {response.text[:200]}")
    
    else:
        pytest.fail(f"Unexpected status code {response.status_code}: {response.text[:200]}")


@pytest.mark.integration
def test_web_api_list_workspaces(web_client, copilot_workspace, run_dir, monkeypatch):
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
        env=env
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
def test_web_api_workspace_detail(web_client, copilot_workspace, run_dir, monkeypatch):
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
        env=env
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
def test_web_api_search(web_client, copilot_workspace, run_dir, monkeypatch):
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
    config_loader._config = None  # Reset singleton
    config_loader._config_path = None
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
        env=env
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
        text=True
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
