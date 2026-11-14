"""Fetch a page and turn it into readable text.

Two ways, in this order:

  1. **HTTP**: one request, no browser. Covers JSON endpoints, feeds, APIs and
     every server-rendered page. Costs milliseconds and no RAM.
  2. **Chromium**: only when HTTP is not enough: 403/429, a redirect to a bot
     wall, or too little text because the page loads its content with
     JavaScript. Called as a subprocess with `--dump-dom`, not through
     Playwright: on ARM Linux (a Raspberry Pi, say) the system browser is the
     more robust option, and the same code runs unchanged on a Mac.

The decision is made per fetch and stored with the result (`mode`), so the
interface shows which sources are expensive.

Chromium runs **serially**: one browser next to a local model is fine on a
small machine, two are not. The lock holds even when several sources are due
at the same time.
"""
from __future__ import annotations

import gzip
import hashlib
import html as _html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import config

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": config.ACCEPT_LANGUAGE,
    "Accept-Encoding": "gzip, deflate",
}
TIMEOUT = 20            # seconds per HTTP fetch


def headers_for(url: str) -> dict:
    """SEC EDGAR blocks undeclared automated tools and asks for name plus contact.

    The declared user agent goes to sec.gov only; every other site sees the
    regular browser user agent. The SEC limit is 10 requests per second, far
    above anything EARN.iq sends.
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host == "sec.gov" or host.endswith(".sec.gov"):
        ua = config.sec_user_agent()
        if ua:
            return {**HEADERS, "User-Agent": ua,
                    "Accept": "application/atom+xml,application/xml,text/html;q=0.8,*/*;q=0.5"}
    return HEADERS


# Chromium is searched for, not assumed: on Debian it is called
# chromium-headless-shell, on a Mac it sits in the Applications folder.
BROWSER_CANDIDATES = [
    config.CHROMIUM,
    "chromium-headless-shell",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
MIN_CHARS = 350         # below this an HTML page counts as empty -> Chromium
MAX_RAW = 4_000_000     # guard against downloads that are no longer a page

_browser_lock = threading.Lock()

_DROP = re.compile(r"(?is)<(script|style|noscript|svg|iframe|template)\b.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_SPACE = re.compile(r"[ \t\xa0]+")
_LINES = re.compile(r"\n{3,}")
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
# The empty shell of a single-page app.
_SPA_SHELL = re.compile(
    r"""(?is)<(div|main)[^>]+id=["'](root|app|__next|__nuxt|svelte|application)["'][^>]*>\s*</\1>""")
# More common than the empty shell is the full skeleton with empty
# placeholders: markup, classes and icons are in the HTML, the values arrive
# via JavaScript. Measured on a home dashboard: 45,709 characters of HTML,
# 510 of them text (1.1 %); after rendering there were 1,892. A minimum
# character count alone would have let it through as "content". A page with
# lots of markup and little text therefore goes to the browser. That is cheap:
# the browser result is only taken if it really yields more.
SKELETON_FROM = 8000    # HTML characters from which the ratio counts at all
SKELETON_RATIO = 0.04   # below this the page counts as not rendered


def _decompress(raw: bytes, encoding: str) -> bytes:
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 - raw bytes are better than nothing
        pass
    return raw


def clean(t: str) -> str:
    t = _html.unescape(t or "")
    t = _SPACE.sub(" ", t)
    t = "\n".join(z.strip() for z in t.splitlines())
    return _LINES.sub("\n\n", t).strip()


def text_from_html(html: str) -> str:
    """Visible text without the page chrome.

    The main area first; if that yields little (pages without <main>,
    consent layers), the whole body.
    """
    stripped = _DROP.sub(" ", html)
    main = ""
    m = re.search(r"(?is)<(main|article)\b[^>]*>(.*?)</\1>", stripped)
    if m:
        main = clean(_TAGS.sub("\n", m.group(2)))
    body = clean(_TAGS.sub("\n", re.sub(r"(?is)^.*?<body\b[^>]*>", "", stripped)))
    return main if len(main) > 400 else (body or main)


def text_from_json(raw: str) -> str:
    """JSON stays JSON, just indented.

    A product catalogue or a release feed as running text would be harder for
    the model to read than the structure, and the structure is the information.
    """
    try:
        d = json.loads(raw)
    except ValueError:
        return clean(raw)
    return json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True)


