# Local foundation — Mac 0.16.0 / tablet 1.8.0

Reader and Downloader remain local and free. No model, token budget, translation
service, publishing task, login agent or new schedule is required or enabled.
The existing simple interface is retained, with visible waiting states described
in [Waiting feedback](WAITING_FEEDBACK.md). The user requested background work be
enabled after this update. Settings → General still provides an explicit pause.
External website-upload schedules are not restarted by this setting.
The tablet now follows the Mac UI; see [tablet UI, cleanup and updates](TABLET_UI_AND_UPDATES.md).

## Data and reading

- Shared SQLite schema 4, transactional progress, stable source/language/edition
  identities and canonical decimal chapter numbers. Titles alone never merge books.
- Originals stay untouched. Incomplete/unidentified inputs are Needs confirmation;
  invalid images/archives are Damaged. Full pixel, size, CRC and path checks gate Ready.
- Images-only CBZ artifacts, numerical page order, exact cache mapping, lazy extraction
  and bounded disposable caches. Downloaded books are never evicted as cache.
- Journalled publication and orphan recovery. Reopening reuses validated work, including
  a chapter published just before an interrupted compatibility-manifest write.
- Independent chapter position/completion, authenticated loopback Reader/media/API,
  strict Host/Origin checks, HttpOnly cookies and no-referrer protection.

## Work ownership and recovery

`Launch_Panelyra.command` owns one engine. Browser tabs are clients; closing a tab
does not cancel work. Explicit quit/terminal close drains owned workers and sends.
Workers require held leases and detect owner loss; stale/reused PIDs are not authority.

- Backend selections and interactive Capture accept up to 10,000 chapters, retaining
  bounded per-chapter safety limits. This is a durable selection limit, not a promise
  that an arbitrary source exposes that many chapters.
- Capture records/checkpoints and chapter handoffs live in SQLite; old JSON is retained
  as compatibility/history. Browser-approved interrupted runs resume at unfinished work
  when the engine is running and unpaused. A waiting-for-review run needs the user again.
  Failed runs require explicit Retry. Explicit Stop/cancel cannot be undone by callbacks.
- Capture recycles its browser every 20 ordinary chapters, uses at most four image
  requests and bounded outstanding results, and serializes local OCR per chapter.
  Continuous same-URL segments retain their segment coordinate rather than resetting it.
- Capture and backend share browser ownership. Contention waits rather than charging a
  source failure. Backend processes are recycled per chapter. Discovery checks checkpoint
  each book; the source circuit breaker does not block unaffected sources.
- App-owned HTTP page staging resumes only against an expected validator/version.
  Changed versions restart; completed cached pages are conditionally revalidated.
  Unknown-version partials restart safely. Vendor-specific backends retain their own
  HTTP capabilities; no generic byte-resume claim is made about third-party binaries.
- Paginated Capture rejects repeated/truncated navigation and must reach the declared
  final page. Decode/count checks cannot prove an upstream website exposed its true
  final story strip; live source-specific acceptance remains separate.

For an explicit dashboard-free current batch only:

```sh
.venv/bin/python -m panelyra.engine --background-batch
```

It exits when the batch stops/finishes or is globally paused; it is not a scheduler.
Legacy Engage and deliberately invoked standalone CLI tools remain independent of
Reader/Downloader. They are not started by this upgrade.

## Tablet delivery and APK

Install `releases/Panelyra-Tablet-v1.8.0-debug.apk` **as an update**, not an uninstall.
Application ID `com.panelyra.tablet` and the existing signing key are retained;
version code advances to 16. Previous v1.7.0, v1.6.2, v1.6.1, v1.6.0 and v1.5.0 APKs
remain available. Tablet data is not reset.

- Persistent transfer/chapter rows, immutable manifests, exact acknowledgements,
  cover provenance/version refresh and pause/cancel/Ready race protection.
- App-owned staging, checksums, recoverable file/Room commits, bounded network responses
  and guaranteed HTTP disconnect on success or failure. Two simultaneous library streams.
- Assisted WebView capture retains its completed-chapter counter when resuming. A
  partially finished job with an unknown next URL stops with an actionable message;
  it cannot silently restart at chapter one or send that wrong route to the Mac.
- Pinned identity survives DHCP address changes. Paired-only HMAC discovery precedes
  TLS pin verification; manual address/Retry remains available if broadcasts are blocked.
- Tablet sends are engine-owned, not tab-owned. Pause closes blocked sockets and retains
  partials. Speed excludes skipped/resumed bytes; ETA excludes verification/cover overhead.
- Optional older companion starts only by explicit current-session request, stops with
  pause/quit and launches no USB watcher. Its delegated Capture uses the same durable
  managed run/checkpoint path. Manual USB resolves canonical catalog artifacts.
- Companion expiry is inspected, never silently rotated. Connections shows expiry and
  offers explicit renewal only while stopped/paused, with private identity/state backup.
  Renewal requires pairing again; books/progress remain intact. Missing/mismatched key
  files are a restore error, not an excuse to generate a new identity.

## Release and restore

Source refresh has immutable per-release before/after snapshots, a durable pending
journal and idempotent recovery. Concurrent edits stop deployment instead of being
overwritten. SQLite is backed up before each installed increment; source and APK
hashes, signing identity and progress preservation are checked after restart.

The existing signing identity has an owner-only local recovery copy, independently
verified through a restore-copy certificate check. Do not publish private key backups.
Android dependencies are locked; an offline clean build is tested. Exact Mac installed
dependency pins and available Mac/Android licence metadata are inventoried. Missing
or inherited licence terms are explicitly unknown, not redistribution clearance.
See the workspace `foundation-work/verification` evidence and `RELEASE_ENGINEERING.md`.

Do not blindly downgrade a schema-4 database to an older engine. Preserve the current
database before restoring an earlier app/data snapshot; restoring old progress can
discard newer reading positions. Original books are not part of a source rollback.

## Acceptance boundaries

Local tests/fixtures are not physical Huawei tests. Installation/upgrade, EMUI sleep,
real Wi-Fi/UDP changes, USB, touch/rotation, offline large chapters and real transfer
speed still need the attached tablet. Live adapter semantic completeness requires an
explicit source capture test. No download, publishing task or background schedule was
reactivated to manufacture that evidence.
