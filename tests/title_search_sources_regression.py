"""Source-specific routing, result identity, redirect safety, and pagination."""
import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from panelyra.services.title_search import COMMUNITY_SOURCES,DEFAULT_SOURCES,search_request,parse_madara,search_source,SOURCES
from panelyra.services.chapter_counts import result_rank
class Response:
 def __init__(self,status=200,html='',headers=None):self.status_code=status;self.html=html;self.headers=headers or {}
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def raise_for_status(self):pass
 def iter_content(self,*a):yield self.html.encode()
class Tests(unittest.TestCase):
 def setUp(self):
  guard=patch('panelyra.services.chapter_counts.get_chapter_count',return_value={'chapter_count':None,'chapter_count_kind':'unknown'})
  guard.start();self.addCleanup(guard.stop)
 def test_bundled_community_source_inventory(self):
  self.assertGreaterEqual(len(COMMUNITY_SOURCES),35)
  self.assertLessEqual(len(COMMUNITY_SOURCES),60)
  self.assertIn('AGR Comics',COMMUNITY_SOURCES)
  self.assertIn('Reaper_Scans',COMMUNITY_SOURCES)
  self.assertIn('Reaper_Scans',DEFAULT_SOURCES)
  self.assertTrue(set(COMMUNITY_SOURCES).issubset(DEFAULT_SOURCES))
  self.assertNotIn('MangaDex',DEFAULT_SOURCES)
 def test_title_relevance_precedes_chapter_count(self):
  rows=[
   {'title':'A Completely Different Adventure','source':'A','chapter_count':900},
   {'title':'The Hero and His Brother','source':'B','chapter_count':500},
   {'title':'Brother Dao','source':'C','chapter_count':12},
   {'title':'Bro','source':'D','chapter_count':1},
  ]
  ranked=sorted(rows,key=lambda row:result_rank(row,'bro'))
  self.assertEqual([row['title'] for row in ranked],['Bro','Brother Dao','The Hero and His Brother','A Completely Different Adventure'])
  self.assertTrue(all(url.startswith('https://') for url in COMMUNITY_SOURCES.values()))
 def test_actual_search_routes(self):
  self.assertEqual(search_request('Kayn','fog')[1]['q'],'fog')
  self.assertEqual(search_request('Asura','solo')[0],'https://asurascans.com/browse')
  self.assertNotIn('post_type',search_request('Thunder','solo')[1])
  self.assertEqual(search_request('WeebCentral','solo')[0],'https://weebcentral.com/search/data')
  self.assertEqual(search_request('Rithar','hero')[1],{'title':'hero'})
 def test_nested_series_links_and_titles(self):
  html='<a href="/series/comic/fog-land?ref=browse"><img alt="Fog Land">MANHWA</a><a href="/series/comic/fog-land">Fog Land</a><a href="/series/comic/fog-land/chapter/1">Fog Land 1</a><a href="https://other.test/series/comic/fog-land">Fog Land</a>'
  rows=parse_madara(html,SOURCES['Kayn'],'Kayn','fog')
  self.assertEqual([(r['title'],r['url']) for r in rows],[('Fog Land',SOURCES['Kayn']+'/series/comic/fog-land')])
 def test_mangapill_and_weebcentral_paths(self):
  for source,path in [('MangaPill','/manga/123/solo-leveling'),('WeebCentral','/series/01J76XYCPSY3C4BNPBRY8JMCBE/Solo-Leveling')]:
   rows=parse_madara(f'<a href="{path}">Solo Leveling</a><a href="{path}/chapter/1">Solo Leveling</a>',SOURCES[source],source,'solo leveling')
   self.assertEqual(len(rows),1)
 def test_exact_query_and_empty_layout(self):
  rows=parse_madara('<a href="/manga/solo">Solo</a><a href="/manga/leveling">Leveling</a><a href="/manga/both">Solo Leveling</a>',SOURCES['Thunder'],'Thunder','solo leveling')
  self.assertEqual(len(rows),1)
  self.assertEqual(parse_madara('<h1>No series found</h1>',SOURCES['Thunder'],'Thunder','zzz'),[])
  for message in ('No matches found. Try a different search...', 'No comics match “happy face” with these filters'):
   self.assertEqual(parse_madara(f'<h2>{message}</h2>',SOURCES['Drake'],'Drake','happy face'),[])
  self.assertEqual(parse_madara('<h1>Search happy face</h1><h2>Not Found</h2>',SOURCES['Madara'],'Madara','happy face'),[])
  with self.assertRaises(ValueError):parse_madara('<h1>Unexpected response</h1>',SOURCES['Thunder'],'Thunder','zzz')
 def test_safe_canonical_redirect(self):
  with patch('panelyra.services.title_search.requests.get',side_effect=[Response(301,headers={'Location':'https://en-thunderscans.com/search/'}),Response(html='<a href="/comics/solo">Solo</a>')]) as get:
   rows,notice=search_source('Thunder','solo');self.assertEqual(len(rows),1);self.assertFalse(notice);self.assertEqual(get.call_count,2)
  with patch('panelyra.services.title_search.requests.get',return_value=Response(301,headers={'Location':'https://unrelated.test/'})) as get:
   rows,notice=search_source('Thunder','solo');self.assertFalse(rows);self.assertIn('another host',notice);self.assertEqual(get.call_count,1)
 def test_challenge_and_rate_limit(self):
  for status,word in [(403,'verification'),(429,'rate-limiting')]:
   with patch('panelyra.services.title_search.requests.get',return_value=Response(status)):
    rows,notice=search_source('Thunder','solo');self.assertFalse(rows);self.assertIn(word,notice)
 def test_pagination_source_filter_and_handoff(self):
  from streamlit.testing.v1 import AppTest
  rows=[{'title':f'Solo {i:02}','url':f'https://en-thunderscans.com/comics/solo-{i}','source':'Thunder','cover':''} for i in range(25)]
  with patch('panelyra.services.title_search.search_titles',return_value=(rows,{})):
   app=AppTest.from_string('from panelyra.services.title_search import render_title_search\nrender_title_search()').run()
   app.text_input(key='manga_search_query').set_value('solo');next(b for b in app.button if b.label=='Search').click().run();self.assertFalse(app.exception)
   self.assertEqual(len([b for b in app.button if b.label=='Choose title']),12)
   self.assertFalse(any(widget.label=='Results page' for widget in app.selectbox))
   app.button(key='manga_page_next_bottom').click().run();self.assertEqual(len([b for b in app.button if b.label=='Choose title']),12)
   app.button(key='manga_choose_12').click().run();self.assertEqual(app.session_state['manga_selected_series']['title'],'Solo 12');self.assertFalse(app.exception)
 def test_failed_sources_do_not_bury_successful_results(self):
  from streamlit.testing.v1 import AppTest
  rows=[{'title':'Investment Playbook','url':'https://en-thunderscans.com/comics/investment','source':'Thunder','cover':''}]
  notices={f'Offline {index}':'Source unavailable.' for index in range(40)}
  with patch('panelyra.services.title_search.search_titles',return_value=(rows,notices)):
   app=AppTest.from_string('from panelyra.services.title_search import render_title_search\nrender_title_search()').run()
   app.text_input(key='manga_search_query').set_value('investment');next(b for b in app.button if b.label=='Search').click().run()
   self.assertFalse(app.exception);self.assertEqual(len([b for b in app.button if b.label=='Choose title']),1)
   self.assertEqual([item.label for item in app.expander],['Search sources'])
   self.assertFalse(any(button.label.startswith('Retry ') for button in app.button))
if __name__=='__main__':unittest.main()
