"""Database persistence for low-level parsed workspaces."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any, Optional, cast

from src.extract.models import (
    AttachmentRef,
    CommandEnvelope,
    ContentBlockRecord,
    ParsedSession,
    ParsedWorkspace,
    ParserIssue,
    SessionEventRecord,
    SessionLinkRecord,
    TokenUsage,
    ToolCallRecord,
    ToolResultRecord,
    WorkspaceDescriptor,
)
from src.shared.database.db_extract import sanitize_unicode
from src.shared.database.db_schema import ensure_parsed_tables, json_dumps_for_db, parse_json_field
from src.shared.logging.logger import get_logger

logger = get_logger(__name__)


@dataclass
class _SessionRowState:
    title: str
    source_path: str
    started_at_ms: Optional[int]
    ended_at_ms: Optional[int]
    parent_session_id: str
    relationship_type: str
    metadata: dict[str, Any]
    issues: list[ParserIssue]


def delete_parsed_workspace(
    conn: sqlite3.Connection,
    workspace_id: str,
    agent_name: str,
) -> dict[str, int]:
    """Delete raw parsed persistence for one agent workspace."""
    ensure_parsed_tables(conn)
    try:
        deleted = _delete_parsed_workspace_rows(conn, workspace_id, agent_name)
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise


def upsert_parsed_workspace(conn: sqlite3.Connection, parsed_workspace: ParsedWorkspace) -> dict[str, int]:
    """Replace persisted parsed rows for a workspace with the supplied parsed data."""
    ensure_parsed_tables(conn)
    descriptor = parsed_workspace.descriptor
    cursor = conn.cursor()

    try:
        _delete_parsed_workspace_rows(conn, descriptor.workspace_id, descriptor.agent_name)

        cursor.execute(
            """
            INSERT INTO parsed_workspaces (
                workspace_id, agent_name, workspace_name, workspace_folder, source_root,
                descriptor_metadata_json, workspace_metadata_json, issues_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                descriptor.workspace_id,
                descriptor.agent_name,
                sanitize_unicode(descriptor.workspace_name),
                sanitize_unicode(descriptor.workspace_folder),
                sanitize_unicode(descriptor.source_root),
                json_dumps_for_db(descriptor.metadata),
                json_dumps_for_db(parsed_workspace.metadata),
                json_dumps_for_db([issue.to_dict() for issue in parsed_workspace.issues]),
            ),
        )
        workspace_row_id = cast(int, cursor.lastrowid)

        counts = {
            "workspaces": 1,
            "sessions": 0,
            "events": 0,
            "content_blocks": 0,
            "tool_calls": 0,
            "tool_results": 0,
            "attachments": 0,
            "session_links": 0,
        }

        # Track inserted session rows so duplicate session_ids within the same
        # workspace (e.g. a Codex session resumed across multiple rollout files)
        # are merged into a single session row instead of violating the
        # UNIQUE(workspace_row_id, session_id) constraint. Event and link indices
        # are assigned from a per-row running counter so they never collide on
        # their own UNIQUE(session_row_id, index) constraints.
        session_row_by_id: dict[str, int] = {}
        event_cursor_by_row: dict[int, int] = {}
        link_cursor_by_row: dict[int, int] = {}
        session_state_by_row: dict[int, _SessionRowState] = {}

        for session in parsed_workspace.sessions:
            session_row_id = session_row_by_id.get(session.session_id)
            if session_row_id is None:
                cursor.execute(
                    """
                    INSERT INTO parsed_sessions (
                        workspace_row_id, session_id, title, source_path,
                        started_at_ms, ended_at_ms, parent_session_id, relationship_type,
                        metadata_json, issues_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_row_id,
                        session.session_id,
                        sanitize_unicode(session.title),
                        sanitize_unicode(session.source_path),
                        session.started_at_ms,
                        session.ended_at_ms,
                        session.parent_session_id or None,
                        session.relationship_type or None,
                        json_dumps_for_db(session.metadata),
                        json_dumps_for_db([issue.to_dict() for issue in session.issues]),
                    ),
                )
                session_row_id = cast(int, cursor.lastrowid)
                session_row_by_id[session.session_id] = session_row_id
                event_cursor_by_row[session_row_id] = 0
                link_cursor_by_row[session_row_id] = 0
                session_state_by_row[session_row_id] = _session_row_state_from_session(session)
                counts["sessions"] += 1
            else:
                state = session_state_by_row[session_row_id]
                _merge_session_row_state(state, session)
                _update_session_row(cursor, session_row_id, state)

            event_offset = event_cursor_by_row[session_row_id]
            event_index_by_source = {
                event.index: event_offset + event_ordinal
                for event_ordinal, event in enumerate(session.events)
            }

            for link in session.links:
                link_index = link_cursor_by_row[session_row_id]
                link_cursor_by_row[session_row_id] = link_index + 1
                trigger_event_index = link.trigger_event_index
                if trigger_event_index is not None:
                    trigger_event_index = event_index_by_source.get(
                        trigger_event_index,
                        event_offset + trigger_event_index,
                    )
                cursor.execute(
                    """
                    INSERT INTO parsed_session_links (
                        session_row_id, link_index, target_session_id, relationship_type,
                        trigger_event_index, trigger_tool_call_id, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_row_id,
                        link_index,
                        link.target_session_id,
                        link.relationship_type,
                        trigger_event_index,
                        link.trigger_tool_call_id or None,
                        json_dumps_for_db(link.extra),
                    ),
                )
                counts["session_links"] += 1

            for event in session.events:
                event_index = event_cursor_by_row[session_row_id]
                event_cursor_by_row[session_row_id] = event_index + 1
                event_row_id = _insert_event(cursor, session_row_id, event, event_index=event_index)
                counts["events"] += 1

                for block_index, block in enumerate(event.content_blocks):
                    cursor.execute(
                        """
                        INSERT INTO parsed_content_blocks (
                            event_row_id, block_index, kind, text, data_json, raw_json, extra_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_row_id,
                            block_index,
                            block.kind,
                            sanitize_unicode(block.text),
                            json_dumps_for_db(block.data),
                            json_dumps_for_db(block.raw),
                            json_dumps_for_db(block.extra),
                        ),
                    )
                    counts["content_blocks"] += 1

                for call_index, tool_call in enumerate(event.tool_calls):
                    cursor.execute(
                        """
                        INSERT INTO parsed_tool_calls (
                            event_row_id, call_index, call_id, name, kind,
                            arguments_json, arguments_text, file_paths_json,
                            spawned_session_id, status, raw_json, extra_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_row_id,
                            call_index,
                            tool_call.call_id or None,
                            tool_call.name or None,
                            tool_call.kind or None,
                            json_dumps_for_db(tool_call.arguments),
                            sanitize_unicode(tool_call.arguments_text),
                            json_dumps_for_db(tool_call.file_paths),
                            tool_call.spawned_session_id,
                            tool_call.status,
                            json_dumps_for_db(tool_call.raw),
                            json_dumps_for_db(tool_call.extra),
                        ),
                    )
                    counts["tool_calls"] += 1

                for result_index, tool_result in enumerate(event.tool_results):
                    cursor.execute(
                        """
                        INSERT INTO parsed_tool_results (
                            event_row_id, result_index, tool_call_id, kind, text,
                            structured_content_json, is_error, status, raw_json, extra_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_row_id,
                            result_index,
                            tool_result.tool_call_id or None,
                            tool_result.kind or None,
                            sanitize_unicode(tool_result.text),
                            json_dumps_for_db(tool_result.structured_content),
                            None if tool_result.is_error is None else int(tool_result.is_error),
                            tool_result.status,
                            json_dumps_for_db(tool_result.raw),
                            json_dumps_for_db(tool_result.extra),
                        ),
                    )
                    counts["tool_results"] += 1

                for attachment_index, attachment in enumerate(event.attachments):
                    cursor.execute(
                        """
                        INSERT INTO parsed_attachments (
                            event_row_id, attachment_index, kind, path, title, media_type,
                            raw_json, extra_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_row_id,
                            attachment_index,
                            attachment.kind,
                            sanitize_unicode(attachment.path),
                            sanitize_unicode(attachment.title),
                            sanitize_unicode(attachment.media_type),
                            json_dumps_for_db(attachment.raw),
                            json_dumps_for_db(attachment.extra),
                        ),
                    )
                    counts["attachments"] += 1

        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        logger.exception(
            "Failed to upsert parsed workspace %s/%s",
            descriptor.agent_name,
            descriptor.workspace_id,
        )
        raise


