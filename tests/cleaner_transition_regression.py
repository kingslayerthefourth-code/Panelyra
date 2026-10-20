import sys,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from panelyra.screens.library import apply_clean_transition
class State(dict):
    __getattr__=dict.__getitem__
    __setattr__=dict.__setitem__
class CleanerTransition(unittest.TestCase):
    def test_completed_queue_keeps_the_current_chapter(self):
        st=SimpleNamespace(session_state=State(reader_chapter='01',reader_clean_batch=['01'],_reader_clean_transition={'queue':[],'chapter':None}))
        apply_clean_transition(st)
        self.assertEqual(st.session_state.reader_clean_batch,[])
        self.assertEqual(st.session_state.reader_chapter,'01')
        self.assertNotIn('_reader_clean_transition',st.session_state)
        apply_clean_transition(st)
    def test_batch_advances_to_the_next_review(self):
        st=SimpleNamespace(session_state=State(reader_chapter='01',reader_clean_batch=['01','02'],_reader_clean_transition={'queue':['02'],'chapter':'02'}))
        apply_clean_transition(st)
        self.assertEqual(st.session_state.reader_clean_batch,['02'])
        self.assertEqual(st.session_state.reader_chapter,'02')
if __name__=='__main__':unittest.main()
