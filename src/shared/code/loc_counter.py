"""
Lines of Code (LOC) Counter for workspace projects.

Counts lines of code in a project folder, respecting:
1. .gitignore patterns (if present)
2. Master ignore patterns from configuration
3. Configured file extensions for code and documentation

Does NOT require git to be installed - implements gitignore pattern matching natively.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from src.shared.config.config_loader import get_config


@dataclass
class LOCResult:
    """Result of LOC counting operation."""
    total_code_loc: int = 0
    total_doc_loc: int = 0
    code_files_count: int = 0
    doc_files_count: int = 0
    # Optional detailed breakdown by extension
    code_by_extension: Dict[str, int] = field(default_factory=dict)
    doc_by_extension: Dict[str, int] = field(default_factory=dict)


class GitignorePattern:
    """Represents a single gitignore pattern with its matching logic."""
    
    def __init__(self, pattern: str, base_path: Path, is_negation: bool = False):
        self.original = pattern
        self.base_path = base_path
        self.is_negation = is_negation
        self.is_directory_only = pattern.endswith('/')
        self.is_anchored = '/' in pattern.rstrip('/')
        
        # Remove trailing slash for matching
        pattern = pattern.rstrip('/')
        
        # Convert gitignore pattern to regex
        self.regex = self._pattern_to_regex(pattern)
    
    def _pattern_to_regex(self, pattern: str) -> re.Pattern:
        """Convert a gitignore pattern to a compiled regex."""
        # Escape special regex characters except * and ?
        regex = ""
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == '*':
                if i + 1 < len(pattern) and pattern[i + 1] == '*':
                    # ** matches everything including /
                    if i + 2 < len(pattern) and pattern[i + 2] == '/':
                        regex += "(?:.*/)?(?:.*/)?"
                        i += 3
                        continue
                    else:
                        regex += ".*"
                        i += 2
                        continue
                else:
                    # * matches everything except /
                    regex += "[^/]*"
            elif c == '?':
                regex += "[^/]"
            elif c == '[':
                # Character class - find closing bracket
                j = i + 1
                if j < len(pattern) and pattern[j] == '!':
                    j += 1
                if j < len(pattern) and pattern[j] == ']':
                    j += 1
                while j < len(pattern) and pattern[j] != ']':
                    j += 1
                if j < len(pattern):
                    regex += pattern[i:j+1].replace('!', '^', 1)
                    i = j
                else:
                    regex += re.escape(c)
            elif c in '.^$+{}|()\\':
                regex += '\\' + c
            else:
                regex += c
            i += 1
        
        # Anchor pattern appropriately
        if self.is_anchored:
            regex = "^" + regex
        else:
            regex = "(?:^|/)" + regex
        
        regex += "(?:/.*)?$"
        
        return re.compile(regex)
    
    def matches(self, path: str, is_dir: bool = False) -> bool:
        """Check if a path matches this pattern."""
        # Directory-only patterns don't match files
        if self.is_directory_only and not is_dir:
            return False
        
        # Normalize path separators
        path = path.replace('\\', '/')
        
        return bool(self.regex.search(path))


class GitignoreMatcher:
    """Matches paths against gitignore patterns."""
    
    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.patterns: List[GitignorePattern] = []
    
    def add_patterns(self, patterns: List[str], pattern_base: Optional[Path] = None):
        """Add patterns from a list (e.g., from config or .gitignore file)."""
        base = pattern_base or self.base_path
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern or pattern.startswith('#'):
                continue
            
            is_negation = pattern.startswith('!')
            if is_negation:
                pattern = pattern[1:]
            
            self.patterns.append(GitignorePattern(pattern, base, is_negation))
    
    def add_gitignore_file(self, gitignore_path: Path):
        """Add patterns from a .gitignore file."""
        if not gitignore_path.exists():
            return
        
        try:
            with open(gitignore_path, 'r', encoding='utf-8', errors='ignore') as f:
                patterns = f.read().splitlines()
            self.add_patterns(patterns, gitignore_path.parent)
        except (IOError, OSError):
            pass
    
    def is_ignored(self, path: Path, is_dir: bool = False) -> bool:
        """Check if a path should be ignored.
        
        Args:
            path: Absolute or relative path to check
            is_dir: Whether the path is a directory
            
        Returns:
            True if path should be ignored
        """
        # Get relative path from base
        try:
            rel_path = path.relative_to(self.base_path)
        except ValueError:
            rel_path = path
        
        rel_str = str(rel_path).replace('\\', '/')
        
        # Check each pattern in order (later patterns can override earlier ones)
        ignored = False
        for pattern in self.patterns:
            if pattern.matches(rel_str, is_dir):
                ignored = not pattern.is_negation
        
        return ignored


def get_loc_config() -> Dict:
    """Get LOC counting configuration from config.yaml.
    
    All configuration must be defined in config.yaml under loc_counting section.
    Required keys: ignore_patterns, code_extensions, doc_extensions, use_gitignore, max_file_size_mb
    """
    config = get_config()
    loc_config = config.loc_counting
    
    # All config comes from config.yaml - no defaults in code
    return {
        "ignore_patterns": loc_config.ignore_patterns,
        "code_extensions": loc_config.code_extensions,
        "doc_extensions": loc_config.doc_extensions,
        "use_gitignore": loc_config.use_gitignore,
        "max_file_size_mb": loc_config.max_file_size_mb,
        "max_files_to_process": loc_config.max_files_to_process,
    }


def count_lines_in_file(file_path: Path, max_size_mb: float = 10) -> int:
    """Count lines in a single file.
    
    Args:
        file_path: Path to the file
        max_size_mb: Maximum file size in MB to process
        
    Returns:
        Number of lines, or 0 if file cannot be read
    """
    try:
        # Skip very large files (probably not source code)
        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > max_size_mb:
            return 0
        
        # Try to read with UTF-8, fall back to latin-1
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return sum(1 for _ in f)
        except (IOError, OSError):
            return 0
            
    except (IOError, OSError):
        return 0


def count_loc(workspace_folder: str) -> LOCResult:
    """Count lines of code in a workspace folder.
    
    Args:
        workspace_folder: Path to the workspace/project folder
        
    Returns:
        LOCResult with total_code_loc and total_doc_loc
    """
    from src.shared.logging.logger import get_logger
    from src.shared.io.paths import resolve_workspace_path
    
    logger = get_logger(__name__)
    
    result = LOCResult()
    
    # Handle invalid/empty paths
    if not workspace_folder or workspace_folder in ("N/A", ""):
        logger.warning(f"Cannot count LOC: workspace folder is empty or invalid: '{workspace_folder}'")
        return result
    
    # Try to resolve remote paths (e.g., VSCode WSL remotes)
    resolved_path, was_resolved = resolve_workspace_path(workspace_folder)
    
    if was_resolved:
        logger.info(f"Resolved remote workspace path: {workspace_folder} -> {resolved_path}")
        workspace_folder = resolved_path
    elif workspace_folder.startswith("vscode-remote://"):
        # Could not resolve the remote path
        logger.warning(f"Cannot count LOC for VSCode remote workspace: {workspace_folder}")
        logger.warning("  Could not resolve to an accessible local path")
        return result
    
    workspace_path = Path(workspace_folder)
    if not workspace_path.exists():
        logger.warning(f"Cannot count LOC: workspace path does not exist: {workspace_folder}")
        return result
    
    if not workspace_path.is_dir():
        logger.warning(f"Cannot count LOC: workspace path is not a directory: {workspace_folder}")
        return result
    
    # Get configuration
    loc_config = get_loc_config()
    ignore_patterns = loc_config["ignore_patterns"]
    code_extensions = set(ext.lower() for ext in loc_config["code_extensions"])
    doc_extensions = set(ext.lower() for ext in loc_config["doc_extensions"])
    use_gitignore = loc_config["use_gitignore"]
    max_file_size_mb = loc_config["max_file_size_mb"]
    max_files_to_process = loc_config.get("max_files_to_process", 50000)
    
    # Set up ignore matcher
    matcher = GitignoreMatcher(workspace_path)
    
    # Add master ignore patterns from config
    matcher.add_patterns(ignore_patterns)
    
    # Add .gitignore patterns if present and enabled
    if use_gitignore:
        gitignore_path = workspace_path / ".gitignore"
        matcher.add_gitignore_file(gitignore_path)
    
    # Get all known workspace folders to filter out nested workspaces
    # This prevents counting the same code multiple times when a parent folder
    # contains multiple child workspace folders
    from src.shared.workspace_discovery import get_all_workspace_folders
    all_workspace_folders = get_all_workspace_folders()
    current_workspace_normalized = workspace_path.as_posix().lower()
    
    # Build set of nested workspace folders (workspaces that are children of current)
    nested_workspaces: set[str] = set()
    for ws_folder in all_workspace_folders:
        # Skip the current workspace itself
        if ws_folder == current_workspace_normalized:
            continue
        # Check if this workspace is a child of the current workspace
        if ws_folder.startswith(current_workspace_normalized + "/"):
            nested_workspaces.add(ws_folder)
    
    if nested_workspaces:
        logger.info(f"Found {len(nested_workspaces)} nested workspace(s) that will be excluded from LOC count")
    
    # Track total files processed to avoid runaway loops
    files_processed = 0
    dirs_scanned = 0
    last_progress = 0
    
    # Walk the directory tree
    for root, dirs, files in os.walk(workspace_path):
        root_path = Path(root)
        dirs_scanned += 1
        
        # Log progress every 100 directories
        if dirs_scanned - last_progress >= 100:
            logger.progress(f"   Scanned {dirs_scanned} dirs, processed {files_processed} files...")
            last_progress = dirs_scanned
        
        # Filter out ignored directories and nested workspaces (modify in place to skip them)
        filtered_dirs = []
        for d in dirs:
            dir_path = root_path / d
            # Skip if ignored by gitignore patterns
            if matcher.is_ignored(dir_path, is_dir=True):
                continue
            # Skip if this directory is a nested workspace
            dir_normalized = dir_path.as_posix().lower()
            if dir_normalized in nested_workspaces:
                logger.debug(f"Skipping nested workspace: {d}")
                continue
            filtered_dirs.append(d)
        dirs[:] = filtered_dirs
        
        # Also check nested .gitignore files
        if use_gitignore:
            nested_gitignore = root_path / ".gitignore"
            if nested_gitignore.exists():
                matcher.add_gitignore_file(nested_gitignore)
        
        # Process files
        for filename in files:
            # Check file limit to prevent hanging on huge directories
            if files_processed >= max_files_to_process:
                return result  # Return what we have so far
            
            file_path = root_path / filename
            
            # Skip ignored files
            if matcher.is_ignored(file_path, is_dir=False):
                continue
            
            # Get file extension
            ext = file_path.suffix.lower()
            
            # Count lines based on file type
            if ext in code_extensions:
                lines = count_lines_in_file(file_path, max_file_size_mb)
                result.total_code_loc += lines
                result.code_files_count += 1
                result.code_by_extension[ext] = result.code_by_extension.get(ext, 0) + lines
                files_processed += 1
                
            elif ext in doc_extensions:
                lines = count_lines_in_file(file_path, max_file_size_mb)
                result.total_doc_loc += lines
                result.doc_files_count += 1
                result.doc_by_extension[ext] = result.doc_by_extension.get(ext, 0) + lines
                files_processed += 1
    
    return result


def count_loc_safe(workspace_folder: str) -> Tuple[int, int]:
    """Safe wrapper for count_loc that returns (0, 0) on any error.
    
    Args:
        workspace_folder: Path to the workspace/project folder
        
    Returns:
        Tuple of (total_code_loc, total_doc_loc)
    """
    from src.shared.logging.logger import get_logger
    logger = get_logger(__name__)
    
    try:
        result = count_loc(workspace_folder)
        
        # Log if we got zero results (might indicate a problem)
        if result.total_code_loc == 0 and result.total_doc_loc == 0:
            logger.info(f"LOC count returned 0 for {workspace_folder} (this may be expected for remote workspaces)")
        
        return (result.total_code_loc, result.total_doc_loc)
    except Exception as e:
        logger.warning(f"LOC counting failed for {workspace_folder}: {e}")
        logger.info("  Will use sum of code changes as fallback for metrics")
        return (0, 0)
