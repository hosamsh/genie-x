
"""Claude Code Data Extractor."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.shared.database.db_extract import get_session_file_meta, upsert_session_file_meta
from src.shared.database.db_schema import ensure_session_file_meta_table
from src.shared.logging.logger import get_logger
from src.shared.models.turn import Turn, CodeEdit
from src.shared.models.workspace import WorkspaceInfo, WorkspaceActivity, ExtractedWorkspace
from src.shared.io.paths import normalize_path, is_valid_session_id
from ..agent_extractor import AgentExtractor
from .dag import Branch, DagEntry, build_dag, detect_forks

logger = get_logger(__name__)

# Hard cap on session file size (bytes); files larger than this are skipped.
_DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB


class DagDetectedError(RuntimeError):
    """Raised when appended JSONL lines contain DAG UUIDs and require a full re-parse."""

# Default Claude directory
def get_claude_dir() -> Path:
    return Path.home() / ".claude"

def get_projects_dir() -> Path:
    return get_claude_dir() / "projects"

def get_history_file() -> Path:
    return get_claude_dir() / "history.jsonl"

def encode_project_path(project_path: str) -> str:
    """Reimplements the logic: projectPath.replace(/[:/\\.]/g, "-")"""
    if not project_path:
        return ""
    return re.sub(r'[:/\\.]', '-', project_path)

@dataclass
class ClaudeWorkspaceMeta:
    workspace_id: str
    workspace_name: str
    workspace_folder: str
    path: Path # Path to the project's jsonl files directory

class ClaudeCodeExtractor(AgentExtractor):
    AGENT_NAME = "claude_code"

    def _get_claude_dir(self) -> Path:
        """Get Claude directory from config or use default."""
        if self.config:
            claude_dir = self.config.get('claude_dir')
            if claude_dir:
                return Path(claude_dir)
        return get_claude_dir()

    def _get_projects_dir(self) -> Path:
        """Get projects directory from config or use default."""
        return self._get_claude_dir() / "projects"

    def _get_history_file(self) -> Path:
        """Get history file from config or use default."""
        return self._get_claude_dir() / "history.jsonl"

    def _get_max_file_bytes(self) -> int:
        """Return the maximum allowed session file size in bytes from config."""
        if self.config:
            mb = self.config.get("max_session_file_size_mb")
            if mb is not None:
                try:
                    return int(mb) * 1024 * 1024
                except (ValueError, TypeError):
                    pass
        return _DEFAULT_MAX_FILE_BYTES

    def _get_meta_db_path(self) -> Path:
        return self._get_claude_dir() / "extractor_meta.sqlite"

    def _get_meta_connection(self) -> sqlite3.Connection:
        meta_db_path = self._get_meta_db_path()
        meta_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(meta_db_path))
        ensure_session_file_meta_table(conn)
        return conn

    def read_jsonl_from_offset(self, path: Path, offset: int) -> Tuple[List[dict], int]:
        """Read JSONL messages from *offset* and return parsed messages plus new offset."""
        parsed: List[dict] = []
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            for line in handle:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if offset > 0 and message.get("uuid") and message.get("parentUuid"):
                    raise DagDetectedError(f"DAG message appended to {path}")
                parsed.append(message)
            new_offset = handle.tell()
        return parsed, new_offset

    def _read_full_jsonl(self, session_file: Path) -> Tuple[List[dict], int]:
        messages: List[dict] = []
        with open(session_file, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return messages, handle.tell()

    def _load_session_messages(self, session_file: Path) -> Optional[List[dict]]:
        """Load a session file, skipping unchanged files using session_file_meta."""
        try:
            stat = session_file.stat()
        except OSError as exc:
            logger.error("Error stating session file %s: %s", session_file, exc)
            return None

        if stat.st_size > self._get_max_file_bytes():
            logger.warning("Skipping oversized Claude session file: %s", session_file)
            return None

        conn = self._get_meta_connection()
        try:
            meta = get_session_file_meta(conn, session_file.stem, self.AGENT_NAME)
            if meta and meta.get("file_size") == stat.st_size and meta.get("file_path") == str(session_file):
                return None

            if meta and meta.get("file_size", 0) < stat.st_size:
                try:
                    self.read_jsonl_from_offset(session_file, int(meta.get("last_offset", 0)))
                except DagDetectedError:
                    logger.debug("Appended DAG content detected; forcing full re-parse of %s", session_file.name)

            messages, last_offset = self._read_full_jsonl(session_file)
            upsert_session_file_meta(
                conn,
                session_id=session_file.stem,
                agent=self.AGENT_NAME,
                file_path=str(session_file),
                file_size=stat.st_size,
                last_offset=last_offset,
                message_count=len(messages),
            )
            return messages
        finally:
            conn.close()

    def _parse_timestamp_ms(self, timestamp_value: str) -> int:
        if not timestamp_value:
            return 0
        try:
            dt = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return 0

    def _build_dag_entries(self, messages: List[dict]) -> List[DagEntry]:
        entries: List[DagEntry] = []
        for index, msg in enumerate(messages):
            msg_type = msg.get("type")
            if msg_type not in {"user", "assistant"}:
                continue
            entries.append(
                DagEntry(
                    uuid=str(msg.get("uuid", "") or msg.get("id", "")),
                    parent_uuid=str(msg.get("parentUuid", "") or ""),
                    msg_type=str(msg_type),
                    line_index=index,
                    message=msg,
                    timestamp_ms=self._parse_timestamp_ms(str(msg.get("timestamp", ""))),
                )
            )
        return entries

    def _normalize_command_text(self, text: str) -> Optional[str]:
        """Convert Claude XML command envelopes into readable slash commands."""
        if "<command-name>" not in text:
            return text

        name_match = re.search(r"<command-name>\s*([^<]+?)\s*</command-name>", text, flags=re.DOTALL)
        if not name_match:
            return None

        args_match = re.search(r"<command-args>\s*([^<]+?)\s*</command-args>", text, flags=re.DOTALL)
        command_name = name_match.group(1).strip()
        command_args = args_match.group(1).strip() if args_match else ""
        normalized = f"/{command_name}"
        if command_args:
            normalized = f"{normalized} {command_args}"
        stripped = re.sub(r"</?[^>]+>", "", normalized).strip()
        return stripped or None

    def _normalize_agent_session_id(self, agent_id: str) -> str:
        return agent_id if agent_id.startswith("agent-") else f"agent-{agent_id}"

    def _extract_subagent_mappings(self, messages: List[dict]) -> Dict[str, str]:
        """Return tool_use_id -> agent session id mappings from queue/progress events."""
        mapping: Dict[str, str] = {}
        for msg in messages:
            msg_type = msg.get("type")
            if msg_type == "queue-operation" and msg.get("operation") == "enqueue":
                tool_use_id = (
                    msg.get("tool_use_id")
                    or msg.get("toolUseId")
                    or msg.get("parentToolUseID")
                    or msg.get("parentToolUseId")
                    or ""
                )
                content = json.dumps(msg, ensure_ascii=False)
                task_match = re.search(r'"task[_-]?id"\s*:\s*"([^"]+)"', content, flags=re.IGNORECASE)
                if not task_match:
                    task_match = re.search(r"agent-([A-Za-z0-9_-]+)", content)
                if tool_use_id and task_match:
                    mapping[str(tool_use_id)] = self._normalize_agent_session_id(task_match.group(1))
            elif msg_type == "progress" and msg.get("data", {}).get("type") == "agent_progress":
                data = msg.get("data", {})
                tool_use_id = (
                    msg.get("parentToolUseID")
                    or msg.get("parentToolUseId")
                    or data.get("parentToolUseID")
                    or data.get("parentToolUseId")
                    or ""
                )
                agent_id = data.get("agentId") or ""
                if tool_use_id and agent_id:
                    mapping[str(tool_use_id)] = self._normalize_agent_session_id(str(agent_id))
        return mapping

    def _session_has_content(self, session_file: Path) -> bool:
        """Check if a session file has actual conversation content (user/assistant messages).

        Session files may only contain metadata like file-history-snapshot which means
        the session was started but no actual conversation occurred.
        Files exceeding the size limit are also treated as having no content.
        """
        try:
            file_size = session_file.stat().st_size
        except OSError:
            return False

        if file_size > self._get_max_file_bytes():
            logger.warning(
                "Skipping oversized session file %s (%.1f MB > limit)",
                session_file.name,
                file_size / (1024 * 1024),
            )
            return False

        try:
            content = session_file.read_text(encoding='utf-8', errors='replace')
            for line in content.strip().split('\n'):
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get('type')
                    if msg_type in ('user', 'assistant'):
                        return True
                except json.JSONDecodeError:
                    continue
            return False
        except Exception:
            return False

    def scan_workspaces(self) -> List[WorkspaceInfo]:
        """Scan ~/.claude/history.jsonl and projects dir."""
        workspaces = []
        projects_dir = self._get_projects_dir()
        history_file = self._get_history_file()

        if not history_file.exists():
            return []

        # Read history to find known workspaces
        known_projects = set()
        
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        p_path = entry.get('project')
                        if p_path:
                            known_projects.add(p_path)
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception as e:
            logger.error(f"Error reading history.jsonl: {e}")

        # Verify existence on disk and create WorkspaceInfo
        for p_path in known_projects:
            encoded = encode_project_path(p_path)
            ws_dir = projects_dir / encoded
            
            # Check if directory exists or if we have at least one session file that matches
            # Actually Claude Code stores files in `projects/encoded_path/` ??
            # Based on previous analysis: 
            # ~/.claude/projects/C--code-learn-interview/.jsonl files exist inside?
            # OR ~/.claude/projects/C--code-learn-interview.jsonl ?
            
            # Let's double check the storage logic I found earlier:
            # "projectsDir = join(claudeDir, "projects")"
            # "const projectPath = join(projectsDir, dir.name)" -> It is a directory.
            
            if ws_dir.exists() and ws_dir.is_dir():
                # Count sessions that have actual conversation content
                session_files = list(ws_dir.glob("*.jsonl"))
                valid_session_count = 0
                
                # Get last modified
                last_modified = 0
                for sf in session_files:
                    try:
                        mtime = sf.stat().st_mtime
                        if mtime > last_modified:
                            last_modified = mtime
                        # Check if session has real content (user/assistant messages)
                        if self._session_has_content(sf):
                            valid_session_count += 1
                    except OSError:
                        pass
                
                # Skip workspaces with no valid sessions
                if valid_session_count == 0:
                    logger.debug(f"Skipping workspace {encoded} - no sessions with content")
                    continue
                
                dt = datetime.fromtimestamp(last_modified, tz=timezone.utc) if last_modified > 0 else datetime.now(timezone.utc)

                workspaces.append(WorkspaceInfo(
                    workspace_id=encoded, # Use encoded path as ID
                    workspace_name=Path(p_path).name,
                    workspace_folder=p_path,
                    agents=[self.AGENT_NAME],
                    session_count=valid_session_count,
                ))
        
        return workspaces

    @classmethod
    def create(cls, workspace_id: str, **kwargs) -> "ClaudeCodeExtractor":
        return cls(workspace_id)

    def extract_sessions(self) -> ExtractedWorkspace:
        """Extract all turns from the workspace."""
        
        # We need to find the real path from the ID (which is the encoded path)
        # Note: In scan_workspaces we used encoded path as ID.
        encoded_path = self.workspace_id
        projects_dir = self._get_projects_dir()
        ws_dir = projects_dir / encoded_path
        
        all_turns: List[Turn] = []
        
        if not ws_dir.exists():
            logger.warning(f"Workspace directory not found: {ws_dir}")
            return ExtractedWorkspace(
                workspace_id=self.workspace_id,
                agent_name=self.AGENT_NAME,
                turns=[],
                session_count=0,
                code_metrics=[],
            )

        # Get original project path from history if possible, or infer?
        # We can try to decode or just look up in history.
        # For now, let's look up in history since we have the encoded ID.
        # But scanning history every time is inefficient.
        # We will infer name from ID for now.

        # We need to find the "Project Path" (e.g. c:/code/...)
        # Read from history.jsonl to find the actual folder path for this encoded workspace
        actual_folder_path = None
        history_file = self._get_history_file()
        if history_file.exists():
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                            p_path = entry.get('project')
                            if p_path and encode_project_path(p_path) == encoded_path:
                                actual_folder_path = p_path
                                break
                        except (json.JSONDecodeError, KeyError):
                            continue
            except Exception as e:
                logger.warning(f"Could not read history.jsonl for folder lookup: {e}")

        # Fallback: if we couldn't find the folder in history, use encoded path
        if not actual_folder_path:
            logger.warning(f"Could not find actual folder path for {encoded_path}, using encoded path")
            actual_folder_path = encoded_path

        all_jsonl_files = list(ws_dir.glob("*.jsonl"))
        session_files = [f for f in all_jsonl_files if not f.stem.startswith("agent-")]
        agent_files = [f for f in all_jsonl_files if f.stem.startswith("agent-")]
        loaded_sessions: dict[str, tuple[list[dict], set[tuple[str, str]]]] = {}
        agent_parent_sessions: dict[str, str] = {}
        session_subagent_maps: dict[str, dict[str, str]] = {}

        for sf in session_files:
            session_id = sf.stem
            if not is_valid_session_id(session_id):
                logger.warning("Skipping session with invalid ID derived from filename: %s", sf.name)
                continue
            try:
                messages = self._load_session_messages(sf)
                if messages is None:
                    continue
                loaded_sessions[session_id] = (messages, self._extract_session_fingerprint(messages))
                subagent_map = self._extract_subagent_mappings(messages)
                session_subagent_maps[session_id] = subagent_map
                for agent_session_id in subagent_map.values():
                    agent_parent_sessions.setdefault(agent_session_id, session_id)
            except Exception as e:
                logger.error(f"Error loading session {sf}: {e}")

        sessions_to_skip = self._find_subset_sessions(loaded_sessions)
        if sessions_to_skip:
            logger.info(f"Skipping {len(sessions_to_skip)} subset session(s): {sessions_to_skip}")

        for session_id, (messages, _) in loaded_sessions.items():
            if session_id in sessions_to_skip:
                continue
            try:
                dag_entries = self._build_dag_entries(messages)
                branches = detect_forks(dag_entries) if build_dag(dag_entries) else [Branch(entry_indices=list(range(len(dag_entries))))]
                if not branches:
                    branches = [Branch(entry_indices=list(range(len(dag_entries))))]

                for branch in branches:
                    branch_messages = [dag_entries[index].message for index in branch.entry_indices]
                    branch_session_id = session_id
                    branch_parent_session_id = ""
                    branch_relationship_type = ""
                    if branch.branch_uuid:
                        branch_session_id = f"{session_id}-{branch.branch_uuid}"
                        branch_parent_session_id = session_id
                        branch_relationship_type = "fork"

                    all_turns.extend(
                        self._convert_session(
                            branch_session_id,
                            branch_messages,
                            encoded_path,
                            actual_folder_path,
                            parent_session_id=branch_parent_session_id,
                            relationship_type=branch_relationship_type,
                            subagent_map=session_subagent_maps.get(session_id, {}),
                        )
                    )
            except Exception as e:
                logger.error(f"Error extracting session {session_id}: {e}")

        for agent_file in agent_files:
            try:
                agent_session_id = agent_file.stem
                if not is_valid_session_id(agent_session_id):
                    logger.warning("Skipping agent session with invalid ID: %s", agent_file.name)
                    continue
                agent_messages = self._load_session_messages(agent_file)
                if agent_messages is None:
                    continue
                parent_session_id = agent_parent_sessions.get(agent_session_id) or self._detect_parent_session_id(agent_messages) or ""
                all_turns.extend(
                    self._convert_session(
                        agent_session_id,
                        agent_messages,
                        encoded_path,
                        actual_folder_path,
                        parent_session_id=parent_session_id,
                        relationship_type="subagent" if parent_session_id else "",
                        subagent_map={},
                    )
                )
            except Exception as e:
                logger.error(f"Error extracting agent session {agent_file}: {e}")

        # Sort all turns by timestamp
        all_turns.sort(key=lambda t: t.timestamp_ms or 0)

        # Count unique sessions
        unique_sessions = set(t.session_id for t in all_turns)

        return ExtractedWorkspace(
            workspace_id=self.workspace_id,
            agent_name=self.AGENT_NAME,
            turns=all_turns,
            session_count=len(unique_sessions),
            code_metrics=[],
        )

    def _extract_session_fingerprint(self, messages: List[dict]) -> set[tuple[str, str]]:
        """Extract a fingerprint from session messages for deduplication.
        
        Returns a set of (timestamp, content_preview) tuples for user messages.
        This allows detecting when one session is a subset of another.
        """
        fingerprint: set[tuple[str, str]] = set()
        
        for msg in messages:
            if msg.get('type') != 'user':
                continue
            
            ts = msg.get('timestamp', '')
            content = msg.get('message', {}).get('content', '')
            
            # Extract text preview from content
            preview = ''
            if isinstance(content, str):
                preview = content[:200]
            elif isinstance(content, list):
                for block in content:
                    if block.get('type') == 'text':
                        preview = block.get('text', '')[:200]
                        break
            
            if ts or preview:  # Include if we have either
                fingerprint.add((ts, preview))
        
        return fingerprint
    
    def _find_subset_sessions(
        self, 
        loaded_sessions: dict[str, tuple[list[dict], set[tuple[str, str]]]]
    ) -> set[str]:
        """Find sessions that are complete subsets of other sessions.
        
        Returns session IDs that should be skipped (they are subsets of larger sessions).
        """
        sessions_to_skip: set[str] = set()
        session_ids = list(loaded_sessions.keys())
        
        for i in range(len(session_ids)):
            s1_id = session_ids[i]
            _, fp1 = loaded_sessions[s1_id]
            
            # Skip empty sessions
            if not fp1:
                sessions_to_skip.add(s1_id)
                continue
            
            for j in range(i + 1, len(session_ids)):
                s2_id = session_ids[j]
                _, fp2 = loaded_sessions[s2_id]
                
                if not fp2:
                    continue
                
                # Check if one is a subset of the other
                if fp1.issubset(fp2) and fp1 != fp2:
                    # s1 is a subset of s2, skip s1
                    sessions_to_skip.add(s1_id)
                    logger.debug(f"Session {s1_id[:8]}... is subset of {s2_id[:8]}...")
                elif fp2.issubset(fp1) and fp2 != fp1:
                    # s2 is a subset of s1, skip s2
                    sessions_to_skip.add(s2_id)
                    logger.debug(f"Session {s2_id[:8]}... is subset of {s1_id[:8]}...")
        
        return sessions_to_skip

    def _convert_session(
        self,
        session_id: str,
        messages: List[dict],
        workspace_encoded: str,
        workspace_folder: str,
        parent_session_id: str = "",
        relationship_type: str = "",
        subagent_map: Optional[Dict[str, str]] = None,
    ) -> List[Turn]:
        """Convert raw Claude Code messages to aggregated turns.
        
        Claude Code stores each tool_use and tool_result as separate messages, which leads to:
        - Multiple consecutive assistant messages (one per tool call)
        - Multiple consecutive user messages (one per tool result)
        - Empty text when only tool blocks are present
        
        This method aggregates consecutive same-role messages into single turns,
        and filters out:
        - Messages with isMeta=True that are pure system messages (Caveat prefix, command prompts)
        - Messages with isCompactSummary=True (conversation summary injections)
        - Messages containing only <local-command-stdout> (command output)
        - Tool-result-only user messages (they're responses to assistant tool calls)
        - System and file-history-snapshot messages
        - Synthetic/error messages

        Command messages (containing <command-name> tags) are no longer blanket-filtered;
        instead their text is normalised to ``/cmdname`` form and kept as user turns.

        It also cleans user messages by:
        - Removing "Caveat:..." prefixes
        - Removing <local-command-stdout>...</local-command-stdout> blocks
        """

        subagent_map = subagent_map or {}

        def is_synthetic_or_error_message(msg: dict) -> bool:
            """Check if message is synthetic (system-generated) or an API error.

            These messages should be filtered as they're not part of the actual conversation:
            - isApiErrorMessage=True: API errors like 'Invalid API key'
            - model='<synthetic>': System-generated messages not from the LLM
            """
            if msg.get('isApiErrorMessage'):
                return True
            model = msg.get('message', {}).get('model', '')
            if model == '<synthetic>':
                return True
            return False

        def is_subagent_trigger(text: str) -> bool:
            """Check if this message triggers a subagent task."""
            return 'Create a Task with subagent_type' in text

        def extract_subagent_prompt(text: str) -> str:
            """Extract the prompt from a 'Create a Task with subagent_type' message."""
            match = re.search(r'the prompt "([^"]+)"', text)
            return match.group(1) if match else ""

        def clean_user_text(text: str) -> str:
            """Clean user message text by removing system prefixes, command output, and control chars."""
            if not text:
                return text

            # Remove control characters (backspace \x08, etc.) that may have been captured
            # from terminal input - these cause DB viewers to display as BLOB
            # Keep newline (\n), carriage return (\r), and tab (\t)
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

            # Remove the "Caveat:..." prefix if present
            caveat_pattern = (
                r'^Caveat: The messages below were generated by the user while running local commands\.'
                r' DO NOT respond to these messages or otherwise consider them in your response'
                r' unless the user explicitly asks you to\.\s*'
            )
            text = re.sub(caveat_pattern, '', text, flags=re.MULTILINE)

            # Remove <local-command-stdout>...</local-command-stdout> blocks
            text = re.sub(r'<local-command-stdout>.*?</local-command-stdout>\s*', '', text, flags=re.DOTALL)

            return text.strip()

        # First pass: collect and aggregate messages, tracking command context
        aggregated: list[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        prev_was_command = False  # Track if previous user message was a command
        prev_text = ""  # Track previous message text for subagent prompt detection
        subagent_prompt = ""  # Track expected subagent prompt to filter
        in_subagent_context = False  # Track if we're inside a subagent task

        for msg in messages:
            m_type = msg.get('type')

            # Skip non-chat messages
            if m_type not in ('user', 'assistant'):
                continue

            # Skip compact summary injections (they duplicate history context)
            if msg.get('isCompactSummary'):
                continue

            # Get metadata
            is_meta = msg.get('isMeta', False)
            content_data = msg.get('message', {})
            raw_content = content_data.get('content')
            ts_iso = msg.get('timestamp', '')

            # Parse timestamp
            ts_ms = 0
            if ts_iso:
                try:
                    dt = datetime.fromisoformat(ts_iso.replace('Z', '+00:00'))
                    ts_ms = int(dt.timestamp() * 1000)
                except ValueError:
                    pass

            # Extract request/model info (available at top level for assistant messages)
            request_id = msg.get('requestId', '')
            model_id = content_data.get('model', '')

            # Source token counts from the API usage field (Epic 4.1)
            usage = content_data.get('usage', {}) or {}
            source_input_tokens: int = (
                usage.get('cache_read_input_tokens')
                or usage.get('input_tokens')
                or content_data.get('contextTokens')
                or 0
            )
            source_output_tokens: int = (
                usage.get('output_tokens')
                or content_data.get('outputTokens')
                or 0
            )

            # Handle <command-name> messages: normalize to /cmdname text (Epic 7)
            raw_str = str(raw_content) if raw_content else ""
            if '<command-name>' in raw_str:
                normalized_cmd = self._normalize_command_text(raw_str)
                if is_subagent_trigger(raw_str):
                    # Subagent trigger: extract prompt and enter subagent context
                    subagent_prompt = extract_subagent_prompt(raw_str)
                    in_subagent_context = True
                    prev_was_command = True
                    prev_text = raw_str
                    continue
                if normalized_cmd:
                    # Keep as a normalized user turn (e.g. "/help")
                    text_parts = [normalized_cmd]
                    tools = []
                    files = []
                    code_edits = []
                    thinking = ""
                    is_tool_result_only = False
                    full_text = normalized_cmd
                    # Fall through to aggregation below
                    # (skip the normal block parsing)
                    if current and current['role'] == m_type:
                        current['text_parts'].extend(text_parts)
                        if ts_ms > 0 and (current['ts_ms'] == 0 or ts_ms < current['ts_ms']):
                            current['ts_ms'] = ts_ms
                            current['ts_iso'] = ts_iso
                    else:
                        if current:
                            aggregated.append(current)
                        current = {
                            'role': m_type,
                            'text_parts': text_parts,
                            'tools': [],
                            'files': [],
                            'code_edits': [],
                            'thinking': "",
                            'ts_ms': ts_ms,
                            'ts_iso': ts_iso,
                            'model_id': model_id,
                            'request_id': request_id,
                            'source_input_tokens': 0,
                            'source_output_tokens': 0,
                        }
                    prev_was_command = False
                    prev_text = full_text
                    continue
                # Could not parse command name – skip
                prev_was_command = True
                prev_text = raw_str
                continue

            # Extract content from this message
            text_parts = []
            tools = []
            files = []
            code_edits = []
            thinking = ""
            is_tool_result_only = False
            current_subagent_session_id = ""

            if isinstance(raw_content, str):
                text_parts.append(raw_content)
            elif isinstance(raw_content, list):
                has_text = False
                has_tool_result = False
                
                for block in raw_content:
                    b_type = block.get('type')
                    if b_type == 'text':
                        text = block.get('text', '')
                        if text.strip():
                            text_parts.append(text)
                            has_text = True
                    elif b_type == 'thinking':
                        thinking += block.get('thinking', '') + "\n"
                    elif b_type == 'tool_use':
                        t_name = block.get('name')
                        tool_use_id = str(block.get('id', '') or block.get('tool_use_id', '') or '')
                        if t_name:
                            tools.append(t_name)
                        # Extract file paths and code edits
                        t_input = block.get('input', {})
                        for key in ('file_path', 'path', 'file'):
                            if key in t_input:
                                files.append(normalize_path(t_input[key]))
                        
                        # Extract code edits from Write and Edit tools
                        if t_name == 'Write' and 'file_path' in t_input:
                            fp = normalize_path(t_input['file_path'])
                            content = t_input.get('content', '')
                            lang = self._detect_language(fp)
                            code_edits.append(CodeEdit(
                                file_path=fp,
                                language=lang,
                                code_after=content,
                                extra={'tool': 'Write'}
                            ))
                        elif t_name == 'Edit' and 'file_path' in t_input:
                            fp = normalize_path(t_input['file_path'])
                            old_str = t_input.get('old_string', '')
                            new_str = t_input.get('new_string', '')
                            lang = self._detect_language(fp)
                            code_edits.append(CodeEdit(
                                file_path=fp,
                                language=lang,
                                code_before=old_str,
                                code_after=new_str,
                                diff=f"--- old\n+++ new\n@@ @@\n-{old_str}\n+{new_str}",
                                extra={'tool': 'Edit'}
                            ))
                        if t_name in {'Task', 'Agent'} and tool_use_id and tool_use_id in subagent_map:
                            current_subagent_session_id = subagent_map[tool_use_id]
                    elif b_type == 'tool_result':
                        has_tool_result = True
                
                # User message with only tool_result blocks = response to assistant's tool calls
                if m_type == 'user' and has_tool_result and not has_text:
                    is_tool_result_only = True

            # Get the full text for analysis
            full_text = "\n".join(text_parts)

            # Filter: subagent prompt following a "Create a Task" message
            if m_type == 'user' and in_subagent_context:
                if subagent_prompt and full_text.strip().startswith(subagent_prompt[:50]):
                    prev_was_command = True
                    prev_text = full_text
                    continue

            # Filter: subagent trigger text (from non-command-name sources)
            if is_subagent_trigger(full_text):
                subagent_prompt = extract_subagent_prompt(full_text)
                in_subagent_context = True
                prev_was_command = True
                prev_text = full_text
                continue

            # Handle assistant messages
            if m_type == 'assistant':
                # Skip synthetic/error messages (isApiErrorMessage or model='<synthetic>')
                if is_synthetic_or_error_message(msg):
                    prev_was_command = False
                    prev_text = full_text
                    continue

                if prev_was_command or in_subagent_context:
                    if 'Task' in tools or in_subagent_context:
                        prev_was_command = False
                        prev_text = full_text
                        continue
                    prev_was_command = False
                    prev_text = full_text
                    continue

            # Reset command tracking and subagent context for non-command user messages
            if m_type == 'user':
                prev_was_command = False
                if not is_tool_result_only:
                    in_subagent_context = False
                    subagent_prompt = ""

            # Skip tool-result-only user messages
            if is_tool_result_only:
                prev_text = full_text
                continue

            # Clean user message text (remove Caveat prefix and local-command-stdout)
            if m_type == 'user':
                full_text = clean_user_text(full_text)
                text_parts = [full_text] if full_text else []


            # Skip if no content left after cleaning (but keep thinking-only messages for aggregation)
            if not full_text and not tools and not thinking:
                prev_text = full_text
                continue
            
            prev_text = full_text
            
            # Check if we should aggregate with current turn
            if current and current['role'] == m_type:
                # Same role - aggregate
                current['text_parts'].extend(text_parts)
                current['tools'].extend(tools)
                current['files'].extend(files)
                current['code_edits'].extend(code_edits)
                current['thinking'] += thinking
                # Keep the earliest timestamp
                if ts_ms > 0 and (current['ts_ms'] == 0 or ts_ms < current['ts_ms']):
                    current['ts_ms'] = ts_ms
                    current['ts_iso'] = ts_iso
                # Keep first non-empty model_id and request_id
                if model_id and not current.get('model_id'):
                    current['model_id'] = model_id
                if request_id and not current.get('request_id'):
                    current['request_id'] = request_id
                # Sum source token counts across merged messages
                current['source_input_tokens'] = current.get('source_input_tokens', 0) + source_input_tokens
                current['source_output_tokens'] = current.get('source_output_tokens', 0) + source_output_tokens
                if current_subagent_session_id and not current.get('subagent_session_id'):
                    current['subagent_session_id'] = current_subagent_session_id
            else:
                # Different role or first message - save current and start new
                if current:
                    aggregated.append(current)
                current = {
                    'role': m_type,
                    'text_parts': text_parts,
                    'tools': tools,
                    'files': files,
                    'code_edits': code_edits,
                    'thinking': thinking,
                    'ts_ms': ts_ms,
                    'ts_iso': ts_iso,
                    'model_id': model_id,
                    'request_id': request_id,
                    'source_input_tokens': source_input_tokens,
                    'source_output_tokens': source_output_tokens,
                    'subagent_session_id': current_subagent_session_id if 'current_subagent_session_id' in locals() else "",
                }

        # Don't forget the last turn
        if current:
            aggregated.append(current)

        # Second pass: filter out empty turns and orphaned assistant turns at start
        # Also convert to Turn objects
        turns = []
        turn_idx = 0
        session_started = False  # Track if we've seen a user turn yet

        for agg in aggregated:
            text = "\n".join(agg['text_parts']).strip()
            tools = sorted(list(set(agg['tools'])))
            files = sorted(list(set(agg['files'])))
            code_edits = agg.get('code_edits', [])
            thinking = agg['thinking'].strip()
            model_id = agg.get('model_id', '')
            request_id = agg.get('request_id', '')
            src_in = agg.get('source_input_tokens', 0)
            src_out = agg.get('source_output_tokens', 0)

            # Skip turns with no meaningful content
            if not text and not tools:
                continue

            # Ensure session starts with a user turn
            # Skip any assistant turns before the first user turn
            if not session_started:
                if agg['role'] == 'assistant':
                    # Skip orphaned assistant turn at the start
                    continue
                else:
                    session_started = True

            # Build extra dict – only populate source tokens for assistant turns
            extra: Dict[str, Any] = {}
            if agg['role'] == 'assistant' and (src_in or src_out):
                extra["source_input_tokens"] = src_in
                extra["source_output_tokens"] = src_out
            elif agg['role'] == 'user' and src_in:
                extra["source_input_tokens"] = src_in
            if agg.get('subagent_session_id'):
                extra["subagent_session_id"] = agg['subagent_session_id']

            turn = Turn(
                session_id=session_id,
                turn=turn_idx,
                role=agg['role'],
                original_text=text,
                workspace_id=workspace_encoded,
                workspace_name=workspace_encoded,
                workspace_folder=workspace_folder,
                session_name=session_id,
                agent_used=self.AGENT_NAME,
                timestamp_ms=agg['ts_ms'],
                timestamp_iso=agg['ts_iso'],
                ts=str(agg['ts_ms']),
                files=files,
                tools=tools,
                code_edits=code_edits if agg['role'] == 'assistant' else [],
                thinking_text=thinking if agg['role'] == 'assistant' and thinking else "",
                model_id=model_id if agg['role'] == 'assistant' else "",
                request_id=request_id if agg['role'] == 'assistant' else "",
                extra=extra,
                parent_session_id=parent_session_id,
                relationship_type=relationship_type,
            )
            turns.append(turn)
            turn_idx += 1

        return turns

    def _detect_language(self, file_path: str) -> str:
        """Detect programming language from file extension."""
        ext_map = {
            '.py': 'python',
            '.js': 'javascript',
            '.ts': 'typescript',
            '.tsx': 'typescriptreact',
            '.jsx': 'javascriptreact',
            '.json': 'json',
            '.yaml': 'yaml',
            '.yml': 'yaml',
            '.md': 'markdown',
            '.html': 'html',
            '.css': 'css',
            '.scss': 'scss',
            '.sql': 'sql',
            '.sh': 'shellscript',
            '.bash': 'shellscript',
            '.rs': 'rust',
            '.go': 'go',
            '.java': 'java',
            '.c': 'c',
            '.cpp': 'cpp',
            '.h': 'c',
            '.hpp': 'cpp',
            '.cs': 'csharp',
            '.rb': 'ruby',
            '.php': 'php',
            '.swift': 'swift',
            '.kt': 'kotlin',
            '.r': 'r',
            '.xml': 'xml',
            '.toml': 'toml',
            '.ini': 'ini',
            '.cfg': 'ini',
            '.env': 'dotenv',
            '.gitignore': 'ignore',
        }
        ext = Path(file_path).suffix.lower()
        return ext_map.get(ext, 'plaintext')

    def get_latest_activity(self) -> Optional[WorkspaceActivity]:
        return None

    def cleanup(self) -> None:
        pass

    # -----------------------------------------------------------------------
    # Private utilities
    # -----------------------------------------------------------------------

    def _read_jsonl_lines(self, file_path: Path, start_offset: int = 0) -> List[dict]:
        """Read JSONL lines from *file_path* starting at *start_offset* bytes.

        Args:
            file_path:    Path to the JSONL file.
            start_offset: Byte offset to seek to before reading (0 = read all).

        Returns:
            Parsed message dicts from valid JSON lines.
        """
        messages: List[dict] = []
        try:
            with open(file_path, 'rb') as fh:
                if start_offset > 0:
                    fh.seek(start_offset)
                for raw_line in fh:
                    line = raw_line.decode('utf-8', errors='replace').strip()
                    if not line:
                        continue
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.error("Cannot read %s: %s", file_path, exc)
        return messages

    def _detect_parent_session_id(self, messages: List[dict]) -> Optional[str]:
        """Try to detect a parent session ID from agent session messages.

        Claude Code agent files sometimes embed the initiating session's ID in
        a top-level ``parentSessionId`` field or in a ``queue-operation`` record.
        """
        for msg in messages:
            parent = msg.get('parentSessionId')
            if parent:
                return parent
            # queue-operation records carry the source session
            if msg.get('type') == 'queue-operation':
                parent = msg.get('sessionId') or msg.get('sourceSessionId')
                if parent:
                    return parent
        return None

    def extract_sessions_incremental(
        self,
        conn: sqlite3.Connection,
    ) -> "ExtractedWorkspace":
        """Compatibility wrapper for callers that already hold a metadata connection."""
        ensure_session_file_meta_table(conn)
        return self.extract_sessions()
