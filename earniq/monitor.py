"""The monitor runner: fetch due sources, compare, judge, store.

A single thread, serial on purpose. The expensive steps are Chromium and the
model, and both share the machine with everything else. Fetching twenty
sources in parallel would not speed anything up, because judging them queues
up behind the model anyway.

Each source carries its own `due` time. The runner sleeps in short steps and
looks at what is due, so a changed interval takes effect immediately, and so
does "check now" from the interface.

Two things keep the load down:
  * **Jitter**: the next run lands at interval +/- 20 %, otherwise all sources
    start in lockstep after a restart.
  * **Only judge on change**: an unchanged page text gets no second verdict.
    That is the lever that turns hourly fetching into a handful of model calls
    per day.
"""
from __future__ import annotations

import difflib
import random
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta

from . import db, fetch, judge

WAKE = threading.Event()        # wakes the runner for "check now"
TICK = 20                       # seconds between two looks at the clock
PAUSE = 2.0                     # seconds between two sources
MAX_BACKOFF_FACTOR = 8          # backoff caps at interval x 8

_state = {"running": "", "since": "", "last": "", "fetches": 0, "findings": 0, "errors": 0}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def state() -> dict:
    return dict(_state)


def log(c: sqlite3.Connection, text: str, level: str = "info") -> None:
    c.execute("INSERT INTO log(time, level, text) VALUES(?,?,?)", (now(), level, text[:1000]))
    c.commit()


def _next(interval_min: int, errors: int = 0) -> str:
    minutes = max(5, int(interval_min))
    if errors:
        minutes *= min(2 ** errors, MAX_BACKOFF_FACTOR)
    jitter = minutes * random.uniform(-0.2, 0.2)
    return (datetime.now() + timedelta(minutes=minutes + jitter)).isoformat(timespec="seconds")


def set_due(c: sqlite3.Connection, source_id: int, immediately: bool = False) -> None:
    if immediately:
        c.execute("UPDATE sources SET due=? WHERE id=?", (now(), source_id))
        c.commit()
        WAKE.set()
        return
    r = c.execute("SELECT s.errors, m.interval_min FROM sources s JOIN monitors m ON m.id=s.monitor_id "
                  "WHERE s.id=?", (source_id,)).fetchone()
    if r:
        c.execute("UPDATE sources SET due=? WHERE id=?", (_next(r["interval_min"], r["errors"]), source_id))
        c.commit()


# Repeats: a page often changes only incidentally (counters, order, timestamps),
# and the model then reports the same content as a hit again. In one week that
# produced 240 hits, of which 38 were distinct (one release note 34 times). A
# hit therefore only counts if the monitor had nothing equivalent in the last
# REPEAT_DAYS: same quote (beginning) or a very similar summary. The finding is
# still stored (time series), just without the hit flag and with a pointer to
# the first one.
REPEAT_DAYS = 30


def _norm(t: str) -> str:
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def repeat_of(c: sqlite3.Connection, monitor_id: int, summary: str, quote: str) -> int | None:
    """Id of an earlier equivalent hit of the same monitor, otherwise None."""
    since = (datetime.now() - timedelta(days=REPEAT_DAYS)).isoformat(timespec="seconds")
    q, s = _norm(quote)[:40], _norm(summary)[:120]
    for r in c.execute("SELECT id, summary, quote FROM findings WHERE monitor_id=? AND hit=1 AND time>=? "
                       "ORDER BY id DESC LIMIT 300", (monitor_id, since)):
        if len(q) >= 20 and q == _norm(r["quote"])[:40]:
            return r["id"]
        if s and difflib.SequenceMatcher(None, s, _norm(r["summary"])[:120]).ratio() >= 0.8:
            return r["id"]
    return None


