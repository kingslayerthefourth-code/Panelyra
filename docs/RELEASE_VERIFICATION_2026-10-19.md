# Source update verification — 19 October 2026

This is a source refresh, not a new signed public binary release.

## Checked

Eighteen focused Python regression entrypoints passed: UI audit, Downloads UI,
reading redesign, reader progress, local Reader, downloader, Capture dispatch,
Capture resume, series updates, quiet updates, download reliability, tablet push,
tablet delivery, tablet companion, tablet app updates, foundation, engine lifecycle,
and waiting feedback. The foundation suite skips its optional external publishing
helper when `PANELYRA_PUBLISH_HELPER` is not provided.

The existing offline Android `testDebugUnitTest` run passed all 26 tests, including
empty-cleanup-plan rejection. Test-fixture TLS credentials are deliberately public;
production signing keys, device identities, and cleanup inventories are excluded.

Two old test expectations were corrected to match the current source: Capture uses
four image workers, and compact search retains provider notices internally instead
of rendering the retired warning/retry interface.

## Known verification limits

`seamless_download_regression` passes six cases but its change-title Streamlit
AppTest case times out while rerendering the chapter text input, including a rerun
with a 15-second fixture timeout. This is not recorded as a passing suite. It needs
separate investigation; current fixture checks are not proof of live source search
or chapter completeness.

Physical Huawei transfer, device sleep/network behavior, new-machine setup,
and remote source captures were not exercised by this release check. Android SDK
and optional downloader executables are not included in the source commit. Local
APK references in release notes do not imply those binaries are published.

Existing downloaded books, conversations, progress, credentials, and local release
artifacts are not part of this update. No project-wide license was assigned.
