# Panelyra

**Your chapters, in one place.**

Panelyra is a local, library-first Streamlit app for reading and downloading chapters. Library is the home page; Downloads searches supported sources, while Capture saves images, CBZ archives and optional OCR from a chapter URL.

The sidebar is **Library, Downloads, Capture**, followed by **Engage, Activity, Settings**. Capture and Engage use one setup screen with advanced options collapsed. Configuration drafts persist locally. Studio, Dashboard and the floating chat interface have been retired; existing assets and records remain intact.

See [the UI audit](docs/UI_UX_AUDIT.md) for verification and known limitations.

## Folder guide

See [docs/FOLDER_GUIDE.md](docs/FOLDER_GUIDE.md) for the complete layout.

- `panelyra/`: application packages; root Python files are working CLI entrypoints and shared engines.
- `config/`: preferences and `examples/` for setup. The private `.env` stays at the root for compatibility.
- `data/history/`: comments, read markers, feedback, and failure evidence.
- `data/updates/`: library update history, sources, failures, and status.
- `manhwa/` and `manhwa_clean/`: original and cleaned reading libraries.
- `outputs/`: generated reports and the Capture promo in `promo/capture-novatoon/`.
- `runs/`: per-run records and checkpoints.
- `archive/`: earlier backups, export snapshots, and legacy scripts.
- `tests/`, `scripts/`, `docs/`: verification, maintenance, and documentation.

## Workspaces

- **Library:** searchable covers, direct reading resume, edition selection and chapter files. Trim page edges crops margins only and preserves Originals. Tablet delivery is available in chapter actions.
- **Downloads:** search, choose a series and chapter range, then download. Review staged archives before importing into Library. Missing chapters and failed search sources are reported separately.
- **Capture:** images, CBZ archives and optional OCR. Advanced selectors and performance options are collapsed; browser review and explicit start/stop remain available.
- **Engage:** contextual comments with review, submission and verification on authorized targets.
- **Activity:** durable run receipts, failure details and report export. Retired Studio records remain readable but cannot be relaunched.
- **Settings:** General, Connections, Storage and Help/diagnostics.

User libraries, progress, presets, conversations and historical records are preserved. Internal screen renderers live in `panelyra/screens/`; automation engines retain their interfaces.

## Authorization

Use Panelyra only on sites and content you own, administer, or have explicit permission to test or archive. Browser workflows require an explicit start or review action.

Panelyra runs locally. Search, source downloads, browser workflows and optional external research contact their selected services.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Copy local configuration examples:

```bash
cp config/examples/urls.txt.example urls.txt
cp config/examples/comments.txt.example comments.txt
cp config/examples/research_context.json.example research_context.json
cp config/examples/.env.example .env
```

The AI interface has been removed. No local model is required or contacted by the app. Existing conversation files remain on disk.

## Launch

```bash
streamlit run panelyra/app.py
```

On macOS, the single-click launcher is:

```bash
./Launch_Panelyra.command
```

The launcher resolves its own folder, activates `.venv` when present, and opens the
Panelyra Library on `http://localhost:8517`. Use this launcher rather than a
legacy `Launch_AsuraAutomation.command` from an older project copy.

## Browser Profile

New installations use:

```text
~/Library/Application Support/Google/Chrome/PanelyraProfile
```

If the legacy `AutomationProfile` exists and `PanelyraProfile` does not, Panelyra reuses the legacy profile so authenticated sessions are preserved.

To launch Chrome manually with remote debugging:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome/PanelyraProfile"
```

## Engage CLI

```bash
python bot.py \
  --start-url "https://staging.example.com/chapter-1" \
  --max-chapters 3 \
  --chapter-routing auto
```

`Performance` and `Maximum` accelerate scrolling, routing, and cooldowns while retaining visible typing. The Live view reports scanned chapters, verified submissions, failures, stop reasons, and progress.

Engage can optionally pause for review of the first generated comment. The approved text is used only for the first chapter; subsequent chapters use contextual generation.

### Supported Reader Layouts

Engage and Capture share the same reader-routing model:

- **Auto detect** chooses between continuous, next-chapter, paginated, and mixed readers.
- **Continuous** supports long-strip manhwa/manhua pages and multiple chapter sections sharing one URL.
- **Next chapter** supports anchors, buttons, chapter dropdowns, and visible chapter lists.
- **Paginated** follows internal Next Page controls or `page`, `p`, and `reader_page` URL parameters before treating the chapter as complete.
- **Mixed** reads every internal page, then uses a distinct Next Chapter control.

Named selector profiles keep Next Page, Next Chapter, page counter, chapter list, comment editor, Submit, Thanks, and confirmation selectors separate. Site-specific JavaScript readers may still require a custom selector profile; Panelyra reports the detected layout and failed control in the Live log.

## Capture Output

Capture groups every chapter beneath one folder named after the series:

```text
manhwa/
  since-im-immune-to-corruption-i-start-with-a-disaster-class-demon-sword/
    manifest.json
    01/
      images/
      chapter-pages.cbz
      ocr.txt
      image_urls.txt
      metadata.json
    02/
      images/
      chapter-pages.cbz
      ocr.txt
      image_urls.txt
      metadata.json
