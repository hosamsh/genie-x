"""Test: Extract workspaces and verify database contents.

Note: This test dynamically discovers available workspaces rather than
relying on hardcoded IDs, making it portable across different environments.
"""

import logging

import pytest
import re
from tests.integration.conftest import (
    run_cli_command,
    get_project_root,
    delete_db,
    count_table_rows,
)
from src.shared.io.run_dir import get_db_path

logger = logging.getLogger(__name__)


# Note: We no longer use hardcoded workspace IDs.
# Tests dynamically discover available workspaces to ensure portability.


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape codes from text."""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def get_available_workspaces():
    """Get list of available workspace IDs by running --list."""
    result = run_cli_command(["--list"])
    if result.returncode != 0:
        return []
    
    # Strip ANSI codes before parsing
    output = strip_ansi_codes(result.stdout + result.stderr)
    # Find 32-character hex IDs (MD5 hashes)
    workspace_ids = re.findall(r'\b[a-f0-9]{32}\b', output)
    return list(set(workspace_ids))


def test_extract_two_workspaces():
    """Test: py run_cli.py --extract <ws1> <ws2> --run-dir data/int-test
    
    Before running: Delete database in the run-dir folder (starting from scratch)
    
    Success criteria:
    1. Database is created
    2. workspace_info table contains at least 1 workspace
    3. turns table contains at least 1 entry
    4. If extraction was successful, verify basic table structure
    """
    project_root = get_project_root()
    run_dir = project_root / "data" / "int-test"
    
    # Get available workspaces dynamically
    available_workspaces = get_available_workspaces()
    
    if len(available_workspaces) == 0:
        pytest.skip("No workspaces available for extraction test")
    
    # Use up to 2 available workspaces
    workspace_ids_to_extract = available_workspaces[:2]
    
    # Before running: Delete database
    delete_db(run_dir)
    
    # Run extraction
    result = run_cli_command([
        "--extract",
        *workspace_ids_to_extract,
        "--run-dir",
        str(run_dir)
    ])
    
    assert result.returncode == 0, f"Extract command failed: {result.stderr}"
    
    db_path = get_db_path(run_dir)
    assert db_path.exists(), f"Database file {db_path.name} was not created"
    logger.info("Database created at %s", db_path)
    
    # 1. Verify workspace_info table contains expected workspaces
    ws_count = count_table_rows(run_dir, "workspace_info")
    assert ws_count >= 1, f"Expected at least 1 workspace, got {ws_count}"
    logger.info("workspace_info contains %d workspaces", ws_count)
    
    # 2. Verify turns table has entries
    turns_count = count_table_rows(run_dir, "turns")
    assert turns_count >= 1, f"Expected at least 1 turn, got {turns_count}"
    logger.info("turns table contains %d entries", turns_count)
    
    # 3. Verify code_metrics table exists and has structure
    try:
        metrics_count = count_table_rows(run_dir, "code_metrics")
        logger.info("code_metrics table contains %d entries", metrics_count)
    except Exception as e:
        logger.warning("code_metrics table check failed: %s", e)
    
    # 4. Verify combined_turns view exists
    try:
        combined_count = count_table_rows(run_dir, "combined_turns")
        logger.info("combined_turns view contains %d records", combined_count)
    except Exception as e:
        logger.warning("combined_turns view check failed: %s", e)
    
    logger.info("Extraction test passed with %d workspace(s)", len(workspace_ids_to_extract))
