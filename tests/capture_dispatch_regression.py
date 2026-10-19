"""Failure isolation and Naver reader-banner regressions."""
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from panelyra.automation import capture_dispatch as capture


class CaptureTests(unittest.TestCase):
    def test_age_banner_is_not_a_chapter_failure(self):
        page = 'https://image-comic.pstatic.net/webtoon/851610/1/page.jpg'
        soup = BeautifulSoup('<div id="comic_view_area"><img src="https://image-comic.pstatic.net/static/agerate/age_15_white.jpg"><img src="' + page + '"><img src="' + page + '"><img src="https://image-comic.pstatic.net/webtoon/851610/2/page.jpg"></div>', 'html.parser')
        self.assertEqual(capture.naver_story_images(soup, '851610', 1), [page])

    def test_failure_does_not_stop_next_episode(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'naver-851610').mkdir()
            args = argparse.Namespace(start_url='https://comic.naver.com/webtoon/detail?titleId=851610&no=1', max_chapters=3, output_dir=root)
            seen = []
            def episode(single):
                number = capture.naver_identity(single.start_url)[1]
                seen.append(number)
                if number == 2:
                    raise ValueError('login required')
                return [{'episode': number}]
            with patch.object(capture, '_capture_naver_episode', side_effect=episode), patch.object(capture.time, 'sleep'):
                with self.assertRaises(ValueError):
                    capture.capture_naver(args)
            self.assertEqual(seen, [1, 2, 3])

    def test_transient_failure_retries(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'naver-851610').mkdir()
            args = argparse.Namespace(start_url='https://comic.naver.com/webtoon/detail?titleId=851610&no=1', max_chapters=1, output_dir=root)
            with patch.object(capture, '_capture_naver_episode', side_effect=[TimeoutError('temporary'), [{'episode': 1}]]) as worker, patch.object(capture.time, 'sleep'):
                self.assertEqual(len(capture.capture_naver(args)), 1)
                self.assertEqual(worker.call_count, 2)


if __name__ == '__main__':
    unittest.main()
