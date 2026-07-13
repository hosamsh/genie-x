"""
Configuration loader with environment variable substitution.

Provides typed configuration properties for all pipeline stages.
Use get_config() to get the singleton Config instance.
"""

import yaml
import os
import re
import threading
from pathlib import Path
from typing import Dict, Any, Optional
from dotenv import load_dotenv

from src.shared.config.models import (
    AgentType,
    ExtractConfig,
    WebConfig,
    TokenEstimationConfig,
    ModelDefaultsConfig,
    LoggingConfig,
    LOCCountingConfig,
    SearchConfig,
)


class Config:
    """Load and manage configuration with environment variable substitution.
    
    Provides typed properties for each pipeline stage with validation.
    
    Available typed properties:
        - web: WebConfig - Web application settings
        - search: SearchConfig - Search and indexing settings
        - token_estimation: TokenEstimationConfig - Token estimation settings
        - model_defaults: ModelDefaultsConfig - Model defaults and timelines
        - logging: LoggingConfig - Application logging settings
        - loc_counting: LOCCountingConfig - Lines-of-code counting settings
    
    Usage:
        config = get_config()
        
        # Access typed configurations via properties (PREFERRED)
        web_config = config.web
        
        # Or use raw get() for dynamic/custom config sections
        value = config.get("logging.level")
    """
    
    def __init__(self, config_path: str = "config/config.yaml"):
        # Load environment variables from .env file if it exists
        env_file = Path("config/.env")
        if env_file.exists():
            load_dotenv(env_file)
        
        self.config_path = Path(config_path)
        self._config = self._load_config()
        
        # Lazy-loaded typed configs (created on first access)
        self._web: Optional[WebConfig] = None
        self._search: Optional[SearchConfig] = None
        self._token_estimation: Optional[TokenEstimationConfig] = None
        self._model_defaults: Optional[ModelDefaultsConfig] = None
        self._logging: Optional[LoggingConfig] = None
        self._loc_counting: Optional[LOCCountingConfig] = None
    
    def _load_config(self) -> Dict[str, Any]:
        """Load YAML config with environment variable substitution."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        
        with open(self.config_path, encoding='utf-8') as f:
            config_str = f.read()
        
        # Replace ${VAR_NAME} with environment variables
        def replace_env(match):
            var_name = match.group(1)
            value = os.getenv(var_name)
            if value is None:
                # Keep placeholder if env var not set
                return match.group(0)
            # Convert Windows backslashes to forward slashes for YAML compatibility
            return value.replace('\\', '/')
        
        config_str = re.sub(r'\$\{(\w+)\}', replace_env, config_str)
        return yaml.safe_load(config_str)
    
    @property
    def web(self) -> WebConfig:
        """Get web application configuration."""
        if self._web is None:
            self._web = WebConfig.from_dict(self.get("web", {}))
        return self._web

    @property
    def search(self) -> SearchConfig:
        """Get search configuration."""
        if self._search is None:
            self._search = SearchConfig.from_dict(self.get("search", {}))
        return self._search
    
    @property
    def token_estimation(self) -> TokenEstimationConfig:
        """Get token estimation configuration."""
        if self._token_estimation is None:
            self._token_estimation = TokenEstimationConfig.from_dict(self.get("token_estimation", {}))
        return self._token_estimation
    
    @property
    def model_defaults(self) -> ModelDefaultsConfig:
        """Get model defaults configuration."""
        if self._model_defaults is None:
            self._model_defaults = ModelDefaultsConfig.from_dict(self.get("model_defaults", {}))
        return self._model_defaults
    
    @property
    def logging(self) -> LoggingConfig:
        """Get logging configuration."""
        if self._logging is None:
            self._logging = LoggingConfig.from_dict(self.get("logging", {}))
        return self._logging
    
    @property
    def loc_counting(self) -> LOCCountingConfig:
        """Get LOC counting configuration."""
        if self._loc_counting is None:
            self._loc_counting = LOCCountingConfig.from_dict(self.get("loc_counting", {}))
        return self._loc_counting
    
    def get(self, path: str, default=None) -> Any:
        """
        Get config value by dot notation (e.g., 'azure.endpoint').
        
        Args:
            path: Dot-separated path to config value
            default: Default value if path not found
            
        Returns:
            Configuration value or default
        """
        keys = path.split('.')
        value = self._config
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return default
            if value is None:
                return default
        return value
    
    def get_all(self) -> Dict[str, Any]:
        """Get entire configuration dictionary."""
        return self._config.copy()
    
    def reload(self):
        """Reload configuration from file and clear cached typed configs."""
        self._config = self._load_config()
        # Clear cached typed configs so they get recreated with new values
        self._web = None
        self._search = None
        self._token_estimation = None
        self._model_defaults = None
        self._logging = None
        self._loc_counting = None


# Singleton instance
_config: Optional[Config] = None
_config_path: Optional[str] = None
_config_lock = threading.Lock()

def get_config(config_path: Optional[str] = None) -> Config:
    """
    Get singleton Config instance (thread-safe).
    
    Uses double-checked locking to prevent race conditions in multi-threaded
    environments (e.g., web server with multiple worker threads).
    
    Args:
        config_path: Path to config file (only used on first call, defaults to "config/config.yaml")
        
    Returns:
        Config instance
    """
    global _config, _config_path
    
    # If config_path not provided, use existing singleton or default
    if config_path is None:
        if _config is not None:
            # Return existing singleton
            return _config
        # No singleton yet, use default
        config_path = "config/config.yaml"
    
    # Use default path if a non-path value is passed (safety check)
    if config_path in ("copilot", "claude_code", "copilot_cli"):
        # Safety: if someone accidentally passes agent name, use default path
        config_path = "config/config.yaml"
    
    # Double-checked locking pattern for thread safety
    if _config is None or _config_path != config_path:
        with _config_lock:
            # Check again inside lock to prevent duplicate initialization
            if _config is None or _config_path != config_path:
                _config = Config(config_path)
                _config_path = config_path
    
    return _config

def reload_config():
    """Reload configuration from file."""
    global _config
    if _config is not None:
        _config.reload()

def get_extract_config(agent: AgentType) -> ExtractConfig:
    """Get extraction configuration for specific agent."""
    config = get_config()
    extract_config = config.get(f"extract.{agent}", {})
    return ExtractConfig.from_dict(agent, extract_config)

def load_env() -> None:
    """Load environment variables from config/.env file."""
    env_file = Path("config/.env")
    if env_file.exists():
        load_dotenv(env_file)
