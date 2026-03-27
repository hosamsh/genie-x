"""
Extraction Data Provider - Provides extraction statistics and code metrics.

Queries are performed by workspace_folder (normalized, case-insensitive) to
enable cross-agent consolidation. When multiple agents (copilot, claude_code,
cursor) work on the same folder, they share the same workspace_folder even
if they have different workspace_ids.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional


def _normalize_folder(folder: str) -> str:
    """Normalize folder path for case-insensitive comparison.

    Uses ``str.replace`` instead of ``Path.as_posix()`` so that URI-style
    paths (e.g. ``vscode-remote://wsl+…``) are not mangled (``://`` → ``:/``).
    """
    return folder.replace("\\", "/").lower() if folder else ""


class ExtractionDataProvider:
    """
    Provides extraction-related data for dashboard elements.
    
    Handles workspace info, code metrics, model usage, timelines,
    languages, and tool usage.
    
    All queries use workspace_folder for cross-agent consolidation.
    """

    def __init__(self, db_connection: sqlite3.Connection, workspace_id: str, workspace_folder: Optional[str] = None):
        self.conn = db_connection
        self.workspace_id = workspace_id
        # Use workspace_folder if provided, otherwise we'll resolve it lazily
        self._workspace_folder = workspace_folder
        self._extraction_cache: Optional[Dict] = None
        self._code_metrics_cache: Optional[Dict] = None
    
    @property
    def workspace_folder(self) -> str:
        """Get the workspace folder, resolving from DB if not provided."""
        if self._workspace_folder:
            return self._workspace_folder
        
        # Try to resolve from database
        cursor = self.conn.execute(
            """SELECT workspace_folder FROM turns 
               WHERE workspace_id = ? AND workspace_folder IS NOT NULL AND workspace_folder != ''
               LIMIT 1""",
            (self.workspace_id,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            self._workspace_folder = row[0]
            return self._workspace_folder
        
        # Fallback to workspace_id as folder (shouldn't happen normally)
        return self.workspace_id
    
    def _folder_filter(self) -> str:
        """Get normalized folder for SQL queries."""
        return _normalize_folder(self.workspace_folder)

    def get_extraction_stats(self) -> Dict[str, Any]:
        """Get extraction statistics for the workspace."""
        if self._extraction_cache:
            return self._extraction_cache

        folder_filter = self._folder_filter()
        
        # Query workspace_info - aggregate across all workspace_ids with same folder
        cursor = self.conn.execute(
            """SELECT workspace_name, workspace_folder, 
                      GROUP_CONCAT(DISTINCT agent_used) as agents,
                      SUM(session_count) as session_count, 
                      SUM(turn_count) as turn_count,
                      SUM(total_code_loc) as total_code_loc, 
                      SUM(total_doc_loc) as total_doc_loc, 
                      SUM(extraction_duration_ms) as extraction_duration_ms
               FROM workspace_info 
               WHERE LOWER(REPLACE(workspace_folder, '\\', '/')) = ?
               GROUP BY LOWER(REPLACE(workspace_folder, '\\', '/'))""",
            (folder_filter,)
        )
        row = cursor.fetchone()

        if not row:
            return {
                "session_count": 0,
                "turn_count": 0,
                "sessions_with_code": 0,
                "extraction_duration_ms": 0,
                "total_visible_tokens": 0,
            }

        # Get token counts from turns table
        cursor = self.conn.execute(
            """SELECT SUM(CAST(original_text_tokens AS INTEGER)),
                      SUM(CAST(cleaned_text_tokens AS INTEGER)),
                      SUM(CAST(code_tokens AS INTEGER)),
                      SUM(CAST(tool_tokens AS INTEGER))
               FROM turns 
               WHERE LOWER(REPLACE(workspace_folder, '\\', '/')) = ?""",
            (folder_filter,)
        )
        token_row = cursor.fetchone()
        total_visible_tokens = sum(x or 0 for x in token_row) if token_row else 0

        total_code_loc = row[5] or 0

        # Fallback: use total_lines_added as an approximation
        if total_code_loc == 0:
            cursor = self.conn.execute(
                """SELECT SUM(CAST(total_lines_added AS INTEGER)) FROM turns 
                   WHERE LOWER(REPLACE(workspace_folder, '\\', '/')) = ?""",
                (folder_filter,)
            )
            fallback_row = cursor.fetchone()
            if fallback_row and fallback_row[0]:
                total_code_loc = fallback_row[0]

        self._extraction_cache = {
            "workspace_name": row[0],
            "workspace_folder": row[1],
            "agents": (row[2] or "").split(",") if row[2] else [],
            "session_count": row[3] or 0,
            "turn_count": row[4] or 0,
            "total_code_loc": total_code_loc,
            "total_doc_loc": row[6] or 0,
            "extraction_duration_ms": row[7] or 0,
            "total_visible_tokens": total_visible_tokens,
        }

        return self._extraction_cache

    def get_code_metrics(self) -> Dict[str, Any]:
        """Get code metrics for the workspace."""
        if self._code_metrics_cache:
            return self._code_metrics_cache

        folder_filter = self._folder_filter()

        # code_metrics table doesn't have workspace_folder, so we join with turns
        # or query all workspace_ids that share the same folder
        cursor = self.conn.execute(
            """SELECT COUNT(DISTINCT cm.session_id) as sessions_with_code,
                      SUM(cm.lines_added) as total_lines_added,
                      SUM(cm.lines_removed) as total_lines_removed
               FROM code_metrics cm
               JOIN turns t ON cm.request_id = t.request_id
               WHERE LOWER(REPLACE(t.workspace_folder, '\\', '/')) = ?""",
            (folder_filter,)
        )
        metrics_row = cursor.fetchone()

        sessions_with_code = metrics_row[0] or 0
        total_lines_added = metrics_row[1] or 0
        total_lines_removed = metrics_row[2] or 0

        # Fallback to turns table
        if total_lines_added == 0 and total_lines_removed == 0:
            cursor = self.conn.execute(
                """SELECT COUNT(DISTINCT session_id) as sessions_with_code,
                          SUM(CAST(total_lines_added AS INTEGER)) as total_lines_added,
                          SUM(CAST(total_lines_removed AS INTEGER)) as total_lines_removed
                   FROM turns 
                   WHERE LOWER(REPLACE(workspace_folder, '\\', '/')) = ?
                     AND (total_lines_added > 0 OR total_lines_removed > 0)""",
                (folder_filter,)
            )
            fallback_row = cursor.fetchone()
            if fallback_row:
                sessions_with_code = fallback_row[0] or sessions_with_code
                total_lines_added = fallback_row[1] or total_lines_added
                total_lines_removed = fallback_row[2] or total_lines_removed

        extraction_stats = self.get_extraction_stats()
        total_code_loc = extraction_stats.get("total_code_loc", 0)
        total_doc_loc = extraction_stats.get("total_doc_loc", 0)

        # Calculate AI contribution percentage
        ai_contribution_pct = 0
        if total_lines_added > 0:
            if total_code_loc > 0:
                ai_contribution_pct = min(100.0, round((total_lines_added / total_code_loc) * 100, 1))
            else:
                ai_contribution_pct = 100.0

        self._code_metrics_cache = {
            "sessions_with_code": sessions_with_code,
            "total_lines_added": total_lines_added,
            "total_lines_removed": total_lines_removed,
            "total_code_loc": total_code_loc,
            "total_doc_loc": total_doc_loc,
            "ai_contribution_pct": ai_contribution_pct,
            "total_files_edited": 0,
        }

        return self._code_metrics_cache

    def get_model_usage(self, **kwargs) -> List[Dict[str, Any]]:
        """Get model usage distribution by lines of code."""
        folder_filter = self._folder_filter()
        
        # code_metrics table doesn't have workspace_folder, so we join with turns
        cursor = self.conn.execute(
            """SELECT cm.model_id, SUM(cm.lines_added) as locs
               FROM code_metrics cm
               JOIN turns t ON cm.request_id = t.request_id
               WHERE LOWER(REPLACE(t.workspace_folder, '\\', '/')) = ? AND cm.model_id IS NOT NULL
               GROUP BY cm.model_id
               ORDER BY locs DESC""",
            (folder_filter,)
        )
        model_usage = [{"model": row[0], "locs": row[1]} for row in cursor.fetchall()]

        # Fallback to turns table
        if not model_usage:
            cursor = self.conn.execute(
                """SELECT model_id, SUM(CAST(total_lines_added AS INTEGER)) as locs
                   FROM turns 
                   WHERE LOWER(REPLACE(workspace_folder, '\\', '/')) = ? AND model_id IS NOT NULL AND total_lines_added > 0
                   GROUP BY model_id
                   ORDER BY locs DESC""",
                (folder_filter,)
            )
            model_usage = [{"model": row[0], "locs": row[1]} for row in cursor.fetchall()]

        return model_usage

    def get_ai_contribution_breakdown(self) -> List[Dict[str, Any]]:
        """Get AI contribution as a breakdown for doughnut chart.
        
        Returns empty list if there's no code data, so the chart won't be rendered.
        """
        code_metrics = self.get_code_metrics()
        ai_loc = code_metrics.get("total_lines_added", 0)
        total_loc = code_metrics.get("total_code_loc", 0)
        
        # Only return data if there's actual code in the workspace
        if total_loc == 0 and ai_loc == 0:
            return []
        
        other_loc = max(0, total_loc - ai_loc)

        return [
            {"label": "AI Generated", "value": ai_loc},
            {"label": "Other Code", "value": other_loc},
        ]

    def get_code_timeline(self) -> List[Dict[str, Any]]:
        """Get code contribution timeline."""
        folder_filter = self._folder_filter()
        
        cursor = self.conn.execute(
            """SELECT (SUBSTR(REPLACE(t.timestamp_iso, ' ', 'T'), 1, 13) || ':00') as date, 
                      SUM(cm.lines_added) as added,
                      SUM(cm.lines_removed) as removed
               FROM code_metrics cm
               JOIN turns t ON cm.request_id = t.request_id
               WHERE LOWER(REPLACE(t.workspace_folder, '\\', '/')) = ? AND t.timestamp_iso IS NOT NULL AND t.timestamp_iso != ''
               GROUP BY date
               ORDER BY date""",
            (folder_filter,)
        )
        return [{"date": row[0], "added": row[1] or 0, "removed": row[2] or 0} for row in cursor.fetchall()]

    def get_languages(self, **kwargs) -> List[Dict[str, Any]]:
        """Get languages used in the workspace."""
        folder_filter = self._folder_filter()
        
        cursor = self.conn.execute(
            """SELECT primary_language, COUNT(*) as change_count
               FROM turns
               WHERE LOWER(REPLACE(workspace_folder, '\\', '/')) = ? AND primary_language IS NOT NULL AND primary_language != ''
               GROUP BY primary_language
               ORDER BY change_count DESC
               LIMIT 10""",
            (folder_filter,)
        )
        return [{"language": row[0], "change_count": row[1]} for row in cursor.fetchall()]

    def get_tool_usage(self, **kwargs) -> List[Dict[str, Any]]:
        """Get tool usage statistics."""
        folder_filter = self._folder_filter()
        
        cursor = self.conn.execute(
            """SELECT tools FROM turns WHERE LOWER(REPLACE(workspace_folder, '\\', '/')) = ? AND tools IS NOT NULL AND tools != ''""",
            (folder_filter,)
        )

        tool_counts = {}
        for row in cursor.fetchall():
            try:
                tools_list = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                if isinstance(tools_list, list):
                    for tool in tools_list:
                        tool_name = tool if isinstance(tool, str) else tool.get("name", "unknown")
                        tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        return [
            {"tool": name, "count": count}
            for name, count in sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)
        ]

    def get_session_timeline(self) -> List[Dict[str, Any]]:
        """Get session creation timeline."""
        folder_filter = self._folder_filter()
        
        cursor = self.conn.execute(
            """SELECT DATE(first_timestamp) as date, COUNT(*) as session_count
               FROM (
                   SELECT session_id, MIN(timestamp_iso) as first_timestamp
                   FROM turns
                   WHERE LOWER(REPLACE(workspace_folder, '\\', '/')) = ?
                   GROUP BY session_id
               )
               GROUP BY date
               ORDER BY date""",
            (folder_filter,)
        )
        return [{"date": row[0], "sessions": row[1]} for row in cursor.fetchall()]

    def get_word_cloud_terms(
        self,
        min_word_length: int = 4,
        top_models: int = 8,
        max_words: int = 300,
        exclude_patterns: Optional[List[str]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Get word frequency data for word cloud visualization (workspace-scoped).
        
        Returns groups (user, all assistants, per-model) with word frequency lists
        for both response text and thinking text.
        """
        import re
        from typing import Any as AnyType
        
        # Common English stopwords
        _STOPWORDS = {
            "the", "be", "to", "of", "and", "a", "in", "that", "have", "i",
            "it", "for", "not", "on", "with", "he", "as", "you", "do", "at",
            "this", "but", "his", "by", "from", "they", "we", "say", "her", "she",
            "or", "an", "will", "my", "one", "all", "would", "there", "their",
            "is", "was", "are", "can", "if", "so", "up", "out", "about", "who",
            "get", "which", "go", "me", "when", "make", "than", "look", "write",
            "its", "see", "him", "two", "has", "like", "time", "no", "just", "way",
            # Add some common conversational fillers
            "ok", "okay", "yes", "yeah", "sure", "well", "now", "then", "let", "lets", "us"
        }
        
        def _tokenize(text: str) -> List[str]:
            """Tokenize text into lowercase words."""
            return re.findall(r'\b[a-z]{2,}\b', text.lower())
        
        def _safe_int(val: AnyType, default: int = 0) -> int:
            try:
                return int(val) if val is not None else default
            except (ValueError, TypeError):
                return default

        max_words_per_group = max_words
        
        exclude_patterns = exclude_patterns or []
        compiled_patterns: List[re.Pattern] = []
        for pattern in exclude_patterns:
            if pattern:
                try:
                    compiled_patterns.append(re.compile(pattern))
                except re.error:
                    pass

        # Get top assistant models by turn count in this workspace
        folder_filter = self._folder_filter()
        top_model_ids: List[str] = []
        try:
            cur = self.conn.execute(
                """
                SELECT COALESCE(model_id, '') as model_id, COUNT(*) as c
                FROM turns
                WHERE LOWER(REPLACE(workspace_folder, '\\', '/')) = ?
                  AND role = 'assistant'
                  AND COALESCE(model_id, '') != ''
                GROUP BY COALESCE(model_id, '')
                ORDER BY c DESC
                LIMIT ?
                """,
                (folder_filter, top_models),
            )
            top_model_ids = [r[0] for r in cur.fetchall() if r and r[0]]
        except sqlite3.OperationalError:
            top_model_ids = []

        return {}

    def get_agentic_coding_time_stats(self) -> Dict[str, Any]:
        """Get aggregated agentic coding time for this workspace.

        Agentic coding time is calculated from assistant.timestamp - user.timestamp,
        capped to exclude outliers (idle gaps) and with fallback for identical timestamps.
        Returns time in minutes for workspace-level display.
        """
        folder_filter = self._folder_filter()
        cap_ms = self._get_agentic_response_time_cap_ms()
        try:
            cursor = self.conn.execute(
                """
                SELECT SUM(
                    CASE 
                        WHEN a.timestamp_ms > u.timestamp_ms 
                         AND (a.timestamp_ms - u.timestamp_ms) <= ?
                        THEN (a.timestamp_ms - u.timestamp_ms)
                        WHEN a.timestamp_ms > 0 AND u.timestamp_ms > 0
                        THEN 60000
                        ELSE 0
                    END
                ) as total_ms
                FROM turns a
                JOIN turns u
                  ON u.session_id = a.session_id
                 AND u.turn = a.responding_to_turn
                 AND u.role = 'user'
                WHERE a.role = 'assistant'
                  AND LOWER(REPLACE(a.workspace_folder, '\\', '/')) = ?
                  AND u.timestamp_ms IS NOT NULL
                  AND u.timestamp_ms > 0
                  AND a.timestamp_ms IS NOT NULL
                  AND a.timestamp_ms > 0
                """,
                (cap_ms, folder_filter)
            )
            row = cursor.fetchone()
            total_ms = int(row[0] or 0)
        except sqlite3.OperationalError:
            total_ms = 0

        total_minutes = round(total_ms / 60_000, 1) if total_ms else 0
        total_hours = round(total_ms / 3_600_000, 2) if total_ms else 0

        return {
            "total_active_time_ms": total_ms,
            "total_minutes": total_minutes,
            "total_hours": total_hours,
            "cap_ms": cap_ms,
        }

    def _get_response_time_median_ms(self) -> int:
        """Median of response_time_ms for assistant turns in this workspace (ms)."""
        folder_filter = self._folder_filter()
        try:
            cursor = self.conn.execute(
                """
                WITH ordered AS (
                  SELECT
                    response_time_ms AS time_ms,
                    ROW_NUMBER() OVER (ORDER BY response_time_ms) AS rn,
                    COUNT(*) OVER () AS cnt
                  FROM turns
                  WHERE role = 'assistant'
                    AND response_time_ms IS NOT NULL
                    AND response_time_ms > 0
                    AND LOWER(REPLACE(workspace_folder, '\\', '/')) = ?
                ),
                median AS (
                  SELECT AVG(time_ms) AS med
                  FROM ordered
                  WHERE rn IN ((cnt+1)/2, (cnt+2)/2)
                )
                SELECT COALESCE(med, 0) FROM median
                """,
                (folder_filter,)
            )
            med = cursor.fetchone()[0] or 0
            return int(med)
        except sqlite3.OperationalError:
            return 0

    def _get_agentic_response_time_cap_ms(self) -> int:
        """Dynamic cap for response_time_ms based on median (to exclude outliers)."""
        median_ms = self._get_response_time_median_ms()
        if median_ms <= 0:
            return 600_000  # fallback cap (10 minutes)

        cap = int(median_ms * 10.0)  # 10x median multiplier
        cap = max(60_000, min(3_600_000, cap))  # floor 1 min, ceiling 1 hour
        return cap
