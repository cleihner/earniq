"""Signals: collect headlines per company, score each one, keep the time series,
plus closing prices and earnings dates.

Why next to the monitors: a monitor finding judges *one* page and names its
most important item. Sentiment and volume over time need every headline
individually, with a date. That is what this module does:

    Google News RSS per day (after:/before:)  -> headlines (one row per article)
    local model, 30 titles per call            -> sentiment -2..2, relevance 0..3, topic
    Yahoo chart API, Nasdaq, Euronext          -> prices (close per trading day)
    SEC 8-K Item 2.02 + configuration          -> earnings (past results dates)

The Google archive thins out with age (one large cap: 6 hits for a day three
months back, 78 for a day in the current week). Backfilled days therefore
carry `backfilled=1`; volume is only comparable from the start of live
capture (`live_since`).

One thread, serial, with pauses: a local model usually serves other clients too.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import config, db, fetch, llm

EXAMPLE = config.CONFIG_DIR / "watchlist.example.json"
CONFIG = config.DATA_DIR / "watchlist.json"     # editable through the interface

STATE = {"step": "", "last_fetch": "", "last_scoring": "", "error": "", "backfill": ""}
WAKE = threading.Event()

GOOGLE = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{lang}"
YAHOO = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
SEC = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
       "&type=8-K&dateb=&owner=include&count=40&output=atom")

FETCH_EVERY_MIN = 60         # re-query today and yesterday per company
PRICES_EVERY_H = 6
EARNINGS_EVERY_H = 24
PAUSE_GOOGLE_S = 2.5         # between two Google queries
PAUSE_MODEL_S = 4            # between two scoring calls: room for other model clients
PER_CALL = 30                # titles per model call

GROUPS = ("portfolio", "watchlist", "auto")
LANGUAGES = {"en": ("en-US", "US", "en"), "de": ("de", "DE", "de"), "fr": ("fr", "FR", "fr"),
             "es": ("es", "ES", "es"), "it": ("it", "IT", "it"), "nl": ("nl", "NL", "nl")}


# ── Configuration and time ───────────────────────────────────────────────
def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"companies": []}


def companies() -> list[dict]:
    return load_config().get("companies", [])


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def today() -> date:
    return datetime.now(timezone.utc).date()


def live_since() -> str:
    """First day of live capture. From the configuration, else the first start."""
    v = load_config().get("live_since")
    if v:
        return v
    with closing(db.connect()) as c:
        return db.get(c, "first_start", today().isoformat())


def backfill_from() -> date:
    v = load_config().get("backfill_from")
    if v:
        return date.fromisoformat(v)
    return date.fromisoformat(live_since()) - timedelta(days=30)


def init() -> None:
    if not CONFIG.exists() and EXAMPLE.exists() and config.SEED_EXAMPLES:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    with closing(db.connect()) as c:
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('first_start', ?)", (today().isoformat(),))
        for f in companies():
            for t in f.get("past", []):
                c.execute("INSERT OR IGNORE INTO earnings(company, day, kind, source) VALUES(?,?,?,?)",
                          (f["key"], t["day"], t.get("kind", ""), t.get("source", "config")))
        c.commit()


def is_due(c, key: str, hours: float) -> bool:
    w = db.get(c, key, "")
    if not w:
        return True
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(w).replace(tzinfo=timezone.utc) > timedelta(hours=hours)
    except ValueError:
        return True


# ── Fetching ─────────────────────────────────────────────────────────────
def _feed(url: str) -> list[dict]:
    code, b = fetch.raw(url)
    if code != 200 or not b:
        raise RuntimeError(f"HTTP {code}")
    root = ET.fromstring(b)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        # Google appends " - Publisher" to the title
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].strip()
        try:
            t = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        out.append({"key": (it.findtext("guid") or it.findtext("link") or title)[:300],
                    "title": title[:400], "publisher": source[:120],
                    "link": (it.findtext("link") or "")[:600],
                    "time": t.strftime("%Y-%m-%dT%H:%M:%S"), "day": t.date().isoformat()})
    return out


def google_url(q: str, s: dict) -> str:
    return GOOGLE.format(q=urllib.parse.quote_plus(q), hl=s.get("hl", "en-US"),
                         gl=s.get("gl", "US"), lang=s.get("lang", "en"))


def query_day(c, company: dict, day: date, backfilled: bool) -> int:
    """All searches of a company for exactly one day. Returns the number of new headlines."""
    from . import articles
    new = 0
    for s in company.get("searches", []):
        q = f'{s["q"]} after:{day.isoformat()} before:{(day + timedelta(days=1)).isoformat()}'
        for m in _feed(google_url(q, s)):
            if m["day"] != day.isoformat():
                continue          # Google occasionally rounds the time window
            if articles.duplicate_of_article(c, company["key"], m["title"], m["day"]):
                continue          # the same story is already there as a read article
            cur = c.execute(
                "INSERT OR IGNORE INTO headlines(company, key, title, publisher, link, time, day, "
                "captured, backfilled) VALUES(?,?,?,?,?,?,?,?,?)",
                (company["key"], m["key"], m["title"], m["publisher"], m["link"],
                 m["time"], m["day"], now(), 1 if backfilled else 0))
            new += cur.rowcount
        time.sleep(PAUSE_GOOGLE_S)
    c.commit()
    return new


def fetch_live() -> None:
    with closing(db.connect()) as c:
        for f in companies():
            for day in (today() - timedelta(days=1), today()):
                try:
                    query_day(c, f, day, backfilled=False)
                except Exception as e:  # noqa: BLE001
                    STATE["error"] = f"{f['name']}: {e}"
        db.put(c, "signals_fetch", now())
        c.commit()
    STATE["last_fetch"] = now()


def backfill() -> bool:
    """One day per company per call, backwards to `backfill_from`. True while something is missing."""
    start = backfill_from()
    missing = False
    with closing(db.connect()) as c:
        for f in companies():
            key = f"signals_backfilled_{f['key']}"
            done = db.get(c, key, "")
            day = (date.fromisoformat(done) - timedelta(days=1)) if done else today() - timedelta(days=2)
            limit = max(start, today() - timedelta(days=int(f["backfill_days"]))) if f.get("backfill_days") else start
            if day < limit:
                continue
            missing = True
            try:
                query_day(c, f, day, backfilled=True)
                db.put(c, key, day.isoformat())
                c.commit()
                STATE["backfill"] = f"{f['name']} {day.isoformat()}"
            except Exception as e:  # noqa: BLE001
                STATE["error"] = f"Backfill {f['name']}: {e}"
    return missing


def _prices_yahoo(f: dict) -> list[tuple[str, float, str]]:
    code, b = fetch.raw(YAHOO.format(symbol=urllib.parse.quote(f["quote"])))
    if code != 200:
        raise RuntimeError(f"Yahoo HTTP {code}")
    from zoneinfo import ZoneInfo
    r = json.loads(b)["chart"]["result"][0]
    zone, cur = r["meta"].get("exchangeTimezoneName") or "UTC", r["meta"].get("currency") or ""
    return [(datetime.fromtimestamp(ts, ZoneInfo(zone)).date().isoformat(), float(cl), cur)
            for ts, cl in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if cl is not None]


def _prices_nasdaq(f: dict) -> list[tuple[str, float, str]]:
    start = (today() - timedelta(days=370)).isoformat()
    url = (f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(f['ticker'])}/historical?assetclass=stocks"
           f"&fromdate={start}&todate={today().isoformat()}&limit=400")
    code, b = fetch.raw(url, extra_headers={"Accept": "application/json, text/plain, */*",
                                            "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    if code != 200:
        raise RuntimeError(f"Nasdaq HTTP {code}")
    d = json.loads(b)
    out = []
    for z in ((d.get("data") or {}).get("tradesTable") or {}).get("rows") or []:
        m, t, y = z["date"].split("/")
        out.append((f"{y}-{m}-{t}", float(z["close"].replace("$", "").replace(",", "")), "USD"))
    if not out:
        raise RuntimeError("Nasdaq returned no prices")
    return out


def _prices_euronext(f: dict) -> list[tuple[str, float, str]]:
    start = (today() - timedelta(days=370)).isoformat()
    url = (f"https://live.euronext.com/en/ajax/AwlHistoricalPrice/getFullDownloadAjax/{f['euronext']}?format=csv"
           f"&decimal_separator=.&date_form=d/m/Y&op=&adjusted=Y&base100=&startdate={start}&enddate={today().isoformat()}")
    code, b = fetch.raw(url)
    if code != 200:
        raise RuntimeError(f"Euronext HTTP {code}")
    out = []
    for z in b.decode("utf-8-sig", errors="replace").splitlines():
        t = z.split(";")
        if len(t) > 5 and re.fullmatch(r"\d{2}/\d{2}/\d{4}", t[0]):
            d, m, y = t[0].split("/")
            try:
                out.append((f"{y}-{m}-{d}", float(t[5]), "EUR"))
            except ValueError:
                pass
    if not out:
        raise RuntimeError("Euronext returned no prices")
    return out


def fetch_prices() -> None:
    """Closing prices: Yahoo first, else Nasdaq (US) or Euronext.

    Yahoo answers with 429 after a few calls. A block is not circumvented;
    EARN.iq switches to another public source instead."""
    failed = []
    with closing(db.connect()) as c:
        for f in companies():
            if not (f.get("quote") or f.get("ticker")):
                continue          # no ticker (e.g. fresh from Discover), no price
            f = {**f, "quote": f.get("quote") or f.get("ticker")}
            attempts = []
            ways = [("Yahoo", _prices_yahoo)] + ([("Euronext", _prices_euronext)] if f.get("euronext") else [("Nasdaq", _prices_nasdaq)])
            for name, way in ways:
                try:
                    prices = way(f)
                except Exception as e:  # noqa: BLE001
                    attempts.append(f"{name}: {e}")
                    time.sleep(2)
                    continue
                for day, close, cur in prices:
                    if day >= today().isoformat():
                        continue          # trading day still open: no close yet
                    c.execute("INSERT INTO prices(company, day, close, currency) VALUES(?,?,?,?) "
                              "ON CONFLICT(company, day) DO UPDATE SET close=excluded.close, currency=excluded.currency",
                              (f["key"], day, round(close, 4), cur))
                c.commit()
                break
            else:
                failed.append(f"{f['name']} ({'; '.join(attempts)})")
            time.sleep(3)
        db.put(c, "signals_prices", now())
        c.commit()
    if failed:
        STATE["error"] = "Prices: " + "; ".join(failed)[:300]


def fetch_earnings_dates() -> None:
    """Past earnings dates from the SEC: 8-K filings with Item 2.02 (Results of Operations).

    Needs EARNIQ_SEC_USER_AGENT; the SEC rejects undeclared tools."""
    with closing(db.connect()) as c:
        if config.sec_user_agent():
            for f in companies():
                if not f.get("cik"):
                    continue
                code, b = fetch.raw(SEC.format(cik=f["cik"]))
                if code != 200:
                    continue
                ns = {"a": "http://www.w3.org/2005/Atom"}
                try:
                    for e in ET.fromstring(b).findall("a:entry", ns):
                        content = ET.tostring(e, encoding="unicode")
                        if "2.02" not in content:
                            continue
                        m = re.search(r"<(?:\w+:)?filing-date>(\d{4}-\d{2}-\d{2})<", content) or \
                            re.search(r"(\d{4}-\d{2}-\d{2})T", e.findtext("a:updated", "", ns))
                        if m:
                            c.execute("INSERT OR IGNORE INTO earnings(company, day, kind, source) VALUES(?,?,?,?)",
                                      (f["key"], m.group(1), "Results", "SEC 8-K 2.02"))
                except ET.ParseError:
                    continue
                time.sleep(1)
        db.put(c, "signals_earnings", now())
        c.commit()


# ── Scoring ──────────────────────────────────────────────────────────────
SYSTEM = """You score news headlines about a listed company from the point of view of an investor who wants to know whether sentiment ahead of the next quarterly results is leaning positive or negative.

