"""Unit tests for src.shared.io.paths – is_valid_session_id and is_contained_in."""
from __future__ import annotations


import pytest

from src.shared.io.paths import is_valid_session_id, is_contained_in


# =============================================================================
# is_valid_session_id
# =============================================================================

class TestIsValidSessionId:
    """Verify safe/unsafe session ID classification."""

    # --- valid inputs ---
    def test_simple_alphanumeric(self):
        assert is_valid_session_id("abc123") is True

    def test_uppercase(self):
        assert is_valid_session_id("UPPER123") is True

    def test_hyphen_allowed(self):
        assert is_valid_session_id("session-abc-123") is True

    def test_underscore_allowed(self):
        assert is_valid_session_id("session_abc_123") is True

    def test_mixed_chars(self):
        assert is_valid_session_id("A1b-C2d_E3f") is True

    def test_uuid_like(self):
        # UUIDs contain hyphens – common session ID format
        assert is_valid_session_id("550e8400-e29b-41d4-a716-446655440000") is True

    # --- invalid inputs ---
    def test_empty_string(self):
        assert is_valid_session_id("") is False

    def test_dot(self):
        assert is_valid_session_id("session.id") is False

    def test_slash(self):
        assert is_valid_session_id("session/id") is False

    def test_backslash(self):
        assert is_valid_session_id("session\\id") is False

    def test_dotdot_traversal(self):
        assert is_valid_session_id("../../etc/passwd") is False

    def test_space(self):
        assert is_valid_session_id("my session") is False

    def test_null_byte(self):
        assert is_valid_session_id("session\x00id") is False

    def test_percent_encoded(self):
        assert is_valid_session_id("session%2Fid") is False


# =============================================================================
# is_contained_in
# =============================================================================

class TestIsContainedIn:
    """Verify containment / directory-traversal detection."""

    def test_direct_child_contained(self, tmp_path):
        child = tmp_path / "child"
        child.mkdir()
        assert is_contained_in(child, tmp_path) is True

    def test_nested_child_contained(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        assert is_contained_in(nested, tmp_path) is True

    def test_root_equals_root(self, tmp_path):
        # A path is contained in itself
        assert is_contained_in(tmp_path, tmp_path) is True

    def test_parent_not_contained(self, tmp_path):
        assert is_contained_in(tmp_path.parent, tmp_path) is False

    def test_sibling_not_contained(self, tmp_path):
        sibling = tmp_path.parent / "sibling"
        sibling.mkdir(exist_ok=True)
        assert is_contained_in(sibling, tmp_path) is False

    def test_dotdot_traversal_rejected(self, tmp_path):
        # Constructing a path that escapes root via ".."
        escape = tmp_path / ".." / "other"
        assert is_contained_in(escape, tmp_path) is False

    def test_symlink_escape_rejected(self, tmp_path):
        # Create a symlink inside root pointing outside it
        outside = tmp_path.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "escape_link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this platform")
        assert is_contained_in(link, tmp_path) is False

    def test_non_existent_child_still_checked(self, tmp_path):
        # Path doesn't have to exist; containment is purely lexical after resolve
        phantom = tmp_path / "non_existent_subdir"
        assert is_contained_in(phantom, tmp_path) is True
