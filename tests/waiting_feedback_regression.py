"""Waiting feedback must be visible and measured, without starting real jobs."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelyra.services.waiting_feedback import queue_feedback, duration, note


class WaitingFeedbackTests(unittest.TestCase):
    def test_eta_needs_two_real_downloads_and_a_running_engine(self):
        job = {"status": "running", "chapters": {
            "1": {"status": "downloaded", "duration_seconds": 10},
            "2": {"status": "already_saved"}, "3": {"status": "pending"}}}
        self.assertIsNone(queue_feedback(job, running=True)["eta"])
        job["chapters"]["2"] = {"status": "downloaded", "duration_seconds": 20}
        self.assertEqual(queue_feedback(job, running=True)["eta"], 15)
        self.assertIsNone(queue_feedback(job, paused=True, running=True)["eta"])
        self.assertIsNone(queue_feedback(job, running=False)["eta"])
        job['chapters']['3'] = {'status': 'downloading', 'started_at': 1}
        self.assertIsNone(queue_feedback(job, running=True, now=100)["eta"])

    def test_current_phase_and_actual_completed_fraction(self):
        job = {"status": "running", "chapters": {"1": {"status": "downloaded"},
            "2": {"status": "downloading", "stage": "Checking archive", "started_at": 80}}}
        result = queue_feedback(job, running=True, now=100)
        self.assertEqual(result["elapsed"], 20)
        self.assertEqual(result["fraction"], .5)
        self.assertIn("Checking archive", result["label"])
        self.assertIn("Paused", queue_feedback(job, paused=True)["label"])
        self.assertEqual(duration(-8), "0s")
        self.assertNotEqual(note(0), note(9))

    def test_already_queued_is_not_presented_as_ready(self):
        job = {'status': 'completed', 'chapters': {'1': {'status': 'already_queued'}}}
        result = queue_feedback(job)
        self.assertEqual(result['ready'], 0)
        self.assertEqual(result['elsewhere'], 1)
        self.assertNotIn('Ready', result['label'])

    def test_download_click_reuses_snapshot_without_network_or_navigation(self):
        import streamlit as st
        from panelyra.services.manga_backend_ui import _start_download
        from types import SimpleNamespace
        st.session_state["manga_selected_series"] = {"url": "https://fixture.test/book", "source": "Fixture"}
        st.session_state["manga_catalog_task"] = SimpleNamespace(url="https://fixture.test/book", snapshot=lambda: {"chapters": {"1": {"status": "public_link"}}})
        with patch("panelyra.automation.manga_backend.build_command"), \
             patch("panelyra.automation.download_queue.enqueue", return_value={"id": "receipt"}) as enqueue, \
             patch("panelyra.automation.download_queue.ensure_worker"), \
             patch("panelyra.automation.download_queue.job_dir", return_value=Path("/tmp/receipt")), \
             patch("panelyra.services.chapter_availability.get_catalog", side_effect=AssertionError("network on click")):
            _start_download("https://fixture.test/book", "1", "Book")
            self.assertIn("1", enqueue.call_args.args[5]["chapters"])
            self.assertEqual(st.session_state["manga_latest_job"], "receipt")
            self.assertNotEqual(st.session_state.get("manga_download_tab"), "Queue")
        st.session_state.clear()

    def test_discovery_shows_results_and_progress_together(self):
        from streamlit.testing.v1 import AppTest
        from types import SimpleNamespace
        from panelyra.services.title_discovery import SearchSnapshot
        task = SimpleNamespace(query="Book", created_at=time.monotonic(),
            snapshot=lambda: SearchSnapshot([{"title": "Book", "url": "https://fixture.test/book", "source": "Test", "cover": "", "chapter_count": 5}], {}, 1, 3, False, False, ""),
            request_details=lambda rows: None)
        app = AppTest.from_string("from panelyra.services.title_search import render_title_search\nrender_title_search()")
        app.session_state["manga_title_task"] = task
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any("1 titles found" in p.proto.text for p in app.get("progress")))
        self.assertTrue(any("Book" in str(m.value) for m in app.markdown))

    def test_queued_receipt_explains_global_pause_without_launching_work(self):
        from streamlit.testing.v1 import AppTest
        job = {"id": "receipt", "title": "Book", "status": "queued", "chapters": {"1": {"status": "pending"}}}
        with patch("panelyra.automation.download_queue.read_queue", return_value={"paused": False, "jobs": [job]}), \
             patch("panelyra.automation.download_queue.worker_running", return_value=False), \
             patch("panelyra.engine.policy", return_value=True):
            app = AppTest.from_string("import streamlit as st\nst.session_state.manga_latest_job = 'receipt'\nfrom panelyra.services.manga_backend_ui import _render_current_download\n_render_current_download()").run()
            self.assertFalse(app.exception)
            self.assertTrue(any("Background work is paused" in i.value for i in app.info))
            self.assertEqual(app.get("progress")[0].value, 0)


if __name__ == "__main__": unittest.main()
