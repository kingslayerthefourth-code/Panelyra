# Local release reproducibility and recovery

The private local distribution retains its installed debug signing identity so the
tablet can upgrade without uninstalling. This is not a new public/App Store release
key. Changing that identity requires a separate distribution decision; never silently
regenerate it or reset the tablet to get an install through.

## Build inputs

- Android Gradle/Compose/KSP plugin versions are fixed in `android/build.gradle.kts`.
- Resolved app/test dependencies are in `android/app/gradle.lockfile`.
- Gradle 9.3.1, JDK 17.0.20 and the already installed SDK 36/build-tools 36.0.0 are
  the tested local toolchain. `./gradlew --offline --no-daemon clean testDebugUnitTest
  assembleDebug` must pass before shipping. Tests require the documented local Python
  fixture runtime, not access to real sources or real books.
- `verification/dependencies/mac-installed.lock` pins 73 installed distributions;
  metadata hashes and available licences are in `mac-inventory.json`. This records the
  existing environment. A new machine still needs matching wheels/runtime; a freshly
  reconstructed Mac wheelhouse was not certified by reusing installed packages.
  The same pins ship as `requirements.foundation.lock`; ordinary upgrades do not
  reinstall or downgrade the existing environment.
- `android-inventory.json` records 266 locked modules and cached POM-declared terms.
  Missing/inherited terms remain unknown. The optional downloader's licence remains
  with its separately pinned vendor distribution. This is not legal clearance to
  redistribute all dependencies as a commercial bundle.

## Upgrade and restore

1. Keep background work paused. Take a non-overwriting SQLite backup and source
   hashes with `snapshot_increment.py --version <new-version>`.
2. Add touched paths to the guarded deployment list; `release_foundation.py extend`
   snapshots newly touched originals without replacing prior baselines.
3. `restart_engine.py` validates the exact owner, drains it, applies/reconciles the
   pending deployment journal, and starts the app again. Older APK filenames are not
   reused for a different binary.
4. Verify database integrity/FKs, chapter/progress hashes, paused policy, installed
   source/APK hashes and signing certificate. Run read-only catalog/media smoke checks.
5. A pending journal can contain only its recorded old or new file hashes. Unexpected
   edits block recovery. Restore sources only from that release's `before` snapshots;
   preserve the current database first. Never overwrite a newer database with an older
   snapshot without accepting that newer progress may be lost.

`backup_signing_key.py` creates a local mode-0700 directory/mode-0600 key recovery copy
and verifies a temporary restore-copy certificate. It does not print private material.
Keep a separate secure offline copy for machine loss. Restore the **same** keystore
before building updates; verify the APK certificate against the retained previous APK.
Do not commit, upload or package private keys or pairing credentials.