def run_source(c: sqlite3.Connection, s: sqlite3.Row) -> dict:
    """One pass for one source. Returns a short status."""
    mon = c.execute("SELECT * FROM monitors WHERE id=?", (s["monitor_id"],)).fetchone()
    if mon is None:
        return {"status": "monitor missing"}

    mode = (s["mode"] or "").strip() or (mon["mode"] or "auto")
    _state["running"] = s["url"]
    _state["since"] = now()
    r = fetch.page(s["url"], mode)
    _state["running"] = ""
    _state["fetches"] += 1

    before = c.execute(
        "SELECT f.id, f.hash, t.text FROM fetches f LEFT JOIN contents t ON t.fetch_id=f.id "
        "WHERE f.source_id=? AND f.error='' ORDER BY f.id DESC LIMIT 1", (s["id"],)).fetchone()

    first = before is None
    changed = bool(r["hash"]) and (first or r["hash"] != (before["hash"] or ""))
    delta = ""
    if changed and not first and before["text"]:
        delta = fetch.diff(before["text"], r["text"])

    cur = c.execute(
        "INSERT INTO fetches(source_id, time, mode, http_code, duration_ms, chars, hash, "
        "changed, diff, title, error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (s["id"], now(), r["mode"], r["http_code"], r["duration_ms"], r["chars"],
         r["hash"], 1 if changed else 0, delta, r["title"], r["error"] or r.get("blocked", "")))
    fetch_id = cur.lastrowid
    if r["text"]:
        c.execute("INSERT INTO contents(fetch_id, text) VALUES(?,?)", (fetch_id, r["text"]))

    # A bot wall is not content. It is stored so you can see how the site
    # turns you away, but it is not judged, and the source goes into backoff
    # instead of hitting the same wall every hour.
    if r["error"] or not r["text"] or r.get("blocked"):
        errors = (s["errors"] or 0) + 1
        status = r["error"] or r.get("blocked") or f"empty (HTTP {r['http_code']})"
        if r.get("blocked") and errors == 1:
            log(c, f"{s['url']}: {r['blocked']} via {r['mode']}. "
                   "Set the mode to 'always Chromium' or drop the source.", "warning")
        c.execute("UPDATE sources SET last_run=?, last_status=?, errors=?, due=? WHERE id=?",
                  (now(), status[:200], errors, _next(mon["interval_min"], errors), s["id"]))
        c.commit()
        _state["errors"] += 1
        _state["last"] = now()
        return {"status": status, "fetch_id": fetch_id}

    # Fill in a title as long as the source has none.
    if r["title"] and not (s["title"] or "").strip():
        c.execute("UPDATE sources SET title=? WHERE id=?", (r["title"][:200], s["id"]))

    # If the last fetch stayed unjudged (model offline, no answer), catch up
    # now, even if the page has not changed since. Otherwise exactly this page
    # would fall through for good: the hash matches again.
    catch_up = bool(before) and not c.execute(
        "SELECT 1 FROM findings WHERE fetch_id=?", (before["id"],)).fetchone()
    judge_it = changed or first or catch_up or not mon["only_on_change"]
    status = "changed" if changed and not first else ("first fetch" if first else "unchanged")

    if judge_it:
        max_chars = db.get_int(c, "max_chars", 12000)
        v = judge.evaluate(dict(mon), {"url": s["url"], "title": r["title"] or s["title"]},
                           r["text"], delta, max_chars)
        if not v["usable"]:
            # No verdict is better than an invented one: neither a missing
            # model nor a truncated answer is stored, otherwise a score 0 would
            # sit there later that looks like "not relevant". The fetch stays,
            # the verdict is caught up (see `catch_up` above).
            #
            # Two causes, two waiting times: if the model was unreachable it
            # will be back soon; if the answer was unusable it is this source,
            # and the backoff keeps it from occupying the model every ten
            # minutes for nothing.
            if v["reached"]:
                errors = (s["errors"] or 0) + 1
                when = _next(mon["interval_min"], errors)
            else:
                errors = s["errors"] or 0
                when = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds")
            log(c, f"{s['url']}: {v['error']}. Verdict will be caught up.", "warning")
            c.execute("UPDATE sources SET last_run=?, last_status=?, errors=?, due=? WHERE id=?",
                      (now(), f"fetched, verdict pending ({v['error']})"[:200], errors, when, s["id"]))
            c.commit()
            _state["last"] = now()
            return {"status": "verdict pending", "fetch_id": fetch_id}
        hit = v["score"] >= (mon["threshold"] or 0)
        reasoning = v["reasoning"]
        earlier = repeat_of(c, s["monitor_id"], v["summary"], v["quote"]) if hit else None
        if earlier:
            hit = False
            reasoning = f"Repeat of finding #{earlier}. {reasoning or ''}".strip()
        c.execute(
            "INSERT INTO findings(monitor_id, source_id, fetch_id, time, score, hit, summary, "
            "reasoning, quote, fields, changed, model, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["monitor_id"], s["id"], fetch_id, now(), v["score"], 1 if hit else 0, v["summary"],
             reasoning, v["quote"], v["fields"], 1 if (changed and not first) else 0,
             v["model"], v["duration_ms"]))
        _state["findings"] += 1
        status += f", scored {v['score']}" + (f" (repeat of #{earlier})" if earlier else "")
        if v["error"]:
            status += f" ({v['error']})"
            log(c, f"{s['url']}: verdict incomplete: {v['error']}", "warning")

    c.execute("UPDATE sources SET last_run=?, last_status=?, errors=0, due=? WHERE id=?",
              (now(), status[:200], _next(mon["interval_min"]), s["id"]))
    c.commit()
    _state["last"] = now()
    return {"status": status, "fetch_id": fetch_id}


