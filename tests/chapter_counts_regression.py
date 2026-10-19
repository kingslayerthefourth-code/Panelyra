"""Count chapters, not the highest chapter number or recommendation metadata."""
import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from panelyra.services import chapter_counts as c
class Tests(unittest.TestCase):
 def setUp(self):c._CACHE.clear()
 def test_reported_total_ignores_animation_and_recommendations(self):
  html='<h1>Happy Face</h1><div><span>Chapters</span><span><span class="sr-only">78</span><span aria-hidden="true">777888</span></span></div><a href="/series/other"><span>999</span> chapters</a>'
  self.assertEqual(c.parse_chapter_count(html,'https://example.com/series/happy','Kayn'),{'chapter_count':78,'chapter_count_kind':'reported'})
 def test_heading_and_metadata_total(self):
  for html,count in [('<h2>201<!-- --> Chapters</h2>',201),('<div>52 <span>chapters</span></div>',52)]:
   self.assertEqual(c.parse_chapter_count(html,'https://example.com/series/happy','Test')['chapter_count'],count)
 def test_gaps_duplicates_and_other_series(self):
  html='<div id="chapterlist"><a href="/manga/book/chapter-1">Chapter 1</a><a href="/manga/book/chapter-1">Chapter 1</a><a href="/manga/book/chapter-3">Chapter 3</a><a href="/manga/book/chapter-3.5">Chapter 3.5</a><a href="/manga/other/chapter-99">Chapter 99</a></div>'
  self.assertEqual(c.parse_chapter_count(html,'https://example.com/manga/book','Test'),{'chapter_count':3,'chapter_count_kind':'partial'})
 def test_unknown_is_not_zero(self):
  value=c.parse_chapter_count('<h1>Just a moment</h1>','https://example.com/book','Test')
  self.assertIsNone(value['chapter_count']);self.assertEqual(c.chapter_count_label(value),'Chapter count unavailable')
 def test_source_ranking(self):
  rows=[{'title':'Happy Face','source':'Small','chapter_count':52},{'title':'Happy Face','source':'Large','chapter_count':78},{'title':'Happy Face','source':'Unknown'},{'title':'Happy Face sequel','source':'Other','chapter_count':200}]
  self.assertEqual([r['source'] for r in sorted(rows,key=lambda r:c.result_rank(r,'happy face'))],['Large','Small','Unknown','Other'])
 def test_cache_and_http_failure(self):
  row={'url':'https://example.com/manga/happy','source':'Test'}
  with patch.object(c,'_fetch',return_value='<h2>52 Chapters</h2>') as fetch:
   self.assertEqual(c.get_chapter_count(row)['chapter_count'],52);c.get_chapter_count(row);self.assertEqual(fetch.call_count,1)
  c._CACHE.clear()
  with patch.object(c,'_fetch',side_effect=ValueError('unavailable')):self.assertIsNone(c.get_chapter_count(row)['chapter_count'])
 def test_weebcentral_full_list(self):
  row={'source':'WeebCentral','url':'https://weebcentral.com/series/01J76XYCPSY3C4BNPBRY8JMCBE/Solo-Leveling'}
  html='<button hx-get="https://weebcentral.com/series/01J76XYCPSY3C4BNPBRY8JMCBE/full-chapter-list">Show All Chapters</button>'
  chapter='<a href="/chapters/01J76XZ666GREP4DQDKEP1YDZG">Chapter 200</a>'
  with patch.object(c,'_fetch',side_effect=[html,chapter]) as fetch:
   self.assertEqual(c.get_chapter_count(row),{'chapter_count':1,'chapter_count_kind':'listed'});self.assertEqual(fetch.call_count,2)
 def test_enrichment_preserves_source_and_progress(self):
  rows=[{'title':'Book','source':'Small','url':'https://a.test/manga/book'},{'title':'Book','source':'Large','url':'https://b.test/manga/book'}];steps=[]
  with patch.object(c,'get_chapter_count',side_effect=lambda r:{'chapter_count':52 if r['source']=='Small' else 78,'chapter_count_kind':'reported'}):
   result=c.enrich_chapter_counts(rows,lambda done,total:steps.append((done,total)))
  self.assertEqual([r['chapter_count'] for r in result],[52,78]);self.assertNotIn('chapter_count',rows[0]);self.assertEqual(steps[-1],(2,2))
if __name__=='__main__':unittest.main()
