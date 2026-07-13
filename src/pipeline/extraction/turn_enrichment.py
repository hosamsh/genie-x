"""Converts raw Turns to enriched Turns with tokens, languages, metrics, and cleaned text."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from src.shared.models.turn import Turn, CodeEdit, EnrichedTurn, calculate_turn_metrics, calculate_response_times
from src.shared.code.code_metrics import calculate_metrics, count_diff_lines
from src.shared.code.language_utils import detect_languages_from_files
from src.shared.code.tool_taxonomy import normalize_tool_category
from src.shared.llm.token_utils import estimate_tokens, estimate_tool_tokens
from src.shared.llm.model_names import normalize_model_id
from src.shared.text.text_shrinker import TextShrinker
from src.shared.logging.logger import get_logger
from src.shared.config.config_loader import get_config

logger = get_logger(__name__)

_text_shrinker = None


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000

def _get_text_shrinker() -> TextShrinker:
    """Get or create the module-level TextShrinker instance."""
    global _text_shrinker
    if _text_shrinker is None:
        _text_shrinker = TextShrinker()
    return _text_shrinker


def _resolve_model_fallback(agent_used: Optional[str], timestamp_ms: Optional[int]) -> Optional[str]:
    """Resolve fallback model_id from config based on agent and timestamp.
    
    Uses model_defaults config to find the appropriate default model:
    1. If model_defaults is disabled, returns None
    2. Looks up the agent-specific config for supported agents
    3. Finds the most recent timeline entry before the turn's timestamp
    4. Falls back to the agent's "default" if no timeline match
    
    Args:
        agent_used: The agent name (e.g., "copilot", "claude_code")
        timestamp_ms: The turn's timestamp in milliseconds
        
    Returns:
        The fallback model_id or None if no fallback configured
    """
    if not agent_used:
        return None
    
    config = get_config()
    model_defaults = config.model_defaults
    
    if not model_defaults.enabled:
        return None
    
    # Get agent-specific config
    agent_lower = agent_used.lower()
    agent_defaults: Dict[str, Dict[str, Any]] = {
        "copilot": model_defaults.copilot,
        "claude_code": model_defaults.claude_code,
        "copilot_cli": model_defaults.copilot_cli,
    }
    agent_config = agent_defaults.get(agent_lower, {})
    
    if not agent_config:
        return None
    
    # Try timeline lookup if we have a timestamp
    timeline = agent_config.get("timeline", {})
    if timeline and timestamp_ms:
        turn_date = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        
        # Find the most recent timeline entry before or on the turn date
        matching_model = None
        matching_date = None
        for date_str, model in timeline.items():
            if date_str <= turn_date:
                if matching_date is None or date_str > matching_date:
                    matching_date = date_str
                    matching_model = model
        
        if matching_model:
            return matching_model
    
    # Fall back to agent default
    return agent_config.get("default")


def enrich_code_edit(base: CodeEdit) -> CodeEdit:
    """Add lizard metrics (before, after, delta) to CodeEdit."""
    edit = CodeEdit(
        file_path=base.file_path,
        language=base.language,
        code_before=base.code_before,
        code_after=base.code_after,
        diff=base.diff,
        extra=dict(base.extra) if base.extra else {},
    )
    
    if 'delta_metrics' in edit.extra:
        return edit
    
    before_content = base.code_before or ""
    after_content = base.code_after or ""
    
    filename = base.file_path if base.file_path else f"code.{base.language}" if base.language else "code.py"
    
    before_metrics = calculate_metrics(before_content, filename) if before_content else {
        "nloc": 0, 
        "average_cyclomatic_complexity": 0, 
        "token_count": 0, 
        "max_cyclomatic_complexity": 0
    }
    after_metrics = calculate_metrics(after_content, filename) if after_content else {
        "nloc": 0, 
        "average_cyclomatic_complexity": 0, 
        "token_count": 0, 
        "max_cyclomatic_complexity": 0
    }
    
    lines_added, lines_removed = count_diff_lines(before_content, after_content)
    
    delta_metrics = {
        "nloc": after_metrics["nloc"] - before_metrics["nloc"],
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "cyclomatic_complexity": (
            after_metrics["average_cyclomatic_complexity"] 
            - before_metrics["average_cyclomatic_complexity"]
        ),
        "token_count": after_metrics["token_count"] - before_metrics["token_count"],
    }
    
    edit.extra["before_metrics"] = before_metrics
    edit.extra["after_metrics"] = after_metrics
    edit.extra["delta_metrics"] = delta_metrics
    
    return edit


def estimate_code_tokens(code_edits: List[CodeEdit]) -> int:
    """Estimate total tokens in code edits (uses code_after)."""
    total = 0
    for edit in code_edits:
        if edit.code_after:
            total += estimate_tokens(edit.code_after)
    return total


def _detect_languages_from_code_edits(code_edits: List[CodeEdit]) -> List[str]:
    """Detect programming languages from code edit file paths and content."""
    languages = set()
    
    file_paths = [edit.file_path for edit in code_edits if edit.file_path]
    if file_paths:
        detected = detect_languages_from_files(file_paths)
        languages.update(detected)
    
    for edit in code_edits:
        if edit.language and edit.language not in ('', 'unknown', 'text'):
            languages.add(edit.language)
    
    return sorted(languages)


def _compute_tool_categories(tools: List[str]) -> List[str]:
    """Return a sorted, deduplicated list of canonical tool categories for a turn's tools."""
    return sorted({normalize_tool_category(t) for t in tools if t})


