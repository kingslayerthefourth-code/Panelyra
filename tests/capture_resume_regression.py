"""Offline Capture state, chunking and validator-bound HTTP resume fixtures."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from automation_runtime import RunRecord, RunStore
from panelyra.services.capture_store import CaptureStore
from panelyra.services.page_transfer import fetch_page
from panelyra.services.worker_runtime import WorkerInterrupted


def png():
    output = io.BytesIO()
    Image.new('RGB', (40, 80), 'red').save(output, 'PNG')
    return output.getvalue()


class Response:
    def __init__(self, status, data=b'', headers=None, fail=False):
        self.status_code, self.data, self.headers, self.fail = status, data, headers or {}, fail
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self):
        if self.status_code >= 400: raise ValueError('HTTP failure')
    def iter_content(self, size):
        yield self.data
        if self.fail: raise TimeoutError('fixture interrupted')


class CaptureResumeTests(unittest.TestCase):
    def test_concurrent_patch_updates_stop_and_json_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = RunStore(RunRecord.create('download', {}, root/'run.log'), root/'runs')
            second = RunStore.load(first.path)
            first.update(progress_current=4)
            second.update(pid=100)
            self.assertEqual(RunStore.load(first.path).record.progress_current, 4)
            first.update(status='stopped')
            second.update(status='interrupted', progress_current=0)
            first.path.unlink()
            restored = RunStore.load(first.path)
            self.assertEqual(restored.record.status, 'stopped')
            self.assertEqual(restored.record.progress_current, 4)

    def test_refuses_unmanaged_resume_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = RunStore(RunRecord.create('download', {}, root/'log'), root/'runs')
            with self.assertRaises(ValueError): run.capture.command(run.record.run_id, ['sh', '-c', 'anything'])

    def test_interrupted_52_chapters_resume_and_browser_recycles(self):
        import manhwa_ocr_downloader as capture
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); output = root/'manhwa/book'; output.mkdir(parents=True)
            run = RunStore(RunRecord.create('download', {}, root/'log'), root/'runs')
            class Driver:
                current_url = 'https://fixture.example/book/chapter-1'
            driver = Driver(); session = SimpleNamespace(driver=driver)
            seen, opens = [], []
            interrupted = [False]
            def open_driver(profile, url, reason):
                opens.append(url); driver.current_url = url; return session
            def load(driver, url, reason): driver.current_url = url; return True
            def process(driver, **options):
                index = options['chapter_index']
                if index == 3 and not interrupted[0]:
                    interrupted[0] = True
                    raise WorkerInterrupted('fixture shutdown')
                seen.append(index)
                return capture.ChapterDownload(index=index, url=driver.current_url, title=f'Chapter {index}',
                    text_file='', image_urls_file='', image_count=1, character_count=0, status='ok')
            with patch.object(capture, 'reporter_from_environment', return_value=run), \
                 patch.object(capture, 'open_capture_driver', side_effect=open_driver), \
                 patch.object(capture.bot, 'close_driver_session'), \
                 patch.object(capture.bot, 'dismiss_page_interruptions'), \
                 patch.object(capture.bot, 'load_url_with_retries', side_effect=load), \
                 patch.object(capture, 'maybe_open_capture_chapter'), \
                 patch.object(capture, 'series_title_from_page', return_value='Book'), \
                 patch.object(capture, 'resolve_series_output_dir', return_value=output), \
                 patch.object(capture, 'chapter_title', side_effect=lambda driver,index: f'Chapter {index}'), \
                 patch.object(capture, 'reader_snapshot'), \
                 patch.object(capture, 'capture_reader_layout', return_value='next-chapter'), \
                 patch.object(capture, 'ocr_current_chapter', side_effect=process), \
                 patch.object(capture, 'resolve_next_chapter_url', side_effect=lambda d,url,title,**kw: capture.next_numbered_chapter_url(url)):
                options = dict(start_url=driver.current_url, max_chapters=52, output_dir=root/'manhwa', save_ocr=False)
                with self.assertRaises(WorkerInterrupted): capture.download_manhwa_ocr(**options)
                self.assertEqual(run.capture.resume(run.record.run_id)['chapter_index'], 3)
                capture.download_manhwa_ocr(**options)
            self.assertEqual(seen, list(range(1, 53)))
            self.assertEqual(opens, [options['start_url'], 'https://fixture.example/book/chapter-3',
                                    'https://fixture.example/book/chapter-23', 'https://fixture.example/book/chapter-43'])
            self.assertEqual(run.record.progress_current, 52)
            self.assertEqual(run.record.status, 'completed')

    def test_partial_bytes_resume_only_with_matching_version(self):
        payload = png(); cut = len(payload)//2
        with tempfile.TemporaryDirectory() as temporary:
            requests = []
            responses = iter([Response(200, payload[:cut], {'ETag':'"one"', 'Content-Length':str(len(payload))}, True),
                              Response(206, payload[cut:], {'ETag':'"one"', 'Content-Range':f'bytes {cut}-{len(payload)-1}/{len(payload)}'}),
                              Response(304)])
            def get(url, **options): requests.append(options['headers']); return next(responses)
            session = SimpleNamespace(get=get)
            with self.assertRaises(TimeoutError): fetch_page(session, 'https://fixture/page', {}, 1, temporary)
            self.assertEqual(fetch_page(session, 'https://fixture/page', {}, 1, temporary), payload)
            self.assertEqual(requests[1]['Range'], f'bytes={cut}-')
            self.assertEqual(fetch_page(session, 'https://fixture/page', {}, 1, temporary), payload)
            self.assertEqual(requests[2]['If-None-Match'], '"one"')

    def test_version_change_restarts_and_size_is_bounded(self):
        payload = png()
        with tempfile.TemporaryDirectory() as temporary:
            responses = iter([Response(200, b'old', {'ETag':'"one"'}, True),
                              Response(200, payload, {'ETag':'"two"', 'Content-Length':str(len(payload))}),
                              Response(200, b'x', {'Content-Length':'50000001'})])
            session = SimpleNamespace(get=lambda *a,**kw: next(responses))
            with self.assertRaises(TimeoutError): fetch_page(session, 'https://fixture/page', {}, 1, temporary)
            self.assertEqual(fetch_page(session, 'https://fixture/page', {}, 1, temporary), payload)
            with self.assertRaises(ValueError): fetch_page(session, 'https://fixture/huge', {}, 1, temporary)


if __name__ == '__main__': unittest.main()
