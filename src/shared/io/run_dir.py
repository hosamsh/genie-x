"""Run directory and database path utilities.

Centralized module for all run directory and database path operations.
All code that needs to access the database should use these functions.
"""

import sys
from pathlib import Path
from typing import overload, Literal

from src.shared.logging.logger import get_logger

logger = get_logger(__name__)

# Default database filename
DEFAULT_DB_FILENAME = "genie.db"


def get_db_filename() -> str:
    """Get the database filename from config.
    
    Returns:
        Database filename (default: genie.db)
    """
    try:
        from src.shared.config.config_loader import get_config
        config = get_config()
        if hasattr(config, 'web') and hasattr(config.web, 'db_filename'):
            return config.web.db_filename or DEFAULT_DB_FILENAME
    except Exception:
        pass
    return DEFAULT_DB_FILENAME


def get_db_path(run_dir: Path) -> Path:
    """Get the database path for a given run directory.
    
    Args:
        run_dir: Path to the run directory
        
    Returns:
        Path to the database file
    """
    return run_dir / get_db_filename()


@overload
def require_db_path(run_dir: Path, exit_on_missing: Literal[True] = True) -> Path: ...

@overload
def require_db_path(run_dir: Path, exit_on_missing: Literal[False]) -> Path | None: ...


def require_db_path(run_dir: Path, exit_on_missing: bool = True) -> Path | None:
    """Get and validate the database path exists.
    
    Args:
        run_dir: Path to the run directory
        exit_on_missing: If True, exit with error if DB not found. If False, return None.
        
    Returns:
        Path to the database file if it exists, None otherwise (if exit_on_missing=False)
    """
    db_path = get_db_path(run_dir)
    
    if not db_path.exists():
        logger.error(f"Database not found: {db_path}")
        if exit_on_missing:
            sys.exit(1)
        return None
    
    return db_path
