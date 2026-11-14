"""Discover: companies outside your watchlist that stand out in the news.

Candidates come from two places:
  (a) companies mentioned in articles that were read for your watchlist, and
  (b) broad market searches ("raises full-year guidance", "hebt Prognose an" ...).

The model names the company and the direction. Whether it is a listed company,
and which ticker, is checked by code against the Yahoo search (Nasdaq as a
fallback), not by the model.
"""
from __future__ import annotations

import difflib
import json
import math
import re
import time
import urllib.parse
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import articles, db, fetch, llm, signals

DEFAULT_MARKET_SEARCHES = [
    ("raises full-year guidance", "en-US"), ("raises outlook shares", "en-US"),
    ("record quarterly revenue shares", "en-US"), ("record order backlog", "en-US"),
    ("beats estimates raises forecast", "en-US"), ("wins contract billion", "en-US"),
    ("hebt Prognose an Aktie", "de-DE"), ("Rekordumsatz Aktie", "de-DE"), ("Auftragseingang Rekord", "de-DE"),
    ("relève ses prévisions", "fr-FR"), ("chiffre d'affaires record action", "fr-FR"),
]
MARKET_EVERY_H = 3
MAIN_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "GER", "PAR", "AMS", "LSE", "MIL", "MCE", "SWX", "EBS",
                  "TAI", "TWO", "TYO", "JPX", "HKG", "SHH", "SHZ", "KSC", "KOE", "STO", "CPH", "HEL", "OSL", "BRU",
                  "LIS", "VIE", "TOR", "ASX", "NSI", "BSE", "SAO", "IOB", "FRA", "XETRA"}
LEGAL_FORMS = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|ag|se|sa|s\.a|nv|n\.v|"
                         r"spa|s\.p\.a|ab|asa|oyj|holdings?|group|gruppe|groupe|kgaa|gmbh|the)\b\.?", re.I)
FILLER = {"motor", "motors", "industries", "industrie", "international", "worldwide", "a", "s", "as", "asa", "b", "n", "v",
          "de", "der", "global", "aktiengesellschaft", "kgaa", "sca", "nv", "oyj", "plc", "inc", "spa", "adr", "ads"}

STATE = {"market": ""}


def name_key(name: str) -> str:
    n = LEGAL_FORMS.sub(" ", (name or "").lower())
    n = re.sub(r"[^a-z0-9äöüéèàç&]+", " ", n).strip()
    return re.sub(r"\s+", "-", n)[:60]


def company_for(key: str) -> str | None:
    """Does the name belong to one of your companies? Then its key."""
    for f in signals.companies():
        if key in (name_key(f["name"]), name_key(f.get("ticker", ""))):
            return f["key"]
    return None


def is_own(key: str) -> bool:
    """Portfolio or watchlist. Auto-watched companies stay candidates in Discover."""
    f = company_for(key)
    return bool(f) and next((x.get("group", "portfolio") for x in signals.companies() if x["key"] == f), "portfolio") != "auto"


def _market_searches() -> list[tuple[str, str]]:
    custom = signals.load_config().get("market_searches")
    if custom:
        return [(x["q"], x.get("mkt", "en-US")) for x in custom]
    return DEFAULT_MARKET_SEARCHES


def fetch_market() -> None:
    limit = (signals.today() - timedelta(days=2)).isoformat()
    with closing(db.connect()) as c:
        for q, mkt in _market_searches():
            try:
                for a in articles.bing(q, mkt):
                    if a["day"] < limit:
                        continue
                    c.execute("INSERT OR IGNORE INTO articles(company, url, title, publisher, description, time, day, captured) "
                              "VALUES('_market',?,?,?,?,?,?,?)", (a["url"], a["title"], a["publisher"], a["description"],
                                                                   a["time"], a["day"], signals.now()))
            except Exception as e:  # noqa: BLE001
                STATE["market"] = f"Bing '{q}': {e}"
            time.sleep(2)
        db.put(c, "signals_market", signals.now())
        c.commit()


SYSTEM_MARKET = """You get headlines with a short description from the business press. For each item, find the listed company it is about and whether the item is positive or negative for its business.

Answer ONLY with JSON: {"m":[{"i":1,"company":"name or null","direction":1|0|-1,"signal":"guidance|record|order|analysts|acquisition|other","reason":"short, with the number if one is given"}]}
- company: null if it is not a single listed company (industry reports, countries, private companies, funds).
- Only what is in the title or description. Do not add anything."""


