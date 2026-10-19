# Panelyra for Huawei tablets

Native, Google-Play-Services-free Reader + Downloader for EMUI/Android 9+.

## Receive from Mac (1.5.0)

Open **Connections & transfers → Receive from Mac** and enter the displayed tablet address/code in Mac **Settings → Connections**. Pairing is persistent; only unused setup codes expire. Receive mode stays available in the background through a visible connected-device notification. See `../docs/TABLET_RECEIVE.md`.

## Behavior

- The permanent library contains only validated `chapter-pages.cbz` files.
- Direct capture is attempted first and rejects backward or repeated chapter routes.
- Static capture failures fall back to a securely paired Mac automatically.
- Without a paired Mac, dynamic/login sources can be opened in the assisted WebView.
- Selected Mac deliveries arrive automatically without a file picker or import step.
- Partial deliveries stay in app-owned staging storage and never appear as complete.
- Books and independent chapter progress remain available offline.
- v1.4 migrates existing Room records and recovers valid orphan CBZs.

## Build

Requirements: JDK 17, Android SDK 36, Build Tools 36.0.0, Gradle 9.3.1.

```bash
./gradlew :app:testDebugUnitTest :app:assembleDebug
```

This project does not depend on Google Play Services. Install the generated debug
APK by USB/ADB or Huawei's package installer. Use only with content you are
authorized to archive.

Install as an update; do not uninstall the previous app. Preserve the existing
debug signing keystore when building an update for an already-installed APK.

## Regression and native acceptance tests

Room/library tests run with `:app:testDebugUnitTest`. From the repository root,
run `python -m unittest discover -s tests -p 'test_tablet_*.py'` for the
authenticated Mac delivery API and in-process companion startup tests.

The native acceptance runners intentionally target only `emulator-5554`, never a
physical tablet. Build `:app:assembleDebugAndroidTest` first. Set `ANDROID_HOME` to
the Android SDK and `PANELYRA_TEST_FIXTURE` to a new, isolated temporary directory.
Start `python tests/emulator_fixture_server.py "$PANELYRA_TEST_FIXTURE"` in another
terminal, then run `python tests/run_emulator_acceptance.py`. The fixture serves
synthetic images only, on localhost ports 19443/19444.

`python tests/run_emulator_upgrade.py` resets the **emulator's** synthetic app
data, installs the retained v1.3.1 APK, reproduces the five-files/one-record bug,
then upgrades to the current APK and verifies recovery and preserved progress.
Do not use a real library as the fixture directory. Stop the fixture server when
finished. Physical Huawei/EMUI behavior still requires device testing.
