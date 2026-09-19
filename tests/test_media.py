from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_message.core.media import (
    FILE_MAX_BYTES,
    IMAGE_MAX_BYTES,
    kind_for_path,
    resolve_project_file,
    safe_filename,
    size_limit_for_kind,
)


class MediaHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "plot.png").write_bytes(b"png")
        (self.root / "report.pdf").write_bytes(b"pdf")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_kind_and_limits_follow_extension(self) -> None:
        self.assertEqual(kind_for_path(Path("a.PNG")), "image")
        self.assertEqual(kind_for_path(Path("a.pdf")), "file")
        self.assertEqual(size_limit_for_kind("image"), IMAGE_MAX_BYTES)
        self.assertEqual(size_limit_for_kind("file"), FILE_MAX_BYTES)

    def test_resolve_accepts_existing_in_project_file(self) -> None:
        resolved = resolve_project_file(self.root, "plot.png")
        self.assertEqual(resolved, (self.root / "plot.png").resolve())

    def test_resolve_rejects_escape_absolute_missing_and_oversize(self) -> None:
        self.assertIsNone(resolve_project_file(self.root, "../outside.txt"))
        self.assertIsNone(resolve_project_file(self.root, "/etc/passwd"))
        self.assertIsNone(resolve_project_file(self.root, "missing.txt"))
        with patch("agent_message.core.media.FILE_MAX_BYTES", 1):
            self.assertIsNone(resolve_project_file(self.root, "report.pdf"))

    def test_resolve_rejects_symlink_escape(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.txt"
        outside.write_text("secret")
        try:
            link = self.root / "link.txt"
            link.symlink_to(outside)
            self.assertIsNone(resolve_project_file(self.root, "link.txt"))
        finally:
            outside.unlink()

    def test_safe_filename_strips_directories(self) -> None:
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(safe_filename(" report.pdf "), "report.pdf")
        self.assertIsNone(safe_filename(".."))
        self.assertIsNone(safe_filename(None))



if __name__ == "__main__":
    unittest.main()
