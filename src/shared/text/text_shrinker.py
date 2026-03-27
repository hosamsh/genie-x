"""
Text Shrinker using text-line-classifier model.

Intelligently shrinks large text blocks by detecting cleanable regions
(consecutive code, logs, or none lines) and truncating them while 
preserving representative samples.
"""

from dataclasses import dataclass
from typing import List, Optional
import importlib.util
from pathlib import Path
import re

from src.shared.config.config_loader import get_config


@dataclass
class ShrinkConfig:
    """Configuration for text shrinking."""
    min_chars_to_shrink: int = 1024
    min_consecutive_lines: int = 8
    keep_ratio: float = 0.10
    truncate_marker: str = "[...truncated...]"
    protect_chars: int = 512  # Never truncate first/last N characters
    
    @classmethod
    def from_config(cls) -> "ShrinkConfig":
        """Load configuration from config.yaml."""
        config = get_config()
        return cls(
            min_chars_to_shrink=config.get("extract.text_shrinker.min_chars_to_shrink", 1024),
            min_consecutive_lines=config.get("extract.text_shrinker.min_consecutive_lines", 8),
            keep_ratio=config.get("extract.text_shrinker.keep_ratio", 0.10),
            truncate_marker=config.get("extract.text_shrinker.truncate_marker", "[...truncated...]"),
            protect_chars=config.get("extract.text_shrinker.protect_chars", 512),
        )


@dataclass
class CleanableBlock:
    """Represents a block of lines that can be shrunk."""
    start_idx: int
    end_idx: int  # exclusive
    
    @property
    def length(self) -> int:
        return self.end_idx - self.start_idx


_cached_classifier = None

def _get_classifier():
    """Lazy import of classifier from text-line-classifier folder (has hyphen in name)."""
    global _cached_classifier
    if _cached_classifier is not None:
        return _cached_classifier
    
    import sys
    
    # The folder has hyphens so we need to add the parent to sys.path
    # and create a proper package structure dynamically
    text_dir = Path(__file__).parent  # src/shared/text
    model_dir = text_dir / "text-line-classifier" / "model"
    
    # Add model directory to path so relative imports work
    model_dir_str = str(model_dir)
    if model_dir_str not in sys.path:
        sys.path.insert(0, model_dir_str)
    
    # Now we can import features directly (it's in the path)
    # and create a fake package for relative imports
    features_spec = importlib.util.spec_from_file_location(
        "model.features", 
        model_dir / "features.py",
        submodule_search_locations=[model_dir_str]
    )
    if features_spec is None or features_spec.loader is None:
        raise ImportError(f"Cannot load features module from {model_dir}")
    features_module = importlib.util.module_from_spec(features_spec)
    sys.modules["model"] = type(sys)("model")
    sys.modules["model.features"] = features_module
    sys.modules["model"].__path__ = [model_dir_str]
    features_spec.loader.exec_module(features_module)
    
    # Now load classifier with the package structure in place
    classifier_spec = importlib.util.spec_from_file_location(
        "model.classifier",
        model_dir / "classifier.py",
        submodule_search_locations=[model_dir_str]
    )
    if classifier_spec is None or classifier_spec.loader is None:
        raise ImportError(f"Cannot load classifier module from {model_dir}")
    classifier_module = importlib.util.module_from_spec(classifier_spec)
    sys.modules["model.classifier"] = classifier_module
    classifier_spec.loader.exec_module(classifier_module)
    
    _cached_classifier = classifier_module.LineClassifier()
    return _cached_classifier


# Shared tokenization + stopword list used across providers
# This is a conservative superset combining prior stopword lists
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "you", "your", "are", "was", "were",
    "have", "has", "had", "will", "can", "could", "should", "would", "not", "but", "what", "when",
    "where", "which", "who", "why", "how", "its", "it's", "to", "of", "in", "on", "at", "as", "by",
    "or", "if", "is", "be", "we", "they", "them", "their", "i", "me", "my", "our", "us", "so",
    "an", "a", "it", "there", "here", "than", "then", "also", "just", "into", "about", "over", "under",
    "please", "thanks",
    # Additional common conversational fillers from extraction provider
    "ok", "okay", "yes", "yeah", "sure", "well", "now", "let", "lets"
}


def tokenize(text: str) -> List[str]:
    """Tokenize text into a normalized list of lowercase words.

    Uses a permissive token regex that preserves identifiers and snake_case,
    then lowercases the tokens so STOPWORDS can be applied uniformly.
    """
    if not text:
        return []
    return re.findall(r"[A-Za-z][A-Za-z0-9_]+", text.lower())


