import os
import tempfile
import time
from pathlib import Path
from unittest import TestCase

from docker.paste_cleanup import remove_expired_pastes


class PasteCleanupTests(TestCase):
    def test_cleanup_only_removes_expired_paste_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            old_html = root / "old.html"
            old_text = root / "old.txt"
            recent_text = root / "recent.txt"
            old_style = root / "style.css"
            nested_paste = root / "nested" / "old.html"

            for path in (old_html, old_text, recent_text, old_style, nested_paste):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("content", encoding="utf-8")

            now = time.time()
            old_timestamp = now - 366 * 24 * 60 * 60
            for path in (old_html, old_text, old_style, nested_paste):
                os.utime(path, (old_timestamp, old_timestamp))

            removed = remove_expired_pastes(root, now - 365 * 24 * 60 * 60)

            self.assertEqual(removed, 2)
            self.assertFalse(old_html.exists())
            self.assertFalse(old_text.exists())
            self.assertTrue(recent_text.exists())
            self.assertTrue(old_style.exists())
            self.assertTrue(nested_paste.exists())
