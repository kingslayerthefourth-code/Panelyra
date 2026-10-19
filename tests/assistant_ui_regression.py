"""AI retirement: no navigation, controls or model requests; old routes remain safe."""
from pathlib import Path
from unittest.mock import patch
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from streamlit.testing.v1 import AppTest
with patch("panelyra.assistant.local_model.LocalModelClient.status", side_effect=AssertionError("Model must not be contacted")), patch("panelyra.services.preferences.save_preferences"):
    app=AppTest.from_file(str(ROOT/"app.py"),default_timeout=45).run()
    for route in ["Library","Lyra","Assistant","Settings","Capture Live","Engage Live","Run History"]:
        app.session_state["page"]=route
        app.run()
        assert not app.exception, app.exception
        assert not app.chat_input
        assert not any("Lyra" in b.label or b.label == "AI" for b in app.button)
        assert not app.session_state["show_lyra_in_immersive_reader"]
    assert "Lyra" not in [b.label for b in app.sidebar.button]
print("AI removal: routes, reader toggle, actions and no model calls passed")
