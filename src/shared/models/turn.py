"""Turn-related models for chat extraction.

Contains Turn (raw extraction) and EnrichedTurn (with computed fields).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, cast

from .dataclass_mixin import DataclassIO

@dataclass
class CodeEdit(DataclassIO):
    """Code edit as extracted from agent source.
    
    Contains fields found in source data.
    Lizard metrics are computed during enrichment and stored in extra dict.
    """
    file_path: str
    language: str  # Language ID from source (may need normalization)
    
    # Raw content (if available in source)
    code_before: Optional[str] = None
    code_after: Optional[str] = None
    diff: Optional[str] = None
    
    # Agent-specific extras (preserves unknown fields, includes metrics after enrichment)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Turn(DataclassIO):
    """Raw turn data as extracted from agent source.
    
    Contains only fields found in the source data.
    Token counts, language detection, and metrics are added during enrichment to create EnrichedTurn.
    """
    
    # Core identity
    session_id: str
    turn: int
    role: str
    
    # Raw content (as found in source - no cleaning applied)
    original_text: str = ""
    
    # Thinking content (for reasoning models like Claude Sonnet thinking variants)
    # Extracted from consecutive bubbles and concatenated into a single turn
    thinking_text: str = ""
    thinking_duration_ms: int = 0
    
    # Source metadata (as found in agent data)
    workspace_id: str = ""
    workspace_name: str = ""
    workspace_folder: str = ""
    session_name: str = ""
    agent_used: str = ""
    model_id: str = ""  # Raw model ID from source (not yet normalized)
    request_id: str = ""
    merged_request_ids: List[str] = field(default_factory=list)
    
    # Raw timestamps (as found in source)
    timestamp_ms: Optional[int] = None
    timestamp_iso: Optional[str] = None
    ts: str = ""
    
    # Raw associated data (as found in source)
    files: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    
    # Raw code edits (as extracted, no metrics yet)
    code_edits: List[CodeEdit] = field(default_factory=list)
    
    # Relationship tracking (for subagent / forked sessions)
    parent_session_id: Optional[str] = None
    relationship_type: Optional[str] = None  # "subagent", "fork", etc.

    # Agent-specific extras (preserves unknown fields)
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Turn":
        """Create a Turn, stashing unknown keys in .extra."""
        instance = super().from_dict(payload)

        # Convert code_edits dicts to CodeEdit objects if present
        if instance.code_edits and isinstance(instance.code_edits, list):
            instance.code_edits = [
                CodeEdit.from_dict(cast(Dict[str, Any], item)) if isinstance(item, dict) else item
                for item in instance.code_edits
            ]

        return instance


@dataclass
class EnrichedTurn(Turn):
    """Enriched turn with computed metrics and normalized fields.
    
    Inherits raw fields from Turn.
    Adds cleaned text, token counts, language detection, response timing, and aggregated code metrics.
    """
    # Note: Core fields inherited from Turn:
    # session_id, turn, role, original_text, workspace_id, workspace_name,
    # workspace_folder, session_name, agent_used, model_id, request_id,
    # merged_request_ids, timestamp_ms, timestamp_iso, ts, files, tools, extra
    
    # Cleaned text - computed during enrichment from original_text
    cleaned_text: str = ""
    
    # Token counts - computed during enrichment
    original_text_tokens: int = 0  # Tokens in original text before cleaning
    cleaned_text_tokens: int = 0   # Tokens in cleaned_text after cleaning
    code_tokens: int = 0           # Tokens from attached/generated code
    tool_tokens: int = 0           # Tokens from tool metadata/invocations
    system_tokens: int = 0         # Reserved for system prompt overhead (future)
    session_history_tokens: int = 0  # Cumulative tokens from all previous turns in session
    thinking_tokens: int = 0       # Tokens in thinking content (reasoning models)
    
    # Language detection - computed during enrichment
    languages: List[str] = field(default_factory=list)
    primary_language: Optional[str] = None
    
    # Response timing - computed during enrichment
    responding_to_turn: Optional[int] = None
    response_time_ms: Optional[int] = None
    
    # Aggregated code metrics - computed from code_edits during enrichment
    total_lines_added: Optional[int] = None
    total_lines_removed: Optional[int] = None
    total_nloc_change: Optional[int] = None
    weighted_complexity_change: Optional[float] = None
    
    # Override code_edits type to use enriched CodeEdit
    code_edits: List[CodeEdit] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        """Calculate total tokens as sum of all token types.
        
        This combines:
        - original_text_tokens: Base text tokens (original before cleaning)
        - code_tokens: Tokens from code content
        - tool_tokens: Tokens from tool definitions/invocations
        - system_tokens: System prompt overhead (future)
        
        Note: We use original_text_tokens (not cleaned) for total because that
        represents what was actually sent to/from the API.
        """
        return (
            self.original_text_tokens +
            self.code_tokens +
            self.tool_tokens +
            self.system_tokens
        )

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "EnrichedTurn":
        """Create a turn, stashing unknown keys in .extra."""
        # Use parent logic to handle basic fields and extra
        instance = cast("EnrichedTurn", super().from_dict(payload))

        # Convert code_edits dicts to CodeEdit objects if present
        if instance.code_edits and isinstance(instance.code_edits, list):
            instance.code_edits = [
                CodeEdit.from_dict(cast(Dict[str, Any], item)) if isinstance(item, dict) else item
                for item in instance.code_edits
            ]

        return instance



def calculate_turn_metrics(turn: EnrichedTurn) -> EnrichedTurn:
    """Calculate aggregate code metrics for a turn from its code_edits.
    
    Calculates:
    - files: List of unique file paths from code_edits (single source of truth)
    - total_lines_added: Sum of lines_added from all edits
    - total_lines_removed: Sum of lines_removed from all edits  
    - total_nloc_change: Sum of nloc delta from all edits
    - weighted_complexity_change: Max complexity change across edits (identifies high-impact changes)
    
    Args:
        turn: Turn object with code_edits
        
    Returns:
        Turn with metrics fields populated
    """
    if not turn.code_edits:
        return turn
    
    total_lines_added = 0
    total_lines_removed = 0
    total_nloc_change = 0
    max_complexity_change = 0.0
    edited_files: set = set()
    
    for edit in turn.code_edits:
        # Collect file paths from code_edits (single source of truth for files)
        if edit.file_path:
            edited_files.add(edit.file_path)
        
        extra = edit.extra or {}
        delta = extra.get('delta_metrics', {})
        
        # Lines added/removed
        total_lines_added += delta.get('lines_added', 0)
        total_lines_removed += delta.get('lines_removed', delta.get('lines_replaced', 0))
        
        # NLOC change
        total_nloc_change += delta.get('nloc', 0)
        
        # Track max complexity change (absolute value to capture both increases and decreases)
        complexity = delta.get('cyclomatic_complexity', 0)
        if abs(complexity) > abs(max_complexity_change):
            max_complexity_change = complexity
    
    # Update files from code_edits - this is the single source of truth
    if edited_files:
        turn.files = sorted(edited_files)
    
    turn.total_lines_added = total_lines_added if total_lines_added > 0 else None
    turn.total_lines_removed = total_lines_removed if total_lines_removed > 0 else None
    turn.total_nloc_change = total_nloc_change if total_nloc_change != 0 else None
    turn.weighted_complexity_change = max_complexity_change if max_complexity_change != 0 else None
    
    return turn


def calculate_response_times(turns: List[EnrichedTurn]) -> List[EnrichedTurn]:
    """Calculate response_time_ms and responding_to_turn for each assistant turn.
    
    For each assistant turn, calculates:
    - responding_to_turn: Turn number of the user message this assistant turn responds to
    - response_time_ms: Time from user message to this assistant turn (milliseconds)
    
    Args:
        turns: List of Turn objects, should be sorted by session_id and turn number
        
    Returns:
        Modified turns list with response time fields added
    """
    # Group turns by session_id
    sessions: Dict[str, List[EnrichedTurn]] = {}
    for turn in turns:
        if not turn.session_id:
            continue
        if turn.session_id not in sessions:
            sessions[turn.session_id] = []
        sessions[turn.session_id].append(turn)
    
    # Process each session separately
    for session_id, session_turns in sessions.items():
        # Sort by turn number (ensure correct order)
        session_turns.sort(key=lambda t: t.turn)
        
        # Track the most recent user turn we've seen
        last_user_turn: Optional[EnrichedTurn] = None
        
        # Process turns in order
        for turn in session_turns:
            if turn.role == 'user':
                # Update last_user_turn when we see a user turn
                last_user_turn = turn
                # User turns don't have response times
                turn.responding_to_turn = None
                turn.response_time_ms = None
            
            elif turn.role == 'assistant':
                # Check if response_time_ms was pre-extracted (e.g., from Copilot's result.timings)
                extra = turn.extra or {}
                pre_extracted_response_time = extra.get("response_time_ms")
                
                # This assistant turn is responding to the last user turn
                if last_user_turn:
                    turn.responding_to_turn = last_user_turn.turn
                    
                    if pre_extracted_response_time:
                        turn.response_time_ms = pre_extracted_response_time
                    else:
                        # Calculate from timestamps
                        user_timestamp = last_user_turn.timestamp_ms
                        assistant_timestamp = turn.timestamp_ms
                        
                        if user_timestamp is not None and assistant_timestamp is not None:
                            # Calculate time from user message to this assistant turn
                            response_time_ms = assistant_timestamp - user_timestamp
                            
                            # Only store if positive (sanity check)
                            if response_time_ms >= 0:
                                turn.response_time_ms = response_time_ms
                            else:
                                # Negative time = data issue, set to None
                                turn.response_time_ms = None
                        else:
                            # Missing timestamps
                            turn.response_time_ms = None
                else:
                    # No user turn found (assistant turn before any user turn)
                    turn.responding_to_turn = None
                    # Still use pre-extracted response time if available
                    turn.response_time_ms = pre_extracted_response_time
    
    return turns