def score_market() -> bool:
    with closing(db.connect()) as c:
        rows = c.execute("SELECT id, company, url, title, description, publisher, time, day FROM articles "
                         "WHERE company='_market' AND state='new' ORDER BY time DESC LIMIT 20").fetchall()
        if not rows:
            return False
        listing = "\n".join(f"{i}. {z['title']} | {z['description'][:300]}" for i, z in enumerate(rows, 1))
        try:
            answer, _model, _finish = llm.chat([{"role": "system", "content": SYSTEM_MARKET},
                                                {"role": "user", "content": listing}],
                                               max_tokens=60 * len(rows) + 200, temperature=0.0)
        except Exception as e:  # noqa: BLE001
            STATE["market"] = f"Model: {type(e).__name__}"
            return False
        d = llm.json_from(answer)
        done = set()
        for e in (d.get("m") or []) if isinstance(d, dict) else []:
            try:
                i = int(e.get("i"))
            except (TypeError, ValueError):
                continue
            if not 1 <= i <= len(rows):
                continue
            z = rows[i - 1]
            done.add(z["id"])
            name = str(e.get("company") or "").strip()
            if not name or name.lower() in ("null", "none"):
                continue
            k = name_key(name)
            if not k:
                continue
            try:
                direction = max(-1, min(1, int(e.get("direction", 0))))
            except (TypeError, ValueError):
                direction = 0
            reason = str(e.get("reason") or "")[:200]
            own = company_for(k)
            if own:
                # An item about one of your companies: into its signals, as a medium-relevance signal
                c.execute("INSERT OR IGNORE INTO headlines(company, key, title, publisher, link, time, day, captured, backfilled, "
                          "sentiment, relevance, topic, key_point, scored) VALUES(?,?,?,?,?,?,?,?,0,?,2,'other',?,?)",
                          (own, f"market:{z['url'][:280]}", z["title"], z["publisher"], z["url"], z["time"], z["day"],
                           signals.now(), direction, reason, signals.now()))
                if is_own(k):
                    continue
            c.execute("INSERT OR IGNORE INTO mentions(key, name, direction, signal, reason, origin, publisher, url, title, day, captured) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (k, name[:80], direction, str(e.get("signal") or "")[:20],
                                                        reason, "market", z["publisher"], z["url"], z["title"], z["day"], signals.now()))
        for z in rows:
            c.execute("UPDATE articles SET state=? WHERE id=?", ("scored" if z["id"] in done else "skipped", z["id"]))
        c.commit()
    return True


def names_match(a: str, b: str) -> bool:
    """Accept a ticker hit only if the company name fits the name from the source."""
    x, y = name_key(a), name_key(b)
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) < len(y) else (y, x)
    if long_.startswith(short + "-"):
        # suffix made only of filler words ("Toyota Motor", "Novo Nordisk A/S"), not "Siemens Energy"
        return set(long_[len(short) + 1:].split("-")) <= FILLER
    # only near-identical spellings (typos, umlauts); "continental" is not "tri-continental"
    return difflib.SequenceMatcher(None, x, y).ratio() >= 0.92


_NEXT_TICKER_CHECK = [0.0]


