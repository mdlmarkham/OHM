"""Tests for ohm.net_safety — validate_local_path and safe_fetch_pinned."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from ohm.net_safety import validate_local_path
from ohm.exceptions import ValidationError


class TestValidateLocalPath:
    """Test validate_local_path with and without root."""

    # ── With root configured ──

    def test_valid_path_within_root(self, tmp_path):
        """A path inside root is accepted."""
        safe = tmp_path / "file.txt"
        safe.write_text("hello")
        result = validate_local_path("file.txt", root=str(tmp_path))
        assert "file.txt" in result

    def test_nested_path_within_root(self, tmp_path):
        """A nested path inside root is accepted."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "doc.txt").write_text("content")
        result = validate_local_path("sub/doc.txt", root=str(tmp_path))
        assert "doc.txt" in result

    def test_absolute_path_within_root(self, tmp_path):
        """An absolute path inside root is accepted when root is set."""
        f = tmp_path / "file.txt"
        f.write_text("content")
        result = validate_local_path(str(f), root=str(tmp_path))
        assert "file.txt" in result

    def test_path_escape_rejected(self, tmp_path):
        """A path that escapes root is rejected."""
        with pytest.raises(ValidationError, match="escapes ingestion root"):
            validate_local_path("../../../etc/passwd", root=str(tmp_path))

    def test_absolute_path_outside_root_rejected(self, tmp_path):
        """An absolute path outside root is rejected."""
        with pytest.raises(ValidationError, match="escapes ingestion root"):
            validate_local_path("/etc/passwd", root=str(tmp_path))

    # ── Without root (unconfigured) ──

    def test_relative_path_allowed_without_root(self):
        """A simple relative path without traversal is allowed without root."""
        result = validate_local_path("file.txt")
        assert "file.txt" in result

    def test_traversal_rejected_without_root(self):
        """Path traversal is rejected even without root."""
        with pytest.raises(ValidationError, match="path traversal"):
            validate_local_path("../../../etc/passwd")

    def test_absolute_path_rejected_without_root(self):
        """Absolute paths are rejected without a configured root (security fix)."""
        import platform

        if platform.system() == "Windows":
            abs_path = "C:\\Windows\\System32\\config\\SAM"
        else:
            abs_path = "/etc/passwd"
        with pytest.raises(ValidationError, match="absolute path"):
            validate_local_path(abs_path)

    def test_absolute_path_rejected_without_root_windows(self):
        """Windows-style absolute paths are also rejected."""
        with pytest.raises(ValidationError, match="absolute path"):
            validate_local_path("C:/Users/secret/key.pem")

    def test_empty_path_rejected(self):
        """Empty path is rejected."""
        with pytest.raises(ValidationError, match="non-empty"):
            validate_local_path("")

    # ── Symlink rejection ──

    def test_symlink_rejected_without_root(self, tmp_path):
        """Symlinks are rejected even without root."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("Cannot create symlinks on this platform")
        with pytest.raises(ValidationError, match="symlink"):
            validate_local_path(str(link))


class TestSafeFetchPinned:
    """Test safe_fetch_pinned URL validation (without hitting the network)."""

    def test_non_http_scheme_rejected(self):
        """Non-http(s) schemes are rejected."""
        from ohm.net_safety import validate_fetch_url

        with pytest.raises(ValidationError, match="scheme"):
            validate_fetch_url("file:///etc/passwd")

    def test_ftp_scheme_rejected(self):
        from ohm.net_safety import validate_fetch_url

        with pytest.raises(ValidationError, match="scheme"):
            validate_fetch_url("ftp://example.com/file")

    def test_no_scheme_rejected(self):
        from ohm.net_safety import validate_fetch_url

        with pytest.raises(ValidationError, match="scheme"):
            validate_fetch_url("example.com/file")

    def test_valid_http_url_accepted(self):
        from ohm.net_safety import validate_fetch_url

        result = validate_fetch_url("http://example.com/file")
        assert "example.com" in result

    def test_valid_https_url_accepted(self):
        from ohm.net_safety import validate_fetch_url

        result = validate_fetch_url("https://example.com/file")
        assert "example.com" in result