def get_parsed_workspace(
    conn: sqlite3.Connection,
    workspace_id: str,
    agent_name: str,
) -> Optional[ParsedWorkspace]:
    """Load one persisted parsed workspace."""
    ensure_parsed_tables(conn)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, workspace_name, workspace_folder, source_root,
               descriptor_metadata_json, workspace_metadata_json, issues_json
        FROM parsed_workspaces
        WHERE workspace_id = ? AND agent_name = ?
        """,
        (workspace_id, agent_name),
    )
    row = cursor.fetchone()
    if not row:
        return None

    workspace_row_id = row[0]
    descriptor = WorkspaceDescriptor(
        workspace_id=workspace_id,
        agent_name=agent_name,
        workspace_name=row[1] or "",
        workspace_folder=row[2] or "",
        source_root=row[3] or "",
        metadata=cast(dict[str, Any], parse_json_field(row[4], {})),
    )
    workspace_metadata = cast(dict[str, Any], parse_json_field(row[5], {}))
    workspace_issues = _parse_issues(row[6])

    cursor.execute(
        """
        SELECT id, session_id, title, source_path, started_at_ms, ended_at_ms,
               parent_session_id, relationship_type, metadata_json, issues_json
        FROM parsed_sessions
        WHERE workspace_row_id = ?
        ORDER BY started_at_ms ASC, id ASC
        """,
        (workspace_row_id,),
    )

    sessions: list[ParsedSession] = []
    for session_row in cursor.fetchall():
        session_row_id = session_row[0]
        links = _load_session_links(cursor, session_row_id)
        events = _load_session_events(cursor, session_row_id)
        sessions.append(
            ParsedSession(
                session_id=session_row[1],
                agent_name=agent_name,
                workspace_id=workspace_id,
                workspace_name=descriptor.workspace_name,
                workspace_folder=descriptor.workspace_folder,
                title=session_row[2] or "",
                source_path=session_row[3] or "",
                started_at_ms=session_row[4],
                ended_at_ms=session_row[5],
                parent_session_id=session_row[6] or "",
                relationship_type=session_row[7] or "",
                events=events,
                links=links,
                metadata=cast(dict[str, Any], parse_json_field(session_row[8], {})),
                issues=_parse_issues(session_row[9]),
            )
        )

    return ParsedWorkspace(
        descriptor=descriptor,
        sessions=sessions,
        issues=workspace_issues,
        metadata=workspace_metadata,
    )


def _session_row_state_from_session(session: ParsedSession) -> _SessionRowState:
    return _SessionRowState(
        title=session.title,
        source_path=session.source_path,
        started_at_ms=session.started_at_ms,
        ended_at_ms=session.ended_at_ms,
        parent_session_id=session.parent_session_id,
        relationship_type=session.relationship_type,
        metadata=dict(session.metadata),
        issues=list(session.issues),
    )


def _merge_session_row_state(state: _SessionRowState, session: ParsedSession) -> None:
    if session.title:
        state.title = session.title
    if session.source_path:
        state.source_path = session.source_path
    if session.started_at_ms is not None and (
        state.started_at_ms is None or session.started_at_ms < state.started_at_ms
    ):
        state.started_at_ms = session.started_at_ms
    if session.ended_at_ms is not None and (
        state.ended_at_ms is None or session.ended_at_ms > state.ended_at_ms
    ):
        state.ended_at_ms = session.ended_at_ms
    if session.parent_session_id:
        state.parent_session_id = session.parent_session_id
    if session.relationship_type:
        state.relationship_type = session.relationship_type
    state.metadata.update(session.metadata)
    state.issues.extend(session.issues)


def _update_session_row(cursor: sqlite3.Cursor, session_row_id: int, state: _SessionRowState) -> None:
    cursor.execute(
        """
        UPDATE parsed_sessions
           SET title = ?, source_path = ?, started_at_ms = ?, ended_at_ms = ?,
               parent_session_id = ?, relationship_type = ?, metadata_json = ?,
               issues_json = ?, updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
        """,
        (
            sanitize_unicode(state.title),
            sanitize_unicode(state.source_path),
            state.started_at_ms,
            state.ended_at_ms,
            state.parent_session_id or None,
            state.relationship_type or None,
            json_dumps_for_db(state.metadata),
            json_dumps_for_db([issue.to_dict() for issue in state.issues]),
            session_row_id,
        ),
    )


def _delete_parsed_workspace_rows(
    conn: sqlite3.Connection,
    workspace_id: str,
    agent_name: str,
) -> dict[str, int]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM parsed_workspaces WHERE workspace_id = ? AND agent_name = ?",
        (workspace_id, agent_name),
    )
    row = cursor.fetchone()
    if not row:
        return {
            "workspaces": 0,
            "sessions": 0,
            "events": 0,
            "content_blocks": 0,
            "tool_calls": 0,
            "tool_results": 0,
            "attachments": 0,
            "session_links": 0,
        }

    workspace_row_id = row[0]
    deleted: dict[str, int] = {}
    statements = [
        (
            "attachments",
            "DELETE FROM parsed_attachments WHERE event_row_id IN (SELECT id FROM parsed_events WHERE session_row_id IN (SELECT id FROM parsed_sessions WHERE workspace_row_id = ?))",
        ),
        (
            "tool_results",
            "DELETE FROM parsed_tool_results WHERE event_row_id IN (SELECT id FROM parsed_events WHERE session_row_id IN (SELECT id FROM parsed_sessions WHERE workspace_row_id = ?))",
        ),
        (
            "tool_calls",
            "DELETE FROM parsed_tool_calls WHERE event_row_id IN (SELECT id FROM parsed_events WHERE session_row_id IN (SELECT id FROM parsed_sessions WHERE workspace_row_id = ?))",
        ),
        (
            "content_blocks",
            "DELETE FROM parsed_content_blocks WHERE event_row_id IN (SELECT id FROM parsed_events WHERE session_row_id IN (SELECT id FROM parsed_sessions WHERE workspace_row_id = ?))",
        ),
        (
            "session_links",
            "DELETE FROM parsed_session_links WHERE session_row_id IN (SELECT id FROM parsed_sessions WHERE workspace_row_id = ?)",
        ),
        (
            "events",
            "DELETE FROM parsed_events WHERE session_row_id IN (SELECT id FROM parsed_sessions WHERE workspace_row_id = ?)",
        ),
        (
            "sessions",
            "DELETE FROM parsed_sessions WHERE workspace_row_id = ?",
        ),
        (
            "workspaces",
            "DELETE FROM parsed_workspaces WHERE id = ?",
        ),
    ]
    for label, statement in statements:
        cursor.execute(statement, (workspace_row_id,))
        deleted[label] = cursor.rowcount
    return deleted


def _insert_event(cursor: sqlite3.Cursor, session_row_id: int, event: SessionEventRecord, event_index: int | None = None) -> int:
    cursor.execute(
        """
        INSERT INTO parsed_events (
            session_row_id, event_index, event_type, role, timestamp_ms, timestamp_iso,
            message_id, request_id, model_id, text, thinking_text,
            token_usage_json, command_json, file_paths_json, raw_json, extra_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_row_id,
            event.index if event_index is None else event_index,
            event.event_type,
            event.role or None,
            event.timestamp_ms,
            event.timestamp_iso or None,
            event.message_id or None,
            event.request_id or None,
            event.model_id or None,
            sanitize_unicode(event.text),
            sanitize_unicode(event.thinking_text),
            json_dumps_for_db(event.token_usage.to_dict()) if event.token_usage else "",
            json_dumps_for_db(event.command.to_dict()) if event.command else "",
            json_dumps_for_db(event.file_paths),
            json_dumps_for_db(event.raw),
            json_dumps_for_db(event.extra),
        ),
    )
    return cast(int, cursor.lastrowid)


