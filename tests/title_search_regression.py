"""Offline title discovery and UI integration tests; no network or downloads."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelyra.services.title_search import _pagination_items, parse_madara, parse_mangadex, search_titles, safe_url


class TitleSearchTests(unittest.TestCase):
    def setUp(self):
        # UI unit tests must never scan the user's production library/database.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        guard = patch('panelyra.automation.manga_backend.ROOT', Path(temp.name))
        guard.start()
        self.addCleanup(guard.stop)
        for name, value in (
            ("panelyra.services.chapter_counts.get_chapter_count", {"chapter_count": None, "chapter_count_kind": "unknown"}),
            ("panelyra.services.chapter_availability.get_catalog", {"chapters": {}, "complete": False}),
        ):
            guard = patch(name, return_value=value)
            guard.start()
            self.addCleanup(guard.stop)

    def test_pagination_window(self):
        self.assertEqual(_pagination_items(1, 3), [1, 2, 3])
        self.assertEqual(_pagination_items(1, 12), [1, 2, 3, 4, 5, None, 12])
        self.assertEqual(_pagination_items(6, 12), [1, None, 5, 6, 7, None, 12])
        self.assertEqual(_pagination_items(12, 12), [1, None, 8, 9, 10, 11, 12])

    def test_results_use_previous_and_next_buttons(self):
        from streamlit.testing.v1 import AppTest
        matches = [
            {"title": f"Test title {index:02d}", "url": f"https://example.com/manga/{index}", "source": "Test", "cover": ""}
            for index in range(13)
        ]
        with patch("panelyra.services.title_search.search_titles", return_value=(matches, {})):
            app = AppTest.from_string(
                "from panelyra.services.title_search import render_title_search\nrender_title_search()"
            ).run()
            app.text_input(key="manga_search_query").set_value("test")
            next(button for button in app.button if button.label == "Search").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(any(widget.label == "Results page" for widget in app.selectbox))
            self.assertTrue(any(button.label == "›" for button in app.button))
            app.button(key="manga_page_next_bottom").click().run()
            self.assertEqual(app.session_state["manga_results_page"], 2)
            self.assertTrue(any(button.label == "‹" and not button.disabled for button in app.button))

    def test_series_only_and_cover(self):
        html = '''<a href="/comics/lackey/"><img src="/cover.jpg" alt="Devil's Lackey">Color 8 Ongoing</a>
        <a href="/comics/lackey/">Devil's Lackey</a>
        <a href="/comics/lackey/chapter-195">Lackey chapter</a>
        <a href="https://unrelated.test/comics/lackey/">Lackey</a>'''
        results = parse_madara(html, "https://example.com", "Test", "lackey")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Devil's Lackey")
        self.assertEqual(results[0]["cover"], "https://example.com/cover.jpg")

    def test_challenges_are_not_empty_results(self):
        for message in ("Access Denied", "Verify you are human", "Log in to continue"):
            with self.subTest(message=message), self.assertRaises(ValueError):
                parse_madara(message, "https://example.com", "Test", "lackey")

    def test_catalog(self):
        identity = "12345678-1234-1234-1234-123456789abc"
        results = parse_mangadex({"data": [{"id": identity, "attributes": {"title": {"en": "Lackey"}}, "relationships": [{"type": "cover_art", "attributes": {"fileName": "cover.jpg"}}]}]})
        self.assertEqual(results[0]["url"], f"https://mangadex.org/title/{identity}")
        self.assertTrue(results[0]["cover"].endswith(".256.jpg"))

    def test_validation(self):
        self.assertEqual(safe_url("javascript:alert(1)", "https://example.com"), "")
        for query, sources in (("a", ["Thunder"]), ("lackey", [])):
            with self.assertRaises(ValueError):
                search_titles(query, sources)

    def test_partial_failure(self):
        with patch("panelyra.services.title_search.search_source", side_effect=lambda name, query: ([{"title": "Lackey"}], "") if name == "Thunder" else ([], "Unavailable")):
            results, notices = search_titles("lackey", ["Thunder", "Kayn"])
        self.assertEqual(len(results), 1)
        self.assertEqual(notices, {"Kayn": "Unavailable"})

    def test_ui_selects_title_without_starting_download(self):
        from streamlit.testing.v1 import AppTest
        match = {"title": "Lackey", "url": "https://en-thunderscans.com/comics/lackey/", "source": "Thunder", "cover": ""}
        # Selecting/queueing UI does not execute the external binary. Use an
        # existing fixture path so this regression also runs in offline builds.
        with patch("panelyra.automation.manga_backend.BINARY", Path(sys.executable)), patch("panelyra.services.title_search.search_titles", return_value=([match], {})), patch("panelyra.services.manga_backend_ui._start_download") as launch:
            app = AppTest.from_string("from panelyra.services.manga_backend_ui import render_manga_backend\nrender_manga_backend()").run()
            app.text_input(key="manga_search_query").set_value("lackey")
            next(b for b in app.button if b.label == "Search").click().run()
            self.assertFalse(app.exception)
            app.button(key="manga_choose_0").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["manga_selected_series"]["url"], match["url"])
            launch.assert_not_called()
            self.assertFalse(app.button(key="manga_download").disabled)
            app.text_input(key="manga_chapters").set_value("1-3")
            app.button(key="manga_download").click().run()
            launch.assert_called_once_with(match["url"], "1-3", "Lackey")
            app.button(key="manga_change_title").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any(b.key == "manga_choose_0" for b in app.button))


if __name__ == "__main__":
    unittest.main()
