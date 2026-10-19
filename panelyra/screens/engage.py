"""Engage screen."""
from __future__ import annotations

def render(*, CONTENT_MODE_OPTIONS, SCROLL_PRESETS, apply_reader_defaults, apply_selected_reader_mode_defaults, generate_comment_preview, load_presets, load_selector_profiles, normalized_speed_name, preflight_url, reader_defaults_for_mode, render_page_header, running, set_page, st, start_bot):
    comment_runtime = st.session_state.bot_runtimes["comment"]
    apply_reader_defaults(str(st.session_state.get("content_mode", "Auto")))
    selector_profiles = load_selector_profiles()
    if st.session_state.selected_selector_profile not in selector_profiles:
        st.session_state.selected_selector_profile = next(iter(selector_profiles))
    active_selector_values = selector_profiles[st.session_state.selected_selector_profile]
    for field_name, profile_key in (
        ("engage_next_link_selector", "next_link"),
        ("engage_next_page_selector", "next_page"),
        ("engage_page_counter_selector", "page_counter"),
        ("engage_chapter_list_selector", "chapter_list"),
        ("engage_comment_editor_selector", "comment_editor"),
        ("engage_submit_button_selector", "submit"),
        ("engage_thanks_button_selector", "thanks"),
        ("engage_confirmation_selector", "confirmation"),
    ):
        if field_name not in st.session_state:
            st.session_state[field_name] = active_selector_values.get(profile_key, "")
    render_page_header("Engage", "Prepare contextual chapter comments and review each run.")
    with st.expander("Use a saved configuration", expanded=False):
        presets = load_presets()
        preset_columns = st.columns([3, 1])
        selected_preset = preset_columns[0].selectbox(
            "Saved configuration",
            options=list(presets),
            key="selected_workflow_preset",
            label_visibility="collapsed",
        )
        if preset_columns[1].button("Apply", key="engage_apply_preset", width="stretch"):
            selected_values = presets[selected_preset]
            for key in ("scroll_preset", "chapter_routing", "max_chapters", "use_thanks_marker"):
                if key in selected_values:
                    st.session_state[key] = (
                        normalized_speed_name(selected_values[key])
                        if key == "scroll_preset"
                        else selected_values[key]
                    )
            st.toast(f"Applied {selected_preset}.")
            st.rerun()
    target_columns = st.columns([3, 1])
    target_columns[0].text_input(
        "Starting URL",
        key="start_url",
        placeholder="https://staging.example.com/chapter-1",
        help="Paste the first chapter page Panelyra should inspect. The page must be reachable in Chrome.",
    )
    target_columns[1].number_input(
        "Maximum chapters",
        key="max_chapters",
        min_value=1,
        max_value=10_000,
        step=1,
    )
    st.selectbox(
        "Reader layout",
        options=["auto", "next-link", "infinite-scroll", "paginated", "mixed"],
        key="chapter_routing",
        format_func=lambda value: {
            "auto": "Auto detect",
            "next-link": "Next chapter control",
            "infinite-scroll": "Continuous chapters",
            "paginated": "Next page reader",
            "mixed": "Pages plus next chapter",
        }[value],
    )
    with st.expander("Review the first comment", expanded=False):
        st.checkbox(
            "Use reviewed text for chapter one",
            key="review_first_comment",
        )
        st.text_input(
            "Preview focus",
            key="comment_preview_subject",
            placeholder="A character, clue, conflict, or turning point",
        )
        if st.button("Regenerate preview", key="regenerate_comment_preview"):
            st.session_state.comment_preview_text = generate_comment_preview(
                str(st.session_state.comment_preview_subject)
            )
        st.text_area(
            "First comment",
            key="comment_preview_text",
            height=110,
            disabled=not st.session_state.review_first_comment,
        )

    with st.expander("Advanced · behavior, context and selectors"):
        behavior_columns = st.columns(2)
        with behavior_columns[0]:
            st.selectbox(
                "Performance mode",
                options=list(SCROLL_PRESETS),
                key="scroll_preset",
            )
            st.caption(SCROLL_PRESETS[normalized_speed_name(st.session_state.scroll_preset)]["description"])
            st.checkbox(
                "Thank and mark completed chapters",
                key="use_thanks_marker",
            )
        with behavior_columns[1]:
            st.radio(
                "Content reader",
                options=list(CONTENT_MODE_OPTIONS),
                key="content_mode",
                on_change=apply_selected_reader_mode_defaults,
            )
            st.caption("Auto is recommended for most targets.")

        with st.expander("Optional context", expanded=False):
            st.text_input("Local context file", key="research_file")
            st.text_input(
                "Context endpoint",
                key="research_endpoint",
                placeholder="http://127.0.0.1:8000/research",
            )

        with st.expander("Advanced configuration", expanded=False):
            profile_columns = st.columns([3, 1])
            profile_columns[0].selectbox(
                "Selector profile",
                options=list(selector_profiles),
                key="selected_selector_profile",
            )
            if profile_columns[1].button("Apply profile", width="stretch"):
                values = selector_profiles[st.session_state.selected_selector_profile]
                for field_name, profile_key in (
                    ("engage_next_link_selector", "next_link"),
                    ("engage_next_page_selector", "next_page"),
                    ("engage_page_counter_selector", "page_counter"),
                    ("engage_chapter_list_selector", "chapter_list"),
                    ("engage_comment_editor_selector", "comment_editor"),
                    ("engage_submit_button_selector", "submit"),
                    ("engage_thanks_button_selector", "thanks"),
                    ("engage_confirmation_selector", "confirmation"),
                ):
                    st.session_state[field_name] = values.get(profile_key, "")
                st.rerun()

            selector_columns = st.columns(2)
            selector_columns[0].text_input("Next chapter", key="engage_next_link_selector")
            selector_columns[1].text_input("Next page", key="engage_next_page_selector")
            selector_columns[0].text_input("Page counter", key="engage_page_counter_selector")
            selector_columns[1].text_input("Chapter list", key="engage_chapter_list_selector")
            selector_columns[0].text_input("Comment editor", key="engage_comment_editor_selector")
            selector_columns[1].text_input("Submit control", key="engage_submit_button_selector")
            selector_columns[0].text_input("Thanks control", key="engage_thanks_button_selector")
            selector_columns[1].text_input("Confirmation", key="engage_confirmation_selector")

            st.text_input("Content container", key="content_container_selector")
            st.text_input("Chapter image selector", key="ocr_image_selector")
            st.slider(
                "OCR confidence",
                min_value=0.10,
                max_value=0.95,
                step=0.01,
                key="ocr_confidence",
            )
            if normalized_speed_name(st.session_state.scroll_preset) == "Custom":
                performance_columns = st.columns(2)
                performance_columns[0].number_input(
                    "Minimum scroll pixels", min_value=1, max_value=50_000, key="custom_scroll_min_px"
                )
                performance_columns[1].number_input(
                    "Maximum scroll pixels", min_value=1, max_value=50_000, key="custom_scroll_max_px"
                )
                performance_columns[0].number_input(
                    "Minimum scroll pause", min_value=0.0, max_value=10.0, key="custom_scroll_min_delay"
                )
                performance_columns[1].number_input(
                    "Maximum scroll pause", min_value=0.0, max_value=10.0, key="custom_scroll_max_delay"
                )
                performance_columns[0].number_input(
                    "Minimum typing delay", min_value=0.0, max_value=2.0, key="custom_type_min_delay"
                )
                performance_columns[1].number_input(
                    "Maximum typing delay", min_value=0.0, max_value=2.0, key="custom_type_max_delay"
                )
                performance_columns[0].number_input(
                    "Minimum cooldown", min_value=0.0, max_value=60.0, key="custom_cooldown_min"
                )
                performance_columns[1].number_input(
                    "Maximum cooldown", min_value=0.0, max_value=60.0, key="custom_cooldown_max"
                )
                st.number_input(
                    "Submission verification wait",
                    min_value=0.5,
                    max_value=30.0,
                    key="custom_submit_wait",
                )

    speed_name = normalized_speed_name(st.session_state.scroll_preset)
    speed = SCROLL_PRESETS[speed_name]
    if speed_name == "Custom":
        scroll_min_px = int(st.session_state.custom_scroll_min_px)
        scroll_max_px = int(st.session_state.custom_scroll_max_px)
        scroll_min_delay = float(st.session_state.custom_scroll_min_delay)
        scroll_max_delay = float(st.session_state.custom_scroll_max_delay)
        type_min_delay = float(st.session_state.custom_type_min_delay)
        type_max_delay = float(st.session_state.custom_type_max_delay)
        cooldown_min_seconds = float(st.session_state.custom_cooldown_min)
        cooldown_max_seconds = float(st.session_state.custom_cooldown_max)
        post_submit_wait_seconds = float(st.session_state.custom_submit_wait)
    else:
        scroll_min_px = int(speed["min_px"])
        scroll_max_px = int(speed["max_px"])
        scroll_min_delay = float(speed["min_delay"])
        scroll_max_delay = float(speed["max_delay"])
        type_min_delay = float(speed["type_min_delay"])
        type_max_delay = float(speed["type_max_delay"])
        cooldown_min_seconds = float(speed["cooldown_min_seconds"])
        cooldown_max_seconds = float(speed["cooldown_max_seconds"])
        post_submit_wait_seconds = float(speed["post_submit_wait_seconds"])
    visible_type_chars = int(speed["visible_type_chars"])
    reuse_loaded_images = bool(speed["reuse_loaded_images"])
    content_mode_cli = CONTENT_MODE_OPTIONS[str(st.session_state.content_mode)]
    default_container_selector, default_image_selector, _ = reader_defaults_for_mode(str(st.session_state.content_mode))
    effective_content_container_selector = str(st.session_state.content_container_selector).strip() or default_container_selector
    effective_ocr_image_selector = str(st.session_state.ocr_image_selector).strip() or default_image_selector

    launch_blockers = preflight_url(str(st.session_state.start_url))
    if not str(st.session_state.engage_comment_editor_selector).strip():
        launch_blockers.append("add a comment editor selector")
    if not str(st.session_state.engage_submit_button_selector).strip():
        launch_blockers.append("add a submit selector")
    if st.session_state.chapter_routing in {"paginated", "mixed"} and not str(
        st.session_state.engage_next_page_selector
    ).strip():
        launch_blockers.append("add a next-page selector")
    if scroll_max_px < scroll_min_px or scroll_max_delay < scroll_min_delay:
        launch_blockers.append("fix the scroll range")
    if type_max_delay < type_min_delay or cooldown_max_seconds < cooldown_min_seconds:
        launch_blockers.append("fix the timing range")

    st.caption(f"Up to {st.session_state.max_chapters} chapters · {speed_name} · {st.session_state.content_mode}")
    st.caption("Starting Engage can submit comments to the selected site. Use browser review to inspect the target first.")
    if launch_blockers and st.session_state.start_url:
        st.warning("Before starting: " + "; ".join(launch_blockers))
    prepare_column, direct_column, _ = st.columns([1, 1, 2])
    prepare_clicked = prepare_column.button(
        "Browser review",
        icon=":material/open_in_browser:",
        disabled=running or bool(launch_blockers),
        width="stretch",
    )
    direct_clicked = direct_column.button(
        "Start Engage",
        icon=":material/play_arrow:",
        type="primary",
        disabled=running or bool(launch_blockers),
        width="stretch",
    )

    def launch_engage(wait_for_login: bool) -> None:
        st.session_state.bot_process, st.session_state.log_path, st.session_state.start_signal_path = start_bot(
            str(st.session_state.start_url).strip(),
            int(st.session_state.max_chapters),
            wait_for_login=wait_for_login,
            research_file=str(st.session_state.research_file),
            research_endpoint=str(st.session_state.research_endpoint),
            use_thanks_marker=bool(st.session_state.use_thanks_marker),
            chapter_routing=str(st.session_state.chapter_routing),
            scroll_min_px=scroll_min_px,
            scroll_max_px=scroll_max_px,
            scroll_min_delay=scroll_min_delay,
            scroll_max_delay=scroll_max_delay,
            reflection_every_n_comments=int(st.session_state.reflection_every_n_comments),
            content_mode=content_mode_cli,
            content_container_selector=effective_content_container_selector,
            ocr_image_selector=effective_ocr_image_selector,
            ocr_confidence=float(st.session_state.ocr_confidence),
            type_min_delay=type_min_delay,
            type_max_delay=type_max_delay,
            visible_type_chars=visible_type_chars,
            reuse_loaded_images=reuse_loaded_images,
            cooldown_min_seconds=cooldown_min_seconds,
            cooldown_max_seconds=cooldown_max_seconds,
            post_submit_wait_seconds=post_submit_wait_seconds,
            next_link_selector=str(st.session_state.engage_next_link_selector).strip(),
            next_page_selector=str(st.session_state.engage_next_page_selector).strip(),
            page_counter_selector=str(st.session_state.engage_page_counter_selector).strip(),
            chapter_list_selector=str(st.session_state.engage_chapter_list_selector).strip(),
            comment_editor_selector=str(st.session_state.engage_comment_editor_selector).strip(),
            submit_button_selector=str(st.session_state.engage_submit_button_selector).strip(),
            thanks_button_selector=str(st.session_state.engage_thanks_button_selector).strip(),
            confirmation_selector=str(st.session_state.engage_confirmation_selector).strip(),
            first_comment_override=(
                str(st.session_state.comment_preview_text)
                if st.session_state.review_first_comment
                else ""
            ),
        )
        set_page("Engage Live")
        st.rerun()

    if prepare_clicked:
        launch_engage(True)
    if direct_clicked:
        launch_engage(False)
