"""
Shared database utilities for SQLite operations.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, TYPE_CHECKING

from src.shared.logging.logger import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


def connect_db(db_path: Path) -> sqlite3.Connection:
    """Connect to the pipeline database.
    
    Creates the database and initializes schema if it doesn't exist.
    
    Args:
        db_path: Path to the database file
        
    Returns:
        SQLite connection object
    """
    # Ensure parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    # If database doesn't exist, initialize it with the pipeline schema
    if not db_path.exists():
        conn = init_shared_db(db_path, verbose=False)
        logger.info(f"Initialized new database: {db_path}")
        return conn
    
    return sqlite3.connect(str(db_path))


def init_shared_db(db_path: Path, verbose: bool = True) -> sqlite3.Connection:
    """Initialize the unified shared database with all tables.
    
    This is the single entry point for database initialization across all stages.
    Creates or connects to the database and ensures all required tables exist:
    - turns (extraction)
    - turns_fts (full-text search index)
    - turn_embeddings (semantic search embeddings)
    - combined_turns (combined user-assistant pairs)
    - code_metrics (code edit metrics)
    - workspace_info (workspace metadata)
    
    Args:
        db_path: Path to the database file
        verbose: Whether to print progress messages
        
    Returns:
        SQLite connection object
    """
    if verbose and not db_path.exists():
        logger.progress(f"\n[DB] Initializing shared database: {db_path.name}")
    
    # Create parent directory if needed
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Connect to database
    conn = sqlite3.connect(str(db_path))
    
    # Create all tables
    ensure_turns_table(conn)
    ensure_turns_fts_table(conn)
    ensure_turn_embeddings_table(conn)
    ensure_combined_turns_view(conn)
    ensure_code_metrics_table(conn)
    ensure_workspace_info_table(conn)
    ensure_session_file_meta_table(conn)
    ensure_parsed_tables(conn)
    ensure_turn_detail_tables(conn)
    
    if verbose:
        cursor = conn.cursor()
        
        # Get table counts
        cursor.execute("SELECT COUNT(*) FROM turns")
        turns_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM combined_turns")
        combined_count = cursor.fetchone()[0]
        
        logger.progress("  Tables ready:")
        logger.progress(f"    - turns: {turns_count:,} rows")
        logger.progress(f"    - combined_turns: {combined_count:,} rows")
    
    return conn

def ensure_workspace_info_table(conn: sqlite3.Connection) -> None:
    """Create workspace_info table if it doesn't exist.
    
    Stores metadata about extracted workspaces including extraction timing
    and workspace details. This table is populated each time a workspace
    is extracted (via any method: CLI, web, pipeline).
    
    Note: created_at = first extraction, updated_at = last extraction
    """
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workspace_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL UNIQUE,
            workspace_name TEXT,
            workspace_folder TEXT,
            agent_used TEXT,
            
            -- Extraction timing
            extraction_duration_ms INTEGER,
            
            -- Metadata
            session_count INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            
            -- Lines of Code metrics
            total_code_loc INTEGER DEFAULT 0,
            total_doc_loc INTEGER DEFAULT 0,
            
            -- Timestamps (created_at = first extraction, updated_at = last extraction)
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create index for efficient lookups
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workspace_info_id ON workspace_info(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workspace_info_agent ON workspace_info(agent_used)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workspace_info_updated ON workspace_info(updated_at)")
    
    conn.commit()
    logger.debug("Ensured workspace_info table exists")

def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    """Add *column* to *table* if it does not already exist (SQLite migration helper)."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()
        logger.info("Migrated %s: added column %s %s", table, column, definition)


