"""Exercise actual Streamlit widgets with isolated library/state and no network."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from streamlit.testing.v1 import AppTest
from tablet_delivery import DeliveryStore
from tablet_push import load_device
import tablet_ui

class PushUiTest(unittest.TestCase):
    def test_pair_choose_and_queue_selected_chapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            folder=root/'manhwa/book/01';folder.mkdir(parents=True)
            page=root/'page.png';Image.new('RGB',(30,40),'teal').save(page)
            (folder/'metadata.json').write_text(json.dumps({'series_title':'Book'}))
            with zipfile.ZipFile(folder/'chapter-pages.cbz','w') as z:z.write(page,'01.png')
            script=f"from pathlib import Path\nfrom tablet_ui import render_connections\nrender_connections(Path({str(root)!r}))"
            with patch('tablet_ui.push_worker') as worker, patch('tablet_ui.PushClient') as Client:
                Client.return_value.pair.return_value={'address':'192.168.1.20:8590','pin':'a'*64,'token':'b'*64,'device':'Test Tablet'}
                app=AppTest.from_string(script,default_timeout=30).run()
                app.text_input[0].set_value('192.168.1.20:8590')
                app.text_input[1].set_value('ABCD EFGH JKLM')
                next(b for b in app.button if b.label=='Connect to tablet').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(load_device(root)['device'],'Test Tablet')
                self.assertTrue(any('Pairing saved' in x.value for x in app.success))
                app.button(key='push_choose').click().run()
                app.multiselect(key='push_chapters').set_value(['book/1']).run()
                app.button(key='push_send').click().run()
                self.assertFalse(app.exception)
                rows=DeliveryStore(root).list()
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['transport'],'push')
                self.assertEqual(rows[0]['chapters'][0]['chapter'],'1')
                worker.return_value.start.assert_called()
            tablet_ui.library_store.clear()

if __name__=='__main__':unittest.main()
