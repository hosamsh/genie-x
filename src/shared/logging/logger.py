"""Logger implementation for the genie-x pipeline.

Provides a custom logger class that:
- Outputs to console with colored formatting
- Supports emoji-rich progress messages
- Has a special 'progress' level for inline output (no newline)
- Thread-safe singleton initialization
"""

import io
import logging
import sys
import threading
from typing import Optional, Union

# Define log levels
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "PROGRESS": 25,  # Between INFO and WARNING, for progress messages
}

# Register custom PROGRESS level
logging.addLevelName(LOG_LEVELS["PROGRESS"], "PROGRESS")

# ANSI color codes for terminal output
class Colors:
    """ANSI color codes for terminal output."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    
    # Foreground colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"


class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors to log messages based on level."""
    
    LEVEL_COLORS = {
        logging.DEBUG: Colors.GRAY,
        logging.INFO: Colors.CYAN,
        LOG_LEVELS["PROGRESS"]: Colors.GREEN,
        logging.WARNING: Colors.YELLOW,
        logging.ERROR: Colors.RED,
        logging.CRITICAL: Colors.RED + Colors.BOLD,
    }
    
    def __init__(self, fmt: Optional[str] = None, datefmt: Optional[str] = None, use_colors: bool = True):
        super().__init__(fmt, datefmt)
        self.use_colors = use_colors
    
    def format(self, record: logging.LogRecord) -> str:
        if self.use_colors:
            color = self.LEVEL_COLORS.get(record.levelno, Colors.RESET)
            # Format the message with color
            record.msg = f"{color}{record.msg}{Colors.RESET}"
        return super().format(record)


class ProgressStreamHandler(logging.StreamHandler):
    """Custom stream handler that supports inline progress output with UTF-8 encoding.
    
    When handling PROGRESS level messages that end with specific markers,
    outputs without a newline to allow inline updates.
    """
    
    def __init__(self, stream=None):
        """Initialize handler with UTF-8 encoded stream."""
        if stream is None:
            # Wrap stdout with UTF-8 encoding for emoji support
            stream = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding='utf-8',
                errors='replace',
                line_buffering=True
            )
        super().__init__(stream)
    
    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            stream = self.stream
            
            # Check if this is an inline progress message
            is_inline = getattr(record, 'inline', False)
            
            if is_inline:
                stream.write(msg)
                stream.flush()
            else:
                stream.write(msg + self.terminator)
                self.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


class PipelineLogger(logging.Logger):
    """Extended logger with progress method for pipeline output."""
    
    def progress(self, msg: str, *args, inline: bool = False, **kwargs):
        """Log a progress message.
        
        Args:
            msg: The message to log
            inline: If True, don't add newline (for same-line updates)
            *args: Format arguments for the message
            **kwargs: Additional logging keyword arguments
        """
        if self.isEnabledFor(LOG_LEVELS["PROGRESS"]):
            extra = kwargs.get('extra', {})
            extra['inline'] = inline
            kwargs['extra'] = extra
            self._log(LOG_LEVELS["PROGRESS"], msg, args, **kwargs)
    
    def banner(self, text: str, char: str = "=", width: int = 80):
        """Log a banner message.
        
        Args:
            text: Banner text to display
            char: Character to use for the banner lines
            width: Width of the banner in characters
        """
        line = char * width
        self.progress(line)
        self.progress(text)
        self.progress(line)
    
    def section(self, text: str):
        """Log a section header.
        
        Args:
            text: Section header text
        """
        self.progress(f"\n{text}")
        self.progress("-" * len(text))


