"""
Configuration data models.

All configuration dataclasses are defined here for consistency.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List

from src.shared.models.dataclass_mixin import DataclassIO


class ConfigurationError(Exception):
    """Raised when configuration is invalid or missing required values."""
    pass


# Type alias for agent names - using str to allow extensibility
# Registered agents are validated at runtime via agent_registry
AgentType = str


# ============================================================================
# Core Configuration Models
# ============================================================================


@dataclass
class ExtractConfig(DataclassIO):
    """Configuration for workspace extraction (per agent).
    
    All config fields from YAML are stored in 'extra' and accessed via get() method.
    This ensures consistency - all config comes from the agent's YAML section.
    """
    
    agent: AgentType
    extra: Dict[str, Any] = field(default_factory=dict)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get config value from extra dict."""
        return self.extra.get(key, default)
    
    @classmethod
    def from_dict(cls, agent: AgentType, data: Dict[str, Any]) -> "ExtractConfig":
        """Create from configuration dictionary.
        
        All fields from YAML go into 'extra' for consistent access.
        """
        # All config goes into extra
        extra = dict(data)
        
        return cls(
            agent=agent,
            extra=extra,
        )


@dataclass
class WebConfig:
    """Configuration for the web application."""
    run_dir: str = "data/web"
    port: int = 8000
    db_filename: str = "genie.db"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WebConfig":
        return cls(
            run_dir=data.get("run_dir", "data/web"),
            port=data.get("port", 8000),
            db_filename=data.get("db_filename", "genie.db"),
        )


@dataclass
class SearchConfig:
    """Configuration for search and indexing."""

    default_mode: str = "hybrid"
    rrf_k: int = 60
    keyword_fetch_limit: int = 200
    semantic_fetch_limit: int = 200
    semantic_min_score: float = 0.3
    semantic_strict_min_score: float = 0.5
    semantic_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_batch_size: int = 64
    max_page_size: int = 100
    auto_embed_on_extraction: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SearchConfig":
        return cls(
            default_mode=data.get("default_mode", "hybrid"),
            rrf_k=int(data.get("rrf_k", 60)),
            keyword_fetch_limit=int(data.get("keyword_fetch_limit", 200)),
            semantic_fetch_limit=int(data.get("semantic_fetch_limit", 200)),
            semantic_min_score=float(data.get("semantic_min_score", 0.3)),
            semantic_strict_min_score=float(data.get("semantic_strict_min_score", 0.5)),
            semantic_model=data.get("semantic_model", "sentence-transformers/all-MiniLM-L6-v2"),
            embedding_batch_size=int(data.get("embedding_batch_size", 64)),
            max_page_size=int(data.get("max_page_size", 100)),
            auto_embed_on_extraction=bool(data.get("auto_embed_on_extraction", False)),
        )


# ============================================================================
# Token Estimation Configuration
# ============================================================================

@dataclass
class TokenEstimationConfig:
    """Configuration for token estimation."""
    
    reasoning_buffer_multiplier: float = 2.0
    tool_call_multiplier: float = 1.5
    tool_base_overhead: int = 16
    tool_per_tool_overhead: int = 8
    tool_definition_margin: float = 1.1
    message_overhead: int = 3
    max_context_window: int = 200000
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TokenEstimationConfig":
        """Create from config dictionary."""
        return cls(
            reasoning_buffer_multiplier=data.get("reasoning_buffer_multiplier", 2.0),
            tool_call_multiplier=data.get("tool_call_multiplier", 1.5),
            tool_base_overhead=data.get("tool_base_overhead", 16),
            tool_per_tool_overhead=data.get("tool_per_tool_overhead", 8),
            tool_definition_margin=data.get("tool_definition_margin", 1.1),
            message_overhead=data.get("message_overhead", 3),
            max_context_window=data.get("max_context_window", 200000),
        )


# ============================================================================
# Model Defaults Configuration
# ============================================================================

@dataclass
class ModelDefaultsConfig:
    """Configuration for model defaults and timelines."""
    
    enabled: bool = True
    copilot: Dict[str, Any] = field(default_factory=dict)  # timeline + default
    cursor: Dict[str, Any] = field(default_factory=dict)   # timeline + default
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelDefaultsConfig":
        """Create from config dictionary."""
        return cls(
            enabled=data.get("enabled", True),
            copilot=data.get("copilot", {}),
            cursor=data.get("cursor", {}),
        )


# ============================================================================
# Utility Configuration Models
# ============================================================================

@dataclass
class LoggingConfig:
    """Configuration for application logging."""
    
    level: str = "INFO"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LoggingConfig":
        """Create from config dictionary."""
        return cls(
            level=data.get("level", "INFO"),
        )


@dataclass
class LOCCountingConfig:
    """Configuration for lines-of-code counting."""
    
    use_gitignore: bool = True
    max_file_size_mb: int = 10
    max_files_to_process: int = 50000
    ignore_patterns: List[str] = field(default_factory=list)
    code_extensions: List[str] = field(default_factory=list)
    doc_extensions: List[str] = field(default_factory=list)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LOCCountingConfig":
        """Create from config dictionary."""
        def _to_list(val: Any) -> List[str]:
            """Normalize a config value to a list of strings.

            Accepts either a YAML list or a comma-separated string.
            """
            if isinstance(val, str):
                return [p.strip() for p in val.split(",") if p.strip()]
            if isinstance(val, list):
                return [str(p).strip() for p in val if str(p).strip()]
            return []

        def _normalize_extensions(items: List[str]) -> List[str]:
            """Normalize file-extension entries: dedupe, lowercase, ensure leading dot.

            Preserves order of first occurrence.
            """
            seen = set()
            out: List[str] = []
            for it in items:
                s = it.strip().lower()
                if not s:
                    continue
                # Add leading dot for plain extensions (e.g. 'md' -> '.md')
                if not s.startswith('.'):
                    # if looks like an extension (no path separators and short), add dot
                    if '/' not in s and '\\' not in s and len(s) <= 10:
                        s = '.' + s
                if s in seen:
                    continue
                seen.add(s)
                out.append(s)
            return out

        ignore = _to_list(data.get("ignore_patterns", []))
        code_exts = _normalize_extensions(_to_list(data.get("code_extensions", [])))
        doc_exts = _normalize_extensions(_to_list(data.get("doc_extensions", [])))

        return cls(
            use_gitignore=data.get("use_gitignore", True),
            max_file_size_mb=data.get("max_file_size_mb", 10),
            max_files_to_process=data.get("max_files_to_process", 50000),
            ignore_patterns=ignore,
            code_extensions=code_exts,
            doc_extensions=doc_exts,
        )

