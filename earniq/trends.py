"""Sentiment trends: report when the news flow around a stock turns.

Auto-watch: the strongest candidates from Discover (at most 20, only with a
ticker) get a lean time series automatically: Google News headlines and their
sentiment, no article reading, archive only 14 days back. Anything that
delivers fewer than three headlines in 30 days, or gets hidden, drops out
again. "Watch" turns an auto-watched company into a regular one.

Trend rule, for every group (portfolio, watchlist, auto):
  sentiment of the last 7 days differs by at least 25 points from the 21 days
  before, with at least 10 relevant headlines in the 7 days, and volume at
  least 1.5x the level before. Volume is only compared on live-captured days:
  the archive thins out with age and would fake every increase.
  Without 14 live baseline days yet, the stricter rule without volume applies:
  at least 35 points and 15 headlines.
At most one trend per company and direction in 7 days. Every trend goes out
as an alert (counts towards the daily alert limit).
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta

from . import alerts, db, discovery, signals

AUTO_MAX = 20
W = signals.ARTICLE_WEIGHT
GROUP_LABEL = {"portfolio": "portfolio", "watchlist": "watchlist", "auto": "auto-watch"}


# ── Auto-watch ───────────────────────────────────────────────────────────
def maintain_auto() -> None:
    today = signals.today()
    auto = [f for f in signals.companies() if f.get("group") == "auto"]
    status = {}
    with closing(db.connect()) as c:
        for r in c.execute("SELECT key, status FROM candidates"):
            status[r["key"]] = r["status"]
        # out: hidden, or in for 30 days with hardly any headlines
        for f in auto:
            k = discovery.name_key(f["name"])
            since = f.get("since") or today.isoformat()
            n = c.execute("SELECT COUNT(*) FROM headlines WHERE company=? AND day>=? AND backfilled=0",
                          (f["key"], (today - timedelta(days=30)).isoformat())).fetchone()[0]
            if status.get(k) == "hidden" or (date.fromisoformat(since) <= today - timedelta(days=30) and n < 3):
                signals.remove_company(f["key"])
                discovery.STATE["market"] = f"auto-watch ended: {f['name']}"
    auto = [f for f in signals.companies() if f.get("group") == "auto"]
    free = AUTO_MAX - len(auto)
    if free <= 0:
        return
    present = {discovery.name_key(f["name"]) for f in signals.companies()}
    for k in discovery.candidates(14, "new")["candidates"]:
        if free <= 0:
            break
        if not k.get("ticker") or k["key"] in present:
            continue
        exch = (k.get("exchange") or "").upper()
        lang = "de" if exch in ("XETRA", "GER", "FRANKFURT", "FRA") else "fr" if exch in ("PARIS", "PAR") else "en"
        try:
            signals.save_company({"name": k["name"], "ticker": k["ticker"], "quote": k["ticker"], "group": "auto",
                                  "context": f"{k.get('long_name') or k['name']} (auto-watched from Discover).",
                                  "searches": [{"q": f'"{k["name"]}"', "lang": lang}],
                                  "backfill_days": 14, "since": today.isoformat()})
            free -= 1
        except ValueError:
            continue


# ── Trends ───────────────────────────────────────────────────────────────
def _window(c, company: str, start: date, end: date, live_only: bool = False) -> dict:
    extra = " AND backfilled=0" if live_only else ""
    r = c.execute(f"""SELECT SUM(relevance>=1) n, COUNT(DISTINCT day) days,
            SUM(CASE WHEN relevance>=1 THEN sentiment*relevance*{W} ELSE 0 END) sr,
            SUM(CASE WHEN relevance>=1 THEN relevance*{W} ELSE 0 END) r
          FROM headlines WHERE company=? AND day>=? AND day<=? AND sentiment IS NOT NULL{extra}""",
                  (company, start.isoformat(), end.isoformat())).fetchone()
    return {"n": r["n"] or 0, "days": r["days"] or 0, "net": signals.net(r["sr"] or 0, r["r"] or 0)}


def _price_pct(c, company: str, days: int = 7) -> float | None:
    k = c.execute("SELECT day, close FROM prices WHERE company=? ORDER BY day DESC LIMIT 1", (company,)).fetchone()
    if not k:
        return None
    old = c.execute("SELECT close FROM prices WHERE company=? AND day<=? ORDER BY day DESC LIMIT 1",
                    (company, (date.fromisoformat(k["day"]) - timedelta(days=days)).isoformat())).fetchone()
    return round(100 * (k["close"] / old["close"] - 1), 2) if old and old["close"] else None


def current(company: str) -> dict:
    """Current comparison: 7 days against the 21 days before (for display and the rule)."""
    h = signals.today()
    with closing(db.connect()) as c:
        now7 = _window(c, company, h - timedelta(days=6), h)
        base = _window(c, company, h - timedelta(days=27), h - timedelta(days=7))
        live7 = _window(c, company, h - timedelta(days=6), h, live_only=True)
        liveb = _window(c, company, h - timedelta(days=27), h - timedelta(days=7), live_only=True)
        price = _price_pct(c, company)
    delta = None if now7["net"] is None or base["net"] is None else round(now7["net"] - base["net"], 1)
    factor = (round((live7["n"] / max(live7["days"], 1)) / (liveb["n"] / liveb["days"]), 2)
              if liveb["days"] >= 14 and liveb["n"] else None)
    return {"net7": now7["net"], "baseline": base["net"], "delta": delta, "n7": now7["n"], "factor": factor,
            "baseline_live_days": liveb["days"], "price_pct": price}


def _signed(v: float) -> str:
    return ("+" if v > 0 else "-" if v < 0 else "±") + f"{abs(v):.0f}"


def check_trends() -> None:
    h = signals.today()
    for f in signals.companies():
        s = current(f["key"])
        if s["delta"] is None:
            continue
        if s["factor"] is not None:
            ok, rule = abs(s["delta"]) >= 25 and s["n7"] >= 10 and s["factor"] >= 1.5, "sentiment and volume"
        else:
            ok, rule = abs(s["delta"]) >= 35 and s["n7"] >= 15, "sentiment only (not enough live baseline data yet)"
        if not ok:
            continue
        direction = 1 if s["delta"] > 0 else -1
        with closing(db.connect()) as c:
            if c.execute("SELECT 1 FROM trends WHERE company=? AND direction=? AND day>=?",
                         (f["key"], direction, (h - timedelta(days=7)).isoformat())).fetchone():
                continue
            trigger = c.execute("""SELECT title, key_point FROM headlines WHERE company=? AND day>=? AND sentiment*?>0 AND relevance>=2
                                   ORDER BY relevance*ABS(sentiment) DESC, time DESC LIMIT 1""",
                                (f["key"], (h - timedelta(days=6)).isoformat(), direction)).fetchone()
            group = f.get("group", "portfolio")
            text = (f"Sentiment 7 days {_signed(s['net7'])} (before {_signed(s['baseline'])}), {s['n7']} relevant headlines"
                    + (f", {s['factor']}x the usual volume" if s["factor"] else "")
                    + (f", price 7 days {'+' if s['price_pct'] > 0 else ''}{s['price_pct']} %" if s["price_pct"] is not None else "")
                    + (f". Driven by: {(trigger['key_point'] or trigger['title'])[:160]}" if trigger else ""))
            result = "not configured"
            if alerts.configured():
                result = alerts.deliver(c, {"key": f["key"], "company": f"{f['name']} ({GROUP_LABEL.get(group, group)})",
                                            "direction": direction, "topic": "sentiment trend", "text": text[:390], "link": ""},
                                        "trend")
            c.execute("INSERT INTO trends(company, grp, direction, net7, baseline, delta, n7, factor, price_pct, rule, time, day, result) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (f["key"], group, direction, s["net7"], s["baseline"], s["delta"], s["n7"],
                                                            s["factor"], s["price_pct"], rule, signals.now(), h.isoformat(), result))
            c.commit()


def listing(days: int = 21) -> dict:
    h = signals.today()
    names = {f["key"]: f for f in signals.companies()}
    with closing(db.connect()) as c:
        events = []
        for r in c.execute("SELECT * FROM trends WHERE day>=? ORDER BY id DESC", ((h - timedelta(days=days)).isoformat(),)):
            d = dict(r)
            f = names.get(d["company"], {})
            d["name"], d["ticker"], d["group_now"] = f.get("name", d["company"]), f.get("ticker", ""), f.get("group")
            d["evidence"] = [dict(x) for x in c.execute(
                """SELECT title, publisher, link, day, sentiment, relevance, key_point FROM headlines
                   WHERE company=? AND day>=? AND day<=? AND sentiment*?>0 AND relevance>=2
                   ORDER BY relevance*ABS(sentiment) DESC, time DESC LIMIT 4""",
                (d["company"], (date.fromisoformat(d["day"]) - timedelta(days=6)).isoformat(), d["day"], d["direction"]))]
            events.append(d)
    auto = []
    for f in signals.companies():
        if f.get("group") == "auto":
            auto.append({"company": f["key"], "name": f["name"], "ticker": f.get("ticker", ""), "since": f.get("since"), **current(f["key"])})
    auto.sort(key=lambda x: -(abs(x["delta"]) if x["delta"] is not None else -1))
    return {"events": events, "auto": auto, "max": AUTO_MAX}


def step() -> None:
    """Check trends hourly, maintain auto-watch every 2 hours."""
    with closing(db.connect()) as c:
        maintain = signals.is_due(c, "signals_auto_watch", 2)
        check = signals.is_due(c, "signals_trends", 1)
    if maintain:
        maintain_auto()
        with closing(db.connect()) as c:
            db.put(c, "signals_auto_watch", signals.now())
            c.commit()
    if check:
        check_trends()
        with closing(db.connect()) as c:
            db.put(c, "signals_trends", signals.now())
            c.commit()
