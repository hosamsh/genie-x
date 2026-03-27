"""Tests for workspace listing CLI functionality (Feature 1).

Tests:
- T1-1: List workspaces returns table output
- T1-2: List workspaces JSON output
- T1-3: List workspaces empty state

Note: Tests use test_config.yaml which points to isolated storage paths.
"""
import json


def test_list_workspaces_table_output(cli_runner, make_test_config, copilot_workspace):
    """T1-1: Verify --list produces readable table output when workspaces exist."""
    # Create test config pointing to our synthetic workspace
    config_path = make_test_config(
        copilot_storage=copilot_workspace["storage_root"]
    )
    
    result = cli_runner("--list", config_path=config_path)
    
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    
    # Check output contains workspace info
    assert copilot_workspace["workspace_id"] in result.stdout
    # Check for workspace folder (basename or partial path)
    assert "my-test-project" in result.stdout or copilot_workspace["workspace_folder"] in result.stdout


def test_list_workspaces_json_output(cli_runner, make_test_config, copilot_workspace):
    """T1-2: Verify --list --json produces valid JSON with correct structure."""
    # Create test config pointing to our synthetic workspace
    config_path = make_test_config(
        copilot_storage=copilot_workspace["storage_root"]
    )
    
    result = cli_runner("--list", "--json", config_path=config_path)
    
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    
    # Validate JSON structure (pagination wrapper)
    data = json.loads(result.stdout)
    assert isinstance(data, dict), "JSON output should be a dict with pagination"
    assert "workspaces" in data
    
    workspaces = data["workspaces"]
    assert isinstance(workspaces, list)
    assert len(workspaces) >= 1
    
    # Find the workspace matching our fixture (others may appear from auto-discovery)
    matching = [w for w in workspaces if w["workspace_id"] == copilot_workspace["workspace_id"]]
    assert len(matching) == 1, f"Expected fixture workspace not found in {[w['workspace_id'] for w in workspaces]}"
    workspace = matching[0]

    # Check workspace object structure
    assert "workspace_id" in workspace
    assert "name" in workspace


def test_list_workspaces_empty_state(cli_runner, make_test_config):
    """T1-3: Verify graceful handling when no workspaces exist."""
    # Create test config with empty/non-existent paths
    config_path = make_test_config()
    
    result = cli_runner("--list", "--json", config_path=config_path)
    
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    
    # Validate JSON output is empty array (wrapped in pagination object)
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    assert "workspaces" in data
    assert isinstance(data["workspaces"], list)
    assert len(data["workspaces"]) == 0

