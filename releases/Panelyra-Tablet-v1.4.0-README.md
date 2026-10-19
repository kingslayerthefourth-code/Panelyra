# Panelyra Tablet v1.4.0

Install `Panelyra-Tablet-v1.4.0-debug.apk` as an **update**, not by uninstalling the old app. The application ID (`com.panelyra.tablet`) and signing certificate match v1.3.1. Previous APKs are retained.

## Sending books

1. Open desktop Panelyra → Reader → **Send to Huawei**.
2. If not already paired, open tablet Reader → the computer icon → **Pair with short code**. Use the Mac address discovered automatically, or type the displayed address and expiring code.
3. On Mac, select books, **All missing chapters**, all selected chapters, or individual chapters, then **Send to Huawei**.
4. Keep both devices on the same Wi-Fi. Open Panelyra on the tablet; each validated chapter and its cover appears in Reader automatically. There is no file-picker or second import step.

The tablet keeps its own books and reading progress offline. A sleeping/unavailable Mac never deletes tablet chapters. If EMUI stops delivery, reopening Panelyra resumes the persistent queue. Pause/resume/cancel are in the secondary Connections & transfers panel; active deliveries also have notification controls.

Reader uses charcoal/black surfaces, soft purple accents, ~250 dp covers, and an optional pale-lilac light theme. Comics are not tinted. Previous, Next, All chapters, Continue reading, and explicit chapter loading/error/retry states are provided.

LocalSend remains optional: open/share a received CBZ or library ZIP **with Panelyra**. It imports the granted file directly without opening another picker. The bundled Original ZIP naming convention carries book/chapter labels; unnamed CBZs open as their own imported book. USB/manual import is secondary.

## Repairs and reliability

- Series upserts no longer delete child chapters. Room migration preserves existing rows/progress, removes global checksum uniqueness, and adds persistent transfer records.
- Numeric chapter identities unify `01` and `1`. Supplied manifest identities and matching source routes resolve aliases; unrelated source identities are not merged by title.
- Startup recovers valid orphan CBZs. Future chapters carry sidecar recovery metadata. Damaged archives are reported by book/chapter, not published by successful deliveries.
- Pages are naturally ordered (`1, 2, 10`), duplicate/unsafe paths and corrupt CRCs are rejected, and an ordered cache manifest allows reuse. Only disposable caches are evicted.
- Cover provenance/version is tracked; a thumbnail does not suppress a supplied or changed cover.
- One delivery worker coordinates the queue, with at most two simultaneous chapter downloads. Staging and partial files are app-owned, checksums bind resumption to a specific version, and acknowledgements follow file/Room publication.
- Mac identity/certificate survives DHCP address changes; discovered addresses are accepted only after saved-pin authentication. Catalog validation/digests are cached by file signature.
- The companion and code display run in Panelyra's existing Mac process, avoiding a detached Python helper that macOS may block from reading Desktop files. Opening Panelyra starts it once across screen reruns.
- Direct and assisted captures publish successful chapters immediately. Cancelled work does not trigger automatic fallback/retry; resumable capture checkpoints and progressive Mac artifacts retain finished chapters.

## Verification on 2026-10-01

- Android build and seven Room/library regression tests passed, plus existing routing tests.
- Authenticated HTTPS tests passed for selection, durable queue, wrong-token rejection, pause/cancel, acknowledgement versions, Range resumption, changed files/covers, and duplicate paths.
- AOSP Android 35 tablet emulator passed native secure pairing, saved-pin reconnection to a changed address, automatic selected five-chapter delivery with **zero picker interaction**, supplied covers, chapter navigation, completion, cache reuse, and offline reading.
- Actual v1.3.1 → v1.4 installation/migration was tested with the reproduced state: five CBZ folders but only chapter 5 in Room. All five were restored and chapter 5's panel/scroll/completed state survived.
- An interrupted emulator delivery retained completed chapters and resumed when reopened.
- Mac UI selection smoke test queued exactly chapters 1 and 3, not the whole catalog.
- In-process Mac companion startup/reuse and its HTTPS health endpoint passed an isolated regression test.
- Actual Mac catalog: 66 validated Original chapters, six books, 952,110,282 archive bytes. Initial validation: 3.26 seconds; unchanged catalog: 0.034 seconds.
- Fresh synthetic five-chapter delivery: 1.80 seconds, excluding pairing. Cached reopening: approximately 1.5–7.6 ms in the emulator. These small-fixture measurements are **not** Huawei Wi-Fi throughput claims.

Physical Huawei/EMUI testing is still required: no approved physical device was available to ADB. In particular, EMUI battery suspension, real Wi-Fi interruption/background behavior, rotation/touch/fullscreen, large real chapters, and low-storage behavior on that tablet are not certified by the emulator tests. Low-storage refusal is covered by a repository regression test. USB availability is not required for normal Wi-Fi delivery.

Use downloads only for material you own or are authorized to archive.
