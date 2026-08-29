"""Path utilities for file URI handling and normalization.

Shared utilities used by source parsers.
"""
import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import unquote, urlparse


def is_valid_session_id(session_id: str) -> bool:
    """Return True if *session_id* contains only URL-safe identifier characters.

    Accepted: A-Z, a-z, 0-9, hyphen (-), underscore (_).
    Rejects empty strings, path separators, dots, and traversal sequences so
    session IDs can be used safely as file-system path components.
    """
    if not session_id:
        return False
    return bool(re.match(r'^[A-Za-z0-9_\-]+$', session_id))


def is_contained_in(path: Path, root: Path) -> bool:
    """Return True if *path* resolves to a location inside *root*.

    Both paths are fully resolved (symlinks expanded, '..' collapsed) before
    comparison, so directory-traversal attempts such as
    ``root / ".." / "other"`` are correctly detected and rejected.

    Args:
        path: Candidate path to validate.
        root: Trusted root directory that must contain *path*.

    Returns:
        True when resolved *path* equals or is nested inside resolved *root*.
    """
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def normalize_path(path: str) -> str:
    """Normalize path: backslashes to forward slashes, lowercase drive letter.
    
    Examples:
        C:\\Users\\code -> c:/Users/code
        /c:/path -> c:/path
    """
    if not path:
        return ""
    path = path.replace("\\", "/")
    # Handle leading slash before drive letter on Windows (e.g., /c:/ -> c:/)
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    # Lowercase drive letter
    if len(path) >= 2 and path[1] == ":":
        path = path[0].lower() + path[1:]
    return path


def decode_file_uri(uri: str) -> str:
    """Convert file URI to plain path.
    
    Examples:
        file:///c%3A/path -> c:/path
        file:///home/user -> /home/user
    """
    if not uri:
        return ""
    
    parsed = urlparse(uri)
    if parsed.scheme.lower() == "file":
        path = unquote(parsed.path or "")
    else:
        path = unquote(uri)
    
    return normalize_path(path)


# =============================================================================
# WSL Path Resolution
# =============================================================================

def parse_vscode_remote_uri(uri: str) -> Optional[Tuple[str, str, str]]:
    """Parse a VSCode remote URI into its components.
    
    Args:
        uri: A VSCode remote URI like 'vscode-remote://wsl+ubuntu/path/to/folder'
        
    Returns:
        Tuple of (remote_type, remote_name, path) or None if not a remote URI.
        Example: ('wsl', 'ubuntu', '/mnt/c/code/projects/fakaudio')
    """
    if not uri or not uri.startswith("vscode-remote://"):
        return None
    
    # Parse: vscode-remote://wsl+ubuntu/path/to/folder
    # Remove the scheme
    rest = uri[len("vscode-remote://"):]
    
    # Find the first slash that separates authority from path
    slash_idx = rest.find("/")
    if slash_idx == -1:
        return None
    
    authority = rest[:slash_idx]  # e.g., "wsl+ubuntu"
    path = rest[slash_idx:]       # e.g., "/mnt/c/code/projects/fakaudio"
    
    # Parse authority (format: type+name or type+encoded_name)
    if "+" in authority:
        remote_type, remote_name = authority.split("+", 1)
        return (remote_type.lower(), unquote(remote_name), path)
    
    return None


def resolve_wsl_path(wsl_distro: str, linux_path: str) -> Optional[str]:
    """Resolve a WSL Linux path to an accessible Windows path.
    
    Tries multiple strategies:
    1. Direct WSL localhost path: \\\\wsl.localhost\\{distro}\\{path}
    2. If path is /mnt/X/..., try the Windows drive directly: X:/...
    
    Args:
        wsl_distro: WSL distribution name (e.g., 'ubuntu', 'Ubuntu')
        linux_path: Linux path within WSL (e.g., '/mnt/c/code/project')
        
    Returns:
        Accessible Windows path, or None if no accessible path found.
        Returns None immediately on non-Windows platforms.
    """
    import platform
    if platform.system() != "Windows":
        return None
    
    def path_exists_safe(p: str) -> bool:
        """Check if path exists, handling permission errors."""
        try:
            return Path(p).exists()
        except (PermissionError, OSError):
            return False
    
    # Strategy 1: If path is a /mnt/X mount, try accessing via Windows drive first
    # /mnt/c/code/projects/fakaudio -> c:/code/projects/fakaudio
    # This is more reliable than WSL localhost for mounted drives
    mnt_match = re.match(r'^/mnt/([a-zA-Z])/(.*)$', linux_path)
    if mnt_match:
        drive_letter = mnt_match.group(1).lower()
        rest_of_path = mnt_match.group(2)
        windows_path = f"{drive_letter}:/{rest_of_path}"
        
        if path_exists_safe(windows_path):
            return windows_path
    
    # Strategy 2: Try direct WSL localhost path
    # \\wsl.localhost\Ubuntu\path -> works for native WSL files
    # Build parts without using complex f-strings to avoid Python <3.12 parsing issues
    prefix = "\\\\wsl.localhost\\" + wsl_distro.capitalize()
    windows_style = linux_path.replace('/', "\\")
    wsl_localhost_path = prefix + windows_style

    if path_exists_safe(wsl_localhost_path):
        return wsl_localhost_path

    # Also try lowercase distro name
    prefix_lower = "\\\\wsl.localhost\\" + wsl_distro.lower()
    wsl_localhost_lower = prefix_lower + windows_style
    if path_exists_safe(wsl_localhost_lower):
        return wsl_localhost_lower
    
    return None