def _load_session_links(cursor: sqlite3.Cursor, session_row_id: int) -> list[SessionLinkRecord]:
    cursor.execute(
        """
        SELECT target_session_id, relationship_type, trigger_event_index,
               trigger_tool_call_id, extra_json
         FROM parsed_session_links
        WHERE session_row_id = ?
        ORDER BY link_index ASC
        """,
        (session_row_id,),
    )
    links: list[SessionLinkRecord] = []
    for row in cursor.fetchall():
        links.append(
            SessionLinkRecord(
                target_session_id=row[0],
                relationship_type=row[1],
                trigger_event_index=row[2],
                trigger_tool_call_id=row[3] or "",
                extra=cast(dict[str, Any], parse_json_field(row[4], {})),
            )
        )
    return links


def _load_session_events(cursor: sqlite3.Cursor, session_row_id: int) -> list[SessionEventRecord]:
    cursor.execute(
        """
        SELECT id, event_index, event_type, role, timestamp_ms, timestamp_iso,
               message_id, request_id, model_id, text, thinking_text,
               token_usage_json, command_json, file_paths_json, raw_json, extra_json
        FROM parsed_events
        WHERE session_row_id = ?
        ORDER BY event_index ASC
        """,
        (session_row_id,),
    )
    events: list[SessionEventRecord] = []
    for row in cursor.fetchall():
        event_row_id = row[0]
        token_usage_payload = parse_json_field(row[11], None)
        command_payload = parse_json_field(row[12], None)
        events.append(
            SessionEventRecord(
                index=row[1],
                event_type=row[2],
                role=row[3] or "",
                timestamp_ms=row[4],
                timestamp_iso=row[5] or "",
                message_id=row[6] or "",
                request_id=row[7] or "",
                model_id=row[8] or "",
                text=row[9] or "",
                thinking_text=row[10] or "",
                token_usage=TokenUsage.from_dict(token_usage_payload) if isinstance(token_usage_payload, dict) else None,
                command=CommandEnvelope.from_dict(command_payload) if isinstance(command_payload, dict) else None,
                content_blocks=_load_content_blocks(cursor, event_row_id),
                tool_calls=_load_tool_calls(cursor, event_row_id),
                tool_results=_load_tool_results(cursor, event_row_id),
                attachments=_load_attachments(cursor, event_row_id),
                file_paths=cast(list[str], parse_json_field(row[13], [])),
                raw=cast(dict[str, Any], parse_json_field(row[14], {})),
                extra=cast(dict[str, Any], parse_json_field(row[15], {})),
            )
        )
    return events