def ensure_turns_table(conn: sqlite3.Connection) -> None:
    """Create turns table if it doesn't exist.

    This table stores extraction results (raw conversation turns).
    Includes UNIQUE constraint on (session_id, turn) to prevent duplicates.
    Also applies forward migrations for new columns on existing databases.
    """
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                turn INTEGER NOT NULL,
                role TEXT,
                text TEXT,
                original_text TEXT,  -- Original text before cleaning

                -- Metadata
                workspace_id TEXT,
                workspace_name TEXT,
                workspace_folder TEXT,
                session_name TEXT,
                agent_used TEXT,
                model_id TEXT,
                request_id TEXT,

                -- Timestamps
                timestamp_ms INTEGER,
                timestamp_iso TEXT,
                ts TEXT,

                -- Token usage (unified across supported agents)
                original_text_tokens INTEGER DEFAULT 0,
                cleaned_text_tokens INTEGER DEFAULT 0,
                code_tokens INTEGER DEFAULT 0,
                tool_tokens INTEGER DEFAULT 0,
                system_tokens INTEGER DEFAULT 0,
                session_history_tokens INTEGER DEFAULT 0,
                thinking_tokens INTEGER DEFAULT 0,

                -- Thinking content (for reasoning models)
                thinking_text TEXT,
                thinking_duration_ms INTEGER,

                -- Language info
                primary_language TEXT,
                languages TEXT,  -- JSON array

                -- Context
                files TEXT,  -- JSON array
                tools TEXT,  -- JSON array

                -- Agent-specific fields (nullable)
                merged_request_ids TEXT,  -- JSON array of request IDs merged into this turn

                -- Response time fields (nullable, for assistant turns)
                responding_to_turn INTEGER,
                response_time_ms INTEGER,

                -- Aggregated code metrics (nullable, calculated from code_edits)
                total_lines_added INTEGER,
                total_lines_removed INTEGER,
                total_nloc_change INTEGER,
                weighted_complexity_change REAL,

                -- Session relationship tracking (subagent / fork sessions)
                parent_session_id TEXT,
                relationship_type TEXT,

                UNIQUE(session_id, turn)
            )
        """)

    # Forward migrations: add columns that may be missing from older databases
    _add_column_if_missing(conn, "turns", "parent_session_id", "TEXT")
    _add_column_if_missing(conn, "turns", "relationship_type", "TEXT")

    # Create indexes for common queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workspace_id ON turns(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_id ON turns(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_role ON turns(role)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON turns(timestamp_ms)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_model ON turns(model_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_role_model ON turns(role, model_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent ON turns(agent_used)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_request_id ON turns(request_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workspace_timestamp ON turns(workspace_id, timestamp_ms)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_workspace ON turns(agent_used, workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_responding_to ON turns(responding_to_turn)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_session_responding_role ON turns(session_id, responding_to_turn, role)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_response_time ON turns(response_time_ms) WHERE response_time_ms IS NOT NULL")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_role_timestamp ON turns(role, timestamp_ms)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_role_response_time ON turns(role, response_time_ms)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_workspace ON turns(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_workspace_session ON turns(workspace_id, session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_session_turn_role ON turns(session_id, turn, role)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_session_turn_role_model ON turns(session_id, turn, role, model_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turns_parent_session ON turns(parent_session_id)")

    conn.commit()
    logger.debug("Ensured turns table exists")


def ensure_session_file_meta_table(conn: sqlite3.Connection) -> None:
    """Create session_file_meta table if it doesn't exist.

    Tracks per-file parse state for incremental (offset-based) JSONL reading.
    """
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_file_meta (
            session_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            last_offset INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (session_id, agent)
        )
    """)
    _add_column_if_missing(conn, "session_file_meta", "file_path", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "session_file_meta", "file_size", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "session_file_meta", "last_offset", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "session_file_meta", "message_count", "INTEGER NOT NULL DEFAULT 0")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sfm_file_path ON session_file_meta(file_path)")
    conn.commit()
    logger.debug("Ensured session_file_meta table exists")


