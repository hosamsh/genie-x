"""Tests for config system isolation (Tier 0).

Tests:
- T0-5: Config Loader Isolation Test

This test verifies that the config system supports per-test isolation,
which is a prerequisite for all other CLI tests.
"""
import json
import subprocess
import sys
from pathlib import Path

import yaml


def test_config_loader_isolation_via_cli(tmp_path):
    """T0-5: Verify config system supports per-test isolation via CLI.
    
    This test verifies that:
    1. Different config files can be used independently via --config flag
    2. Config values from one file don't affect another CLI invocation
    3. The CLI properly isolates runs using --config flag
    
    Uses subprocess (black-box) approach since direct import may not work
    from the test directory context.
    """
    # Create a workspace directory with synthetic data so we can verify
    # the config path is actually being used
    # RESPEC/tests/test_config_isolation.py -> parent.parent.parent = project root
    project_root = Path(__file__).parent.parent.parent
    
    # Config A: points to storage_a directory
    storage_a = tmp_path / "storage_a"
    storage_a.mkdir()
    
    # Config B: points to storage_b directory  
    storage_b = tmp_path / "storage_b"
    storage_b.mkdir()
    
    config_a = {
        "extract": {
            "copilot": {"workspace_storage": str(storage_a)},
            "claude_code": {"claude_dir": str(storage_a / "claude")},
            "copilot_cli": {"session_state_dir": str(storage_a / "copilot_cli")}
        },
        "llm_models": {},
        "model_defaults": {"enabled": False},
        "pricing": {"default": {"input": 1.0, "output": 1.0}, "models": {}}
    }
    
    config_b = {
        "extract": {
            "copilot": {"workspace_storage": str(storage_b)},
            "claude_code": {"claude_dir": str(storage_b / "claude")},
            "copilot_cli": {"session_state_dir": str(storage_b / "copilot_cli")}
        },
        "llm_models": {},
        "model_defaults": {"enabled": False},
        "pricing": {"default": {"input": 2.0, "output": 2.0}, "models": {}}
    }
    
    config_a_path = tmp_path / "config_a.yaml"
    config_b_path = tmp_path / "config_b.yaml"
    
    with open(config_a_path, 'w') as f:
        yaml.dump(config_a, f)
    
    with open(config_b_path, 'w') as f:
        yaml.dump(config_b, f)
    
    # Run CLI with config A - should see empty workspaces from storage_a
    result_a = subprocess.run(
        [sys.executable, str(project_root / "run_cli.py"), "--config", str(config_a_path), "--list", "--json"],
        capture_output=True,
        text=True,
        cwd=str(project_root)
    )
    assert result_a.returncode == 0, f"CLI with config A failed: {result_a.stderr}"
    
    # Run CLI with config B - should see empty workspaces from storage_b
    result_b = subprocess.run(
        [sys.executable, str(project_root / "run_cli.py"), "--config", str(config_b_path), "--list", "--json"],
        capture_output=True,
        text=True,
        cwd=str(project_root)
    )
    assert result_b.returncode == 0, f"CLI with config B failed: {result_b.stderr}"
    
    # Both should return empty workspace lists (verifying isolation - each uses its own paths)
    data_a = json.loads(result_a.stdout)
    data_b = json.loads(result_b.stdout)
    
    assert "workspaces" in data_a
    assert "workspaces" in data_b
    assert len(data_a["workspaces"]) == 0, "Expected empty workspaces for config A"
    assert len(data_b["workspaces"]) == 0, "Expected empty workspaces for config B"


def test_cli_config_flag_isolates_runs(cli_runner, tmp_path):
    """Verify --config flag properly isolates CLI runs.
    
    This is a functional test that the CLI respects --config flag.
    """
    # Create config with empty storage paths (should result in empty list)
    config_path = tmp_path / "empty_config.yaml"
    config = {
        "extract": {
            "copilot": {
                "workspace_storage": str(tmp_path / "empty_copilot")
            },
            "claude_code": {
                "claude_dir": str(tmp_path / "empty_claude")
            },
            "copilot_cli": {
                "session_state_dir": str(tmp_path / "empty_copilot_cli")
            }
        },
        "llm_models": {},
        "model_defaults": {"enabled": False},
        "pricing": {
            "default": {"input": 1.0, "output": 1.0},
            "models": {}
        }
    }
    
    with open(config_path, 'w') as f:
        yaml.dump(config, f)
    
    # Run CLI with this config
    result = cli_runner("--list", "--json", config_path=config_path)
    
    # Should succeed (exit 0) with empty workspace list
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    
    import json
    data = json.loads(result.stdout)
    assert "workspaces" in data
    assert len(data["workspaces"]) == 0, "Expected empty workspace list with empty storage paths"
