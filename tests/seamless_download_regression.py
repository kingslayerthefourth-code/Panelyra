"""Concurrency and navigation checks for incremental title discovery; no network."""
import sys
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelyra.services.title_discovery import TitleSearchTask
from panelyra.services.title_search import parse_madara, safe_cover_url, search_titles

ROW = {"title": "Investment Playbook", "url": "https://example.com/manga/book", "source": "Thunder", "cover": ""}


class SeamlessDownloads(unittest.TestCase):
    def test_first_match_is_available_before_slow_sources_finish(self):
        first = Event()
        finish = Event()

        def search(query, sources, **options):
            options["on_results"]([ROW], {}, 1, 2)
            first.set()
            finish.wait(2)
            return [ROW, {**ROW, "url": ROW["url"] + "-late", "source": "Kayn"}], {}

        task = TitleSearchTask("investment", ["Thunder", "Kayn"], search)
        try:
            self.assertTrue(first.wait(1))
            snapshot = task.snapshot()
            self.assertEqual(snapshot.results, [ROW])
            self.assertFalse(snapshot.finished)
            snapshot.results[0]["title"] = "Modified copy"
            self.assertEqual(task.snapshot().results[0]["title"], ROW["title"])
        finally:
            finish.set()
        self.assertTrue(task.wait(1))
        self.assertEqual(len(task.snapshot().results), 2)
        self.assertTrue(task.matches("INVESTMENT", ["Kayn", "Thunder"]))
        self.assertFalse(task.matches("different title", ["Kayn", "Thunder"]))

    def test_visible_chapter_counts_are_requested_once(self):
        counted = Event()
        task = TitleSearchTask("investment", ["Thunder"], lambda *args, **kwargs: ([ROW], {}))
        self.assertTrue(task.wait(1))

        def count(row):
            counted.set()
            return {"chapter_count": 21, "chapter_count_kind": "reported"}

        with patch("panelyra.services.chapter_counts.get_chapter_count", side_effect=count) as lookup:
            task.request_details([ROW, ROW])
            self.assertTrue(counted.wait(1))
            task.request_details([ROW])
            self.assertEqual(lookup.call_count, 1)
        self.assertNotIn("chapter_count", ROW)

    def test_background_search_retains_late_source_matches(self):
        observed = []
        with patch("panelyra.services.title_search.search_source", side_effect=lambda name, query: ([{**ROW, "source": name}], "")):
            rows, notices = search_titles("investment", ["Thunder", "Kayn"], background=True,
                                          include_counts=False, on_results=lambda rows, notes, done, total: observed.append((len(rows), done)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(observed[-1], (2, 2))
        self.assertFalse(notices)

    def test_an_empty_search_can_be_retried_after_reconnecting(self):
        task = TitleSearchTask("investment", ["Thunder"], lambda *args, **kwargs: ([], {"Thunder": "Offline"}))
        self.assertTrue(task.wait(1))
        self.assertFalse(task.matches("investment", ["Thunder"]))

    def test_cover_cdn_and_lazy_srcset_are_preserved(self):
        html = '<a href="/manga/book"><img src="data:image/gif;base64,empty" data-srcset="https://cdn.example.org/book.webp 300w" alt="Investment Playbook"></a>'
        rows = parse_madara(html, "https://example.com", "Test", "investment")
        self.assertEqual(rows[0]["cover"], "https://cdn.example.org/book.webp")
        for bad in ("file:///tmp/book.png", "http://example.com/book.png", "https://127.0.0.1/book", "https://localhost/book", "javascript:alert(1)"):
            self.assertEqual(safe_cover_url(bad, "https://example.com"), "")

    def test_ui_keeps_search_visible_and_does_not_leak_source_failures(self):
        from streamlit.testing.v1 import AppTest
        with patch("panelyra.services.chapter_counts.get_chapter_count", return_value={"chapter_count": None}), patch(
            "panelyra.services.title_search.search_titles", return_value=([ROW], {"Offline source": "Internal error"})
        ):
            app = AppTest.from_string("from panelyra.services.title_search import render_title_search\nrender_title_search()").run()
            app.text_input(key="manga_search_query").set_value("investment")
            next(button for button in app.button if button.label == "Search").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len([button for button in app.button if button.label == "Choose title"]), 1)
            self.assertEqual(len([field for field in app.text_input if field.key == "manga_search_query"]), 1)
            self.assertFalse(app.warning)
            self.assertEqual([panel.label for panel in app.expander], ["Search sources"])

    def test_change_title_preserves_search_and_clears_unrelated_chapter_numbers(self):
        from streamlit.testing.v1 import AppTest
        rows = [ROW, {**ROW, "title": "Investment Sequel", "url": ROW["url"] + "-2"}]
        with patch("panelyra.services.chapter_counts.get_chapter_count", return_value={"chapter_count": None}), patch(
            "panelyra.services.chapter_availability.get_catalog", return_value={"chapters": {}, "complete": False}
        ), patch("panelyra.services.title_search.search_titles", return_value=(rows, {})):
            app = AppTest.from_string("from panelyra.services.manga_backend_ui import render_manga_backend\nrender_manga_backend()").run()
            app.text_input(key="manga_search_query").set_value("investment")
            next(button for button in app.button if button.label == "Search").click().run()
            app.button(key="manga_choose_0").click().run()
            app.text_input(key="manga_chapters").set_value("12-14").run()
            app.button(key="manga_change_title").click().run()
            self.assertEqual(app.text_input(key="manga_search_query").value, "investment")
            app.button(key="manga_choose_1").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.text_input(key="manga_chapters").value, "")


if __name__ == "__main__":
    unittest.main()