For each headline:
- s (sentiment, -2 to 2): how does the item affect expectations for the business and the stock? 2 clearly positive (guidance raised, strong demand, large orders, price target up), 1 slightly positive, 0 neutral or mixed, -1 slightly negative, -2 clearly negative (profit warning, weak demand, export ban, quantified legal risk, price target down).
- r (relevance, 0 to 3): 0 not relevant (a different company or product with a similar name, how-to guides, advertising, customer showcases), 1 background or a plain stock-price report, 2 a real signal about the business, 3 a strong signal with numbers, or from the company itself, a regulator or a major customer.
- t (topic): one of guidance, analysts, demand, supply, order, partnership, product, regulation, management, legal, price, other.

Scale: these companies turn over billions per quarter. An order below USD 1 billion is routine: at most s=1 and r=2. Development or phase-1 contracts without an amount: at most s=1 and r=1. The same story from several outlets gets the same score every time.

Score only what the headline says. Do not add anything. Answer ONLY with JSON in exactly this form:
{"m":[{"i":1,"s":0,"r":1,"t":"price"}]}"""

TOPICS = {"guidance", "analysts", "demand", "supply", "order", "partnership", "product",
          "regulation", "management", "legal", "price", "other"}


def score_headlines() -> int:
    """One model call for up to PER_CALL unscored headlines of one company. Returns the number scored."""
    with closing(db.connect()) as c:
        # Live capture before the archive, new before old
        r = c.execute("SELECT company FROM headlines WHERE sentiment IS NULL "
                      "ORDER BY backfilled, time DESC LIMIT 1").fetchone()
        if not r:
            return 0
        key = r["company"]
        f = next((x for x in companies() if x["key"] == key), {"name": key, "ticker": ""})
        rows = c.execute("SELECT id, title, publisher, day FROM headlines WHERE company=? AND sentiment IS NULL "
                         "ORDER BY backfilled, time DESC LIMIT ?", (key, PER_CALL)).fetchall()
        listing = "\n".join(f"{i}. [{z['day']} · {z['publisher'] or '?'}] {z['title']}" for i, z in enumerate(rows, 1))
        user = (f"Company: {f['name']} ({f.get('ticker', '')}). {f.get('context', '')}\n\n"
                f"Headlines:\n{listing}")
        STATE["step"] = f"scoring {len(rows)} headlines on {f['name']}"
        try:
            answer, model, finish = llm.chat(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                max_tokens=40 * len(rows) + 200, temperature=0.0)
        except Exception as e:  # noqa: BLE001 - model offline, key, timeout
            STATE["error"] = f"Model: {type(e).__name__}"
            return 0
        d = llm.json_from(answer)
        n = 0
        for e in (d.get("m") or []) if isinstance(d, dict) else []:
            try:
                i, s, rel = int(e.get("i")), int(e.get("s")), int(e.get("r"))
            except (TypeError, ValueError):
                continue
            if not (1 <= i <= len(rows)) or not (-2 <= s <= 2) or not (0 <= rel <= 3):
                continue
            t = str(e.get("t") or "").strip().lower()
            c.execute("UPDATE headlines SET sentiment=?, relevance=?, topic=?, model=?, scored=? WHERE id=?",
                      (s, rel, t if t in TOPICS else "other", llm.model_label(model), now(), rows[i - 1]["id"]))
            n += 1
        c.commit()
        STATE["last_scoring"] = now()
        if n == 0:
            STATE["error"] = "model answer without a usable score" + (" (cut off)" if finish == "length" else "")
        return n


# ── Aggregation for the interface ────────────────────────────────────────
ARTICLE_WEIGHT = "(CASE WHEN article_id IS NULL THEN 1.0 ELSE 1.5 END)"   # a read article counts 1.5x


def _days(start: date, end: date):
    t = start
    while t <= end:
        yield t
        t += timedelta(days=1)


def net(sum_sr: float, sum_r: float) -> float | None:
    """-100..100: relevance-weighted mean sentiment of the relevant headlines."""
    return round(50 * sum_sr / sum_r, 1) if sum_r else None


def history(key: str, days: int = 30) -> dict:
    f = next((x for x in companies() if x["key"] == key), None)
    if not f:
        return {}
    end = today()
    start = end - timedelta(days=days - 1)
    lead = start - timedelta(days=6)                 # lead-in for the moving average
    with closing(db.connect()) as c:
        raw = c.execute(f"""SELECT day,
              COUNT(*) n,
              SUM(sentiment IS NULL) open,
              SUM(relevance=0) irrelevant,
              SUM(relevance>=1 AND sentiment>0) pos,
              SUM(relevance>=1 AND sentiment=0) neu,
              SUM(relevance>=1 AND sentiment<0) neg,
              SUM(CASE WHEN relevance>=1 THEN sentiment*relevance*{ARTICLE_WEIGHT} ELSE 0 END) sr,
              SUM(CASE WHEN relevance>=1 THEN relevance*{ARTICLE_WEIGHT} ELSE 0 END) r,
              MAX(backfilled) backfilled
            FROM headlines WHERE company=? AND day>=? AND day<=? GROUP BY day""",
                        (key, lead.isoformat(), end.isoformat())).fetchall()
        per = {x["day"]: dict(x) for x in raw}
        prices = {x["day"]: x["close"] for x in c.execute(
            "SELECT day, close FROM prices WHERE company=? AND day>=? ORDER BY day", (key, (start - timedelta(days=40)).isoformat()))}
        cur = (c.execute("SELECT currency FROM prices WHERE company=? LIMIT 1", (key,)).fetchone() or {"currency": ""})["currency"]
        events = [dict(x) for x in c.execute("SELECT day, kind, source FROM earnings WHERE company=? ORDER BY day", (key,))]
    live = live_since()
    empty = {"n": 0, "open": 0, "irrelevant": 0, "pos": 0, "neu": 0, "neg": 0, "sr": 0, "r": 0, "backfilled": 0}
    series, window = [], []
    for t in _days(lead, end):
        d = {**empty, **per.get(t.isoformat(), {})}
        window = (window + [(d["sr"] or 0, d["r"] or 0)])[-7:]
        if t < start:
            continue
        series.append({"day": t.isoformat(), "n": d["n"], "pos": d["pos"] or 0, "neu": d["neu"] or 0,
                       "neg": d["neg"] or 0, "irrelevant": d["irrelevant"] or 0, "open": d["open"] or 0,
                       "net": net(d["sr"] or 0, d["r"] or 0),
                       "net7": net(sum(a for a, _ in window), sum(b for _, b in window)),
                       "price": prices.get(t.isoformat()),
                       "archive": bool(live and t.isoformat() < live)})
    return {"company": f["key"], "name": f["name"], "ticker": f.get("ticker", ""), "currency": cur,
            "live_since": live, "series": series, "earnings": events,
            "next": next((t for t in f.get("next", []) if t["day"] >= end.isoformat()), None),
            "recap": recap(key, events, prices)}


def recap(key: str, events: list, prices: dict) -> list:
    """Per past earnings date: sentiment of the 14 days before vs the price reaction (day before -> day after)."""
    days_with_price = sorted(prices)
    out = []
    with closing(db.connect()) as c:
        for t in events:
            day = t["day"]
            if day >= today().isoformat():
                continue
            before = [d for d in days_with_price if d < day]
            after = [d for d in days_with_price if d > day]
            if not before or not after:
                continue
            p0, p1 = prices[before[-1]], prices[after[0]]
            start = (date.fromisoformat(day) - timedelta(days=14)).isoformat()
            s = c.execute(f"""SELECT COUNT(*) n, SUM(relevance>=1) nr, SUM(sentiment IS NULL) open,
                    SUM(CASE WHEN relevance>=1 THEN sentiment*relevance*{ARTICLE_WEIGHT} ELSE 0 END) sr,
                    SUM(CASE WHEN relevance>=1 THEN relevance*{ARTICLE_WEIGHT} ELSE 0 END) r
                    FROM headlines WHERE company=? AND day>=? AND day<?""", (key, start, day)).fetchone()
            if not s["n"]:
                continue
            n14 = net(s["sr"] or 0, s["r"] or 0)
            reaction = round(100 * (p1 / p0 - 1), 2)
            out.append({"day": day, "kind": t.get("kind", ""), "headlines": s["n"], "relevant": s["nr"] or 0,
                        "open": s["open"] or 0, "net14": n14, "reaction": reaction,
                        "from_day": before[-1], "to_day": after[0],
                        "match": None if n14 is None or abs(n14) < 5 else (n14 > 0) == (reaction > 0)})
    return out


def overview() -> dict:
    out = []
    for f in companies():
        v = history(f["key"], 28)
        r = v.get("series", [])
        w1, w0 = r[-7:], r[-14:-7]

        def total(w, k):
            return sum(x[k] for x in w)

        def week_net(w):
            n = [x for x in w if x["net"] is not None]
            # mean of the daily values, weighted with the number of relevant headlines
            g = sum(x["pos"] + x["neu"] + x["neg"] for x in n)
            return round(sum(x["net"] * (x["pos"] + x["neu"] + x["neg"]) for x in n) / g, 1) if g else None

        out.append({"company": f["key"], "name": f["name"], "ticker": f.get("ticker", ""), "group": f.get("group", "portfolio"),
                    "next": v.get("next"), "headlines7": total(w1, "n"), "headlines7_prev": total(w0, "n"),
                    "net7": week_net(w1), "net7_prev": week_net(w0),
                    "trend": [{"day": x["day"], "net7": x["net7"]} for x in r],
                    "last_recap": (v.get("recap") or [None])[-1]})
    with closing(db.connect()) as c:
        z = c.execute("SELECT COUNT(*) n, SUM(sentiment IS NULL) open, MIN(day) since FROM headlines").fetchone()
    return {"companies": out, "state": {**STATE, "headlines": z["n"], "open": z["open"] or 0, "since": z["since"],
                                        "live_since": live_since()}}


def headlines_for_day(key: str, day: str) -> list:
    with closing(db.connect()) as c:
        return [dict(x) for x in c.execute(
            "SELECT title, publisher, link, time, sentiment, relevance, topic, backfilled, key_point, article_id FROM headlines "
            "WHERE company=? AND day=? ORDER BY COALESCE(relevance,-1) DESC, ABS(COALESCE(sentiment,0)) DESC, time DESC",
            (key, day))]


# ── Managing companies (Sources tab) ─────────────────────────────────────
_CONFIG_LOCK = threading.Lock()


def _write_config(k: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(k, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG)


def slug(name: str) -> str:
    s = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("é", "e"), ("è", "e")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "company"


def _check_dates(items) -> list[dict]:
    out = []
    for t in items or []:
        day = str(t.get("day", "")).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError(f"Date '{day}' is not in the format YYYY-MM-DD")
        e = {"day": day, "kind": str(t.get("kind", "")).strip()[:80]}
        if "confirmed" in t:
            e["confirmed"] = bool(t["confirmed"])
        if t.get("source"):
            e["source"] = str(t["source"])[:120]
        out.append(e)
    return sorted(out, key=lambda x: x["day"])


def save_company(d: dict, old: str | None = None) -> dict:
    """Add a company (old=None) or replace it. Validates, writes atomically, wakes the runner."""
    name = str(d.get("name", "")).strip()[:60]
    if not name:
        raise ValueError("Name is missing")
    searches = []
    for s in d.get("searches") or []:
        q = str(s.get("q", "")).strip()
        if not q:
            continue
        hl, gl, lang = LANGUAGES.get(str(s.get("lang", "en")).strip().lower()[:2], LANGUAGES["en"])
        searches.append({"q": q[:400], "hl": hl, "gl": gl, "lang": lang})
    if not searches:
        raise ValueError("Add at least one search")
    group = str(d.get("group", "portfolio"))
    group = group if group in GROUPS else "portfolio"
    f = {"key": old or slug(name), "name": name, "group": group,
         "ticker": str(d.get("ticker", "")).strip().upper()[:20],
         "quote": str(d.get("quote", "") or d.get("ticker", "")).strip()[:20],
         "context": str(d.get("context", "")).strip()[:400], "searches": searches,
         "next": _check_dates(d.get("next")), "past": _check_dates(d.get("past"))}
    cik = re.sub(r"\D", "", str(d.get("cik", "")))
    if cik:
        f["cik"] = cik.zfill(10)
    if str(d.get("euronext", "")).strip():
        f["euronext"] = str(d["euronext"]).strip()[:40]
    with _CONFIG_LOCK:
        k = load_config()
        fs = k.setdefault("companies", [])
        bing = [str(x).strip()[:200] for x in (d.get("bing") or []) if str(x).strip()]
        if not bing and old:
            bing = next((x.get("bing", []) for x in fs if x["key"] == old), [])
        if bing:
            f["bing"] = bing
        old_f = next((x for x in fs if x["key"] == old), {}) if old else {}
        for field in ("backfill_days", "since"):          # auto-watch: shorter archive, start date
            if d.get(field) or old_f.get(field):
                f[field] = d.get(field) or old_f.get(field)
        if old is None:
            if any(x["key"] == f["key"] for x in fs):
                raise ValueError(f"'{name}' already exists")
            fs.append(f)
        else:
            i = next((i for i, x in enumerate(fs) if x["key"] == old), None)
            if i is None:
                raise KeyError(old)
            fs[i] = f
        _write_config(k)
    with closing(db.connect()) as c:
        # Reset dates from the configuration, keep the ones from the SEC
        c.execute("DELETE FROM earnings WHERE company=? AND source<>'SEC 8-K 2.02'", (f["key"],))
        for t in f["past"]:
            c.execute("INSERT OR IGNORE INTO earnings(company, day, kind, source) VALUES(?,?,?,?)",
                      (f["key"], t["day"], t.get("kind", ""), t.get("source", "config")))
        if old is None or d.get("_refresh_prices"):
            c.execute("DELETE FROM settings WHERE key IN ('signals_prices','signals_earnings')")
        c.commit()
    WAKE.set()
    return f


def remove_company(key: str) -> None:
    """Removes the company from the configuration. Collected headlines stay in the database."""
    with _CONFIG_LOCK:
        k = load_config()
        before = len(k.get("companies", []))
        k["companies"] = [x for x in k.get("companies", []) if x["key"] != key]
        if len(k["companies"]) == before:
            raise KeyError(key)
        _write_config(k)


WATCHLIST_HEADER = "Watchlist with next earnings date:"


def watchlist_text() -> str:
    """Block for the criteria of the earnings monitor; rewritten on every change."""
    lines = [WATCHLIST_HEADER]
    for f in companies():
        if f.get("group") == "auto":
            continue
        n = next((t for t in f.get("next", []) if t["day"] >= today().isoformat()), None)
        if n:
            when = f"{n.get('kind') or 'results'} on {n['day']}" + ("" if n.get("confirmed", True) else " (estimated)")
        else:
            when = "date not announced yet"
        lines.append(f"- {f['name']} ({f.get('ticker', '')}): {when}")
    return "\n".join(lines)


def companies_with_stats() -> list[dict]:
    with closing(db.connect()) as c:
        st = {r["company"]: dict(r) for r in c.execute(
            "SELECT company, COUNT(*) n, SUM(sentiment IS NULL) open, MAX(time) latest FROM headlines GROUP BY company")}
        past = {}
        for r in c.execute("SELECT company, day, kind, source FROM earnings WHERE day<? ORDER BY day DESC", (today().isoformat(),)):
            past.setdefault(r["company"], []).append(dict(r))
    out = []
    for f in companies():
        s = st.get(f["key"], {})
        out.append({**f, "headlines": s.get("n", 0), "open": s.get("open") or 0, "latest": s.get("latest"),
                    "past_events": past.get(f["key"], [])[:4]})
    return out


# ── Runner ───────────────────────────────────────────────────────────────
def worker() -> None:
    from . import alerts, articles, discovery, trends
    time.sleep(20)
    while True:
        try:
            with closing(db.connect()) as c:
                do_fetch = is_due(c, "signals_fetch", FETCH_EVERY_MIN / 60)
                do_prices = is_due(c, "signals_prices", PRICES_EVERY_H)
                do_earnings = is_due(c, "signals_earnings", EARNINGS_EVERY_H)
            if do_fetch:
                STATE["step"] = "fetching headlines"
                fetch_live()
            if do_prices:
                STATE["step"] = "fetching prices"
                fetch_prices()
            if do_earnings:
                STATE["step"] = "fetching earnings dates"
                fetch_earnings_dates()
            try:
                discovery.check_ticker()     # own throttle: at most one per minute
            except Exception as e:  # noqa: BLE001
                STATE["error"] = f"Ticker: {type(e).__name__}: {e}"
            if score_headlines():
                time.sleep(PAUSE_MODEL_S)
                continue
            worked = False
            for step in (articles.step, discovery.step):
                try:
                    worked = step() or worked
                except Exception as e:  # noqa: BLE001 - extras must not stop the signals
                    STATE["error"] = f"{step.__module__}: {type(e).__name__}: {e}"
            for step in (alerts.check_events, trends.step):
                try:
                    step()
                except Exception as e:  # noqa: BLE001
                    STATE["error"] = f"{step.__module__}: {type(e).__name__}: {e}"
            if worked:
                time.sleep(PAUSE_MODEL_S)
                continue
            STATE["step"] = "backfilling archive"
            if backfill():
                continue
            STATE["step"] = "waiting"
            WAKE.wait(60)      # short, so the ticker check (1 per minute) keeps moving
            WAKE.clear()
        except Exception as e:  # noqa: BLE001 - the thread must not die
            STATE["error"] = f"{type(e).__name__}: {e}"
            time.sleep(60)


def start() -> None:
    init()
    threading.Thread(target=worker, daemon=True, name="earniq-signals").start()
