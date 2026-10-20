"""Launching a run must not assign an already-rendered Streamlit radio key."""
import ast,unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext

class State(dict):
    __getattr__=dict.__getitem__
    def __setitem__(self,key,value):
        if key=='capture_view' and self.get('lock_widgets',False):raise RuntimeError('Widget already rendered')
        super().__setitem__(key,value)
    __setattr__=__setitem__

class WorkspaceTransition(unittest.TestCase):
    def test_launch_is_deferred_until_before_view_widget_render(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'set_page','render_workspace_view'}]
        state=State(page='Capture',capture_view='Configure');state['lock_widgets']=True
        st=SimpleNamespace(session_state=state,container=lambda **kw:nullcontext(),radio=lambda *a,**kw:state[kw['key']])
        env={'st':st,'LEGACY_PAGE_ROUTES':{'Capture Live':('Capture','Live')},'PAGES':['Capture','Library'],
             'WORKSPACE_VIEW_ROUTES':{'Capture':{'Configure':'Capture Setup','Live':'Capture Live'}},'request_scroll_to_top':lambda:None}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'app navigation','exec'),env)
        env['set_page']('Capture Live')
        self.assertEqual(state['capture_view'],'Configure')
        self.assertEqual(state['_workspace_view_request'],('Capture','Live'))
        state['lock_widgets']=False
        self.assertEqual(env['render_workspace_view']('Capture'),'Capture Live')
        self.assertEqual(state['capture_view'],'Live')
        self.assertNotIn('_workspace_view_request',state)
if __name__=='__main__':unittest.main()