def _load_content_blocks(cursor: sqlite3.Cursor, event_row_id: int) -> list[ContentBlockRecord]:
    cursor.execute(
        """
        SELECT block_index, kind, text, data_json, raw_json, extra_json
        FROM parsed_content_blocks
        WHERE event_row_id = ?
        ORDER BY block_index ASC
        """,
        (event_row_id,),
    )
    return [
        ContentBlockRecord(
            index=row[0],
            kind=row[1],
            text=row[2] or "",
            data=cast(dict[str, Any], parse_json_field(row[3], {})),
            raw=cast(dict[str, Any], parse_json_field(row[4], {})),
            extra=cast(dict[str, Any], parse_json_field(row[5], {})),
        )
        for row in cursor.fetchall()
    ]


def _load_tool_calls(cursor: sqlite3.Cursor, event_row_id: int) -> list[ToolCallRecord]:
    cursor.execute(
        """
        SELECT call_id, name, kind, arguments_json, arguments_text,
               file_paths_json, spawned_session_id, status, raw_json, extra_json
        FROM parsed_tool_calls
        WHERE event_row_id = ?
        ORDER BY call_index ASC
        """,
        (event_row_id,),
    )
    return [
        ToolCallRecord(
            call_id=row[0] or "",
            name=row[1] or "",
            kind=row[2] or "",
            arguments=cast(dict[str, Any], parse_json_field(row[3], {})),
            arguments_text=row[4] or "",
            file_paths=cast(list[str], parse_json_field(row[5], [])),
            spawned_session_id=row[6],
            status=row[7],
            raw=cast(dict[str, Any], parse_json_field(row[8], {})),
            extra=cast(dict[str, Any], parse_json_field(row[9], {})),
        )
        for row in cursor.fetchall()
    ]