def _cleanup(c: sqlite3.Connection) -> None:
    """Page texts age out, findings stay.

    The texts are what eats space, and after a few weeks they are only
    evidence; score, summary and quote stay forever so the time series has no
    holes.
    """
    days = db.get_int(c, "retention_days", 60)
    limit = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    c.execute("DELETE FROM contents WHERE fetch_id IN (SELECT id FROM fetches WHERE time < ?)", (limit,))
    c.execute("DELETE FROM fetches WHERE time < ? AND changed=0 AND id NOT IN "
              "(SELECT fetch_id FROM findings WHERE fetch_id IS NOT NULL)", (limit,))
    c.execute("DELETE FROM log WHERE time < ?", (limit,))
    c.commit()


def worker() -> None:
    last_cleanup = 0.0
    while True:
        try:
            with closing(db.connect()) as c:
                if time.time() - last_cleanup > 6 * 3600:
                    _cleanup(c)
                    last_cleanup = time.time()
                rows = c.execute(
                    "SELECT s.* FROM sources s JOIN monitors m ON m.id=s.monitor_id "
                    "WHERE s.active=1 AND m.active=1 AND (s.due IS NULL OR s.due <= ?) "
                    "ORDER BY s.due IS NOT NULL, s.due LIMIT 50", (now(),)).fetchall()
                for s in rows:
                    try:
                        run_source(c, s)
                    except Exception as e:  # noqa: BLE001 - one broken source does not stop the run
                        log(c, f"{s['url']}: {type(e).__name__}: {e}", "error")
                        c.execute("UPDATE sources SET last_run=?, last_status=?, errors=errors+1, "
                                  "due=? WHERE id=?",
                                  (now(), f"{type(e).__name__}"[:200],
                                   _next(60, (s["errors"] or 0) + 1), s["id"]))
                        c.commit()
                        _state["errors"] += 1
                    time.sleep(PAUSE)
        except Exception:  # noqa: BLE001 - database locked or similar: do not die
            time.sleep(10)
        WAKE.wait(timeout=TICK)
        WAKE.clear()


def start() -> None:
    threading.Thread(target=worker, daemon=True, name="earniq-monitors").start()
