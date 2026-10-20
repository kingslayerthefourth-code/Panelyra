"""Downloads Activity receipts come from durable jobs, never fabricated runs."""
import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from panelyra.screens.activity import download_run_records

class Receipts(unittest.TestCase):
    def test_complete_and_partial_receipts_preserve_actual_counts_and_failures(self):
        jobs=[{'id':'one','title':'Example','source':'Source','series_url':'https://example.org/book',
               'status':'completed','updated_at':123,'chapters':{'1':{'status':'downloaded'},'2':{'status':'already_saved'}}},
              {'id':'two','title':'Other','status':'partial','updated_at':124,
               'chapters':{'3':{'status':'downloaded'},'4':{'status':'failed','error':'Login required'}}}]
        with patch('panelyra.automation.download_queue.read_queue',return_value={'jobs':jobs}):
            a,b=download_run_records(Path('/unused'))
        self.assertEqual((a['run_id'],a['progress_current'],a['progress_total']),('one',2,2))
        self.assertEqual(a['settings']['chapters'],'1, 2')
        self.assertEqual((b['status'],b['progress_current'],b['progress_total']),('partial',1,2))
        self.assertEqual(b['stop_reason'],'Login required')
        self.assertEqual(b['bot_type'],'manga_download')

if __name__=='__main__':unittest.main()
