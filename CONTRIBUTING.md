# Contributing

Thanks for considering a contribution. A few principles keep EARN.iq useful:

1. **Code over prompt.** If a model gets something wrong that has a single correct answer (a list lookup, a comparison, a date), fix it in code, not by sharpening the prompt. Add a test that shows the failure.
2. **No verdict is better than an invented one.** Never store a score when the model was unreachable or its answer was incomplete.
3. **Respect the sources.** No bypassing of paywalls, bot walls or rate limits. Keep request rates low and back off on errors.
4. **Stay small.** Standard library for HTTP, Chromium as a subprocess, SQLite, one HTML file for the UI. New dependencies need a good reason.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
EARNIQ_DATA_DIR=./demo-data python -m earniq.demo
EARNIQ_DATA_DIR=./demo-data EARNIQ_WORKERS=0 uvicorn earniq.server:app --reload --port 8106
```

## Pull requests

- One topic per pull request, with a short description of the problem and how you verified the fix.
- `pytest` must pass. New guardrails and parsers need tests.
- For data sources: describe the endpoint, its terms of use and how failures are handled.

## Reporting issues

Please include the model and server you use (e.g. Ollama with qwen3:14b), the relevant log lines from the *System* tab, and, for fetching problems, the URL. Do not post API keys, your watchlist or database contents.
