"""
System Data Provider - Provides system-wide statistics across all workspaces.
"""

from __future__ import annotations

import sqlite3
import time
import re
from collections import Counter
from typing import Any, Dict, List, Optional
from src.web.services.extraction_service import generate_word_lists


# -------------------- Word Cloud Cache (process-wide) --------------------

_WORD_CLOUD_CACHE: Dict[str, Any] = {
    "key": None,
    "ts": 0.0,
    "payload": None,
}

_WORD_CLOUD_MAX_CHARS_PER_TEXT = 2000


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


class SystemDataProvider:
    """
    Provides system-wide data for the overview dashboard.
    
    Unlike workspace-scoped providers, this aggregates data
    across all workspaces in the database.
    """

    def __init__(self, db_connection: sqlite3.Connection):
        self.conn = db_connection
        self._stats_cache: Optional[Dict] = None

        # Cache for response_time_ms distribution stats
        self._response_time_median_ms_cache: Optional[int] = None

        # Heuristic cap to avoid counting long idle gaps (overnight / paused sessions)
        # when using response_time_ms as a proxy for "agentic coding time".
        # NOTE: We now prefer a median-based cap (see `_get_agentic_response_time_cap_ms`).
        self._agentic_time_cap_ms: int = 600_000  # fallback cap (10 minutes)
        self._agentic_time_median_multiplier: float = 10.0
        self._agentic_time_cap_floor_ms: int = 60_000      # 1 minute
        self._agentic_time_cap_ceiling_ms: int = 3_600_000  # 1 hour

    def _get_response_time_median_ms(self) -> int:
        """Median of response_time_ms for assistant turns (ms).

        Uses a window-function-based median calculation in SQLite.
        Returns 0 if unavailable.
        """
        if self._response_time_median_ms_cache is not None:
            return self._response_time_median_ms_cache

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
                ),
                median AS (
                  SELECT AVG(time_ms) AS med
                  FROM ordered
                  WHERE rn IN ((cnt+1)/2, (cnt+2)/2)
                )
                SELECT COALESCE(med, 0) FROM median
                """
            )
            med = cursor.fetchone()[0] or 0
            self._response_time_median_ms_cache = int(med)
            return self._response_time_median_ms_cache
        except sqlite3.OperationalError:
            self._response_time_median_ms_cache = 0
            return 0

    def _get_agentic_response_time_cap_ms(self) -> int:
        """Dynamic cap for response_time_ms based on median (to exclude outliers)."""
        median_ms = self._get_response_time_median_ms()
        if median_ms <= 0:
            return self._agentic_time_cap_ms

        cap = int(median_ms * self._agentic_time_median_multiplier)
        cap = max(self._agentic_time_cap_floor_ms, min(self._agentic_time_cap_ceiling_ms, cap))
        return cap

    def _agentic_time_expr_sql(self) -> str:
        """SQL expression (ms) for estimating agentic time per assistant turn.

        Uses ONLY `response_time_ms` and excludes outliers using a dynamic median-based cutoff.
        (No thinking-time fields are used.)
        """
        cap = self._get_agentic_response_time_cap_ms()
        return (
            "CASE "
            "WHEN response_time_ms IS NULL OR response_time_ms < 0 THEN 0 "
            f"WHEN response_time_ms > {cap} THEN 0 "
            "ELSE response_time_ms END"
        )

    def get_system_stats(self) -> Dict[str, Any]:
        """Get overall system statistics."""
        if self._stats_cache:
            return self._stats_cache

        cursor = self.conn.cursor()

        # Total workspaces
        cursor.execute("SELECT COUNT(*) FROM workspace_info")
        total_workspaces = cursor.fetchone()[0]

        # Total sessions
        cursor.execute("SELECT COUNT(DISTINCT session_id) FROM turns")
        total_sessions = cursor.fetchone()[0]

        # Total turns
        cursor.execute("SELECT COUNT(id) / 2 FROM turns")
        total_turns = cursor.fetchone()[0]

        # Total code lines generated
        cursor.execute(
            "SELECT SUM(CAST(total_lines_added AS INTEGER)) FROM turns WHERE total_lines_added IS NOT NULL"
        )
        total_code_lines = cursor.fetchone()[0] or 0

        self._stats_cache = {
            "total_workspaces": total_workspaces,
            "total_sessions": total_sessions,
            "total_turns": total_turns,
            "total_code_lines": total_code_lines,
        }

        return self._stats_cache

    def get_top_agent_stats(self) -> Dict[str, Any]:
        """Get the most frequently used agent with percentage."""
        cursor = self.conn.cursor()
        
        # Count turns per agent
        cursor.execute("""
            SELECT agent_used, COUNT(*) as cnt
            FROM turns
            WHERE agent_used IS NOT NULL AND agent_used != ''
            GROUP BY agent_used
            ORDER BY cnt DESC
        """)
        rows = cursor.fetchall()
        
        if not rows:
            return {"top_agent": "N/A", "percentage": "0%"}
        
        total = sum(row[1] for row in rows)
        top_agent = rows[0][0]
        top_count = rows[0][1]
        percentage = round((top_count / total) * 100, 1) if total > 0 else 0
        
        return {
            "top_agent": self._format_agent_name(top_agent),
            "percentage": f"{percentage}%"
        }

    def get_top_model_stats(self) -> Dict[str, Any]:
        """Get the most frequently used model with percentage."""
        cursor = self.conn.cursor()
        
        # Count assistant turns per model
        cursor.execute("""
            SELECT model_id, COUNT(*) as cnt
            FROM turns
            WHERE role = 'assistant' 
              AND model_id IS NOT NULL 
              AND model_id != ''
            GROUP BY model_id
            ORDER BY cnt DESC
        """)
        rows = cursor.fetchall()
        
        if not rows:
            return {"top_model": "N/A", "percentage": "0%"}
        
        total = sum(row[1] for row in rows)
        top_model = rows[0][0]
        top_count = rows[0][1]
        percentage = round((top_count / total) * 100, 1) if total > 0 else 0
        
        return {
            "top_model": self._format_model_name(top_model),
            "percentage": f"{percentage}%"
        }

    def _format_agent_name(self, agent: str) -> str:
        """Format agent name for display."""
        if not agent:
            return "Unknown"
        # Capitalize first letter, handle common patterns
        return agent.replace("_", " ").title()

    def get_word_cloud_terms(
        self,
        min_word_length: int = 3,
        top_models: int = 8,
        max_words: int = 500,
        exclude_patterns: Optional[List[str]] = None,
        max_chars_per_text: int = _WORD_CLOUD_MAX_CHARS_PER_TEXT,
        **kwargs
    ) -> Dict[str, Any]:
        """Return word-cloud lists for cleaned turn text.

        Output payload is optimized for client-side slicing:
        - `groups`: [{id, label}, ...]
        - `default_group_id`
        - `word_lists`: { group_id: [[word, weight], ...] }

        Groups included:
        - `user`: all user turns
        - `assistant_all`: all assistant turns
        - `assistant_model::<model_id>` for top assistant models by turn count

        Args:
            min_word_length: Minimum word length to include
            top_models: Number of top models to show
            max_words: Maximum words per group
            exclude_patterns: List of regex patterns to exclude words

        Notes:
        - Uses `turns.text` which stores the cleaned/shrunk text.
        - Uses a small process-wide cache keyed by latest turn id to avoid
          recomputing on every refresh when the DB hasn’t changed.
        """

        max_words_per_group = max_words
        cache_ttl_s = 300

        # Compile exclusion patterns
        compiled_patterns = []
        if exclude_patterns:
            for pattern in exclude_patterns:
                try:
                    compiled_patterns.append(re.compile(pattern))
                except re.error:
                    pass  # Skip invalid patterns

        # Fast invalidation key: if new turns are inserted, max(id) changes.
        try:
            cursor = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM turns")
            max_turn_id = _safe_int(cursor.fetchone()[0], 0)
        except sqlite3.OperationalError:
            max_turn_id = 0

        cache_key = (
            f"turns_max_id={max_turn_id}|minlen={min_word_length}|top_models={top_models}"
            f"|max_words={max_words_per_group}|max_chars={max_chars_per_text}"
        )
        now = time.time()
        if (
            _WORD_CLOUD_CACHE.get("key") == cache_key
            and (now - float(_WORD_CLOUD_CACHE.get("ts") or 0.0)) < cache_ttl_s
            and _WORD_CLOUD_CACHE.get("payload")
        ):
            return _WORD_CLOUD_CACHE["payload"]

        # Top assistant models by turn count
        top_model_ids: List[str] = []
        try:
            cur = self.conn.execute(
                """
                SELECT COALESCE(model_id, '') as model_id, COUNT(*) as c
                FROM turns
                WHERE role = 'assistant'
                  AND COALESCE(model_id, '') != ''
                GROUP BY COALESCE(model_id, '')
                ORDER BY c DESC
                LIMIT ?
                """,
                (top_models,),
            )
            top_model_ids = [r[0] for r in cur.fetchall() if r and r[0]]
        except sqlite3.OperationalError:
            top_model_ids = []

        # Determine if thinking_text column exists
        has_thinking = False
        try:
            self.conn.execute("SELECT thinking_text FROM turns LIMIT 1")
            has_thinking = True
        except sqlite3.OperationalError:
            pass

        # Prepare counters
        user_response = Counter()
        assistant_all_response = Counter()
        assistant_all_thinking = Counter()
        
        # We only track specific models for per-model clouds
        # Delegate the heavy lifting to shared helper
        cols = "role, COALESCE(model_id, '') as model_id, substr(text, 1, ?) as text"
        params: tuple[Any, ...]
        if has_thinking:
            cols += ", substr(thinking_text, 1, ?) as thinking_text"
            params = (max_chars_per_text, max_chars_per_text)
        else:
            cols += ", NULL as thinking_text"
            params = (max_chars_per_text,)

        sql = f"SELECT {cols} FROM turns WHERE (text IS NOT NULL AND text != '') OR (thinking_text IS NOT NULL AND thinking_text != '')"  # nosec B608

        word_lists = generate_word_lists(
            self.conn,
            sql,
            params=params,
            top_model_ids=top_model_ids,
            min_word_length=min_word_length,
            max_words_per_group=max_words_per_group,
            exclude_patterns=[p.pattern for p in compiled_patterns] if compiled_patterns else None,
            max_chars_per_text=max_chars_per_text,
        )

        # Build result payload
        groups: List[Dict[str, str]] = [
            {"id": "user", "label": "User"},
            {"id": "assistant_all", "label": "Assistant (all models)"},
        ]

        for model_id in top_model_ids:
            groups.append(
                {
                    "id": f"assistant_model::{model_id}",
                    "label": f"Assistant: {self._format_model_name(model_id)}",
                }
            )

        # word_lists produced by helper already includes per-group lists

        payload = {
            "groups": groups,
            "default_group_id": "user",
            "word_lists": word_lists,
        }

        _WORD_CLOUD_CACHE["key"] = cache_key
        _WORD_CLOUD_CACHE["ts"] = now
        _WORD_CLOUD_CACHE["payload"] = payload

        return payload

    def get_agentic_coding_time_stats(self) -> Dict[str, Any]:
        """Get aggregated agentic coding time across all sessions.

        Agentic coding time is calculated from assistant.timestamp - user.timestamp,
        capped to exclude outliers (idle gaps) and with fallback for identical timestamps.
        """
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
                  AND u.timestamp_ms IS NOT NULL
                  AND u.timestamp_ms > 0
                  AND a.timestamp_ms IS NOT NULL
                  AND a.timestamp_ms > 0
                """,
                (cap_ms,)
            )
            row = cursor.fetchone()
            total_ms = int(row[0] or 0)
        except sqlite3.OperationalError:
            total_ms = 0

        total_days = round(total_ms / 86_400_000, 2) if total_ms else 0
        total_hours = round(total_ms / 3_600_000, 2) if total_ms else 0

        return {
            "total_active_time_ms": total_ms,
            "total_hours": total_hours,
            "total_days": total_days,
            "median_response_time_ms": self._get_response_time_median_ms(),
            "cap_ms": self._get_agentic_response_time_cap_ms(),
            "cap_multiplier": self._agentic_time_median_multiplier,
        }

    def get_agentic_coding_time_per_model(self, **kwargs) -> List[Dict[str, Any]]:
        """Get aggregated agentic coding time by model.

        Returns distribution in hours for charting.
        """
        cap_ms = self._get_agentic_response_time_cap_ms()
        try:
            cursor = self.conn.execute(
                """
                SELECT 
                    COALESCE(a.model_id, u.model_id, 'unknown') as model_id,
                    SUM(
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
                  AND u.timestamp_ms IS NOT NULL
                  AND u.timestamp_ms > 0
                  AND a.timestamp_ms IS NOT NULL
                  AND a.timestamp_ms > 0
                  AND COALESCE(a.model_id, u.model_id, '') != ''
                GROUP BY COALESCE(a.model_id, u.model_id, 'unknown')
                ORDER BY total_ms DESC
                LIMIT 20
                """,
                (cap_ms,)
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for model_id, total_ms in rows:
            hours = round((total_ms or 0) / 3_600_000, 2)
            results.append({"value": self._format_model_name(model_id), "count": hours})
        return results

    def get_agentic_coding_time_per_agent(self, **kwargs) -> List[Dict[str, Any]]:
        """Get aggregated agentic coding time by supported agent.

        Returns distribution in hours for charting.
        """
        cap_ms = self._get_agentic_response_time_cap_ms()
        try:
            cursor = self.conn.execute(
                """
                SELECT 
                    COALESCE(a.agent_used, u.agent_used, 'unknown') as agent,
                    SUM(
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
                  AND u.timestamp_ms IS NOT NULL
                  AND u.timestamp_ms > 0
                  AND a.timestamp_ms IS NOT NULL
                  AND a.timestamp_ms > 0
                GROUP BY COALESCE(a.agent_used, u.agent_used, 'unknown')
                ORDER BY total_ms DESC
                """,
                (cap_ms,)
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for agent, total_ms in rows:
            hours = round((total_ms or 0) / 3_600_000, 2)
            results.append({"value": self._normalize_agent_name(agent), "count": hours})
        return results

    def get_code_velocity_timeline(self) -> List[Dict[str, Any]]:
        """Get daily net code growth across all workspaces.

        Net growth = total_lines_added - total_lines_removed.
        """
        try:
            cursor = self.conn.execute(
                """SELECT
                       DATE(timestamp_iso) as date,
                       SUM(COALESCE(CAST(total_lines_added AS INTEGER), 0)) as added,
                       SUM(COALESCE(CAST(total_lines_removed AS INTEGER), 0)) as removed
                   FROM turns
                   WHERE timestamp_iso IS NOT NULL
                     AND timestamp_iso != ''
                   GROUP BY date
                   ORDER BY date"""
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for date, added, removed in rows:
            if not date:
                continue
            added_i = int(added or 0)
            removed_i = int(removed or 0)
            results.append(
                {
                    "date": date,
                    "net_growth": added_i - removed_i,
                    "added": added_i,
                    "removed": removed_i,
                }
            )
        return results

    def get_model_productivity_matrix(self) -> List[Dict[str, Any]]:
        """Model productivity table: LOC per code turn by model.

        Uses combined_turns and counts only turns with code changes.
        """
        try:
            cursor = self.conn.execute(
                """
                SELECT
                    COALESCE(model_id, 'unknown') as model_id,
                    SUM(COALESCE(CAST(total_lines_added AS INTEGER), 0)) as total_loc_added,
                    COUNT(*) as code_turns
                FROM combined_turns
                WHERE has_assistant_response = 1
                  AND (COALESCE(total_lines_added, 0) > 0 OR COALESCE(total_lines_removed, 0) > 0)
                GROUP BY COALESCE(model_id, 'unknown')
                HAVING code_turns > 0
                ORDER BY (CAST(total_loc_added AS REAL) / code_turns) DESC
                LIMIT 20
                """
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for model_id, total_loc_added, code_turns in rows:
            total_loc_added_i = int(total_loc_added or 0)
            code_turns_i = int(code_turns or 0)
            loc_per_turn = round((total_loc_added_i / code_turns_i), 2) if code_turns_i else 0
            results.append(
                {
                    "model": self._format_model_name(model_id),
                    "loc_per_turn": loc_per_turn,
                    "total_loc_added": total_loc_added_i,
                    "code_turns": code_turns_i,
                }
            )
        return results

    def get_model_complexity_impact(self) -> List[Dict[str, Any]]:
        """Scatter data: delta complexity vs lines changed per model.

        Returns one point per model.
        """
        try:
            cursor = self.conn.execute(
                """
                SELECT
                    COALESCE(model_id, 'unknown') as model_id,
                    SUM(ABS(COALESCE(CAST(total_lines_added AS INTEGER), 0)) + ABS(COALESCE(CAST(total_lines_removed AS INTEGER), 0))) as lines_changed,
                    SUM(ABS(COALESCE(CAST(weighted_complexity_change AS REAL), 0.0))) as complexity_delta
                FROM combined_turns
                WHERE has_assistant_response = 1
                  AND (COALESCE(total_lines_added, 0) > 0 OR COALESCE(total_lines_removed, 0) > 0)
                GROUP BY COALESCE(model_id, 'unknown')
                ORDER BY lines_changed DESC
                LIMIT 25
                """
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for model_id, lines_changed, complexity_delta in rows:
            results.append(
                {
                    "model": self._format_model_name(model_id),
                    "lines_changed": int(lines_changed or 0),
                    "complexity_delta": round(float(complexity_delta or 0.0), 3),
                }
            )
        return results

    def get_activity_heatmap_agentic_time(self, **kwargs) -> List[Dict[str, Any]]:
        """Heatmap data: agentic time (minutes) by date x hour-of-day.

        Uses combined_turns user timestamp as a proxy for user engagement time.
        Agentic time per turn uses the same median-based response_time cap as other stats.
        """
        # PERF NOTE:
        # Using combined_turns here is extremely expensive because it is a VIEW
        # built with window functions and joins. For system-wide heatmaps we can
        # compute the same metric directly from `turns` by joining assistant turns
        # back to their prompting user turn via (session_id, responding_to_turn).
        cap_ms = self._get_agentic_response_time_cap_ms()

        # Determine the time window to display:
        # - Show the full available duration when < 1 year of data
        # - Otherwise, show the last 1 year
        # Also choose a bucket granularity to keep the heatmap readable:
        # - <= 90 days: daily buckets
        # - > 90 days: weekly buckets (up to ~52 columns for 1y)
        try:
            cursor = self.conn.execute(
                """
                SELECT
                    MIN(timestamp_ms) as min_ts,
                    MAX(timestamp_ms) as max_ts
                FROM turns
                WHERE role = 'user'
                  AND timestamp_ms IS NOT NULL
                  AND timestamp_ms > 0
                """
            )
            row = cursor.fetchone()
            min_user_ts = int((row[0] or 0) if row else 0)
            max_user_ts = int((row[1] or 0) if row else 0)
        except sqlite3.OperationalError:
            return []

        if min_user_ts <= 0 or max_user_ts <= 0 or max_user_ts < min_user_ts:
            return []

        span_ms = max_user_ts - min_user_ts
        span_days = span_ms / 86_400_000

        one_year_ms = 365 * 86_400_000
        window_start_ms = min_user_ts if span_ms <= one_year_ms else (max_user_ts - one_year_ms)

        bucket = 'day' if span_days <= 90 else 'week'

        # Bucket expressions (ISO date strings) based on user turn timestamp
        # - day: YYYY-MM-DD
        # - week: Monday-start week label as YYYY-MM-DD (week start)
        if bucket == 'week':
            date_bucket_sql = "DATE(datetime(u.timestamp_ms / 1000, 'unixepoch'), 'weekday 1', '-7 days')"
        else:
            date_bucket_sql = "DATE(datetime(u.timestamp_ms / 1000, 'unixepoch'))"

        try:
            # Calculate response time from timestamps (assistant - user).
            # For old/backfilled data where timestamps are identical or invalid,
            # use a 1-minute fallback so activity is still visible in the heatmap.
            cursor = self.conn.execute(  # nosec B608 - date_bucket_sql is a hardcoded constant
                f"""
                SELECT
                    {date_bucket_sql} as date,
                    CAST(strftime('%H', datetime(u.timestamp_ms / 1000, 'unixepoch')) AS INTEGER) as hour,
                    ROUND(SUM(
                        CASE 
                            WHEN a.timestamp_ms > u.timestamp_ms 
                             AND (a.timestamp_ms - u.timestamp_ms) <= ?
                            THEN (a.timestamp_ms - u.timestamp_ms)
                            WHEN a.timestamp_ms > 0 AND u.timestamp_ms > 0
                            THEN 60000
                            ELSE 0
                        END
                    ) / 60000.0, 2) as minutes
                FROM turns a
                JOIN turns u
                  ON u.session_id = a.session_id
                 AND u.turn = a.responding_to_turn
                 AND u.role = 'user'
                WHERE a.role = 'assistant'
                  AND u.timestamp_ms >= ?
                  AND u.timestamp_ms IS NOT NULL
                  AND u.timestamp_ms > 0
                  AND a.timestamp_ms IS NOT NULL
                  AND a.timestamp_ms > 0
                GROUP BY {date_bucket_sql},
                         CAST(strftime('%H', datetime(u.timestamp_ms / 1000, 'unixepoch')) AS INTEGER)
                ORDER BY date, hour
                """,
                (cap_ms, window_start_ms),
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for date, hour, minutes in rows:
            if not date:
                continue
            results.append(
                {
                    "date": date,
                    "hour": int(hour or 0),
                    "minutes": float(minutes or 0.0),
                }
            )
        return results

    def _safe_count(self, query: str) -> int:
        """Execute a count query, returning 0 on error."""
        try:
            cursor = self.conn.execute(query)
            return cursor.fetchone()[0] or 0
        except sqlite3.OperationalError:
            return 0

    def get_workspace_session_distribution(self, **kwargs) -> List[Dict[str, Any]]:
        """Get session count distribution by workspace."""
        cursor = self.conn.execute(
            """SELECT workspace_name, session_count
               FROM workspace_info
               ORDER BY session_count DESC"""
        )
        return [{"value": row[0], "count": row[1]} for row in cursor.fetchall()]

    def get_agent_distribution(self) -> List[Dict[str, Any]]:
        """Get distribution of turns by supported agent."""
        cursor = self.conn.execute(
            """SELECT COALESCE(agent_used, 'unknown') as agent, COUNT(*) as cnt
               FROM turns
               GROUP BY COALESCE(agent_used, 'unknown')
               ORDER BY cnt DESC"""
        )
        return [
            {"value": row[0].capitalize() if row[0] else "Unknown", "count": row[1]}
            for row in cursor.fetchall()
        ]

    def _normalize_agent_name(self, agent: str) -> str:
        if not agent:
            return "Unknown"
        agent_lower = str(agent).strip().lower()
        if "copilot_cli" in agent_lower:
            return "Copilot CLI"
        if "copilot" in agent_lower:
            return "Copilot"
        if "claude" in agent_lower:
            return "Claude Code"
        return str(agent).strip().capitalize()

    def get_model_distribution(self, **kwargs) -> List[Dict[str, Any]]:
        """Get distribution of assistant turns by model."""
        cursor = self.conn.execute(
            """SELECT model_id, COUNT(*) as cnt
               FROM turns
               WHERE role = 'assistant' AND model_id IS NOT NULL AND model_id != ''
               GROUP BY model_id
               ORDER BY cnt DESC
               LIMIT 15"""
        )
        return [
            {"value": self._format_model_name(row[0]), "count": row[1]}
            for row in cursor.fetchall()
        ]

    def _format_model_name(self, model_id: str) -> str:
        """Format model ID for display."""
        if not model_id:
            return "Unknown"
        model_id = model_id.replace("inferred:", "")
        model_id = model_id.replace("mixed:", "")
        if len(model_id) > 30:
            model_id = model_id[:27] + "..."
        return model_id

    def get_workspace_code_distribution(self) -> List[Dict[str, Any]]:
        """Get code lines generated per workspace."""
        cursor = self.conn.execute(
            """SELECT w.workspace_name, SUM(CAST(t.total_lines_added AS INTEGER)) as lines
               FROM turns t
               JOIN workspace_info w ON t.workspace_id = w.workspace_id
               WHERE t.total_lines_added IS NOT NULL
               GROUP BY w.workspace_name
               ORDER BY lines DESC
               LIMIT 10"""
        )
        return [{"value": row[0], "count": row[1] or 0} for row in cursor.fetchall()]

    def get_session_timeline(self) -> List[Dict[str, Any]]:
        """Get session count by date across all workspaces."""
        cursor = self.conn.execute(
            """SELECT 
                   DATE(timestamp_iso) as date, 
                   COUNT(DISTINCT session_id) as sessions,
                   COUNT(*) / 2 as turns,
                   COUNT(DISTINCT CASE WHEN files IS NOT NULL AND files != '' AND files != '[]' THEN request_id END) as files_touched_count
               FROM turns
               WHERE timestamp_iso IS NOT NULL
               GROUP BY date
               ORDER BY date"""
        )
        return [
            {
                "date": row[0], 
                "sessions": row[1], 
                "turns": row[2],
                "files_touched": row[3] or 0
            } 
            for row in cursor.fetchall() if row[0]
        ]

    def get_recent_workspaces(self, **kwargs) -> List[Dict[str, Any]]:
        """Get recently updated workspaces with their status."""
        cursor = self.conn.execute(
            """SELECT w.workspace_id, w.workspace_name, w.session_count, w.turn_count,
                      w.updated_at, w.agent_used, w.total_code_loc, w.workspace_folder
               FROM workspace_info w
               ORDER BY w.updated_at DESC
               LIMIT 10"""
        )
        results = []
        for row in cursor.fetchall():
            results.append({
                "workspace_id": row[0],
                "workspace_name": row[1],
                "session_count": row[2],
                "turn_count": row[3],
                "updated_at": row[4],
                "agent_used": row[5] or "Unknown",
                "total_code_loc": row[6] or 0,
                "workspace_folder": row[7] or "",
            })
        return results

    def get_file_complexity_evolution(self) -> List[Dict[str, Any]]:
        """Track average complexity changes per file over time."""
        try:
            cursor = self.conn.execute(
                """
                SELECT 
                    DATE(t.timestamp_iso) as date,
                    cm.file_path,
                    AVG(COALESCE(cm.delta_complexity, 0.0)) as avg_complexity_change
                FROM code_metrics cm
                JOIN turns t ON cm.request_id = t.request_id
                WHERE t.timestamp_iso IS NOT NULL
                  AND t.timestamp_iso != ''
                  AND cm.delta_complexity IS NOT NULL
                GROUP BY date, cm.file_path
                ORDER BY date
                """
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for date, file_path, avg_complexity in rows:
            if not date:
                continue
            results.append({
                "date": date,
                "file_path": file_path,
                "avg_complexity_change": round(float(avg_complexity or 0.0), 3)
            })
        return results

    def get_file_bottlenecks(self, **kwargs) -> List[Dict[str, Any]]:
        """Track files with highest modification times and code changes."""
        try:
            cursor = self.conn.execute(
                """
                SELECT 
                    cm.file_path,
                    w.workspace_name,
                    COUNT(DISTINCT cm.request_id) as times_changed,
                    SUM(COALESCE(cm.lines_added, 0)) as total_lines_added,
                    SUM(COALESCE(cm.lines_removed, 0)) as total_lines_removed,
                    SUM(COALESCE(cm.lines_added, 0) + COALESCE(cm.lines_removed, 0)) as total_changes
                FROM code_metrics cm
                LEFT JOIN workspace_info w ON cm.workspace_id = w.workspace_id
                GROUP BY cm.file_path, w.workspace_name
                ORDER BY times_changed DESC, total_changes DESC
                LIMIT 50
                """
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for file_path, workspace_name, times_changed, lines_added, lines_removed, total_changes in rows:
            # Extract filename from path
            from pathlib import Path
            filename = Path(file_path).name if file_path else file_path
            
            results.append({
                "filename": filename,
                "workspace_name": workspace_name or "Unknown",
                "times_changed": int(times_changed or 0),
                "total_lines_added": int(lines_added or 0),
                "total_lines_removed": int(lines_removed or 0),
                "total_changes": int(total_changes or 0)
            })
        return results

    def get_language_distribution_summary(self, **kwargs) -> List[Dict[str, Any]]:
        """Get summary of language usage (top languages by count)."""
        try:
            cursor = self.conn.execute(
                """
                SELECT primary_language, COUNT(*) as count
                FROM turns
                WHERE primary_language IS NOT NULL
                  AND primary_language != ''
                  AND role = 'assistant'
                GROUP BY primary_language
                ORDER BY count DESC
                LIMIT 20
                """
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        return [
            {"language": row[0], "count": int(row[1] or 0)}
            for row in rows
            if row[0]
        ]

    def get_language_distribution_evolution(self) -> List[Dict[str, Any]]:
        """Track how tech stack (languages) changes over time.
        
        Returns data grouped by date with top language counts as columns.
        Format: [{date: '2024-01-01', Python: 10, JavaScript: 5, ...}, ...]
        """
        try:
            # First, get the top 10 languages overall
            cursor_top = self.conn.execute(
                """
                SELECT primary_language, COUNT(*) as count
                FROM turns
                WHERE primary_language IS NOT NULL
                  AND primary_language != ''
                  AND role = 'assistant'
                GROUP BY primary_language
                ORDER BY count DESC
                LIMIT 10
                """
            )
            top_languages = [row[0] for row in cursor_top.fetchall()]
            
            if not top_languages:
                return []
            
            # Now get counts by date for these languages
            cursor = self.conn.execute(  # nosec B608 - uses parameterized placeholders
                f"""
                SELECT 
                    DATE(timestamp_iso) as date,
                    primary_language,
                    COUNT(*) as count
                FROM turns
                WHERE timestamp_iso IS NOT NULL
                  AND timestamp_iso != ''
                  AND primary_language IN ({','.join('?' * len(top_languages))})
                  AND role = 'assistant'
                GROUP BY date, primary_language
                ORDER BY date, primary_language
                """,
                top_languages
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        # Transform data: Group by date and create a dict with language counts
        from collections import defaultdict
        date_data: Dict[str, Dict[str, int]] = defaultdict(dict)
        
        for date, language, count in rows:
            if not date or not language:
                continue
            date_data[date][language] = int(count or 0)
        
        # Convert to list format with all top languages for each date
        results: List[Dict[str, Any]] = []
        for date in sorted(date_data.keys()):
            row: Dict[str, Any] = {"date": date}
            for lang in top_languages:
                row[lang] = date_data[date].get(lang, 0)
            results.append(row)
        
        return results

    def get_language_complexity(self) -> List[Dict[str, Any]]:
        """Which languages have higher complexity changes."""
        try:
            cursor = self.conn.execute(
                """
                SELECT 
                    t.primary_language,
                    AVG(ABS(COALESCE(cm.delta_complexity, 0.0))) as avg_complexity_change,
                    SUM(ABS(COALESCE(cm.delta_complexity, 0.0))) as total_complexity_change,
                    COUNT(DISTINCT cm.request_id) as edit_count
                FROM code_metrics cm
                JOIN turns t ON cm.request_id = t.request_id
                WHERE t.primary_language IS NOT NULL
                  AND t.primary_language != ''
                  AND cm.delta_complexity IS NOT NULL
                GROUP BY t.primary_language
                ORDER BY avg_complexity_change DESC
                LIMIT 20
                """
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for language, avg_complexity, total_complexity, edit_count in rows:
            if not language:
                continue
            results.append({
                "language": language,
                "avg_complexity_change": round(float(avg_complexity or 0.0), 3),
                "total_complexity_change": round(float(total_complexity or 0.0), 2),
                "edit_count": int(edit_count or 0)
            })
        return results

    def get_code_addition_deletion_ratio_by_model(self, **kwargs) -> List[Dict[str, Any]]:
        """Code addition/deletion ratio by model (closer to 1:1 indicates refactoring)."""
        try:
            cursor = self.conn.execute(
                """
                SELECT 
                    COALESCE(model_id, 'unknown') as model_id,
                    SUM(COALESCE(CAST(total_lines_added AS INTEGER), 0)) as total_added,
                    SUM(COALESCE(CAST(total_lines_removed AS INTEGER), 0)) as total_removed,
                    COUNT(*) as turn_count
                FROM combined_turns
                WHERE has_assistant_response = 1
                  AND (COALESCE(total_lines_added, 0) > 0 OR COALESCE(total_lines_removed, 0) > 0)
                GROUP BY COALESCE(model_id, 'unknown')
                ORDER BY (total_added + total_removed) DESC
                LIMIT 20
                """
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []

        results: List[Dict[str, Any]] = []
        for model_id, total_added, total_removed, turn_count in rows:
            total_added = int(total_added or 0)
            total_removed = int(total_removed or 0)
            
            if total_removed > 0:
                ratio = total_added / total_removed
            elif total_added > 0:
                ratio = float('inf')
            else:
                ratio = 1.0
            
            results.append({
                "model": self._format_model_name(model_id),
                "total_added": total_added,
                "total_removed": total_removed,
                "ratio": round(ratio, 2) if ratio != float('inf') else 999.99,
                "turn_count": int(turn_count or 0)
            })
        return results

    def call_function(self, function_name: str, **kwargs) -> Any:
        """Call a provider function by name."""
        if not hasattr(self, function_name):
            raise ValueError(f"Unknown provider function: {function_name}")

        func = getattr(self, function_name)
        return func(**kwargs) if kwargs else func()