class LoggingManager:
    """Thread-safe singleton manager for logging configuration."""
    
    _instance: Optional['LoggingManager'] = None
    _lock = threading.Lock()
    
    def __init__(self):
        """Initialize the logging manager."""
        self._console_handler: Optional[logging.Handler] = None
        self._root_logger: Optional[logging.Logger] = None
        self._initialized = False
    
    @classmethod
    def get_instance(cls) -> 'LoggingManager':
        """Get the singleton instance (thread-safe)."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def initialize(self):
        """Initialize the logging system with default configuration."""
        with self._lock:
            if self._initialized:
                return
            
            # Set our custom logger class
            logging.setLoggerClass(PipelineLogger)
            
            # Get the actual root logger so all loggers inherit the handler
            self._root_logger = logging.getLogger()
            self._root_logger.setLevel(logging.DEBUG)  # Allow all levels, handlers will filter
            
            # Create console handler with UTF-8 support
            self._console_handler = ProgressStreamHandler()
            self._console_handler.setLevel(logging.INFO)  # Default to INFO level
            
            # Use simple format for console
            console_format = "%(message)s"
            self._console_handler.setFormatter(ColoredFormatter(console_format, use_colors=True))
            
            # Add handler to root logger
            self._root_logger.addHandler(self._console_handler)
            
            self._initialized = True
    
    def reset(self):
        """Reset logging configuration (useful for testing)."""
        with self._lock:
            if self._root_logger and self._console_handler:
                self._root_logger.removeHandler(self._console_handler)
            self._console_handler = None
            self._root_logger = None
            self._initialized = False
    
    def set_level(self, level: int):
        """Set the console log level."""
        if self._console_handler:
            self._console_handler.setLevel(level)
    
    def set_colors(self, use_colors: bool):
        """Enable or disable colored output."""
        if self._console_handler:
            self._console_handler.setFormatter(
                ColoredFormatter("%(message)s", use_colors=use_colors)
            )
    
    @property
    def is_initialized(self) -> bool:
        """Check if logging is initialized."""
        return self._initialized


def setup_logging(
    level: Optional[Union[int, str]] = None,
    use_colors: bool = True,
) -> None:
    """Configure the logging system.
    
    Args:
        level: Console log level (default: read from config, fallback to INFO)
        use_colors: Whether to use colored output (default: True)
        
    Raises:
        ValueError: If an invalid log level is provided
    """
    manager = LoggingManager.get_instance()
    manager.initialize()
    
    # Load from config if level not provided
    if level is None:
        try:
            from src.shared.config.config_loader import get_config
            config = get_config()
            logging_config = config.logging
            level = logging_config.level
        except ImportError:
            # Config module not available, use default
            level = "INFO"
        except AttributeError:
            # Config doesn't have logging section
            level = "INFO"
        except Exception as e:
            # Unexpected error reading config - log warning and use default
            sys.stderr.write(f"Warning: Failed to read logging config: {e}\n")
            level = "INFO"
    
    if isinstance(level, str):
        level = LOG_LEVELS.get(level.upper(), logging.INFO)
    
    manager.set_level(level)
    manager.set_colors(use_colors)


def set_log_level(level: Union[int, str]) -> None:
    """Set the console log level.
    
    Args:
        level: Log level (int or string like 'DEBUG', 'INFO', etc.)
        
    Raises:
        ValueError: If an invalid log level string is provided
    """
    manager = LoggingManager.get_instance()
    manager.initialize()
    
    if isinstance(level, str):
        level_upper = level.upper()
        if level_upper not in LOG_LEVELS:
            raise ValueError(
                f"Invalid log level: {level}. "
                f"Valid levels: {', '.join(LOG_LEVELS.keys())}"
            )
        level = LOG_LEVELS[level_upper]
    
    manager.set_level(level)


def get_logger(name: str) -> PipelineLogger:
    """Get a logger instance for the given module name.
    
    Args:
        name: Module name (typically __name__)
        
    Returns:
        PipelineLogger instance with progress, banner, and section methods
        
    Raises:
        RuntimeError: If logger is not a PipelineLogger instance after initialization
    """
    manager = LoggingManager.get_instance()
    manager.initialize()
    
    # Get logger - should be PipelineLogger due to setLoggerClass
    logger = logging.getLogger(name)
    
    # Verify we got the right type
    if not isinstance(logger, PipelineLogger):
        raise RuntimeError(
            f"Logger '{name}' is not a PipelineLogger instance. "
            f"Got {type(logger)} instead. This indicates a logging configuration issue."
        )
    
    return logger


def reset_logging() -> None:
    """Reset logging configuration. Primarily for testing purposes."""
    manager = LoggingManager.get_instance()
    manager.reset()