def resolve_workspace_path(workspace_folder: str) -> Tuple[str, bool]:
    """Resolve a workspace folder path to an accessible local path.
    
    Handles:
    - Regular local paths (returned as-is)
    - VSCode remote WSL URIs (resolved to accessible Windows path)
    
    Args:
        workspace_folder: Original workspace folder path or URI
        
    Returns:
        Tuple of (resolved_path, was_resolved) where:
        - resolved_path: The accessible local path (or original if not resolvable)
        - was_resolved: True if the path was transformed from a remote URI
    """
    if not workspace_folder:
        return (workspace_folder, False)
    
    # Handle VSCode remote URIs
    if workspace_folder.startswith("vscode-remote://"):
        parsed = parse_vscode_remote_uri(workspace_folder)
        
        if parsed:
            remote_type, remote_name, linux_path = parsed
            
            # Handle WSL remotes
            if remote_type == "wsl":
                resolved = resolve_wsl_path(remote_name, linux_path)
                if resolved:
                    return (resolved, True)
        
        # Could not resolve - return original
        return (workspace_folder, False)
    
    # Regular path - return as-is
    return (workspace_folder, False)


# Directory names that act as containers for individual user home directories
# across operating systems (case-insensitive). A folder sitting directly inside
# one of these is a user home directory, not a project.
_HOME_CONTAINER_DIRS = {"users", "home"}


def _is_root_only_prefix(segments: list[str]) -> bool:
    """True if *segments* form only a filesystem-root/drive/mount prefix.

    Used to distinguish ``.../Users/<name>`` (a home directory) from
    ``.../Users/<name>/project`` (a real folder that merely lives under a home).
    """
    if not segments:
        return True  # POSIX root, e.g. "/home" or "/Users"
    if len(segments) == 1 and segments[0].endswith(":"):
        return True  # Windows drive root, e.g. "c:/Users"
    if len(segments) == 2 and segments[0] == "mnt" and len(segments[1]) == 1:
        return True  # WSL mount, e.g. "/mnt/c/Users"
    return False


def is_home_or_root_dir(path: str) -> bool:
    """Return True if *path* is a filesystem root or a user home directory.

    This is a purely *structural* check with no hardcoded usernames or
    machine-specific paths. It identifies locations that are never real
    project folders, regardless of OS or environment:

    - Filesystem / drive / mount roots: ``/``, ``C:\\``, ``/mnt/c`` ...
    - User home directories: ``/home/<user>``, ``/Users/<user>``,
      ``<drive>:/Users/<user>``, ``/mnt/<drive>/Users/<user>``, ``/root`` ...
    - The current process's own home directory (``Path.home()``).

    These typically appear because CLI agents record the working directory
    they were launched from, which may be a home directory rather than a
    project.
    """
    if not path:
        return False

    normalized = normalize_path(path).rstrip("/").lower()
    if not normalized:
        return True  # POSIX filesystem root "/"

    # The current user's actual home directory (any OS), resolved dynamically.
    try:
        home = normalize_path(str(Path.home())).rstrip("/").lower()
        if home and normalized == home:
            return True
    except (RuntimeError, OSError):
        pass

    segments = [s for s in normalized.split("/") if s]
    if not segments:
        return True

    # Windows drive root, e.g. "c:" (from "C:\").
    if len(segments) == 1 and segments[0].endswith(":"):
        return True

    # Single-segment system home/root, e.g. "/root".
    if segments == ["root"]:
        return True

    # WSL / Unix mount root, e.g. "/mnt/c".
    if len(segments) == 2 and segments[0] == "mnt" and len(segments[1]) == 1:
        return True

    # A home directory sitting directly inside a user-container dir, e.g.
    # "/home/<user>", "/Users/<user>", "c:/Users/<user>", "/mnt/c/Users/<user>".
    # Only matches when nothing but a root/drive/mount prefix precedes the
    # container, so real projects nested under a home dir are preserved.
    if len(segments) >= 2 and segments[-2] in _HOME_CONTAINER_DIRS:
        if _is_root_only_prefix(segments[:-2]):
            return True

    return False