def _via_http(url: str) -> dict:
    t0 = time.time()
    req = urllib.request.Request(url, headers=headers_for(url))
    code, raw, ctype = 0, b"", ""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
            code = f.getcode() or 0
            ctype = (f.headers.get("Content-Type") or "").lower()
            raw = _decompress(f.read(MAX_RAW), (f.headers.get("Content-Encoding") or "").lower())
    except urllib.error.HTTPError as e:
        code = e.code
        ctype = (e.headers.get("Content-Type") or "").lower() if e.headers else ""
        try:
            raw = e.read(MAX_RAW)
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception as e:  # noqa: BLE001 - network, DNS, timeout
        return {"mode": "http", "http_code": 0, "text": "", "title": "",
                "error": f"{type(e).__name__}: {e}"[:300],
                "duration_ms": int((time.time() - t0) * 1000)}

    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        charset = m.group(1)
    try:
        raw_text = raw.decode(charset, "replace")
    except LookupError:
        raw_text = raw.decode("utf-8", "replace")

    shell = False
    if "json" in ctype or (raw_text[:1].strip() in ("{", "[") and "html" not in ctype):
        text, title = text_from_json(raw_text), ""
    else:
        tm = _TITLE.search(raw_text)
        title = clean(tm.group(1))[:200] if tm else ""
        text = text_from_html(raw_text)
        shell = bool(_SPA_SHELL.search(raw_text)) or (
            len(raw_text) >= SKELETON_FROM and len(text) / len(raw_text) < SKELETON_RATIO)
    return {"mode": "http", "http_code": code, "text": text, "title": title,
            "error": "", "shell": shell,
            "duration_ms": int((time.time() - t0) * 1000)}


def browser_path() -> str:
    """First candidate that exists on this machine."""
    for k in BROWSER_CANDIDATES:
        if not k:
            continue
        p = shutil.which(k) or (k if os.path.exists(k) else "")
        if p:
            return p
    return ""


def _via_chromium(url: str) -> dict:
    t0 = time.time()
    program = browser_path()
    if not program:
        return {"mode": "chromium", "http_code": 0, "text": "", "title": "",
                "error": "no Chromium found", "duration_ms": 0}

    # One browser at a time, and a throwaway profile per call: no state that
    # leaks between sources, and no profile that keeps growing.
    with _browser_lock, tempfile.TemporaryDirectory(prefix="earniq-chrome-") as profile:
        cmd = [
            program, "--headless", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--hide-scrollbars",
            f"--lang={config.ACCEPT_LANGUAGE.split(',')[0]}",
            "--window-size=1280,2400",
            f"--virtual-time-budget={config.BROWSER_WAIT_MS}",
            f"--user-data-dir={profile}",
            f"--user-agent={UA}",
            "--dump-dom", url,
        ]
        try:
            e = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                               timeout=config.BROWSER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return {"mode": "chromium", "http_code": 0, "text": "", "title": "",
                    "error": "browser timeout",
                    "duration_ms": int((time.time() - t0) * 1000)}
        except Exception as ex:  # noqa: BLE001
            return {"mode": "chromium", "http_code": 0, "text": "", "title": "",
                    "error": f"{type(ex).__name__}: {ex}"[:300],
                    "duration_ms": int((time.time() - t0) * 1000)}

    dom = e.stdout or ""
    if not dom.strip():
        tail = (e.stderr or "").strip().splitlines()
        return {"mode": "chromium", "http_code": 0, "text": "", "title": "",
                "error": ("empty DOM: " + (tail[-1] if tail else "no message"))[:300],
                "duration_ms": int((time.time() - t0) * 1000)}

    m = _TITLE.search(dom)
    # Chromium returns no HTTP status; 200 here means "the browser delivered
    # something". The block check decides whether that is content.
    return {"mode": "chromium", "http_code": 200, "text": text_from_html(dom),
            "title": clean(m.group(1))[:200] if m else "", "error": "",
            "duration_ms": int((time.time() - t0) * 1000)}


