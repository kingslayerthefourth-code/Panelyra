# Asura Automations

Streamlit and Selenium tools for running controlled QA passes against comment flows on sites you own or administer.

The project includes:

- `app.py` - Streamlit dashboard for launching and monitoring the sequential QA runner.
- `bot.py` - sequential Selenium runner that can navigate chapters, generate context-aware QA comments, and record local read markers.
- `comment_flow_qa.py` - standalone authenticated comment-flow test runner.
- `qa_utils.py` and `history_utils.py` - shared Selenium and JSONL helpers.

## Safety Notes

Use these scripts only on sites where you have permission to test. The standalone runner defaults to dry-run mode and only submits comments when `--confirm-live-posting` is provided.

Runtime data such as URL lists, comment fixtures, research context, history, read markers, logs, and virtual environments are intentionally ignored by Git.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Chrome is required. The scripts use `webdriver-manager` to install a matching ChromeDriver when needed.

## Local Configuration

Copy the sample files before running with private targets:

```bash
cp urls.txt.example urls.txt
cp comments.txt.example comments.txt
cp research_context.json.example research_context.json
```

Then replace the placeholder URLs, comments, and context with staging-safe values.

## Streamlit Dashboard

```bash
streamlit run app.py
```

You can also run the macOS launcher:

```bash
./Launch_AsuraAutomation.command
```

## Sequential Bot

```bash
python bot.py \
  --start-url "https://staging.example.com/chapter-1" \
  --max-chapters 3 \
  --chapter-routing auto
```

Optional local research context:

```bash
python bot.py \
  --start-url "https://staging.example.com/chapter-1" \
  --max-chapters 3 \
  --research-file research_context.json
```

## Standalone Comment Flow QA

Start Chrome with a dedicated QA profile and remote debugging enabled, then log in to the site you are testing:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome/AutomationProfile"
```

Dry run against configured URLs:

```bash
python comment_flow_qa.py --urls-file urls.txt --comment-mode generated --max-comments 3
```

Live posting requires an explicit flag:

```bash
python comment_flow_qa.py --urls-file urls.txt --confirm-live-posting
```