def _resolve_token_counts(
    base: Turn,
    cleaned_text: str,
) -> tuple[int, int]:
    """Resolve original and cleaned token counts, preferring source counts when present."""
    extra: Dict[str, Any] = base.extra or {}

    raw_output = extra.get("source_output_tokens")
    raw_input = extra.get("source_input_tokens")
    source_tokens: Optional[int] = None

    if base.role == "assistant" and raw_output is not None:
        source_tokens = int(raw_output)
    elif base.role == "user" and raw_input is not None:
        source_tokens = int(raw_input)

    if source_tokens is not None:
        original_text_tokens: int = source_tokens
        if cleaned_text:
            estimated_cleaned = estimate_tokens(cleaned_text)
            estimated_original = estimate_tokens(base.original_text) if base.original_text else 1
            ratio = estimated_cleaned / max(estimated_original, 1)
            cleaned_text_tokens: int = min(int(original_text_tokens * ratio), original_text_tokens)
        else:
            cleaned_text_tokens = 0
    else:
        original_text_tokens = estimate_tokens(base.original_text) if base.original_text else 0
        cleaned_text_tokens = estimate_tokens(cleaned_text) if cleaned_text else 0

    return original_text_tokens, cleaned_text_tokens


def enrich_turn(base: Turn) -> EnrichedTurn:
    """Convert Turn to EnrichedTurn with tokens, languages, cleaned text, and code metrics."""
    enriched_edits = []
    for edit in base.code_edits:
        if isinstance(edit, CodeEdit):
            enriched_edits.append(enrich_code_edit(edit))
        elif isinstance(edit, dict):
            base_edit = CodeEdit.from_dict(edit)
            enriched_edits.append(enrich_code_edit(base_edit))
    
    languages = []
    if base.files:
        languages = detect_languages_from_files(base.files)
    if not languages and enriched_edits:
        languages = _detect_languages_from_code_edits(enriched_edits)
    primary_language = languages[0] if languages else None
    
    shrinker = _get_text_shrinker()
    cleaned_text = shrinker.shrink(base.original_text) if base.original_text else ""
    
    thinking_tokens = estimate_tokens(base.thinking_text) if base.thinking_text else 0
    
    model_id = normalize_model_id(base.model_id) or base.model_id
    if not model_id:
        model_id = _resolve_model_fallback(base.agent_used, base.timestamp_ms)

    # Preserve all unknown fields from source; add computed fields below.
    extra: Dict[str, Any] = dict(base.extra) if base.extra else {}

    # Compute and store tool categories in extra (avoids model schema changes).
    if base.tools:
        tool_categories = _compute_tool_categories(base.tools)
        if tool_categories:
            extra["tool_categories"] = tool_categories

    original_text_tokens, cleaned_text_tokens = _resolve_token_counts(base, cleaned_text)

    turn = EnrichedTurn(
        session_id=base.session_id,
        turn=base.turn,
        role=base.role,
        original_text=base.original_text,
        cleaned_text=cleaned_text,
        workspace_id=base.workspace_id,
        workspace_name=base.workspace_name,
        workspace_folder=base.workspace_folder,
        session_name=base.session_name,
        agent_used=base.agent_used,
        request_id=base.request_id,
        merged_request_ids=list(base.merged_request_ids),
        timestamp_ms=base.timestamp_ms,
        timestamp_iso=base.timestamp_iso,
        ts=base.ts,
        files=list(base.files),
        tools=list(base.tools),
        parent_session_id=base.parent_session_id,
        relationship_type=base.relationship_type,
        extra=extra,
        code_edits=enriched_edits,
        model_id=model_id or "",
        languages=languages,
        primary_language=primary_language,
        thinking_text=base.thinking_text,
        thinking_duration_ms=base.thinking_duration_ms,
        original_text_tokens=original_text_tokens,
        cleaned_text_tokens=cleaned_text_tokens,
        code_tokens=estimate_code_tokens(enriched_edits),
        tool_tokens=estimate_tool_tokens(base.tools) if base.tools else 0,
        thinking_tokens=thinking_tokens,
        session_history_tokens=0,
    )
    
    return turn


def enrich_turns(base_turns: List[Union[Turn, EnrichedTurn]], calculate_metrics: bool = True) -> List[EnrichedTurn]:
    """Enrich turns with tokens, languages, metrics; calculate response times."""
    if not base_turns:
        return []
    
    perf_start = time.perf_counter()
    logger.progress(f"  Enriching {len(base_turns)} turns...")
    
    checkpoint = time.perf_counter()
    enriched: List[EnrichedTurn] = []
    for base in base_turns:
        if isinstance(base, EnrichedTurn):
            enriched.append(base)
        else:
            turn = enrich_turn(base)
            enriched.append(turn)
    logger.info(
        f"[PERF] enrich_turns | enrich_each_turn: {_elapsed_ms(checkpoint):.1f}ms "
        f"({len(base_turns)} turns)"
    )
    
    logger.progress("  Adjusting response times...")
    checkpoint = time.perf_counter()
    calculate_response_times(enriched)
    logger.info(f"[PERF] enrich_turns | calculate_response_times: {_elapsed_ms(checkpoint):.1f}ms")
    
    logger.progress("  Calculating static code metrics...")
    if calculate_metrics:
        checkpoint = time.perf_counter()
        for turn in enriched:
            calculate_turn_metrics(turn)
        logger.info(f"[PERF] enrich_turns | calculate_turn_metrics: {_elapsed_ms(checkpoint):.1f}ms")
    
    total_edits = sum(len(t.code_edits) for t in enriched)
    if total_edits > 0:
        logger.progress(f"    Enriched {total_edits} code edits with metrics")
    
    logger.info(f"[PERF] enrich_turns | TOTAL: {_elapsed_ms(perf_start):.1f}ms")
    return enriched
