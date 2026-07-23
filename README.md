# Panelyra

**Automation for every chapter.**

Panelyra is a local Streamlit and Selenium workspace for authorized chapter-content QA. It keeps Engage, Capture, and Studio runs independent while Panelyra Control provides health, history, storage, settings, support tools, and a context-aware Copilot.

The interface uses a sequential workflow: choose a workspace on the Dashboard, complete its focused Configure steps, monitor only that automation in Live, then review its isolated Results. Configuration drafts persist locally between sessions.

## Workspaces

- **Panelyra Engage** scans chapter context, generates original test comments, submits them on authorized targets, verifies results, and tracks read markers.
- **Panelyra Capture** collects authorized chapter images, creates CBZ archives, and produces optional OCR transcripts.
- **Panelyra Studio** renders Word Art, uploads media to an editor, inserts text, and verifies save or publish signals.
- **Panelyra Copilot** answers product questions, diagnoses run evidence, prepares workflow configuration, inspects the attached Chrome tab, and drafts support responses.
- **Panelyra Control** provides onboarding, environment health, run history, report exports, storage management, presets, settings, and help.

Every automation run writes an atomic record under `runs/`. Existing root-level history, read markers, uploads, downloads, and Chrome sessions remain compatible with the pre-Panelyra build.

## Authorization

Use Panelyra only on sites and content you own, administer, or have explicit permission to test or archive. Publishing and capture workflows require an authorization confirmation in the dashboard.

Panelyra runs locally. It sends data outside the Mac only when you configure an external research provider or model API.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Copy local configuration examples:

```bash
cp urls.txt.example urls.txt
cp comments.txt.example comments.txt
cp research_context.json.example research_context.json
cp .env.example .env
```

For optional model-enhanced Copilot responses, add `OPENAI_API_KEY` to `.env`, then restart Panelyra. Copilot remains usable with its local knowledge and diagnostic engine when no key is configured.

## Launch

```bash
streamlit run panelyra/app.py
```

On macOS, the single-click launcher is:

```bash
./Launch_Panelyra.command
```

The launcher resolves its own folder, activates `.venv` when present, and opens the
Panelyra dashboard on `http://localhost:8517`. Use this launcher rather than a
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

Panelyra allows one browser automation workflow at a time because Engage, Capture, and Studio share the authenticated Chrome profile. A second workflow is blocked with a clear busy message until the active run finishes or is stopped.

## Panelyra Copilot

Copilot is available as a dedicated **Assistant** page and as the compact `AI` popover on other pages. It has five modes:

- **Auto** selects a mode from the request and preserves the previous mode for short follow-up questions.
- **Ask** explains Panelyra behavior using the local product knowledge base.
- **Diagnose** evaluates structured run records, recent log events, browser state, selectors, and saved output.
- **Configure** proposes and can apply safe form values without starting a run.
- **Support** drafts user-facing replies and can create an editable local support ticket.

Every answer can expose its evidence and suggested actions. Controlled actions can navigate to a workspace, validate configuration, restore a previous run as a retry draft, inspect Chrome, apply configuration values, or create a support ticket. Copilot never starts, stops, submits, publishes, deletes, or uploads on its own.

Browser inspection reads the active Chrome tab through the local debugging port. DOM evidence is shown in the Assistant page; screenshots are captured only when browser inspection and screenshots are enabled in Settings. External web research is disabled by default and is used only for explicit external-research questions when the model integration is enabled.

## Studio Output

Studio provides a live composition preview with text and panel colors, text alignment, panel placement, and layout style. It can leave the target editor open for review or activate the configured save/publish control.

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
