"""Read full articles for the companies you follow.

Bing News RSS per company returns the publisher URL directly (Google News
links go through a consent page that EARN.iq does not click away). The text
is fetched (HTTP, Chromium if needed) and read by the model: sentiment,
relevance, topic, key point, a verbatim quote and other companies mentioned.

A read article replaces the score of the matching headline and counts 1.5x in
the sentiment. Paywalls and bot walls stay "unreadable": nothing is bypassed.
"""
from __future__ import annotations

import difflib
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import date, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import db, fetch, judge, llm, signals

BING = "https://www.bing.com/news/search?q={q}&format=rss&mkt={mkt}"
MARKETS = {"en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "it": "it-IT", "nl": "nl-NL"}
BING_EVERY_MIN = 60

STATE = {"articles": ""}


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9äöüéèàç]+", " ", (t or "").lower()).strip()


def bing(q: str, mkt: str) -> list[dict]:
    code, b = fetch.raw(BING.format(q=urllib.parse.quote_plus(q), mkt=mkt))
    if code != 200 or not b:
        raise RuntimeError(f"Bing HTTP {code}")
    out = []
    for it in ET.fromstring(b).iter("item"):
        link = it.findtext("link") or ""
        m = re.search(r"[?&]url=([^&]+)", link)
        url = urllib.parse.unquote(m.group(1)) if m else link
        if not url.startswith(("http://", "https://")):
            continue
        source = ""
        for k in it:
            if k.tag.endswith("Source"):
                source = (k.text or "").strip()
        try:
            t = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        out.append({"url": url[:800], "title": (it.findtext("title") or "").strip()[:400],
                    "description": (it.findtext("description") or "").strip()[:800], "publisher": source[:120],
                    "time": t.strftime("%Y-%m-%dT%H:%M:%S"), "day": t.date().isoformat()})
    return out


def fetch_bing() -> None:
    """Per company the newest articles (last 3 days) into the queue."""
    limit = (signals.today() - timedelta(days=3)).isoformat()
    with closing(db.connect()) as c:
        for f in signals.companies():
            if f.get("group") == "auto":
                continue
            mkt = MARKETS.get((f["searches"][0].get("lang") or "en")[:2], "en-US") if f.get("searches") else "en-US"
            default = f'"{f["name"]}"' + (f' {f["ticker"]}' if f.get("ticker") and "." not in f["ticker"] else "")
            for q in f.get("bing") or [default]:
                try:
                    for a in bing(q, mkt):
                        if a["day"] < limit:
                            continue
                        c.execute("INSERT OR IGNORE INTO articles(company, url, title, publisher, description, time, day, captured) "
                                  "VALUES(?,?,?,?,?,?,?,?)", (f["key"], a["url"], a["title"], a["publisher"],
                                                              a["description"], a["time"], a["day"], signals.now()))
                except Exception as e:  # noqa: BLE001
                    STATE["articles"] = f"Bing {f['name']}: {e}"
                time.sleep(2)
            c.commit()
        db.put(c, "signals_bing", signals.now())
        c.commit()


def _excerpt(text: str, title: str, n: int = 7000) -> str:
    """Start at the title: before it there is usually navigation and cookie notices."""
    t = text or ""
    probe = (title or "")[:40].strip()
    i = t.find(probe) if len(probe) > 15 else -1
    return t[i:i + n] if i >= 0 else t[:n]


def read_article() -> bool:
    """Fetch one article. True if there was something to do."""
    with closing(db.connect()) as c:
        a = c.execute("SELECT id, url, title FROM articles WHERE state='new' AND company<>'_market' "
                      "ORDER BY time DESC LIMIT 1").fetchone()
        if not a:
            return False
        s = fetch.page(a["url"])
        if s["blocked"] or s["chars"] < 1500 or s["http_code"] not in (0, 200):
            reason = s["blocked"] or (f"HTTP {s['http_code']}" if s["http_code"] not in (0, 200) else f"only {s['chars']} characters")
            c.execute("UPDATE articles SET state='unreadable', chars=?, reason=? WHERE id=?", (s["chars"], reason[:120], a["id"]))
        else:
            c.execute("UPDATE articles SET state='read', chars=?, text=? WHERE id=?",
                      (s["chars"], _excerpt(s["text"], a["title"]), a["id"]))
        c.commit()
    return True


SYSTEM_ARTICLE = """You read a news article about a listed company from the point of view of an investor who wants to know how sentiment looks ahead of the next quarterly results.

Answer ONLY with JSON:
{"s": -2..2, "r": 0..3, "t": "topic", "key_point": "one sentence with the most important new information, with the number if one is given", "quote": "verbatim sentence from the article that carries the score", "companies": [{"name": "company", "direction": 1|0|-1, "reason": "short"}]}

- s: effect on business and stock. 2 clearly positive, -2 clearly negative, 0 neutral or mixed.
- r: 0 not relevant (other company, how-to, advertising), 1 background or price report, 2 signal about the business, 3 strong signal with numbers, or from the company, a regulator or a major customer.
- t: one of guidance, analysts, demand, supply, order, partnership, product, regulation, management, legal, price, other.
- Scale: orders below USD 1 billion are routine for these companies (at most s=1, r=2).
- companies: other listed companies the article says something positive or negative about regarding their business (at most 5, not the company itself, no media or authorities). Empty list if none.
Do not add anything: everything must be in the article."""


