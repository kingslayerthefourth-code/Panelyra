# Mac → tablet Wi-Fi delivery (Panelyra Android 1.5.0)

Install `releases/Panelyra-Tablet-v1.5.0-debug.apk` on the tablet as an update. Do not uninstall Panelyra: updating preserves its saved books and reading progress. The application ID and signing certificate match v1.4.4.

## Connect once

1. On the tablet, open **Connections & transfers → Receive from Mac**.
2. On the Mac, open **Settings → Connections**, enter the tablet address and its 12-character setup code, and choose **Connect to tablet**.
3. Choose chapters under **Send selected chapters**, or use the existing Library chapter action.

The Mac initiates every connection to the tablet. No Mac firewall changes, cloud service, USB debugging, or incoming Mac connection is required. Both devices must be reachable on the same local network. Guest Wi-Fi/client isolation can still prevent communication.

**Established pairing does not expire.** The tablet keeps its TLS identity in Android Keystore and the pairing token in private, backup-excluded application storage. The Mac saves the pinned identity/token in an owner-readable file. Only an unused setup code expires after five minutes. Stop and restart unpaired Receive mode to generate a new setup code.

Receive mode uses an Android connected-device foreground service with an ongoing notification. Leaving the app does not intentionally stop it. **Stop receiving** turns the listener off while keeping pairing saved; **Forget Mac** revokes pairing. Reopening the app restarts Receive mode if it was enabled. After a tablet reboot, open Panelyra once. Android/EMUI battery restrictions, force-stop, sleep and Wi-Fi changes can interrupt availability without forgetting the pairing. This is persistent pairing, not a promise of uninterrupted connectivity under every power policy.

If the tablet's IP address changes, update its address in Mac Connections and press **Check connection**. The existing pinned identity is verified before saving the new address. Do not reset pairing for a routine Wi-Fi interruption.

## Transfers and recovery

- Only explicitly selected, validated Original chapters are sent, with their covers.
- Saved delivery records live in `data/tablet_companion/deliveries`; the new `push` transport is isolated from legacy tablet polling.
- Chapter identity, size and SHA-256 bind each manifest. The sender verifies the tablet certificate before sending tokens or chapter data.
- An interrupted upload retains its partial bytes in app-owned staging. **Retry** resumes the same version. Invalid checksums never publish a chapter.
- The existing CBZ validator checks archive paths, entries, image dimensions and CRCs before library installation. Installation retains existing chapter progress.
- Already verified chapters are skipped. A transfer becomes Ready only after chapters and covers are installed and acknowledged.
- Pause/Retry/Cancel are on Mac Connections. Failed transfers wait for an explicit retry instead of repeatedly attempting network access.
- Reopen Connections after restarting the Mac dashboard to resume queued/in-progress transfers. A queued transfer is bound to the tablet identity that was selected when it was created.
- Cancelled/stopped transfers can leave app-owned partial files for retry. Only hash-named partial records older than seven days are cleaned up when the receiving server starts. Library chapters are unaffected.

This feature delivers chapters and covers. It does not synchronize reader progress back to the Mac, perform remote capture jobs, or replace the existing optional tablet-to-Mac companion protocol.

## Implementation

- `tablet_push.py`: outbound TLS client, HMAC setup proof, pinning, resumable sender, private connection state and persistent queue worker.
- `tablet_delivery.py`: shared durable queue with explicit transport and destination identity.
- `tablet_ui.py`: first-priority Mac send setup and selected chapter delivery.
- Android `receive/ReceiveServer.kt`: bounded HTTPS parser, authenticated manifest/upload/commit endpoints, partial storage and installer callbacks.
- Android `receive/TabletReceiveService.kt`: stored Android Keystore identity, foreground receiving, persistent pairing and visible state.

## Validation

Python sender, queue and Streamlit widget regressions; existing companion/startup regressions; Android library/routing/receiver unit tests; and a cross-language test running the real Python client against the Kotlin receiver. The latter covers wrong codes, certificate mismatch, wrong tokens, path traversal, interrupted upload, duplicate installation and retained reading progress. Pairing lifetime/restart tests simulate a year passing without expiring an established token.

An emulator-only native acceptance runner is in `tests/run_push_acceptance.py`; it refuses physical devices and uses synthetic chapters. It exercises Android Keystore TLS, chapter/cover imports, service restart, retained pairing/progress and receiving with the activity backgrounded. See the dated verification report for the actual run result. Physical Huawei Wi-Fi and EMUI power management still need a check on the user's device.

Build with the existing Android SDK/JDK and debug signing key. For cross-language unit coverage, set `PANELYRA_TEST_PYTHON` to the Panelyra Python environment and `PANELYRA_PUSH_ROOT` to the project root before `:app:testDebugUnitTest`.
