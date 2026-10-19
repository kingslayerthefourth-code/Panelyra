import sys,tempfile
from pathlib import Path
from unittest.mock import patch,Mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from streamlit.testing.v1 import AppTest
count_guard=patch('panelyra.services.chapter_counts.get_chapter_count',return_value={'chapter_count':None,'chapter_count_kind':'unknown'})
catalog_guard=patch('panelyra.services.chapter_availability.get_catalog',return_value={'chapters':{},'complete':False})
count_guard.start();catalog_guard.start()
match={'title':'Test Series','url':'https://example.com/series/test','source':'Test','cover':'https://example.com/media/test-cover.jpg'}
with tempfile.TemporaryDirectory() as tmp, patch('panelyra.automation.manga_backend.ROOT',Path(tmp)), patch('panelyra.services.title_search.search_titles',return_value=([match],{})), patch('panelyra.automation.download_queue.ensure_worker') as spawn:
 app=AppTest.from_string('from panelyra.services.manga_backend_ui import render_manga_backend\nrender_manga_backend()').run()
 assert not app.exception,app.exception
 app.text_input(key='manga_search_query').set_value('Test Series')
 next(b for b in app.button if b.label=='Search').click().run()
 assert not app.exception,app.exception
 app.button(key='manga_choose_0').click().run()
 assert not app.exception,app.exception
 assert app.session_state['manga_selected_series']['url']==match['url']
 assert app.session_state['manga_selected_series']['cover']==match['cover']
 assert not spawn.called
 app.button(key='manga_download').click().run()
 assert not spawn.called
 assert app.error
 app.text_input(key='manga_chapters').set_value('1-3')
 process=Mock();process.poll.return_value=None;spawn.return_value=process
 app.button(key='manga_download').click().run()
 assert not app.exception,app.exception
 assert spawn.call_count==1
 assert any('added to Queue' in message.value for message in app.success)
 assert spawn.call_args.args[0]==Path(tmp)
 assert not app.button(key='manga_download').disabled
 assert any(t.label=='Queue' for t in app.tabs)
 from panelyra.automation.download_queue import read_queue
 jobs=read_queue(Path(tmp))['jobs']
 assert len(jobs)==1
 assert jobs[0]['cover']==match['cover']
 request=Path(tmp)/'data'/'manga_backend'/('run-queue-'+jobs[0]['id'])/'request.json'
 assert __import__('json').loads(request.read_text())['cover']==match['cover']
 app.button(key='manga_download').click().run()
 assert app.error and len(read_queue(Path(tmp))['jobs'])==1
 fresh=AppTest.from_string('from panelyra.services.manga_backend_ui import render_manga_backend\nrender_manga_backend()').run()
 assert not fresh.exception
 assert any(b.label=='Cancel job' for b in fresh.button)
 assert spawn.call_count==1
 print('Search → choose → chapters → download; no premature launch; validation; persistent queue; duplicate guard; no automatic launch on reopen: passed')
with tempfile.TemporaryDirectory() as tmp, patch('panelyra.automation.manga_backend.ROOT',Path(tmp)),patch('panelyra.automation.download_queue.ensure_worker') as spawn:
 app=AppTest.from_string('from panelyra.services.manga_backend_ui import render_manga_backend\nrender_manga_backend()').run()
 app.radio(key='manga_source_mode').set_value('Paste link').run()
 app.text_input(key='manga_series_url').set_value('not a url')
 app.text_input(key='manga_chapters').set_value('1')
 app.button(key='manga_download').click().run()
 assert not app.exception,app.exception
 assert app.error and not spawn.called
 print('Paste-link validation: passed')
preference_guard=patch('panelyra.services.preferences.save_preferences')
preference_guard.start()
app=AppTest.from_file(str(ROOT/'app.py'),default_timeout=45).run()
assert not app.exception,app.exception
app.button(key='nav_Downloads').click().run()
assert not app.exception,app.exception
assert any(t.label=='Find & download' for t in app.tabs)
app.button(key='nav_Capture').click().run()
assert not app.exception,app.exception
assert not any(t.label=='Find & download' for t in app.tabs)
assert any(t.label=='Starting URL' for t in app.text_input)
print('Full app: Downloads navigation and Capture separation passed')

preference_guard.stop()
count_guard.stop();catalog_guard.stop()
