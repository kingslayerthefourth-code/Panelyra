import sys, tempfile, json, inspect
from pathlib import Path
from unittest.mock import patch, Mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from streamlit.testing.v1 import AppTest
from panelyra.services.manga_backend_ui import chapter_evidence,_start_download
from panelyra.services.title_search import parse_madara,parse_mangadex

def ok(app,label):
 assert not app.exception,(label,[e.message for e in app.exception])
 print(label,'passed',flush=True)
# Search state combinations: provider details stay internal in the compact UI.
match={'title':'Fixture','source':'Thunder','url':'https://en-thunderscans.com/comics/fixture/','cover':''}
for label,results,notices in [('success',[match],{}),('empty',[],{'Thunder':'No matching titles found.'}),('partial',[match],{'Kayn':'Unavailable'}),('failed',[],{'Thunder':'Unavailable','Kayn':'Unavailable'})]:
 with patch('panelyra.services.title_search.search_titles',return_value=(results,notices)), patch('panelyra.services.chapter_counts.get_chapter_count',return_value={'chapter_count':None}):
  app=AppTest.from_string('from panelyra.services.title_search import render_title_search\nrender_title_search()').run()
  app.text_input(key='manga_search_query').set_value('Fixture')
  next(b for b in app.button if b.label=='Search').click().run();ok(app,'Search '+label)
  assert not app.warning
  assert app.session_state['title_search_result'][1:]==(results,notices)
# Download partial completion and active guard; never launch the external downloader.
with tempfile.TemporaryDirectory() as directory:
 p=Path(directory);(p/'1.cbz').write_bytes(b'test');(p/'3.cbz').write_bytes(b'test')
 assert chapter_evidence(p,{'chapters':'1-3'})['missing']==['2']
 assert chapter_evidence(p,{'chapters':'1.5-3.5'})['requested'] is None
 assert chapter_evidence(p,{'chapters':'1,3'})['missing']==[]
 (p/'request.json').write_text(json.dumps({'chapters':'1-3'}))
 process=Mock();process.poll.return_value=0
 with patch('panelyra.services.manga_backend_ui.subprocess.Popen') as spawn:
  app=AppTest.from_string('from panelyra.services.manga_backend_ui import _render_status\n_render_status()')
  app.session_state['manga_process']=process;app.session_state['manga_staging']=directory
  app.run();ok(app,'Partial download receipt');assert any('not all' in w.value for w in app.warning);spawn.assert_not_called()
 process.poll.return_value=None
 with patch('panelyra.services.manga_backend_ui.os.killpg') as stop:
  app.run();app.button(key='manga_stop').click().run();ok(app,'Stop download');stop.assert_called_once()
print('Download evidence passed')
# Library fixture renderer: local temporary files and a fake reader URL. No servers or writes to real books.
code='''import streamlit as st
from pathlib import Path
import html,json,sys,subprocess
from panelyra.screens.library import render
from panelyra.services.reader_progress import ReaderProgressStore
from types import SimpleNamespace
root=Path(st.session_state.fixture_root)
chapter={"images":[root/"page.png"] if st.session_state.images else [],"chapter_dir":root,"title":"Chapter 1","healthy":st.session_state.images,"verified":True}
catalog={"fixture":{"title":"Fixture Book","series_key":"fixture","editions":{name:{"1":chapter} for name in st.session_state.editions}}} if st.session_state.has_books else {}
for key,value in {"bot_runtimes":{},"reader_series":"fixture","reader_edition":"Original","reader_chapter":"missing","lyra_conversation_token":"","show_lyra_in_immersive_reader":False}.items():
 if key not in st.session_state:st.session_state[key]=value
render(APP_DIR=root,PANELYRA_LOG_DIR=root,Path=Path,READER_PROGRESS_FILE=root/"progress.json",ReaderProgressStore=ReaderProgressStore,_chapter_integrity=lambda c:(True,"Ready"),_chapter_number=lambda k:float(k),_reader_cover_data=lambda e:"",html=html,is_process_running=lambda p:False,json=json,local_reader_service=lambda:SimpleNamespace(base_url="http://127.0.0.1:9999",reader_token="test",series_url=lambda series:f"http://127.0.0.1:9999/series?series={series}",reader_url=lambda edition,series,chapter,**kw:f"http://127.0.0.1:9999/{edition}/{series}/{chapter}"),reader_catalog=lambda:catalog,render_cleaner_review=lambda c:st.info("Trim review"),render_page_header=lambda title,subtitle:st.header(title),set_page=lambda p:st.session_state.update(page=p),st=st,start_clean_review=lambda *a:None,subprocess=subprocess,sys=sys)
'''
from PIL import Image
from panelyra.services.reader_progress import ReaderProgressStore
with tempfile.TemporaryDirectory() as directory:
 p=Path(directory);Image.new('RGB',(2,2)).save(p/'page.png')
 store=ReaderProgressStore(p/'progress.json')
 store.update({'series':'fixture','chapter':'deleted','edition':'Original','percent':70,'updated_at':100})
 for label,editions,images,has_books in [('empty',['Original'],True,False),('clean-only',['Clean'],True,True),('missing-images',['Original'],False,True),('both-editions',['Original','Clean'],True,True)]:
  app=AppTest.from_string(code)
  for key,val in {'fixture_root':directory,'editions':editions,'images':images,'has_books':has_books}.items():app.session_state[key]=val
  app.run();ok(app,'Library '+label)
  if label=='clean-only':assert not app.radio and not app.selectbox
  if label=='missing-images':assert any('need review' in m.value for m in app.markdown)
  if label=='both-editions':
   assert not app.selectbox
   app.query_params['manage']='1';app.run();ok(app,'Secondary file management')
   app.radio(key='reader_edition').set_value('Clean').run();ok(app,'Edition switch')
   next(b for b in app.button if b.label=='Trim page edges').click().run();ok(app,'Contextual trim')
   assert any(i.value=='Trim review' for i in app.info)
   app.text_input(key='library_search').set_value('nonexistent').run();ok(app,'Library no search matches')
 store.update({'series':'fixture','chapter':'1','edition':'Original','percent':30,'updated_at':90})
 app=AppTest.from_string(code)
 for key,val in {'fixture_root':directory,'editions':['Original'],'images':True,'has_books':True}.items():app.session_state[key]=val
 app.run();ok(app,'Stale progress ignored for resume')
 links=app.get('link_button');assert any(x.proto.label=='Continue reading' and x.proto.url.endswith('/Original/fixture/1') for x in links)
print('All fixture flows passed')