def classify_text_lines(lines: List[str]) -> List[str]:
    """
    Classify each line using the text-line-classifier model.
    
    Args:
        lines: List of text lines
        
    Returns:
        List of classifications ('code', 'logs', 'text', 'none')
    """
    if not lines:
        return []
    classifier = _get_classifier()
    return classifier.predict(lines)


def detect_cleanable_blocks(
    classifications: List[str],
    min_consecutive: int = 8
) -> List[CleanableBlock]:
    """
    Detect blocks of consecutive lines classified as code, logs, or none
    that are at least min_consecutive lines long.
    
    Args:
        classifications: List of line classifications
        min_consecutive: Minimum consecutive lines to form a cleanable block
        
    Returns:
        List of CleanableBlock objects
    """
    cleanable_types = {'code', 'logs', 'none'}
    blocks: List[CleanableBlock] = []
    
    if not classifications:
        return blocks
    
    current_start: Optional[int] = None
    
    for i, label in enumerate(classifications):
        is_cleanable = label in cleanable_types
        
        if is_cleanable:
            if current_start is None:
                current_start = i
        else:
            # End of a potential block
            if current_start is not None:
                block_length = i - current_start
                if block_length >= min_consecutive:
                    blocks.append(CleanableBlock(start_idx=current_start, end_idx=i))
                current_start = None
    
    # Handle block at end of text
    if current_start is not None:
        block_length = len(classifications) - current_start
        if block_length >= min_consecutive:
            blocks.append(CleanableBlock(start_idx=current_start, end_idx=len(classifications)))
    
    return blocks


def shrink_block(
    lines: List[str],
    keep_ratio: float = 0.10,
    truncate_marker: str = "[...truncated...]"
) -> List[str]:
    """
    Shrink a block of lines by splitting into top, middle, bottom sections
    and keeping only keep_ratio of lines from each.
    
    The block is split into 3 equal parts. If there's a remainder, it goes
    to the middle section.
    
    Args:
        lines: Lines in the block to shrink
        keep_ratio: Fraction of lines to keep from each section (0.0 to 1.0)
        truncate_marker: Marker to insert where content was removed
        
    Returns:
        Shrunk list of lines
    """
    n = len(lines)
    
    if n < 3:
        # Too small to shrink meaningfully
        return lines
    
    # Split into 3 parts: top, middle, bottom
    # Each gets floor(n/3), middle gets the remainder
    section_size = n // 3
    remainder = n % 3
    
    top_size = section_size
    bottom_size = section_size
    middle_size = section_size + remainder
    
    top_lines = lines[:top_size]
    middle_lines = lines[top_size:top_size + middle_size]
    bottom_lines = lines[top_size + middle_size:]
    
    # Process each section
    result: List[str] = []
    
    # Process top section
    shrunk_top = _shrink_section(top_lines, keep_ratio, truncate_marker)
    result.extend(shrunk_top)
    
    # Process middle section
    shrunk_middle = _shrink_section(middle_lines, keep_ratio, truncate_marker)
    result.extend(shrunk_middle)
    
    # Process bottom section
    shrunk_bottom = _shrink_section(bottom_lines, keep_ratio, truncate_marker)
    result.extend(shrunk_bottom)
    
    return result


def _shrink_section(
    lines: List[str],
    keep_ratio: float,
    truncate_marker: str
) -> List[str]:
    """
    Shrink a section by keeping keep_ratio of lines.
    
    Args:
        lines: Lines in the section
        keep_ratio: Fraction to keep (0.0 to 1.0)
        truncate_marker: Marker for truncated content
        
    Returns:
        Shrunk lines with truncate markers
    """
    n = len(lines)
    
    if n == 0:
        return []
    
    # Calculate how many lines to keep (at least 1)
    keep_count = max(1, int(n * keep_ratio))
    
    if keep_count >= n:
        # Keep all lines
        return lines
    
    # Keep first half of keep_count lines, then marker, then last half
    first_half = keep_count // 2
    second_half = keep_count - first_half
    
    if first_half == 0:
        first_half = 1
        second_half = keep_count - 1
    
    result: List[str] = []
    
    # Add first portion
    result.extend(lines[:first_half])
    
    # Add truncate marker
    result.append(truncate_marker)
    
    # Add last portion (if any)
    if second_half > 0:
        result.extend(lines[n - second_half:])
    
    return result


