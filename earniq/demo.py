"""Fill an empty data directory with synthetic demo data.

    EARNIQ_DATA_DIR=./demo-data python -m earniq.demo
    EARNIQ_DATA_DIR=./demo-data EARNIQ_WORKERS=0 uvicorn earniq.server:app --port 8106

Everything generated here is made up (titles start with "[demo]"), so the
interface can be explored without a model and without waiting for real data.
Run it against a fresh directory only; it refuses to touch existing headlines.
"""
from __future__ import annotations

import json
import math
import random
import sys
from contextlib import closing
from datetime import timedelta

from . import db, signals

TEMPLATES = [
    (2, 3, "guidance", "{n} raises full-year outlook after strong quarter"),
    (1, 2, "analysts", "Analysts lift price target on {n} ahead of results"),
    (1, 2, "demand", "{n} sees robust demand from key customers"),
    (0, 1, "price", "{n} shares edge higher in early trading"),
    (0, 1, "other", "What to watch when {n} reports this month"),
    (-1, 2, "supply", "Supply constraints could weigh on {n} margins"),
    (-1, 2, "regulation", "New export rules add uncertainty for {n}"),
    (-2, 3, "guidance", "{n} warns on revenue as orders slow"),
    (0, 0, "other", "Ten gift ideas featuring {n} products"),
]
PUBLISHERS = ["Reuters", "Bloomberg", "Financial Times", "CNBC", "Handelsblatt", "Les Echos", "MarketWatch", "Barron's"]


def main() -> int:
    db.init()
    signals.init()
    from . import server
    server._seed()
    with closing(db.connect()) as c:
        if c.execute("SELECT COUNT(*) FROM headlines").fetchone()[0]:
            print("This data directory already has headlines. Use a fresh EARNIQ_DATA_DIR for the demo.", file=sys.stderr)
            return 1
    rnd = random.Random(42)
    today = signals.today()
    cfg = signals.load_config()
    cfg["live_since"] = (today - timedelta(days=24)).isoformat()
    cfg["backfill_from"] = (today - timedelta(days=90)).isoformat()
    for i, f in enumerate(cfg.get("companies", [])):
        f["next"] = [{"day": (today + timedelta(days=9 + 11 * i)).isoformat(), "kind": "Q4 results", "confirmed": i != 2}]
    signals._write_config(cfg)

    with closing(db.connect()) as c:
        for ci, f in enumerate(signals.companies()):
            key, name = f["key"], f["name"]
            bias = [0.35, -0.1, 0.15][ci % 3]
            price = [228.0, 712.0, 241.0][ci % 3]
            event = today - timedelta(days=34 + 6 * ci)
            c.execute("INSERT OR IGNORE INTO earnings(company, day, kind, source) VALUES(?,?,?,?)",
                      (key, event.isoformat(), "Q3 results", "demo"))
            for back in range(90, -1, -1):
                day = today - timedelta(days=back)
                live = day.isoformat() >= cfg["live_since"]
                wave = math.sin((90 - back) / 9 + ci) * 0.6 + bias
                pre = 1.6 if 0 < (event - day).days <= 7 else 1.0
                n = max(0, int(rnd.gauss((9 if live else 4) * pre, 3)))
                for k in range(n):
                    choices = [t for t in TEMPLATES if (t[0] > 0) == (rnd.random() < 0.5 + wave / 2.5) or t[0] == 0]
                    s, r, topic, title = rnd.choice(choices)
                    hour = rnd.randint(6, 21)
                    c.execute("INSERT OR IGNORE INTO headlines(company, key, title, publisher, link, time, day, captured, backfilled, "
                              "sentiment, relevance, topic, model, scored) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (key, f"demo:{key}:{day}:{k}", "[demo] " + title.format(n=name), rnd.choice(PUBLISHERS), "",
                               f"{day}T{hour:02d}:{rnd.randint(0, 59):02d}:00", day.isoformat(), signals.now(), 0 if live else 1,
                               s, r, topic, "demo", signals.now()))
                if day.weekday() < 5 and day < today:
                    drift = 0.004 * wave + (0.05 * (1 if bias > 0 else -1) if day == event + timedelta(days=1) else 0)
                    price *= 1 + drift + rnd.gauss(0, 0.012)
                    c.execute("INSERT OR REPLACE INTO prices(company, day, close, currency) VALUES(?,?,?,?)",
                              (key, day.isoformat(), round(price, 2), "EUR" if key in ("asml", "sap") else "USD"))
        mon = c.execute("SELECT id FROM monitors WHERE name='Earnings materials'").fetchone()
        if mon:
            src = c.execute("SELECT id, title FROM sources WHERE monitor_id=? ORDER BY id LIMIT 3", (mon["id"],)).fetchall()
            for j, s in enumerate(src):
                fid = c.execute("INSERT INTO fetches(source_id, time, mode, http_code, duration_ms, chars, hash, changed, title) "
                                "VALUES(?,?,?,?,?,?,?,?,?)", (s["id"], signals.now(), "http", 200, 240, 5400, f"demo{j}", 1, s["title"])).lastrowid
                c.execute("INSERT INTO contents(fetch_id, text) VALUES(?,?)",
                          (fid, "[demo] Third quarter results. Revenue rose 8 percent. The company raises its full-year outlook."))
                c.execute("INSERT INTO findings(monitor_id, source_id, fetch_id, time, score, hit, summary, reasoning, quote, fields, changed, model) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (mon["id"], s["id"], fid, signals.now(), [84, 62, 22][j], 1 if j == 0 else 0,
                           ["[demo] Q3 results published with raised full-year outlook.",
                            "[demo] Date for the next results release confirmed.",
                            "[demo] Product news without financial figures."][j],
                           "Synthetic finding for the demo.", "The company raises its full-year outlook" if j == 0 else "",
                           json.dumps({"document_type": ["results release", "earnings date", "other"][j],
                                       "guidance": ["raised", "none", "none"][j], "on_watchlist": True}), 1, "demo"))
        c.commit()
    print(f"Demo data written to {db.DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
