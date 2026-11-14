"""Alerts: push strong events and sentiment trends to a webhook.

Event rule, for companies in the portfolio group: relevance 3, sentiment +/-2,
topic guidance, regulation, legal or management, and either the full article
was read or at least two outlets report the same thing. At most once per
company, topic and direction in 24 hours, and at most EARNIQ_ALERT_MAX_PER_DAY
alerts per day in total (events and trends together).

Formats (EARNIQ_ALERT_FORMAT):
  json     POST the payload as JSON (for n8n, Home Assistant, your own relay)
  ntfy     set the topic URL (https://ntfy.sh/your-topic); sent via ntfy's JSON API
  slack    POST {"text": ...} to a Slack incoming webhook
  discord  POST {"content": ...} to a Discord webhook
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import config, db, signals

STRONG_TOPICS = {"guidance", "regulation", "legal", "management"}
STATE = {"alert": ""}


def configured() -> bool:
    return bool(config.ALERT_WEBHOOK_URL)


def _since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _sent_today(c) -> int:
    return c.execute("SELECT COUNT(*) FROM alerts WHERE time>=? AND result LIKE 'ok%'", (_since(24),)).fetchone()[0]


def message(p: dict) -> str:
    arrow = "▲" if p.get("direction", 0) > 0 else "▼"
    text = f"{arrow} {p['company']} · {p['topic']}: {p['text']}"
    return text + (f"\n{p['link']}" if p.get("link") else "")


def send(payload: dict) -> str:
    """Deliver one alert. Returns 'ok' or the error as text; never raises."""
    if not configured():
        return "not configured"
    url, fmt = config.ALERT_WEBHOOK_URL, config.ALERT_FORMAT
    headers = {"Content-Type": "application/json"}
    if fmt == "ntfy":
        # JSON publishing: POST to the server root with the topic in the body,
        # so titles with non-ASCII characters survive (headers would not).
        parts = urllib.parse.urlsplit(url)
        url = f"{parts.scheme}://{parts.netloc}/"
        body = json.dumps({"topic": parts.path.strip("/").split("/")[-1],
                           "title": f"EARN.iq: {payload['company']}",
                           "message": message(payload),
                           "tags": ["chart_with_upwards_trend" if payload.get("direction", 0) > 0 else "chart_with_downwards_trend"],
                           **({"click": payload["link"]} if payload.get("link") else {})}).encode("utf-8")
    elif fmt == "slack":
        body = json.dumps({"text": message(payload)}).encode("utf-8")
    elif fmt == "discord":
        body = json.dumps({"content": message(payload)[:1900]}).encode("utf-8")
    else:
        body = json.dumps({"source": "earniq", **payload}, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(url, body, headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            return "ok" if 200 <= r.status < 300 else f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:300]


def deliver(c, payload: dict, kind: str, headline_id: int | None = None) -> str:
    """Send with the daily cap and record the result."""
    if _sent_today(c) >= config.ALERT_MAX_PER_DAY:
        result = f"skipped: daily limit of {config.ALERT_MAX_PER_DAY} reached"
    else:
        result = send(payload)
    c.execute("INSERT INTO alerts(company, kind, topic, direction, time, headline_id, text, link, result) VALUES(?,?,?,?,?,?,?,?,?)",
              (payload.get("key", payload["company"]), kind, payload["topic"], payload.get("direction", 0), signals.now(),
               headline_id, payload["text"], payload.get("link", ""), result))
    c.commit()
    STATE["alert"] = f"{payload['company']} {payload['topic']}: {result[:80]}"
    return result


def check_events() -> None:
    if not configured():
        return
    portfolio = {f["key"]: f for f in signals.companies() if f.get("group", "portfolio") == "portfolio"}
    since = _since(6)
    with closing(db.connect()) as c:
        rows = c.execute("""SELECT id, company, title, publisher, link, sentiment, relevance, topic, key_point, article_id
                            FROM headlines WHERE relevance=3 AND ABS(sentiment)=2 AND time>=? AND backfilled=0""", (since,)).fetchall()
        for m in rows:
            if m["company"] not in portfolio or m["topic"] not in STRONG_TOPICS:
                continue
            direction = 1 if m["sentiment"] > 0 else -1
            if c.execute("SELECT 1 FROM alerts WHERE company=? AND kind='event' AND topic=? AND direction=? AND time>=?",
                         (m["company"], m["topic"], direction, _since(24))).fetchone():
                continue
            outlets = c.execute("SELECT COUNT(DISTINCT publisher) FROM headlines WHERE company=? AND topic=? AND sentiment*?>0 "
                                "AND relevance>=2 AND time>=?", (m["company"], m["topic"], direction, since)).fetchone()[0]
            if not m["article_id"] and outlets < 2:
                continue
            text = (m["key_point"] or m["title"])[:380] + (f" ({outlets} outlets)" if outlets >= 2 else f" ({m['publisher']})")
            link = m["link"] if (m["link"] or "").startswith("https://") and "news.google.com" not in m["link"] else ""
            deliver(c, {"key": m["company"], "company": portfolio[m["company"]]["name"], "direction": direction,
                        "topic": m["topic"], "text": text, "link": link}, "event", m["id"])


def recent(n: int = 20) -> list:
    with closing(db.connect()) as c:
        return [dict(x) for x in c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (n,))]
