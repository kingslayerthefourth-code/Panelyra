"""Prevent nested Downloads polling fragments from breaking form submission."""
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class DownloadsFragmentRegression(unittest.TestCase):
    def test_workspace_does_not_wrap_child_pollers_in_another_fragment(self):
        from panelyra.services import manga_backend_ui
        with patch('streamlit.fragment', side_effect=AssertionError('Do not nest polling fragments')), \
             patch('streamlit.container', return_value=nullcontext()) as surface, \
             patch.object(manga_backend_ui, '_render_manga_backend') as render:
            manga_backend_ui.render_manga_backend()
        surface.assert_called_once_with(key='manga_download_workspace')
        render.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
