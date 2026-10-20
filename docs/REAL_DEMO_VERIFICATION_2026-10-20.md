# Real-app demonstration verification — 20 October 2026

The product walkthrough was recorded against the real local installation, not
the synthetic demo library. No scraping or downloading functions were changed.

## Live evidence

- Searched Solo Leveling in Downloads, selected the Asura result, and requested
  chapter 1 only. The durable job completed on its first attempt.
- The resulting archive contains 22 readable images; its ZIP CRC check passes.
  The same chapter was exported through the series menu and checked again.
- Reader returned to exactly the saved scroll position after a series round trip.
- A separate Trimmed edition removes 20 blank bottom-margin pixels from the last
  image and zero pixels from the first. Every Original file's SHA-256 remained
  unchanged; the rebuilt archive passes its CRC check.
- An explicit source check listed the source's chapters and correctly marked
  chapter 1 as saved. No other chapter was queued by the check.
- Capture browser review reached `waiting-for-user` and was explicitly stopped
  without releasing its start signal or downloading any images.
- Activity displays the completed durable Downloads job, its source, requested
  chapter, and 1/1 count. No synthetic receipt was inserted.

## UI defects repaired during verification

- Removed the outer Downloads fragment, which nested its existing polling
  fragments and could duplicate controls during form submission.
- Defer cleaning queue/chapter transitions until before widget construction.
- Defer workspace View transitions until before their radio widget is rendered,
  allowing Capture's review launch to reach Live without a session-state error.
- Include genuine durable Downloads receipts in Activity, with a return-to-queue
  action. Existing Capture/Engage records remain compatible.

## Focused regressions

Passed: `downloads_ui_regression.py`, `downloads_fragment_regression.py`,
`activity_download_receipts_regression.py`, `cleaner_transition_regression.py`,
`workspace_view_transition_regression.py`, `manhwa_cleaner_regression.py`,
`reader_progress_regression.py`, `reader_routing_regression.py`,
`local_reader_regression.py`, `download_reliability_regression.py` (16 tests),
`quiet_updates_regression.py`, `title_search_regression.py` (8 tests), and
`seamless_download_regression.py` (7 tests), `manga_backend_regression.py`
(7 tests), and `settings_navigation_regression.py`.

The last two UI suites initially timed out while scanning the production
library. Their fixtures now use temporary libraries instead; all assertions
remain, and both suites pass without increasing their default timeout.
This is focused verification, not a claim that every source or every stress
suite was tested during video production.

## Recording and publication boundaries

The gray band occurred in Chromium's recorded frames but not viewport
screenshots. Timestamped viewport recording replaces that path; no blanket
bottom crop or cloned-frame tail is used. Voice B and bold subtitles are retained.
Browser session diagnostics are redacted in the exported footage. The user
confirmed permission for brief unblurred artwork excerpts; a full chapter is not
shown or bundled with the video materials. YouTube upload is not authorized.
