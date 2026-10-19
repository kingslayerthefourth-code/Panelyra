"""Offline tests; never launch the external downloader or contact a source."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from panelyra.automation.manga_backend import build_command, archive_pages, import_reviewed_archive


class BackendTests(unittest.TestCase):
    def test_consent_required(self):
        with self.assertRaises(ValueError):
            build_command("https://example.com/series/book", "1", Path("/tmp"), browser_consent=False)

    def test_command_is_bounded_and_preserves_bytes(self):
        command = build_command("https://example.com/series/book", "1,2,10-12", Path("/tmp"), browser_consent=True)
        self.assertIn("none", command)
        self.assertIn("--concurrency-pages", command)
        self.assertEqual(command[2], "1,2,10-12")

    def test_invalid_ranges(self):
        for value in ("", "all", "1-10001", "2-1", ";rm", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_command("https://example.com/book", value, Path("/tmp"), browser_consent=True)

    def test_invalid_urls(self):
        for url in ("file:///tmp/test", "https://u:p@example.com/book", "not a url"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                build_command(url, "1", Path("/tmp"), browser_consent=True)

    def make_archive(self, path, names=("000.png", "001.png")):
        data = io.BytesIO()
        Image.new("RGB", (12, 12), "blue").save(data, format="PNG")
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                archive.writestr(name, data.getvalue())
        return data.getvalue()

    def test_import_preserves_bytes_and_records_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "15.cbz"
            original = self.make_archive(archive)
            history = root / "history.json"
            with patch("panelyra.automation.quiet_updates.updater_lock"):
                dest = import_reviewed_archive(archive, "https://example.com/book", "https://example.com/book/chapter-15", "book", output_dir=root / "manhwa", history_file=history)
                self.assertEqual((dest / "images/000.png").read_bytes(), original)
                self.assertEqual(len(json.loads(history.read_text())), 1)
                self.assertEqual(json.loads((dest / "metadata.json").read_text())["saved_image_count"], 2)
                self.assertIsNone(zipfile.ZipFile(dest / "chapter-pages.cbz").testzip())
                with self.assertRaises(ValueError):
                    import_reviewed_archive(archive, "https://example.com/book", "https://example.com/book/chapter-15", "book", output_dir=root / "manhwa", history_file=history)

    def test_missing_or_unsafe_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "1.cbz"
            for names in (("000.png", "002.png"), ("../000.png",), (), ("000.png", "000.jpg")):
                self.make_archive(archive, names)
                with self.assertRaises(ValueError):
                    archive_pages(archive)

    def test_wrong_identity_does_not_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "15.cbz"
            self.make_archive(archive)
            with self.assertRaises(ValueError):
                import_reviewed_archive(archive, "https://example.com/book", "https://example.com/book/chapter-16", "book", output_dir=root / "manhwa")
            self.assertFalse((root / "manhwa").exists())


if __name__ == "__main__":
    unittest.main()
