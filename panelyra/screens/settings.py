"""Settings screen."""
from __future__ import annotations

def render(*, APP_DIR, DEFAULT_SETTINGS, PAGES, environment_health, load_presets, load_selector_profiles, render_page_header, save_local_preferences, save_presets, save_selector_profiles, st):
    render_page_header("Settings", "Preferences, storage, connections and help.")
    section = st.session_state.get("settings_view", "General")
    if section == "General":
        from panelyra.engine import policy, pause
        paused = st.toggle('Pause background work', value=policy(APP_DIR), help='Pauses local downloads, source checks and tablet sends. Does not start or change any external schedule.')
        if paused != policy(APP_DIR):
            pause(APP_DIR, paused)
        st.caption('No AI model or login-time service is started. Closing a browser tab leaves the local engine running; closing its launcher quits safely.')
        st.selectbox("Default landing page", options=PAGES, key="default_landing_page")
        st.selectbox("Progress animation", ["Full", "Subtle", "Off"], key="progress_animation")
        st.toggle("Mac completion notifications", key="mac_notifications")
        with st.expander("Run display and saved configurations"):
            st.toggle("Show progress history", key="progress_history")
            st.number_input("Console characters", min_value=5000, max_value=200000, step=5000, key="console_max_chars")
            st.select_slider("Auto-refresh seconds", [1, 2, 3, 5, 10], key="auto_refresh_seconds")
            with st.expander("Saved Workflow Presets", expanded=False):
                st.text_input(
                    "Preset name",
                    key="saved_preset_name",
                    placeholder="My staging reader",
                )
                if st.button(
                    "Save current Engage preset",
                    disabled=not st.session_state.saved_preset_name.strip(),
                    width="stretch",
                ):
                    presets = load_presets()
                    presets[st.session_state.saved_preset_name.strip()] = {
                        "scroll_preset": st.session_state.scroll_preset,
                        "chapter_routing": st.session_state.chapter_routing,
                        "max_chapters": int(st.session_state.max_chapters),
                        "use_thanks_marker": bool(st.session_state.use_thanks_marker),
                    }
                    save_presets(presets)
                    st.session_state.selected_workflow_preset = st.session_state.saved_preset_name.strip()
                    st.success("Workflow preset saved locally.")
                    st.rerun()

            with st.expander("Named Selector Profiles", expanded=False):
                profiles = load_selector_profiles()
                active_profile_name = (
                    st.session_state.selected_selector_profile
                    if st.session_state.selected_selector_profile in profiles
                    else next(iter(profiles))
                )
                st.caption(
                    f"Current source: {active_profile_name}. Edit selectors from Engage Setup, then save them under a reusable name."
                )
                st.text_input(
                    "New selector profile name",
                    key="selector_profile_name",
                    placeholder="Owned staging reader",
                )
                if st.button(
                    "Save current selectors",
                    disabled=not st.session_state.selector_profile_name.strip(),
                    width="stretch",
                ):
                    fallback_profile = profiles[active_profile_name]
                    profiles[st.session_state.selector_profile_name.strip()] = {
                        "next_link": str(
                            st.session_state.get("engage_next_link_selector", fallback_profile["next_link"])
                        ),
                        "next_page": str(
                            st.session_state.get("engage_next_page_selector", fallback_profile["next_page"])
                        ),
                        "page_counter": str(
                            st.session_state.get("engage_page_counter_selector", fallback_profile["page_counter"])
                        ),
                        "chapter_list": str(
                            st.session_state.get("engage_chapter_list_selector", fallback_profile["chapter_list"])
                        ),
                        "comment_editor": str(
                            st.session_state.get("engage_comment_editor_selector", fallback_profile["comment_editor"])
                        ),
                        "submit": str(
                            st.session_state.get("engage_submit_button_selector", fallback_profile["submit"])
                        ),
                        "thanks": str(
                            st.session_state.get("engage_thanks_button_selector", fallback_profile["thanks"])
                        ),
                        "confirmation": str(
                            st.session_state.get("engage_confirmation_selector", fallback_profile["confirmation"])
                        ),
                    }
                    save_selector_profiles(profiles)
                    st.session_state.selected_selector_profile = st.session_state.selector_profile_name.strip()
                    st.success("Selector profile saved locally.")
                    st.rerun()

    elif section == "Connections":
        from tablet_ui import render_connections
        render_connections(APP_DIR)
    elif section == "Help":
        st.markdown("### Finding your way")
        st.markdown("**Library** holds your saved books and reading position. Select a cover, then a chapter. Chapter actions include files, tablet delivery and **Trim page edges**.")
        st.markdown("**Downloads** saves verified chapters directly into Library; uncertain source identities stay in Review. **Capture** saves pages from a chapter URL using an isolated browser. Both require an explicit start.")
        st.markdown("**Engage** prepares and submits chapter comments.")
        st.caption("Trim page edges crops image margins. It does not translate or remove text. Studio is retired; its existing files and run records are preserved.")
        with st.expander("Diagnostics"):
            st.dataframe(environment_health(), width="stretch", hide_index=True)
        st.caption("Library files, progress, conversations and history stay local unless you explicitly use a network feature or export them.")
    if section == "General":
        if st.button("Save preferences", type="primary"):
            keys = [key for key in DEFAULT_SETTINGS if key in st.session_state and not key.startswith(("assistant_", "lyra_", "show_lyra"))]
            save_local_preferences({key: st.session_state[key] for key in keys})
            st.success("Preferences saved.")