# Pages that return HTTP 200 but no content: bot walls, consent walls,
# paywalls for machines. Without this check the wall would be stored as a
# regular finding with score 0 and a summary nobody needs.
BLOCK_CODES = {401, 402, 403, 407, 429, 451}
BLOCK_PATTERN = re.compile(
    r"(?i)(tollbit|just a moment\.{0,3}|checking your browser|cf-browser-verification|"
    r"attention required!? \| cloudflare|enable javascript and cookies to continue|"
    r"access denied|zugriff verweigert|are you a robot|unusual traffic from your|"
    r"bitte aktiviere javascript|please enable javascript to view)")


def blocked(r: dict) -> str:
    """Empty string = free, otherwise the reason."""
    if r["http_code"] in BLOCK_CODES:
        return f"blocked (HTTP {r['http_code']})"
    probe = (r["text"] or "")[:1500]
    m = BLOCK_PATTERN.search(probe)
    # Only on short pages: on a long news page "access denied" can simply be
    # part of the text.
    if m and len(r["text"]) < 2500:
        return f"blocked ({m.group(0)[:40]})"
    return ""


def _not_enough(r: dict) -> bool:
    """When HTTP is not enough and Chromium gets its turn."""
    if r["error"]:
        return True
    if r["http_code"] in BLOCK_CODES or r["http_code"] >= 500:
        return True
    if blocked(r):
        return True
    if r.get("shell"):
        return True
    return len(r["text"]) < MIN_CHARS


def page(url: str, mode: str = "auto") -> dict:
    """Fetch a page. Always returns a result, never raises.

    mode: auto (HTTP, Chromium if needed) | http | chromium
    """
    if mode == "chromium":
        r = _via_chromium(url)
    else:
        r = _via_http(url)
        if mode == "auto" and _not_enough(r):
            second = _via_chromium(url)
            # Only take it if the browser really yields more; otherwise the
            # honest HTTP error would be lost to an empty run.
            if len(second["text"]) > len(r["text"]):
                second["duration_ms"] += r["duration_ms"]
                r = second
    r["hash"] = hashlib.sha256(r["text"].encode("utf-8")).hexdigest() if r["text"] else ""
    r["chars"] = len(r["text"])
    r["blocked"] = blocked(r)
    return r


def raw(url: str, timeout: int = 25, extra_headers: dict | None = None) -> tuple[int, bytes]:
    """Plain GET for feeds and APIs. (status, body); 0 on network errors."""
    req = urllib.request.Request(url, headers={**headers_for(url), "Accept": "*/*", **(extra_headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read(MAX_RAW)
            return r.status, _decompress(b, (r.headers.get("Content-Encoding") or "").lower())
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:  # noqa: BLE001 - network down, timeout
        return 0, b""


def diff(old: str, new: str, max_lines: int = 40) -> str:
    """Compact diff for the interface: what was added, what is gone.

    Not a unified diff: on pages with ad blocks or timestamps that would be
    unreadable. A set comparison over lines catches what matters: new items,
    new paragraphs, entries that disappeared.
    """
    a = {z.strip() for z in (old or "").splitlines() if len(z.strip()) > 2}
    b = {z.strip() for z in (new or "").splitlines() if len(z.strip()) > 2}
    added = [z for z in (new or "").splitlines() if z.strip() in (b - a)]
    removed = [z for z in (old or "").splitlines() if z.strip() in (a - b)]
    parts = []
    if added:
        parts.append("+ " + "\n+ ".join(added[:max_lines]))
    if removed:
        parts.append("- " + "\n- ".join(removed[:max_lines]))
    rest = max(0, len(added) - max_lines) + max(0, len(removed) - max_lines)
    if rest:
        parts.append(f"... and {rest} more lines")
    return "\n".join(parts)[:8000]