def _nasdaq_search(name: str) -> list[dict] | None:
    """Fallback for the Yahoo search. Hits in Yahoo format, or None if Nasdaq does not answer.

    An empty list means Nasdaq does not know the name (then the check stays open for Yahoo)."""
    url = "https://api.nasdaq.com/api/autocomplete/slookup/10?search=" + urllib.parse.quote(name)
    code, b = fetch.raw(url, timeout=20, extra_headers={"Accept": "application/json, text/plain, */*",
                                                        "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    if code != 200:
        return None
    try:
        data = json.loads(b).get("data") or []
    except ValueError:
        return None
    out = []
    for x in data:
        exch = str(x.get("exchange") or "")
        if x.get("asset") not in (None, "", "STOCKS") or "ETF" in str(x.get("name", "")).upper():
            continue
        out.append({"symbol": x.get("symbol", ""), "exchange": "NMS" if "NASDAQ" in exch else "NYQ" if "NYSE" in exch else exch,
                    "exchDisp": exch,
                    "longname": re.sub(r"\s+(Common Stock|Ordinary Shares|American Depositary.*|ADS.*|Class [A-Z].*)$", "", str(x.get("name", ""))),
                    "quoteType": "EQUITY"})
    return out


def check_ticker() -> bool:
    """Map one candidate with at least two signals to a ticker via the Yahoo search.

    At most one per minute; if Yahoo answers 429 and Nasdaq does not help, pause 30 minutes."""
    if time.time() < _NEXT_TICKER_CHECK[0]:
        return False
    _NEXT_TICKER_CHECK[0] = time.time() + 60
    with closing(db.connect()) as c:
        r = c.execute("""SELECT m.key, MAX(m.name) name, COUNT(*) n FROM mentions m
                         LEFT JOIN candidates k ON k.key=m.key
                         WHERE k.key IS NULL OR (k.checked IS NULL AND k.status='new' AND (k.attempted IS NULL OR k.attempted<?))
                         GROUP BY m.key HAVING COUNT(*)>=2 ORDER BY COUNT(*) DESC LIMIT 1""",
                      ((datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S"),)).fetchone()
        if not r:
            return False

        def attempted():
            c.execute("INSERT OR IGNORE INTO candidates(key, name, created) VALUES(?,?,?)", (r["key"], r["name"], signals.now()))
            c.execute("UPDATE candidates SET attempted=? WHERE key=?", (signals.now(), r["key"]))
            c.commit()

        url = "https://query2.finance.yahoo.com/v1/finance/search?" + urllib.parse.urlencode({"q": r["name"], "quotesCount": 6, "newsCount": 0})
        code, b = fetch.raw(url)
        if code == 200:
            quotes = [q for q in (json.loads(b).get("quotes") or []) if q.get("quoteType") == "EQUITY"]
        else:
            quotes = _nasdaq_search(r["name"])
            if quotes is None:
                STATE["market"] = f"ticker search HTTP {code}, next attempt in 30 min"
                _NEXT_TICKER_CHECK[0] = time.time() + 1800
                attempted()
                return False
        fitting = [q for q in quotes if names_match(r["name"], q.get("longname") or q.get("shortname") or "")]
        if code != 200 and not fitting:
            attempted()
            STATE["market"] = f"ticker: {r['name']} not found at Nasdaq, Yahoo blocked"
            return True
        main = [q for q in fitting if q.get("exchange") in MAIN_EXCHANGES and q.get("exchange") not in ("FRA", "IOB")] or fitting
        q = main[0] if main else None
        c.execute("INSERT INTO candidates(key, name, ticker, exchange, long_name, status, checked, created) VALUES(?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET ticker=excluded.ticker, exchange=excluded.exchange, long_name=excluded.long_name, "
                  "status=CASE WHEN candidates.status='new' THEN excluded.status ELSE candidates.status END, checked=excluded.checked",
                  (r["key"], r["name"], (q or {}).get("symbol", ""), (q or {}).get("exchDisp") or (q or {}).get("exchange", ""),
                   ((q or {}).get("longname") or (q or {}).get("shortname") or "").strip()[:80],
                   "new" if q else "unlisted", signals.now(), signals.now()))
        c.commit()
    return True


def candidates(days: int = 14, status: str = "new") -> dict:
    """Candidates from the news of the last `days`.

    At least two positive signals from at least two outlets, more positive than
    negative, and only if a matching listed security was found (or the check is
    still pending). Private companies drop out."""
    since = (signals.today() - timedelta(days=days - 1)).isoformat()
    with closing(db.connect()) as c:
        news = {r["key"]: dict(r) for r in c.execute("""SELECT key, MAX(name) name, SUM(direction>0) pos, SUM(direction<0) neg,
                COUNT(*) n, COUNT(DISTINCT publisher) outlets, COUNT(DISTINCT day) days, MAX(day) latest,
                GROUP_CONCAT(DISTINCT signal) signals
              FROM mentions WHERE day>=? GROUP BY key""", (since,))}
        master = {r["key"]: dict(r) for r in c.execute("SELECT * FROM candidates")}
        out = []
        for k, n in news.items():
            if is_own(k):
                continue
            m = master.get(k, {})
            st = m.get("status") or "new"
            if st != status and not (status == "new" and st == "unlisted"):
                continue
            listed = bool(m.get("ticker")) and st != "unlisted"
            pending = not m.get("checked")
            news_ok = n["pos"] >= 2 and n["outlets"] >= 2 and n["pos"] > n["neg"]
            value = (n["pos"] - n["neg"]) * math.log2(1 + n["outlets"]) * (1 + 0.2 * min(n["days"], 5))
            d = {"key": k, "name": n["name"], "ticker": m.get("ticker", ""), "exchange": m.get("exchange", ""),
                 "long_name": m.get("long_name", ""), "status": st, "checked": not pending,
                 "pos": n["pos"], "neg": n["neg"], "outlets": n["outlets"], "signals": n["signals"] or "",
                 "latest": n["latest"] or "", "value": round(value, 2)}
            d["candidate"] = news_ok and (listed or pending)
            out.append(d)
        out.sort(key=lambda x: (not x["candidate"], -x["value"]))
        for d in out[:80]:
            d["evidence"] = [dict(x) for x in c.execute(
                "SELECT direction, signal, reason, origin, publisher, url, title, day FROM mentions WHERE key=? AND day>=? "
                "ORDER BY day DESC, id DESC LIMIT 6", (d["key"], since))]
    return {"candidates": [x for x in out if x["candidate"]], "single": [x for x in out if not x["candidate"]][:40],
            "state": {**STATE, **articles.STATE}}


def set_candidate(key: str, status: str) -> dict:
    with closing(db.connect()) as c:
        r = c.execute("SELECT MAX(name) name FROM mentions WHERE key=?", (key,)).fetchone()
        if not r or not r["name"]:
            raise KeyError(key)
        c.execute("INSERT INTO candidates(key, name, status, created) VALUES(?,?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET status=excluded.status", (key, r["name"], status, signals.now()))
        c.commit()
        return dict(c.execute("SELECT * FROM candidates WHERE key=?", (key,)).fetchone())


def step() -> bool:
    with closing(db.connect()) as c:
        due = signals.is_due(c, "signals_market", MARKET_EVERY_H)
    if due:
        fetch_market()
    return score_market() or due