```

Capture filters obvious promotional assets, validates responses, deduplicates image hashes, and resumes from its manifest. Raw image capture is intended only for content you are authorized to archive.

The Capture Configure flow lets you independently enable raw images, CBZ archive creation, and OCR text. When only CBZ is selected, temporary source images are removed after the archive is built.

Capture's persistent Chrome profile lives at `~/Library/Application Support/Panelyra/Chrome/CaptureProfile`, outside Chrome's protected application-data folder. The legacy `Google/Chrome/PanelyraDownloadProfile` is left untouched; you may need to sign in once in Capture's new browser. Downloaded books are unaffected. An explicit `--chrome-profile-dir` still overrides this default.

Browser automation workflows retain their busy checks and browser locks to avoid conflicting runs.

## Quiet Library Updates

The standalone updater checks every series already present in `manhwa/`, discovers
newly added books on each pass, and saves only chapters newer than the highest local
chapter. Quiet mode uses HTTP and never opens Chrome:

The generic updater recognizes Asura, Thunder, Kayn, Novatoon, and compatible
WordPress/Madara readers. Novatoon chapter images are isolated from its logos,
recommendation covers, avatars, and reader UI artwork.

```bash
python series_update_tracker.py --library --quiet --once
python series_update_tracker.py --library --quiet
```

Protected or malformed chapters are written to `data/updates/failed_updates.json` instead of
interrupting the desktop. Retry them quietly, in invisible Chrome, or in the visible
Capture browser only when you choose to:

```bash
python series_update_tracker.py --retry-failed --quiet
python series_update_tracker.py --retry-failed --headless-browser
python series_update_tracker.py --retry-failed --browser
```

`downloaded_chapters.json` contains successful chapter URLs only.
`update_tracker_status.json` records the latest check, and `series_sources.json`
remembers inferred or manually supplied landing pages. For a book whose chapter
metadata cannot reveal its series page, add this form to `series_sources.json`:

```json
{
  "local-series-folder": {
    "series_urls": ["https://example.com/manga/series-name"]
  }
}
```

Panelyra does not need to remain open. On macOS, automatic three-hour quiet checks
are opt-in:

```bash
python series_update_tracker.py --install-schedule
python series_update_tracker.py --uninstall-schedule
```

The scheduler never selects a browser retry mode. Use these features only for
content you are authorized to archive.

## Huawei Tablet App

`android/` contains the native Panelyra Reader + Downloader APK project for
EMUI/Android 9 and newer. It starts with an empty, Original-only library and
keeps completed chapters as CBZ files in the app's external files directory.

Open **Settings → Connections** in desktop Panelyra to start the HTTPS companion
and display its pairing QR. The tablet attempts a direct download first and uses
the paired Mac automatically when a source needs desktop Chrome. Pairing is
certificate-pinned and the Mac exposes only isolated, completed tablet jobs.

Build the debug APK with JDK 17, Android SDK 36, and Gradle 9.3.1:

```bash
cd android
./gradlew :app:assembleDebug
```

The APK is written to `android/app/build/outputs/apk/debug/app-debug.apk`.

## Legacy Studio assets

Existing Studio files and historical run records are preserved. There is no Studio launch screen in the current product. Old Studio routes redirect to Library with a notice.

For a paginated Capture run:

```bash
python manhwa_ocr_downloader.py \
  --start-url "https://staging.example.com/chapter-1" \
  --max-chapters 3 \
  --reader-layout auto \
  --save-images
```

## Project Layout

```text
panelyra/
  app.py
  automation/
  components/
  models/
  services/
  screens/
  styles/
config/
data/
docs/
outputs/
scripts/
tests/
```

Root-level automation scripts remain as compatibility entry points during the migration.

## Verification

```bash
env PYTHONPYCACHEPREFIX=/tmp/panelyra-pycache .venv/bin/python -m py_compile \
  app.py bot.py automation_runtime.py note_art_bot.py manhwa_ocr_downloader.py

.venv/bin/python tests/dashboard_readiness.py
.venv/bin/python tests/assistant_regression.py
.venv/bin/python tests/assistant_ui_regression.py
.venv/bin/python tests/browser_inspector_e2e.py
.venv/bin/python tests/downloader_regression.py
.venv/bin/python tests/provider_fixture_e2e.py
.venv/bin/python tests/reader_routing_regression.py
.venv/bin/python tests/runtime_regression.py
.venv/bin/python tests/stress_regression.py
```

Browser E2E tests use the local provider-shaped fixture in `tests/provider_fixture_e2e.py`.

## Data Protection

The pre-migration rollback snapshot and inventory are stored under:

```text
backups/pre_panelyra_20260723/
```

Panelyra Storage can prune generated logs, run records, and test reports by retention age. Downloads, uploads, history, read markers, and legacy OCR output are protected from automatic cleanup.
