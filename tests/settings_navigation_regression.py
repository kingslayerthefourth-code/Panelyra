"""Exercise real sidebar and section widgets; never start connections or save preferences."""
import sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from streamlit.testing.v1 import AppTest
with patch('panelyra.services.preferences.save_preferences'), patch('tablet_ui.ensure_companion'), patch('tablet_ui.delivery_status'):
    app=AppTest.from_file(str(ROOT/'panelyra/app.py'),default_timeout=60)
    app.session_state['page']='Activity'
    app.run()
    app.button(key='nav_Settings').click().run()
    for section,expected in [('Connections','Tablet connection'),('Storage','Storage Areas'),('Help','Finding your way'),('General','Default landing page'),('Connections','Tablet connection')]:
        app.radio(key='settings_view').set_value(section).run()
        assert not app.exception,[error.message for error in app.exception]
        assert app.session_state['settings_view']==section
        rendered='\n'.join(str(x.value) for kind in ('markdown','subheader','header') for x in app.get(kind))+'\n'+'\n'.join(x.label for x in app.selectbox)
        assert expected in rendered,(section,rendered)
        app.run()
        assert app.session_state['settings_view']==section
        print(section,'content and rerun passed',flush=True)
