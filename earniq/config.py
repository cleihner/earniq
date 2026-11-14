"""Settings from the environment.

Everything that differs between installations lives here and nowhere else:
paths, the model endpoint, credentials, the alert channel. Nothing in the
code knows a host name. See `.env.example` for the full list.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env support for running without Docker. Real environment variables win."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(Path(os.environ.get("EARNIQ_ENV_FILE", str(ROOT / ".env"))))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _secret(name: str) -> str:
    """Value from NAME, or from the file named in NAME_FILE (Docker/systemd secrets)."""
    value = _env(name)
    if value:
        return value
    path = _env(name + "_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


# Storage
DATA_DIR = Path(_env("EARNIQ_DATA_DIR", str(ROOT / "data"))).expanduser()
CONFIG_DIR = ROOT / "config"

# Model: any OpenAI-compatible chat completions endpoint
# (Ollama, LM Studio, llama.cpp server, mlx_lm.server, vLLM, OpenAI ...)
LLM_BASE_URL = _env("EARNIQ_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_MODEL = _env("EARNIQ_LLM_MODEL")
LLM_TIMEOUT = _int("EARNIQ_LLM_TIMEOUT", 300)
SUMMARY_LANGUAGE = _env("EARNIQ_SUMMARY_LANGUAGE", "English")


def llm_api_key() -> str:
    return _secret("EARNIQ_LLM_API_KEY")


# Fetching
CHROMIUM = _env("EARNIQ_CHROMIUM")
BROWSER_WAIT_MS = _int("EARNIQ_BROWSER_WAIT_MS", 8000)
BROWSER_TIMEOUT_S = _int("EARNIQ_BROWSER_TIMEOUT", 90)
ACCEPT_LANGUAGE = _env("EARNIQ_ACCEPT_LANGUAGE", "en-US,en;q=0.9")


def sec_user_agent() -> str:
    """SEC EDGAR requires a declared user agent with name and contact address."""
    return _secret("EARNIQ_SEC_USER_AGENT")


# Alerts
ALERT_WEBHOOK_URL = _env("EARNIQ_ALERT_WEBHOOK_URL")
ALERT_FORMAT = _env("EARNIQ_ALERT_FORMAT", "json").lower()   # json | ntfy | slack | discord
ALERT_MAX_PER_DAY = _int("EARNIQ_ALERT_MAX_PER_DAY", 8)

# Web interface
BASIC_AUTH = _secret("EARNIQ_BASIC_AUTH")          # "user:password", empty = no login
SEED_EXAMPLES = _env("EARNIQ_SEED_EXAMPLES", "1") not in ("0", "false", "no")
# 0 = no background fetching or scoring (demo data, debugging)
WORKERS = _env("EARNIQ_WORKERS", "1") not in ("0", "false", "no")