def _compute_line_char_positions(lines: List[str]) -> List[tuple]:
    """
    Compute the character start and end position for each line.
    
    Args:
        lines: List of text lines
        
    Returns:
        List of (start_char, end_char) tuples for each line
    """
    positions = []
    current_pos = 0
    for i, line in enumerate(lines):
        start = current_pos
        end = current_pos + len(line)
        positions.append((start, end))
        current_pos = end + 1  # +1 for the newline character
    return positions


def _filter_blocks_for_protection(
    blocks: List[CleanableBlock],
    line_positions: List[tuple],
    text_length: int,
    protect_chars: int
) -> List[CleanableBlock]:
    """
    Filter out blocks that overlap with protected character regions.
    
    The first protect_chars and last protect_chars of the text are protected
    from truncation. Blocks that fall entirely or partially within these
    regions are excluded.
    
    Args:
        blocks: List of CleanableBlock objects
        line_positions: Character positions for each line (start, end)
        text_length: Total length of the text
        protect_chars: Number of characters to protect at start/end
        
    Returns:
        Filtered list of CleanableBlock objects
    """
    if protect_chars <= 0:
        return blocks
    
    protected_start = protect_chars
    protected_end = text_length - protect_chars
    
    # If protected regions overlap (text too short), don't shrink anything
    if protected_end <= protected_start:
        return []
    
    filtered = []
    for block in blocks:
        # Get character range for this block
        block_start_char = line_positions[block.start_idx][0]
        block_end_char = line_positions[block.end_idx - 1][1]
        
        # Skip if block starts before protected_start ends
        if block_start_char < protected_start:
            continue
        
        # Skip if block ends after protected_end starts
        if block_end_char > protected_end:
            continue
        
        # Block is in the shrinkable middle region
        filtered.append(block)
    
    return filtered


def shrink_text(
    text: str,
    config: Optional[ShrinkConfig] = None
) -> str:
    """
    Shrink text by detecting and truncating cleanable blocks.
    
    If the text is below min_chars_to_shrink, returns it unchanged.
    Detects blocks of code/logs/none that are at least min_consecutive_lines long,
    then shrinks each block keeping only keep_ratio of lines with truncate markers.
    
    The first and last protect_chars characters are never truncated.
    
    Args:
        text: Input text to shrink
        config: Configuration (defaults to loading from config.yaml)
        
    Returns:
        Shrunk text
    """
    if config is None:
        config = ShrinkConfig.from_config()
    
    # Check minimum length
    if len(text) < config.min_chars_to_shrink:
        return text
    
    # Split into lines
    lines = text.split('\n')
    
    if len(lines) < config.min_consecutive_lines:
        return text
    
    # Classify each line
    classifications = classify_text_lines(lines)
    
    # Detect cleanable blocks
    blocks = detect_cleanable_blocks(classifications, config.min_consecutive_lines)
    
    if not blocks:
        return text
    
    # Compute character positions for each line
    line_positions = _compute_line_char_positions(lines)
    
    # Filter out blocks that overlap with protected regions
    blocks = _filter_blocks_for_protection(
        blocks, line_positions, len(text), config.protect_chars
    )
    
    if not blocks:
        return text
    
    # Process blocks in reverse order to maintain indices
    result_lines = lines.copy()
    
    for block in reversed(blocks):
        block_lines = result_lines[block.start_idx:block.end_idx]
        shrunk_lines = shrink_block(
            block_lines,
            keep_ratio=config.keep_ratio,
            truncate_marker=config.truncate_marker
        )
        result_lines[block.start_idx:block.end_idx] = shrunk_lines
    
    return '\n'.join(result_lines)


class TextShrinker:
    """
    Text shrinker that uses the text-line-classifier model to intelligently
    shrink large text blocks.
    
    Usage:
        shrinker = TextShrinker()
        shrunk_text = shrinker.shrink(large_text)
        
        # Or with custom config:
        config = ShrinkConfig(min_chars_to_shrink=2048, keep_ratio=0.15)
        shrinker = TextShrinker(config)
        shrunk_text = shrinker.shrink(large_text)
    """
    
    def __init__(self, config: Optional[ShrinkConfig] = None):
        """
        Initialize the text shrinker.
        
        Args:
            config: Configuration for shrinking. If None, loads from config.yaml.
        """
        self.config = config or ShrinkConfig.from_config()
        self._classifier = None
    
    @property
    def classifier(self):
        """Lazy-loaded classifier."""
        if self._classifier is None:
            self._classifier = _get_classifier()
        return self._classifier
    
    def shrink(self, text: str) -> str:
        """
        Shrink the given text.
        
        Args:
            text: Input text
            
        Returns:
            Shrunk text (or original if below threshold)
        """
        return shrink_text(text, self.config)    
