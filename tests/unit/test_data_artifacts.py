"""Data Artifact Tests - Database Schema and Data Contract Verification.

This module tests essential database contracts after extraction:
- Required tables/views exist
- Critical data constraints are enforced
- Referential integrity between tables
- Data completeness checks

Focused on tests that catch real bugs, not exhaustive schema validation.
"""
import sqlite3

import pytest  # noqa: F401 - used for fixtures

from conftest import get_test_db_path


class TestSchemaContracts:
    """Verify essential database schema contracts."""

    def test_all_required_tables_exist(
        self, cli_runner, make_test_config, copilot_workspace, run_dir
    ):
        """Verify all required tables are created after extraction."""
        config_path = make_test_config(copilot_storage=copilot_workspace["storage_root"])
        
        result = cli_runner(
            "--extract", copilot_workspace["workspace_id"],
            "--run-dir", str(run_dir),
            config_path=config_path
        )
        assert result.returncode == 0, f"Extraction failed: {result.stderr}"
        
        db_path = get_test_db_path(run_dir)
        conn = sqlite3.connect(str(db_path))
        
        # Check all required tables exist
        required_tables = ["workspace_info", "turns", "code_metrics", "turn_embeddings"]
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ({})".format(
                ",".join("?" * len(required_tables))
            ),
            required_tables
        )
        existing_tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        
        missing = set(required_tables) - existing_tables
        assert len(missing) == 0, f"Missing required tables: {missing}"

    def test_combined_turns_is_view_not_table(
        self, cli_runner, make_test_config, copilot_workspace, run_dir
    ):
        """Verify combined_turns is a VIEW (architectural invariant)."""
        config_path = make_test_config(copilot_storage=copilot_workspace["storage_root"])
        
        result = cli_runner(
            "--extract", copilot_workspace["workspace_id"],
            "--run-dir", str(run_dir),
            config_path=config_path
        )
        assert result.returncode == 0
        
        db_path = get_test_db_path(run_dir)
        conn = sqlite3.connect(str(db_path))
        
        cursor = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'combined_turns'"
        )
        row = cursor.fetchone()
        conn.close()
        
        assert row is not None, "combined_turns not found"
        assert row[0] == "view", f"combined_turns should be VIEW, got {row[0]}"


class TestDataContracts:
    """Verify critical data constraints are enforced."""

    def test_turns_role_and_agent_values_are_valid(
        self, cli_runner, make_test_config, copilot_workspace, run_dir
    ):
        """Verify role and agent_used contain only valid values."""
        config_path = make_test_config(copilot_storage=copilot_workspace["storage_root"])
        
        result = cli_runner(
            "--extract", copilot_workspace["workspace_id"],
            "--run-dir", str(run_dir),
            config_path=config_path
        )
        assert result.returncode == 0
        
        db_path = get_test_db_path(run_dir)
        conn = sqlite3.connect(str(db_path))
        
        # Check roles
        cursor = conn.execute("SELECT DISTINCT role FROM turns WHERE role IS NOT NULL")
        actual_roles = {row[0] for row in cursor.fetchall()}
        valid_roles = {"user", "assistant", "system"}
        invalid_roles = actual_roles - valid_roles
        
        # Check agents
        cursor = conn.execute("SELECT DISTINCT agent_used FROM turns WHERE agent_used IS NOT NULL")
        actual_agents = {row[0] for row in cursor.fetchall()}
        valid_agents = {"copilot", "claude_code", "copilot_cli", "codex"}
        invalid_agents = actual_agents - valid_agents
        
        conn.close()
        
        assert len(invalid_roles) == 0, f"Invalid role values: {invalid_roles}"
        assert len(invalid_agents) == 0, f"Invalid agent values: {invalid_agents}"

    def test_required_fields_not_null(
        self, cli_runner, make_test_config, copilot_workspace, run_dir
    ):
        """Verify required fields (session_id, workspace_id) are never NULL."""
        config_path = make_test_config(copilot_storage=copilot_workspace["storage_root"])
        
        result = cli_runner(
            "--extract", copilot_workspace["workspace_id"],
            "--run-dir", str(run_dir),
            config_path=config_path
        )
        assert result.returncode == 0
        
        db_path = get_test_db_path(run_dir)
        conn = sqlite3.connect(str(db_path))
        
        cursor = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id IS NULL OR workspace_id IS NULL"
        )
        null_count = cursor.fetchone()[0]
        conn.close()
        
        assert null_count == 0, f"Found {null_count} turns with NULL session_id or workspace_id"