def ensure_parsed_tables(conn: sqlite3.Connection) -> None:
    """Create low-level parsed raw persistence tables if missing."""
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS parsed_workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            workspace_name TEXT,
            workspace_folder TEXT,
            source_root TEXT,
            descriptor_metadata_json TEXT,
            workspace_metadata_json TEXT,
            issues_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(workspace_id, agent_name)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS parsed_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_row_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            title TEXT,
            source_path TEXT,
            started_at_ms INTEGER,
            ended_at_ms INTEGER,
            parent_session_id TEXT,
            relationship_type TEXT,
            metadata_json TEXT,
            issues_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(workspace_row_id, session_id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS parsed_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_row_id INTEGER NOT NULL,
            event_index INTEGER NOT NULL,
            event_type TEXT,
            role TEXT,
            timestamp_ms INTEGER,
            timestamp_iso TEXT,
            message_id TEXT,
            request_id TEXT,
            model_id TEXT,
            text TEXT,
            thinking_text TEXT,
            token_usage_json TEXT,
            command_json TEXT,
            file_paths_json TEXT,
            raw_json TEXT,
            extra_json TEXT,
            UNIQUE(session_row_id, event_index)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS parsed_content_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_row_id INTEGER NOT NULL,
            block_index INTEGER NOT NULL,
            kind TEXT,
            text TEXT,
            data_json TEXT,
            raw_json TEXT,
            extra_json TEXT,
            UNIQUE(event_row_id, block_index)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS parsed_tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_row_id INTEGER NOT NULL,
            call_index INTEGER NOT NULL,
            call_id TEXT,
            name TEXT,
            kind TEXT,
            arguments_json TEXT,
            arguments_text TEXT,
            file_paths_json TEXT,
            spawned_session_id TEXT,
            status TEXT,
            raw_json TEXT,
            extra_json TEXT,
            UNIQUE(event_row_id, call_index)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS parsed_tool_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_row_id INTEGER NOT NULL,
            result_index INTEGER NOT NULL,
            tool_call_id TEXT,
            kind TEXT,
            text TEXT,
            structured_content_json TEXT,
            is_error INTEGER,
            status TEXT,
            raw_json TEXT,
            extra_json TEXT,
            UNIQUE(event_row_id, result_index)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS parsed_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_row_id INTEGER NOT NULL,
            attachment_index INTEGER NOT NULL,
            kind TEXT,
            path TEXT,
            title TEXT,
            media_type TEXT,
            raw_json TEXT,
            extra_json TEXT,
            UNIQUE(event_row_id, attachment_index)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS parsed_session_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_row_id INTEGER NOT NULL,
            link_index INTEGER NOT NULL,
            target_session_id TEXT,
            relationship_type TEXT,
            trigger_event_index INTEGER,
            trigger_tool_call_id TEXT,
            extra_json TEXT,
            UNIQUE(session_row_id, link_index)
        )
        """
    )

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_workspaces_agent_workspace ON parsed_workspaces(agent_name, workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_sessions_workspace_row ON parsed_sessions(workspace_row_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_sessions_session_id ON parsed_sessions(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_events_session_row ON parsed_events(session_row_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_events_request_id ON parsed_events(request_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_events_message_id ON parsed_events(message_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_blocks_event_row ON parsed_content_blocks(event_row_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_tool_calls_event_row ON parsed_tool_calls(event_row_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_tool_results_event_row ON parsed_tool_results(event_row_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_attachments_event_row ON parsed_attachments(event_row_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parsed_links_session_row ON parsed_session_links(session_row_id)")

    conn.commit()
    logger.debug("Ensured parsed raw tables exist")


def ensure_turn_detail_tables(conn: sqlite3.Connection) -> None:
    """Create materialized turn-detail tables for tool runs and subagent refs."""
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS turn_tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn INTEGER NOT NULL,
            tool_index INTEGER NOT NULL,
            call_id TEXT,
            name TEXT,
            kind TEXT,
            arguments_json TEXT,
            arguments_text TEXT,
            file_paths_json TEXT,
            spawned_session_id TEXT,
            status TEXT,
            display_text TEXT,
            results_json TEXT,
            raw_call_json TEXT,
            extra_json TEXT,
            UNIQUE(session_id, turn, tool_index)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS turn_subagent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn INTEGER NOT NULL,
            subagent_index INTEGER NOT NULL,
            subagent_session_id TEXT NOT NULL,
            source_tool_call_id TEXT,
            source_tool_name TEXT,
            relationship_type TEXT,
            title TEXT,
            prompt_text TEXT,
            result_text TEXT,
            turn_count INTEGER DEFAULT 0,
            total_lines_added INTEGER DEFAULT 0,
            total_lines_removed INTEGER DEFAULT 0,
            started_at_ms INTEGER,
            ended_at_ms INTEGER,
            extra_json TEXT,
            UNIQUE(session_id, turn, subagent_session_id)
        )
        """
    )

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turn_tool_calls_workspace ON turn_tool_calls(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turn_tool_calls_session_turn ON turn_tool_calls(session_id, turn)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turn_tool_calls_call_id ON turn_tool_calls(call_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turn_subagent_runs_workspace ON turn_subagent_runs(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turn_subagent_runs_session_turn ON turn_subagent_runs(session_id, turn)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turn_subagent_runs_child_session ON turn_subagent_runs(subagent_session_id)")

    conn.commit()
    logger.debug("Ensured turn detail tables exist")


def ensure_turns_fts_table(conn: sqlite3.Connection) -> None:
    """Create FTS5 table and triggers for turn search."""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts
        USING fts5(
            original_text,
            content='turns',
            content_rowid='id',
            tokenize='unicode61'
        )
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS turns_fts_ai
        AFTER INSERT ON turns
        BEGIN
            INSERT INTO turns_fts(rowid, original_text)
            VALUES (new.id, COALESCE(new.original_text, new.text, ''));
        END
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS turns_fts_ad
        AFTER DELETE ON turns
        BEGIN
            INSERT INTO turns_fts(turns_fts, rowid, original_text)
            VALUES ('delete', old.id, COALESCE(old.original_text, old.text, ''));
        END
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS turns_fts_au
        AFTER UPDATE OF original_text, text ON turns
        BEGIN
            INSERT INTO turns_fts(turns_fts, rowid, original_text)
            VALUES ('delete', old.id, COALESCE(old.original_text, old.text, ''));
            INSERT INTO turns_fts(rowid, original_text)
            VALUES (new.id, COALESCE(new.original_text, new.text, ''));
        END
    """)

    conn.commit()
    logger.debug("Ensured turns_fts table and triggers exist")


def rebuild_turns_fts(conn: sqlite3.Connection) -> None:
    """Rebuild FTS index from turns content."""
    cursor = conn.cursor()
    cursor.execute("INSERT INTO turns_fts(turns_fts) VALUES('delete-all')")
    cursor.execute("""
        INSERT INTO turns_fts(rowid, original_text)
        SELECT id, COALESCE(original_text, text, '')
        FROM turns
    """)
    conn.commit()
    logger.info("Rebuilt turns_fts index")


def ensure_turn_embeddings_table(conn: sqlite3.Connection) -> None:
    """Create turn_embeddings table if it doesn't exist."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS turn_embeddings (
            turn_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            dims INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            text_hash TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (turn_id, model),
            FOREIGN KEY (turn_id) REFERENCES turns(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turn_embeddings_model ON turn_embeddings(model)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_turn_embeddings_updated ON turn_embeddings(updated_at)")
    conn.commit()
    logger.debug("Ensured turn_embeddings table exists")

def ensure_combined_turns_view(conn: sqlite3.Connection) -> None:
    """Create combined_turns view if it doesn't exist.
    
    This VIEW dynamically pairs user-assistant turns from the turns table.
    Replaces the old materialized table approach - data is no longer duplicated.
    The view reconstructs code_edits from the code_metrics table.
    """
    from pathlib import Path
    cursor = conn.cursor()
    
    # Check if old table exists and needs migration to view
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='combined_turns'")
    table_exists = cursor.fetchone() is not None
    
    # Check if view already exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='view' AND name='combined_turns'")
    view_exists = cursor.fetchone() is not None
    
    if table_exists and not view_exists:
        logger.info("Migrating combined_turns from TABLE to VIEW...")
        
        # Backup the old table before dropping it
        cursor.execute("SELECT COUNT(*) FROM combined_turns")
        table_count = cursor.fetchone()[0]
        logger.info(f"  Old table has {table_count:,} records")
        
        # Drop the old table (data will be regenerated from turns + code_metrics)
        cursor.execute("DROP TABLE combined_turns")
        logger.info("  Dropped old combined_turns table")
    
    # Always recreate the view so definition changes take effect on existing DBs.
    cursor.execute("DROP VIEW IF EXISTS combined_turns")

    # Read the view SQL from file
    sql_file = Path(__file__).parent.parent.parent.parent / "_my" / "combined_turns_view_prototype.sql"
    
    if sql_file.exists():
        # Use the prototype SQL file
        sql_content = sql_file.read_text(encoding='utf-8')
        # Replace combined_turns_vw with combined_turns for production use
        sql_content = sql_content.replace("combined_turns_vw", "combined_turns")
        sql_content = sql_content.replace(
            "json(COALESCE(before_metrics, '{}'))",
            "json(COALESCE(NULLIF(before_metrics, ''), '{}'))",
        )
        sql_content = sql_content.replace(
            "json(COALESCE(after_metrics, '{}'))",
            "json(COALESCE(NULLIF(after_metrics, ''), '{}'))",
        )
        sql_content = sql_content.replace(
            "json(COALESCE(delta_metrics, '{}'))",
            "json(COALESCE(NULLIF(delta_metrics, ''), '{}'))",
        )
        cursor.executescript(sql_content)
    else:
        # Fallback: inline view creation
        cursor.execute("""
                CREATE VIEW combined_turns AS
                WITH all_turns_with_next AS (
                    SELECT 
                        t.id, t.session_id, t.turn, t.role, t.workspace_id, t.workspace_name,
                        t.workspace_folder, t.session_name, t.agent_used, t.model_id,
                        t.text, t.original_text, t.files, t.languages, t.primary_language,
                        t.timestamp_ms, t.timestamp_iso, t.request_id,
                        t.original_text_tokens, t.cleaned_text_tokens, t.code_tokens, t.tool_tokens,
                        t.tools, t.response_time_ms,
                        t.total_lines_added, t.total_lines_removed, t.total_nloc_change,
                        t.weighted_complexity_change,
                        LEAD(t.turn) OVER (PARTITION BY t.session_id ORDER BY t.turn) AS next_turn,
                        LEAD(t.role) OVER (PARTITION BY t.session_id ORDER BY t.turn) AS next_role
                    FROM turns t
                ),
                user_turns AS (
                    SELECT 
                        id, session_id, turn, workspace_id, workspace_name, workspace_folder,
                        session_name, agent_used, model_id,
                        text AS user_cleaned_text, original_text AS user_original_text,
                        files AS user_files, languages AS user_languages,
                        primary_language AS user_primary_language,
                        timestamp_ms AS user_timestamp_ms, timestamp_iso AS user_timestamp_iso,
                        request_id AS user_request_id,
                        original_text_tokens AS user_original_text_tokens,
                        cleaned_text_tokens AS user_cleaned_text_tokens,
                        code_tokens AS user_code_tokens, tool_tokens AS user_tool_tokens,
                        next_turn, next_role,
                        ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY turn) - 1 AS exchange_index
                    FROM all_turns_with_next
                    WHERE role = 'user'
                ),
                assistant_turns AS (
                    SELECT 
                        id, session_id, turn, request_id,
                        text AS assistant_cleaned_text, original_text AS assistant_original_text,
                        files AS assistant_files, tools AS assistant_tools,
                        languages AS assistant_languages, primary_language AS assistant_primary_language,
                        timestamp_ms AS assistant_timestamp_ms, timestamp_iso AS assistant_timestamp_iso,
                        original_text_tokens AS assistant_original_text_tokens,
                        cleaned_text_tokens AS assistant_cleaned_text_tokens,
                        code_tokens AS assistant_code_tokens, tool_tokens AS assistant_tool_tokens,
                        response_time_ms, total_lines_added, total_lines_removed,
                        total_nloc_change, weighted_complexity_change,
                        model_id AS assistant_model_id
                    FROM all_turns_with_next
                    WHERE role = 'assistant'
                ),
                code_edits_aggregated AS (
                    SELECT 
                        request_id,
                        '[' || GROUP_CONCAT(
                            json_object(
                                'file_path', file_path,
                                'lines_added', lines_added,
                                'lines_removed', lines_removed,
                                'code_before', code_before,
                                'code_after', code_after,
                                'extra', json_object(
                                    'before_metrics', json(COALESCE(NULLIF(before_metrics, ''), '{}')),
                                    'after_metrics', json(COALESCE(NULLIF(after_metrics, ''), '{}')),
                                    'delta_metrics', json(COALESCE(NULLIF(delta_metrics, ''), '{}'))
                                )
                            )
                        ) || ']' AS code_edits_json
                    FROM code_metrics
                    GROUP BY request_id
                )
                SELECT
                    u.session_id || '_' || u.exchange_index AS id,
                    u.session_id, u.workspace_id, u.exchange_index,
                    NULL AS chunk_id,
                    u.turn AS user_turn_number, a.turn AS assistant_turn_number,
                    u.workspace_name, u.workspace_folder, u.session_name,
                    u.user_cleaned_text, u.user_original_text, u.user_files, u.user_languages,
                    u.user_primary_language, u.user_timestamp_ms, u.user_timestamp_iso,
                    u.user_request_id, u.user_original_text_tokens, u.user_cleaned_text_tokens,
                    u.user_code_tokens, u.user_tool_tokens,
                    COALESCE(a.assistant_cleaned_text, '') AS assistant_cleaned_text,
                    COALESCE(a.assistant_original_text, '') AS assistant_original_text,
                    COALESCE(a.assistant_files, '[]') AS assistant_files,
                    COALESCE(a.assistant_tools, '[]') AS assistant_tools,
                    COALESCE(a.assistant_languages, '[]') AS assistant_languages,
                    a.assistant_primary_language, a.assistant_timestamp_ms, a.assistant_timestamp_iso,
                    COALESCE(a.request_id, '') AS assistant_request_id,
                    COALESCE(a.assistant_original_text_tokens, 0) AS assistant_original_text_tokens,
                    COALESCE(a.assistant_cleaned_text_tokens, 0) AS assistant_cleaned_text_tokens,
                    COALESCE(a.assistant_code_tokens, 0) AS assistant_code_tokens,
                    COALESCE(a.assistant_tool_tokens, 0) AS assistant_tool_tokens,
                    a.response_time_ms,
                    COALESCE(ce.code_edits_json, '[]') AS code_edits,
                    COALESCE(a.total_lines_added, 0) AS total_lines_added,
                    COALESCE(a.total_lines_removed, 0) AS total_lines_removed,
                    COALESCE(a.total_nloc_change, 0) AS total_nloc_change,
                    COALESCE(a.weighted_complexity_change, 0.0) AS weighted_complexity_change,
                    COALESCE(a.assistant_model_id, u.model_id) AS model_id,
                    u.agent_used
                FROM user_turns u
                LEFT JOIN assistant_turns a 
                    ON u.session_id = a.session_id 
                    AND u.next_turn = a.turn
                    AND u.next_role = 'assistant'
                LEFT JOIN code_edits_aggregated ce
                    ON a.request_id = ce.request_id
                ORDER BY u.session_id, u.exchange_index
                """)
    
    logger.info("  Created combined_turns VIEW")

    conn.commit()
    logger.debug("Ensured combined_turns view exists")


def ensure_code_metrics_table(conn: sqlite3.Connection) -> None:
    """Create code_metrics table if it doesn't exist.
    
    Stores code metrics for extracted code edits.
    Includes UNIQUE constraint on (request_id, file_path) to prevent duplicates.
    """
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS code_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,  -- Source request or message identifier
                session_id TEXT,
                file_path TEXT NOT NULL,
                
                -- Context (redundant but useful for direct queries)
                workspace_id TEXT,
                agent_used TEXT,
                model_id TEXT,
                
                -- Common metrics
                delta_nloc INTEGER,
                delta_complexity REAL,
                lines_added INTEGER,
                lines_removed INTEGER,
                
                -- Full metrics JSON
                before_metrics TEXT,
                after_metrics TEXT,
                delta_metrics TEXT,
                
                -- Original code content (before and after the edit)
                code_before TEXT,
                code_after TEXT,
                
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                
                UNIQUE(request_id, file_path)
            )
        """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_metrics_request ON code_metrics(request_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_metrics_session ON code_metrics(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_metrics_workspace ON code_metrics(workspace_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_metrics_agent ON code_metrics(agent_used)")
    
    conn.commit()
    logger.debug("Ensured code_metrics table exists")

def get_primary_key_info(conn: sqlite3.Connection, table_name: str) -> tuple:
    """Get the primary key column name and type from a table or view.
    
    Args:
        conn: SQLite connection
        table_name: Name of the table or view to inspect
        
    Returns:
        Tuple of (column_name, column_type)
        
    Raises:
        ValueError: If no primary key is found
    """
    cursor = conn.cursor()
    
    # Check if it's a view or table
    cursor.execute("""
        SELECT type FROM sqlite_master 
        WHERE name = ? AND type IN ('table', 'view')
    """, (table_name,))
    result = cursor.fetchone()
    
    if not result:
        raise ValueError(f"Table or view '{table_name}' not found")
    
    obj_type = result[0]
    
    # For views, we need to handle special cases
    if obj_type == 'view':
        # Special handling for combined_turns view - uses composite id
        if table_name == 'combined_turns':
            return ('id', 'TEXT')
        
        # For other views, try to find an 'id' column in the view definition
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = cursor.fetchall()
        for col in columns:
            cid, name, col_type, notnull, dflt_value, pk = col
            if name.lower() == 'id':
                return (name, col_type or 'TEXT')
        
        raise ValueError(f"View '{table_name}' has no identifiable primary key column")
    
    # For tables, use PRAGMA table_info to find the primary key
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = cursor.fetchall()
    
    # PRAGMA table_info returns: (cid, name, type, notnull, dflt_value, pk)
    # pk > 0 means it's part of the primary key
    for col in columns:
        cid, name, col_type, notnull, dflt_value, pk = col
        if pk > 0:
            return (name, col_type)
    
    # Fallback if no explicit PK found
    raise ValueError(f"No primary key found in table '{table_name}'")

# JSON utility functions
def parse_json_field(value: Any, default: Any = None) -> Any:
    """Parse JSON field from database, handling both string and already-parsed values.
    
    Useful when reading from SQLite where JSON might be stored as TEXT (string)
    or when data is already parsed.
    
    Args:
        value: Value to parse (can be string, dict, list, or any type)
        default: Default value if parsing fails or value is None/empty
        
    Returns:
        Parsed value if it's JSON, otherwise the original value or default
    """
    if not value:
        return default if default is not None else []
    
    # If already parsed, return as-is
    if isinstance(value, (dict, list)):
        return value
    
    # Try to parse as JSON string
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default if default is not None else []
    
    return value

def json_dumps_for_db(value: Any) -> str:
    """Convert a value to JSON string for database storage.
    
    Args:
        value: Value to convert (dict, list, or any JSON-serializable type)
        
    Returns:
        JSON string representation
    """
    if value is None:
        return ""
    result = json.dumps(value, ensure_ascii=False)
    # Strip lone Unicode surrogates (U+D800-U+DFFF) that survive from malformed
    # source content. They cannot be encoded to UTF-8 and would crash the SQLite
    # write for any JSON column (raw_json, extra_json, metadata_json, ...).
    try:
        result.encode("utf-8")
    except UnicodeEncodeError:
        result = result.encode("utf-8", "ignore").decode("utf-8", "ignore")
    return result