def score_article() -> bool:
    from . import discovery
    with closing(db.connect()) as c:
        a = c.execute("SELECT * FROM articles WHERE state='read' ORDER BY time DESC LIMIT 1").fetchone()
        if not a:
            return False
        f = next((x for x in signals.companies() if x["key"] == a["company"]), {"name": a["company"], "ticker": "", "context": ""})
        user = (f"Company: {f['name']} ({f.get('ticker', '')}). {f.get('context', '')}\n"
                f"Title: {a['title']}\nOutlet: {a['publisher']} · {a['day']}\n\nArticle text:\n{a['text']}")
        try:
            answer, model, finish = llm.chat([{"role": "system", "content": SYSTEM_ARTICLE},
                                              {"role": "user", "content": user}], max_tokens=700, temperature=0.0)
        except Exception as e:  # noqa: BLE001
            STATE["articles"] = f"Model: {type(e).__name__}"
            return False
        d = llm.json_from(answer)
        try:
            s, r = int(d.get("s")), int(d.get("r"))
            assert -2 <= s <= 2 and 0 <= r <= 3
        except (TypeError, ValueError, AssertionError):
            c.execute("UPDATE articles SET state='failed', reason=? WHERE id=?",
                      ("unusable model answer" + (" (cut off)" if finish == "length" else ""), a["id"]))
            c.commit()
            return True
        t = str(d.get("t") or "").lower()
        t = t if t in signals.TOPICS else "other"
        quote = str(d.get("quote") or "").strip()[:300]
        # An order without a billion-scale amount is routine, whatever the model says.
        if t == "order" and not re.search(r"\b(mrd|milliarde\w*|billion\w*|bn|milliard\w*)\b",
                                          f"{d.get('key_point', '')} {quote} {a['title']}", re.I):
            s, r = min(s, 1) if s > 0 else s, min(r, 2)
        if quote and not judge.quote_found(quote, a["text"] or ""):
            quote = "[not found verbatim in text] " + quote
        key_point = str(d.get("key_point") or "").strip()[:400]
        others = []
        for x in (d.get("companies") or [])[:5]:
            if isinstance(x, dict) and str(x.get("name", "")).strip():
                try:
                    direction = max(-1, min(1, int(x.get("direction", 0))))
                except (TypeError, ValueError):
                    direction = 0
                others.append({"name": str(x["name"]).strip()[:80], "direction": direction,
                               "reason": str(x.get("reason", "")).strip()[:200]})
        hid = _link(c, a, s, r, t, key_point)
        c.execute("UPDATE articles SET state='scored', sentiment=?, relevance=?, topic=?, key_point=?, quote=?, companies=?, "
                  "headline_id=?, model=?, scored=? WHERE id=?",
                  (s, r, t, key_point, quote, json.dumps(others, ensure_ascii=False), hid, llm.model_label(model),
                   signals.now(), a["id"]))
        for x in others:
            k = discovery.name_key(x["name"])
            if not k or discovery.is_own(k):
                continue
            c.execute("INSERT OR IGNORE INTO mentions(key, name, direction, signal, reason, origin, publisher, url, title, day, captured) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (k, x["name"], x["direction"], t, x["reason"], f"article:{a['company']}",
                                                        a["publisher"], a["url"], a["title"], a["day"], signals.now()))
        c.commit()
        STATE["articles"] = f"read: {f['name']}: {a['title'][:60]}"
    return True


def _link(c, a, s, r, t, key_point) -> int:
    """Find the matching headline and replace its score, otherwise add a headline of its own."""
    day = date.fromisoformat(a["day"])
    cands = c.execute("SELECT id, title FROM headlines WHERE company=? AND day BETWEEN ? AND ?",
                      (a["company"], (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat())).fetchall()
    target = norm_title(a["title"])
    best, value = None, 0.0
    for k in cands:
        q = difflib.SequenceMatcher(None, target, norm_title(k["title"])).ratio()
        if q > value:
            best, value = k, q
    if best and value >= 0.75:
        c.execute("UPDATE headlines SET sentiment=?, relevance=?, topic=?, key_point=?, article_id=?, scored=? WHERE id=?",
                  (s, r, t, key_point, a["id"], signals.now(), best["id"]))
        return best["id"]
    cur = c.execute("INSERT OR IGNORE INTO headlines(company, key, title, publisher, link, time, day, captured, backfilled, "
                    "sentiment, relevance, topic, key_point, article_id, scored) VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)",
                    (a["company"], "bing:" + a["url"][:280], a["title"], a["publisher"], a["url"], a["time"], a["day"],
                     signals.now(), s, r, t, key_point, a["id"], signals.now()))
    return cur.lastrowid


def duplicate_of_article(c, company: str, title: str, day: str) -> bool:
    """For new Google headlines: is there already a headline from a read article on this?"""
    t = date.fromisoformat(day)
    target = norm_title(title)
    for k in c.execute("SELECT title FROM headlines WHERE company=? AND article_id IS NOT NULL AND day BETWEEN ? AND ?",
                       (company, (t - timedelta(days=1)).isoformat(), (t + timedelta(days=1)).isoformat())):
        if difflib.SequenceMatcher(None, target, norm_title(k["title"])).ratio() >= 0.75:
            return True
    return False


def step() -> bool:
    """One small unit of work. True if something was done."""
    with closing(db.connect()) as c:
        due_bing = signals.is_due(c, "signals_bing", BING_EVERY_MIN / 60)
        due_cleanup = signals.is_due(c, "signals_article_cleanup", 24)
    if due_bing:
        fetch_bing()
    if due_cleanup:
        with closing(db.connect()) as c:
            old = (signals.today() - timedelta(days=30)).isoformat()
            c.execute("UPDATE articles SET text=NULL WHERE day<? AND text IS NOT NULL", (old,))
            db.put(c, "signals_article_cleanup", signals.now())
            c.commit()
    return score_article() or read_article() or due_bing