class TestReferentialIntegrity:
    """Verify referential integrity between tables."""

    def test_turns_workspace_id_exists_in_workspace_info(
        self, cli_runner, make_test_config, copilot_workspace, run_dir
    ):
        """Verify all turns.workspace_id values exist in workspace_info."""
        config_path = make_test_config(copilot_storage=copilot_workspace["storage_root"])
        
        result = cli_runner(
            "--extract", copilot_workspace["workspace_id"],
            "--run-dir", str(run_dir),
            config_path=config_path
        )
        assert result.returncode == 0
        
        db_path = get_test_db_path(run_dir)
        conn = sqlite3.connect(str(db_path))
        
        cursor = conn.execute("""
            SELECT COUNT(*) FROM turns t
            WHERE t.workspace_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM workspace_info w WHERE w.workspace_id = t.workspace_id
            )
        """)
        orphan_count = cursor.fetchone()[0]
        conn.close()
        
        assert orphan_count == 0, f"Found {orphan_count} turns with orphan workspace_id"

    def test_no_duplicate_session_turn_pairs(
        self, cli_runner, make_test_config, copilot_workspace, run_dir
    ):
        """Verify UNIQUE(session_id, turn) constraint - no duplicates."""
        config_path = make_test_config(copilot_storage=copilot_workspace["storage_root"])
        
        result = cli_runner(
            "--extract", copilot_workspace["workspace_id"],
            "--run-dir", str(run_dir),
            config_path=config_path
        )
        assert result.returncode == 0
        
        db_path = get_test_db_path(run_dir)
        conn = sqlite3.connect(str(db_path))
        
        cursor = conn.execute("""
            SELECT session_id, turn, COUNT(*) as cnt
            FROM turns
            GROUP BY session_id, turn
            HAVING cnt > 1
        """)
        duplicates = cursor.fetchall()
        conn.close()
        
        assert len(duplicates) == 0, f"Found duplicate (session_id, turn) pairs: {duplicates}"


class TestDataCompleteness:
    """Verify extraction produces complete, consistent data."""

    def test_extraction_creates_user_and_assistant_turns(
        self, cli_runner, make_test_config, copilot_workspace, run_dir
    ):
        """Verify extraction creates both user and assistant turns."""
        config_path = make_test_config(copilot_storage=copilot_workspace["storage_root"])
        
        result = cli_runner(
            "--extract", copilot_workspace["workspace_id"],
            "--run-dir", str(run_dir),
            config_path=config_path
        )
        assert result.returncode == 0
        
        db_path = get_test_db_path(run_dir)
        conn = sqlite3.connect(str(db_path))
        
        cursor = conn.execute("SELECT role, COUNT(*) FROM turns GROUP BY role")
        role_counts = dict(cursor.fetchall())
        conn.close()
        
        assert role_counts.get("user", 0) > 0, "No user turns extracted"
        assert role_counts.get("assistant", 0) > 0, "No assistant turns extracted"

    def test_workspace_info_counts_match_actual_data(
        self, cli_runner, make_test_config, copilot_workspace, run_dir
    ):
        """Verify workspace_info counts are consistent with turns table."""
        config_path = make_test_config(copilot_storage=copilot_workspace["storage_root"])
        
        workspace_id = copilot_workspace["workspace_id"]
        result = cli_runner(
            "--extract", workspace_id,
            "--run-dir", str(run_dir),
            config_path=config_path
        )
        assert result.returncode == 0
        
        db_path = get_test_db_path(run_dir)
        conn = sqlite3.connect(str(db_path))
        
        # Get recorded counts
        cursor = conn.execute(
            "SELECT session_count, turn_count FROM workspace_info WHERE workspace_id = ?",
            (workspace_id,)
        )
        row = cursor.fetchone()
        recorded_sessions = row[0] if row else 0
        recorded_turns = row[1] if row else 0
        
        # Get actual counts
        cursor = conn.execute(
            "SELECT COUNT(DISTINCT session_id), COUNT(*) FROM turns WHERE workspace_id = ?",
            (workspace_id,)
        )
        actual_sessions, actual_turns = cursor.fetchone()
        conn.close()
        
        assert recorded_sessions == actual_sessions, (
            f"session_count mismatch: recorded={recorded_sessions}, actual={actual_sessions}"
        )
        assert recorded_turns == actual_turns, (
            f"turn_count mismatch: recorded={recorded_turns}, actual={actual_turns}"
        )