def _load_tool_results(cursor: sqlite3.Cursor, event_row_id: int) -> list[ToolResultRecord]:
    cursor.execute(
        """
        SELECT tool_call_id, kind, text, structured_content_json,
               is_error, status, raw_json, extra_json
        FROM parsed_tool_results
        WHERE event_row_id = ?
        ORDER BY result_index ASC
        """,
        (event_row_id,),
    )
    results: list[ToolResultRecord] = []
    for row in cursor.fetchall():
        is_error: Optional[bool]
        if row[4] is None:
            is_error = None
        else:
            is_error = bool(row[4])
        results.append(
            ToolResultRecord(
                tool_call_id=row[0] or "",
                kind=row[1] or "",
                text=row[2] or "",
                structured_content=cast(dict[str, Any], parse_json_field(row[3], {})),
                is_error=is_error,
                status=row[5],
                raw=cast(dict[str, Any], parse_json_field(row[6], {})),
                extra=cast(dict[str, Any], parse_json_field(row[7], {})),
            )
        )
    return results


def _load_attachments(cursor: sqlite3.Cursor, event_row_id: int) -> list[AttachmentRef]:
    cursor.execute(
        """
        SELECT kind, path, title, media_type, raw_json, extra_json
        FROM parsed_attachments
        WHERE event_row_id = ?
        ORDER BY attachment_index ASC
        """,
        (event_row_id,),
    )
    return [
        AttachmentRef(
            kind=row[0],
            path=row[1] or "",
            title=row[2] or "",
            media_type=row[3] or "",
            raw=cast(dict[str, Any], parse_json_field(row[4], {})),
            extra=cast(dict[str, Any], parse_json_field(row[5], {})),
        )
        for row in cursor.fetchall()
    ]


def _parse_issues(value: Any) -> list[ParserIssue]:
    payload = parse_json_field(value, [])
    if not isinstance(payload, list):
        return []
    return [ParserIssue.from_dict(item) for item in payload if isinstance(item, dict)]

