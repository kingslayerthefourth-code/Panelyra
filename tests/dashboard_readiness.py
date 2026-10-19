import sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from streamlit.testing.v1 import AppTest
# Don't overwrite real preferences while exercising routing/drafts.
with patch('panelyra.services.preferences.save_preferences'):
    app=AppTest.from_file(str(ROOT/'app.py'),default_timeout=60).run()
    def ready(label):
        assert not app.exception, (label,[(x.message,x.stack_trace) for x in app.exception])
        print(label, 'OK', flush=True)
    ready('Cold start')
    assert [b.label for b in app.sidebar.button]==['Library','Downloads','Capture','Engage','Activity','Settings']
    for page in ['Library','Downloads','Capture','Engage','Activity','Settings']:
        app.button(key='nav_'+page).click().run();ready(page)
        if page=='Capture':
            assert app.button(key='capture_start')
            assert all('Continue to' not in b.label for b in app.button)
        if page=='Settings':
            for view in ['Connections','Storage','Help','General']:
                app.session_state['settings_view']=view;app.run();ready('Settings '+view)
    for legacy,expected in [('Reader','Library'),('Dashboard','Library'),('Capture Library','Library'),('Studio','Library'),('Studio Live','Library'),('Run History','Activity'),('Assistant','Library'),('Lyra','Library'),('Help','Settings')]:
        app.session_state['page']=legacy;app.run();ready('Migrate '+legacy)
        assert app.session_state['page']==expected
    for page in ['Capture Live','Engage Live','Engage Insights']:
        app.session_state['page']=page;app.run();ready(page)
print('All routes passed')
