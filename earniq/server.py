"""EARN.iq web service: API under /api, interface under /.

    uvicorn earniq.server:app --host 127.0.0.1 --port 8106

There is no user management. Either keep the service on a private network
(LAN, VPN, Tailscale) or set EARNIQ_BASIC_AUTH="user:password", ideally
behind a TLS-terminating reverse proxy.
"""
from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import re
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from . import __version__, alerts, config, db, discovery, llm, monitor, signals, trends

PAGE = config.ROOT / "web" / "index.html"
EXAMPLE_MONITORS = config.CONFIG_DIR / "monitors.example.json"


def _seed() -> None:
    """On a fresh database, load the example monitors so there is something to look at."""
    if not config.SEED_EXAMPLES or not EXAMPLE_MONITORS.exists():
        return
    with closing(db.connect()) as c:
        if c.execute("SELECT 1 FROM monitors LIMIT 1").fetchone() or db.get(c, "seeded", ""):
            return
        db.put(c, "seeded", monitor.now())
        c.commit()
    import_config(ConfigIn(**json.loads(EXAMPLE_MONITORS.read_text(encoding="utf-8"))))
    _update_criteria()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    signals.init()
    _seed()
    if config.WORKERS:
        monitor.start()
        signals.start()
    yield


app = FastAPI(title="EARN.iq", version=__version__, docs_url="/api/docs", redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not config.BASIC_AUTH or request.url.path == "/api/health":
        return await call_next(request)
    expected = "Basic " + base64.b64encode(config.BASIC_AUTH.encode("utf-8")).decode("ascii")
    if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
        return PlainTextResponse("Authentication required", status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="EARN.iq"'})
    return await call_next(request)


def conn():
    return closing(db.connect())


def rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M")


# ── Monitors ────────────────────────────────────────────────────────────
class MonitorIn(BaseModel):
    name: str
    description: str = ""
    criteria: str = ""
    fields: list[dict] = []
    interval_min: int = 45
    mode: str = "auto"
    threshold: int = 60
    only_on_change: bool = True
    active: bool = True


def _monitor_values(e: MonitorIn) -> tuple:
    mode = e.mode if e.mode in ("auto", "http", "chromium") else "auto"
    return (e.description, e.criteria, json.dumps(e.fields, ensure_ascii=False),
            max(5, e.interval_min), mode, max(0, min(100, e.threshold)),
            1 if e.only_on_change else 0, 1 if e.active else 0)


@app.get("/api/monitors")
def list_monitors():
    with conn() as c:
        return rows(c.execute("""
            SELECT m.*,
              (SELECT COUNT(*) FROM sources s WHERE s.monitor_id=m.id)                AS sources,
              (SELECT COUNT(*) FROM sources s WHERE s.monitor_id=m.id AND s.active=1) AS sources_active,
              (SELECT COUNT(*) FROM findings f WHERE f.monitor_id=m.id)               AS findings,
              (SELECT COUNT(*) FROM findings f WHERE f.monitor_id=m.id AND f.hit=1)   AS hits,
              (SELECT MAX(s.last_run) FROM sources s WHERE s.monitor_id=m.id)         AS last_run,
              (SELECT MIN(s.due) FROM sources s WHERE s.monitor_id=m.id AND s.active=1) AS next_run
            FROM monitors m ORDER BY m.name"""))


@app.post("/api/monitors")
def create_monitor(e: MonitorIn):
    name = e.name.strip()
    if not name:
        raise HTTPException(400, "Name is missing")
    with conn() as c:
        if c.execute("SELECT 1 FROM monitors WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, "This name already exists")
        cur = c.execute("INSERT INTO monitors(name, description, criteria, fields, interval_min, mode, "
                        "threshold, only_on_change, active, created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (name,) + _monitor_values(e) + (monitor.now(),))
        c.commit()
        return {"id": cur.lastrowid}


@app.put("/api/monitors/{mid}")
def update_monitor(mid: int, e: MonitorIn):
    with conn() as c:
        if not c.execute("SELECT 1 FROM monitors WHERE id=?", (mid,)).fetchone():
            raise HTTPException(404, "Monitor not found")
        c.execute("UPDATE monitors SET name=?, description=?, criteria=?, fields=?, interval_min=?, "
                  "mode=?, threshold=?, only_on_change=?, active=? WHERE id=?",
                  (e.name.strip(),) + _monitor_values(e) + (mid,))
        c.commit()
        return {"ok": True}


@app.delete("/api/monitors/{mid}")
def delete_monitor(mid: int):
    with conn() as c:
        c.execute("DELETE FROM monitors WHERE id=?", (mid,))
        c.commit()
        return {"ok": True}


@app.post("/api/monitors/{mid}/run")
def run_monitor(mid: int):
    with conn() as c:
        c.execute("UPDATE sources SET due=? WHERE monitor_id=? AND active=1", (monitor.now(), mid))
        c.commit()
    monitor.WAKE.set()
    return {"ok": True}


# ── Sources ─────────────────────────────────────────────────────────────
class SourcesIn(BaseModel):
    text: str = ""            # one URL per line, optionally "URL | title"


class SourceIn(BaseModel):
    title: str = ""
    note: str = ""
    mode: str = ""
    active: bool = True


def urls_from(text: str) -> list[tuple[str, str]]:
    """Lines into (URL, title). Tolerates bullets, comments, columns.

    A link list rarely arrives clean: it is copied from an email, a note or a
    spreadsheet. So read generously instead of making the user format it.
    """
    out, seen = [], set()
    for z in (text or "").splitlines():
        z = z.strip()
        if not z or z.startswith("#"):
            continue
        z = re.sub(r"^[-*•\d.)\s]+", "", z).strip()
        title = ""
        for sep in ("|", "\t", ";"):
            if sep in z:
                a, b = z.split(sep, 1)
                if a.strip().startswith("http"):
                    z, title = a.strip(), b.strip()
                elif b.strip().startswith("http"):
                    title, z = a.strip(), b.strip()
                break
        m = re.search(r"https?://\S+", z)
        if not m:
            continue
        u = m.group(0).rstrip(').,;"\'')
        if not urlparse(u).netloc or u in seen:
            continue
        seen.add(u)
        out.append((u, title[:200]))
    return out


@app.get("/api/monitors/{mid}/sources")
def list_sources(mid: int):
    with conn() as c:
        return rows(c.execute("""
            SELECT s.*,
              (SELECT COUNT(*) FROM fetches f WHERE f.source_id=s.id) AS fetches,
              (SELECT MAX(x.score) FROM findings x WHERE x.source_id=s.id) AS best_score,
              (SELECT x.score FROM findings x WHERE x.source_id=s.id ORDER BY x.id DESC LIMIT 1) AS last_score
            FROM sources s WHERE s.monitor_id=? ORDER BY s.id""", (mid,)))


@app.post("/api/monitors/{mid}/sources")
def add_sources(mid: int, e: SourcesIn):
    pairs = urls_from(e.text)
    if not pairs:
        raise HTTPException(400, "No valid URL found")
    new = 0
    with conn() as c:
        if not c.execute("SELECT 1 FROM monitors WHERE id=?", (mid,)).fetchone():
            raise HTTPException(404, "Monitor not found")
        for u, t in pairs:
            cur = c.execute("INSERT OR IGNORE INTO sources(monitor_id, url, title, due, created) VALUES(?,?,?,?,?)",
                            (mid, u, t, monitor.now(), monitor.now()))
            new += cur.rowcount
        c.commit()
    monitor.WAKE.set()
    return {"new": new, "recognized": len(pairs)}


@app.put("/api/sources/{sid}")
def update_source(sid: int, e: SourceIn):
    with conn() as c:
        c.execute("UPDATE sources SET title=?, note=?, mode=?, active=? WHERE id=?",
                  (e.title[:200], e.note[:500], e.mode if e.mode in ("", "auto", "http", "chromium") else "",
                   1 if e.active else 0, sid))
        c.commit()
        return {"ok": True}


@app.delete("/api/sources/{sid}")
def delete_source(sid: int):
    with conn() as c:
        c.execute("DELETE FROM sources WHERE id=?", (sid,))
        c.commit()
        return {"ok": True}


@app.post("/api/sources/{sid}/run")
def run_source(sid: int):
    with conn() as c:
        monitor.set_due(c, sid, immediately=True)
    return {"ok": True}


@app.get("/api/sources/{sid}/fetches")
def source_fetches(sid: int, limit: int = 50):
    with conn() as c:
        return rows(c.execute("SELECT id, time, mode, http_code, duration_ms, chars, changed, title, error "
                              "FROM fetches WHERE source_id=? ORDER BY id DESC LIMIT ?", (sid, min(limit, 500))))


# ── Findings ────────────────────────────────────────────────────────────
def _findings_where(mon: int | None, min_score: int, hits_only: bool, changed_only: bool,
                    days: int, search: str) -> tuple[str, list]:
    where, args = ["1=1"], []
    if mon:
        where.append("f.monitor_id=?")
        args.append(mon)
    if min_score:
        where.append("f.score >= ?")
        args.append(min_score)
    if hits_only:
        where.append("f.hit=1")
    if changed_only:
        where.append("f.changed=1")
    if days:
        where.append("f.time >= ?")
        args.append((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))
    if search.strip():
        where.append("(f.summary LIKE ? OR f.reasoning LIKE ? OR f.quote LIKE ? "
                     "OR s.url LIKE ? OR s.title LIKE ? OR f.fields LIKE ?)")
        args += [f"%{search.strip()}%"] * 6
    return " AND ".join(where), args


@app.get("/api/findings")
def list_findings(monitor_id: int = 0, min_score: int = 0, hits_only: bool = False,
                  changed_only: bool = False, days: int = 0, search: str = "",
                  limit: int = 100, offset: int = 0, sort: str = "time"):
    where, args = _findings_where(monitor_id or None, min_score, hits_only, changed_only, days, search)
    order = {"time": "f.time DESC, f.id DESC", "score": "f.score DESC, f.time DESC",
             "source": "s.url, f.time DESC"}.get(sort, "f.time DESC")
    with conn() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM findings f JOIN sources s ON s.id=f.source_id WHERE {where}", args).fetchone()["n"]
        rs = c.execute(f"""
            SELECT f.*, s.url, s.title AS source_title, m.name AS monitor_name, m.threshold
            FROM findings f JOIN sources s ON s.id=f.source_id JOIN monitors m ON m.id=f.monitor_id
            WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""", args + [min(limit, 20000), max(0, offset)])
        return {"total": total, "findings": rows(rs)}


@app.get("/api/findings/{fid}")
def get_finding(fid: int):
    with conn() as c:
        r = c.execute("""
            SELECT f.*, s.url, s.title AS source_title, m.name AS monitor_name, m.threshold, x.diff, x.mode,
                   x.http_code, x.chars, x.time AS fetch_time
            FROM findings f JOIN sources s ON s.id=f.source_id JOIN monitors m ON m.id=f.monitor_id
            LEFT JOIN fetches x ON x.id=f.fetch_id WHERE f.id=?""", (fid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Finding not found")
        d = dict(r)
        t = c.execute("SELECT text FROM contents WHERE fetch_id=?", (r["fetch_id"],)).fetchone()
        d["text"] = (t["text"] if t else "")[:40000]
        return d


class Flags(BaseModel):
    seen: bool | None = None
    starred: bool | None = None


@app.post("/api/findings/{fid}/flags")
def set_flags(fid: int, m: Flags):
    with conn() as c:
        if m.seen is not None:
            c.execute("UPDATE findings SET seen=? WHERE id=?", (1 if m.seen else 0, fid))
        if m.starred is not None:
            c.execute("UPDATE findings SET starred=? WHERE id=?", (1 if m.starred else 0, fid))
        c.commit()
        return {"ok": True}


# ── Export ──────────────────────────────────────────────────────────────
@app.get("/api/export/findings.csv")
def export_csv(monitor_id: int = 0, min_score: int = 0, hits_only: bool = False,
               changed_only: bool = False, days: int = 0, search: str = ""):
    """Flat table including the fields of each monitor as their own columns.

    Fields differ per monitor, so the columns are built from the keys that
    actually occur; otherwise the export would be empty or full of foreign
    columns.
    """
    where, args = _findings_where(monitor_id or None, min_score, hits_only, changed_only, days, search)
    with conn() as c:
        rs = c.execute(f"""
            SELECT f.time, m.name AS monitor, s.url, s.title AS source, f.score, f.hit,
                   f.changed, f.summary, f.reasoning, f.quote, f.fields, f.model
            FROM findings f JOIN sources s ON s.id=f.source_id JOIN monitors m ON m.id=f.monitor_id
            WHERE {where} ORDER BY f.time DESC LIMIT 20000""", args).fetchall()
    extra: list[str] = []
    for r in rs:
        try:
            for k in json.loads(r["fields"] or "{}"):
                if k not in extra:
                    extra.append(k)
        except ValueError:
            pass
    buf = io.StringIO()
    w = csv.writer(buf)
    head = ["time", "monitor", "url", "source", "score", "hit", "changed", "summary", "reasoning", "quote", "model"]
    w.writerow(head + extra)
    for r in rs:
        try:
            f = json.loads(r["fields"] or "{}")
        except ValueError:
            f = {}
        w.writerow([r[k] for k in head] + [f.get(k, "") for k in extra])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="findings-{_stamp()}.csv"'})


@app.get("/api/export/findings.json")
def export_json(monitor_id: int = 0, min_score: int = 0, hits_only: bool = False,
                changed_only: bool = False, days: int = 0, search: str = ""):
    d = list_findings(monitor_id, min_score, hits_only, changed_only, days, search, limit=20000)
    return JSONResponse(d["findings"], headers={"Content-Disposition": f'attachment; filename="findings-{_stamp()}.json"'})


# ── Configuration as a file ─────────────────────────────────────────────
# Monitors with criteria, fields and sources in one JSON file: versionable in
# Git, portable to a second machine, and after a rebuild the setup is one
# command instead of twenty clicks.
#
#   curl -X POST --data-binary @monitors.json -H "Content-Type: application/json" \
#        http://localhost:8106/api/import
#
# Matching is by monitor name and URL. An existing monitor is updated, not
# duplicated; existing sources keep their history. Sources not mentioned stay,
# unless the monitor carries "replace": true; then they are removed with their
# findings.
class ConfigIn(BaseModel):
    version: int = 1
    monitors: list[dict] = []


@app.post("/api/import")
def import_config(k: ConfigIn):
    report = {"monitors_new": 0, "monitors_updated": 0, "sources_new": 0,
              "sources_updated": 0, "sources_removed": 0, "messages": []}
    with conn() as c:
        for raw in k.monitors:
            name = str(raw.get("name", "")).strip()
            if not name:
                report["messages"].append("monitor without a name skipped")
                continue
            values = _monitor_values(MonitorIn(**{**raw, "name": name}))
            existing = c.execute("SELECT id FROM monitors WHERE name=?", (name,)).fetchone()
            if existing:
                mid = existing["id"]
                c.execute("UPDATE monitors SET description=?, criteria=?, fields=?, interval_min=?, "
                          "mode=?, threshold=?, only_on_change=?, active=? WHERE id=?", values + (mid,))
                report["monitors_updated"] += 1
            else:
                mid = c.execute("INSERT INTO monitors(name, description, criteria, fields, interval_min, mode, "
                                "threshold, only_on_change, active, created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (name,) + values + (monitor.now(),)).lastrowid
                report["monitors_new"] += 1
            named = set()
            for q in raw.get("sources", []):
                q = q if isinstance(q, dict) else {"url": str(q)}
                url = str(q.get("url", "")).strip()
                if not url.startswith("http"):
                    report["messages"].append(f"'{name}': not a valid URL: {url[:60]}")
                    continue
                named.add(url)
                vals = (str(q.get("title", ""))[:200], str(q.get("note", ""))[:500],
                        str(q.get("mode", ""))[:20], 1 if q.get("active", True) else 0)
                there = c.execute("SELECT id FROM sources WHERE monitor_id=? AND url=?", (mid, url)).fetchone()
                if there:
                    c.execute("UPDATE sources SET title=?, note=?, mode=?, active=? WHERE id=?", vals + (there["id"],))
                    report["sources_updated"] += 1
                else:
                    c.execute("INSERT INTO sources(monitor_id, url, title, note, mode, active, due, created) "
                              "VALUES(?,?,?,?,?,?,?,?)", (mid, url) + vals + (monitor.now(), monitor.now()))
                    report["sources_new"] += 1
            if raw.get("replace") and named:
                marks = ",".join("?" * len(named))
                gone = c.execute(f"SELECT id FROM sources WHERE monitor_id=? AND url NOT IN ({marks})",
                                 [mid, *named]).fetchall()
                for r in gone:
                    c.execute("DELETE FROM sources WHERE id=?", (r["id"],))
                report["sources_removed"] += len(gone)
        c.commit()
    monitor.WAKE.set()
    return report


@app.get("/api/export/config.json")
def export_config():
    """The same structure back, to version and re-import."""
    with conn() as c:
        out = []
        for m in c.execute("SELECT * FROM monitors ORDER BY name").fetchall():
            try:
                fields = json.loads(m["fields"] or "[]")
            except ValueError:
                fields = []
            srcs = c.execute("SELECT url, title, note, mode, active FROM sources WHERE monitor_id=? ORDER BY id",
                             (m["id"],)).fetchall()
            out.append({"name": m["name"], "description": m["description"], "criteria": m["criteria"],
                        "fields": fields, "interval_min": m["interval_min"], "mode": m["mode"],
                        "threshold": m["threshold"], "only_on_change": bool(m["only_on_change"]),
                        "active": bool(m["active"]),
                        "sources": [{"url": s["url"], "title": s["title"], "note": s["note"],
                                     "mode": s["mode"], "active": bool(s["active"])} for s in srcs]})
    return JSONResponse({"version": 1, "monitors": out},
                        headers={"Content-Disposition": f'attachment; filename="earniq-monitors-{_stamp()}.json"'})


# ── System ──────────────────────────────────────────────────────────────
@app.get("/api/log")
def get_log(limit: int = 100):
    with conn() as c:
        return rows(c.execute("SELECT * FROM log ORDER BY id DESC LIMIT ?", (min(limit, 500),)))


@app.get("/api/status")
def status():
    with conn() as c:
        z = c.execute("""
            SELECT (SELECT COUNT(*) FROM monitors WHERE active=1) AS monitors,
                   (SELECT COUNT(*) FROM sources WHERE active=1)  AS sources,
                   (SELECT COUNT(*) FROM fetches)                 AS fetches,
                   (SELECT COUNT(*) FROM findings)                AS findings,
                   (SELECT COUNT(*) FROM findings WHERE hit=1)    AS hits,
                   (SELECT MIN(due) FROM sources WHERE active=1)  AS next_run,
                   (SELECT COUNT(*) FROM sources WHERE errors>0)  AS sources_failing""").fetchone()
        size = db.DB.stat().st_size if db.DB.exists() else 0
        return {**dict(z), "runner": monitor.state(), "model": llm.ping(), "version": __version__,
                "db_mb": round(size / 1048576, 1),
                "retention_days": db.get_int(c, "retention_days", 60),
                "max_chars": db.get_int(c, "max_chars", 12000),
                "alerts": {"configured": alerts.configured(), "format": config.ALERT_FORMAT,
                           "max_per_day": config.ALERT_MAX_PER_DAY},
                "sec_user_agent": bool(config.sec_user_agent())}


class Setting(BaseModel):
    key: str
    value: str


@app.put("/api/settings")
def put_setting(e: Setting):
    if e.key not in db.DEFAULTS:
        raise HTTPException(400, "Unknown setting")
    with conn() as c:
        db.put(c, e.key, e.value)
        c.commit()
        return {"ok": True}


# ── Signals ─────────────────────────────────────────────────────────────
_COMPANY = re.compile(r"^[a-z0-9_-]{1,40}$")


@app.get("/api/signals")
def signals_overview():
    return signals.overview()


@app.get("/api/signals/{company}")
def signals_history(company: str, days: int = 30):
    if not _COMPANY.match(company):
        raise HTTPException(400, "Company")
    v = signals.history(company, max(7, min(120, days)))
    if not v:
        raise HTTPException(404, "Company not configured")
    return v


@app.get("/api/signals/{company}/headlines")
def signals_headlines(company: str, day: str):
    if not _COMPANY.match(company) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(400, "Company or day")
    return signals.headlines_for_day(company, day)


@app.post("/api/signals/wake")
def signals_wake():
    signals.WAKE.set()
    return {"ok": True}


class CompanyIn(BaseModel):
    name: str
    ticker: str = ""
    quote: str = ""
    cik: str = ""
    euronext: str = ""
    context: str = ""
    searches: list[dict] = []
    next: list[dict] = []
    past: list[dict] = []
    add_source: bool = True
    group: str = "portfolio"


def _earnings_monitor_id() -> int | None:
    name = signals.load_config().get("earnings_monitor")
    if not name:
        return None
    with conn() as c:
        r = c.execute("SELECT id FROM monitors WHERE name=?", (name,)).fetchone()
        return r["id"] if r else None


def _update_criteria() -> None:
    """Keep the watchlist block in the criteria of the earnings monitor in line with the configuration."""
    mid = _earnings_monitor_id()
    if not mid:
        return
    with conn() as c:
        r = c.execute("SELECT criteria FROM monitors WHERE id=?", (mid,)).fetchone()
        if not r:
            return
        block = signals.watchlist_text()
        crit = r["criteria"] or ""
        new, n = re.subn(re.escape(signals.WATCHLIST_HEADER) + r"\n(?:- .*\n?)*", lambda _m: block + "\n", crit, count=1)
        if not n:
            new = crit.rstrip() + "\n\n" + block + "\n"
        c.execute("UPDATE monitors SET criteria=? WHERE id=?", (new, mid))
        c.commit()


@app.get("/api/companies")
def list_companies():
    return {"earnings_monitor_id": _earnings_monitor_id(), "companies": signals.companies_with_stats(),
            "languages": sorted(signals.LANGUAGES)}


@app.post("/api/companies")
def create_company(e: CompanyIn):
    try:
        f = signals.save_company(e.model_dump())
    except ValueError as x:
        raise HTTPException(400, str(x))
    mid = _earnings_monitor_id()
    if e.add_source and mid:
        s = f["searches"][0]
        url = signals.google_url(s["q"] + " when:7d", s)
        with conn() as c:
            c.execute("INSERT OR IGNORE INTO sources(monitor_id, url, title, due, created) VALUES(?,?,?,?,?)",
                      (mid, url, f"{f['name']} · Google News", monitor.now(), monitor.now()))
            c.commit()
        monitor.WAKE.set()
    _update_criteria()
    return f


@app.put("/api/companies/{key}")
def update_company(key: str, e: CompanyIn):
    try:
        f = signals.save_company(e.model_dump(), old=key)
    except ValueError as x:
        raise HTTPException(400, str(x))
    except KeyError:
        raise HTTPException(404, "Company not found")
    _update_criteria()
    return f


@app.delete("/api/companies/{key}")
def delete_company(key: str):
    try:
        signals.remove_company(key)
    except KeyError:
        raise HTTPException(404, "Company not found")
    _update_criteria()
    return {"ok": True}


# ── Discover, trends, alerts ────────────────────────────────────────────
@app.get("/api/discover")
def discover(status: str = "new", days: int = 14):
    if status not in ("new", "watched", "hidden"):
        raise HTTPException(400, "Status")
    return discovery.candidates(max(7, min(60, days)), status)


@app.post("/api/discover/{key}/{action}")
def discover_action(key: str, action: str):
    if not re.fullmatch(r"[a-z0-9äöüéèàç&-]{1,60}", key) or action not in ("watch", "hide", "reset"):
        raise HTTPException(400, "Candidate or action")
    try:
        k = discovery.set_candidate(key, {"watch": "watched", "hide": "hidden", "reset": "new"}[action])
    except KeyError:
        raise HTTPException(404, "Candidate not found")
    auto = next((f for f in signals.companies() if f.get("group") == "auto"
                 and discovery.name_key(f["name"]) == key), None)
    if auto and action == "hide":
        signals.remove_company(auto["key"])
    if auto and action == "watch":
        signals.save_company({**auto, "group": "watchlist"}, old=auto["key"])
    elif action == "watch":
        name = k.get("long_name") or k["name"]
        try:
            signals.save_company({"name": k["name"], "ticker": k.get("ticker", ""), "quote": k.get("ticker", ""),
                                  "group": "watchlist",
                                  "context": f"{name}. Watched since {signals.today().isoformat()} (from Discover).",
                                  "searches": [{"q": f'"{k["name"]}"', "lang": "en"}]})
        except ValueError as x:
            if "already exists" not in str(x):
                raise HTTPException(400, str(x))
    _update_criteria()
    return {"ok": True, "candidate": k}


@app.get("/api/trends")
def trend_list(days: int = 21):
    return trends.listing(max(7, min(60, days)))


@app.get("/api/alerts")
def alert_list():
    return {"alerts": alerts.recent(), "configured": alerts.configured(),
            "max_per_day": config.ALERT_MAX_PER_DAY}


@app.get("/api/health")
def health():
    return {"ok": True, "time": monitor.now(), "version": __version__}


# ── Interface ───────────────────────────────────────────────────────────
@app.get("/")
def index():
    if not PAGE.exists():
        return PlainTextResponse("Interface missing: web/index.html", status_code=500)
    return FileResponse(PAGE, headers={"Cache-Control": "no-store"})
