"""API and runner end to end, with the network and the model mocked out."""
import json
from contextlib import closing

from fastapi.testclient import TestClient

from earniq import db, fetch, llm, monitor, server, signals

client = TestClient(server.app)   # no context manager: no background threads


def _page(text):
    return lambda url, mode="auto": {"mode": "http", "http_code": 200, "text": text, "title": "Results",
                                     "error": "", "duration_ms": 5, "hash": str(hash(text)), "chars": len(text),
                                     "blocked": ""}


def test_monitor_flow(fresh_db, monkeypatch):
    r = client.post("/api/monitors", json={"name": "Earnings materials", "criteria": "results",
                                           "fields": [{"name": "guidance", "description": "raised/lowered"}],
                                           "threshold": 60})
    mid = r.json()["id"]
    assert client.post(f"/api/monitors/{mid}/sources", json={"text": "- https://ir.example.com | Example · IR\nnot a url"}).json() == {"new": 1, "recognized": 1}

    text = "Example Corp raised its full-year guidance after record third-quarter revenue."
    monkeypatch.setattr(fetch, "page", _page(text))
    answer = json.dumps({"score": 82, "summary": "Guidance raised.", "reasoning": "Clear raise.",
                         "quote": "raised its full-year guidance", "fields": {"guidance": "raised", "extra": "x"}})
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (answer, "test-model", "stop"))

    with closing(db.connect()) as c:
        s = c.execute("SELECT * FROM sources").fetchone()
        assert monitor.run_source(c, s)["status"].startswith("first fetch, scored 82")
        # unchanged page: no second verdict
        s = c.execute("SELECT * FROM sources").fetchone()
        assert monitor.run_source(c, s)["status"] == "unchanged"

    f = client.get("/api/findings").json()
    assert f["total"] == 1
    hit = f["findings"][0]
    assert hit["hit"] == 1 and json.loads(hit["fields"]) == {"guidance": "raised"}
    detail = client.get(f"/api/findings/{hit['id']}").json()
    assert detail["text"] == text and not detail["quote"].startswith("[not found")
    assert "guidance" in client.get("/api/export/findings.csv").text.splitlines()[0]


def test_model_offline_stores_no_verdict(fresh_db, monkeypatch):
    mid = client.post("/api/monitors", json={"name": "M"}).json()["id"]
    client.post(f"/api/monitors/{mid}/sources", json={"text": "https://example.com"})
    monkeypatch.setattr(fetch, "page", _page("some page text " * 50))

    def offline(*a, **k):
        raise ConnectionRefusedError()
    monkeypatch.setattr(llm, "chat", offline)
    with closing(db.connect()) as c:
        s = c.execute("SELECT * FROM sources").fetchone()
        assert monitor.run_source(c, s)["status"] == "verdict pending"
    assert client.get("/api/findings").json()["total"] == 0


def test_repeat_hits_are_not_counted_twice(fresh_db, monkeypatch):
    mid = client.post("/api/monitors", json={"name": "M", "threshold": 50}).json()["id"]
    client.post(f"/api/monitors/{mid}/sources", json={"text": "https://a.example.com\nhttps://b.example.com"})
    answer = json.dumps({"score": 90, "summary": "Company X raised guidance for fiscal 2026.", "reasoning": "",
                         "quote": "Company X raised guidance for fiscal 2026", "fields": {}})
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (answer, "m", "stop"))
    monkeypatch.setattr(fetch, "page", _page("Company X raised guidance for fiscal 2026. More text here."))
    with closing(db.connect()) as c:
        for s in c.execute("SELECT * FROM sources ORDER BY id").fetchall():
            monitor.run_source(c, s)
    hits = client.get("/api/findings?hits_only=true").json()
    assert hits["total"] == 1


def test_import_export_roundtrip(fresh_db):
    cfg = {"version": 1, "monitors": [{"name": "Orders", "criteria": "big orders", "threshold": 65,
                                       "fields": [{"name": "volume_musd", "cap": {"below": 1000, "score": 30}}],
                                       "sources": [{"url": "https://news.example.com/rss", "title": "News"}]}]}
    r = client.post("/api/import", json=cfg).json()
    assert r["monitors_new"] == 1 and r["sources_new"] == 1
    r = client.post("/api/import", json=cfg).json()
    assert r["monitors_updated"] == 1 and r["sources_updated"] == 1
    out = client.get("/api/export/config.json").json()
    assert out["monitors"][0]["fields"][0]["cap"] == {"below": 1000, "score": 30}


def test_company_crud_updates_watchlist_block(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(signals, "CONFIG", tmp_path / "watchlist.json")
    signals.CONFIG.write_text(json.dumps({"earnings_monitor": "Earnings materials", "companies": []}))
    signals.init()
    client.post("/api/monitors", json={"name": "Earnings materials", "criteria": "Results only.\n\nWatchlist with next earnings date:\n"})
    r = client.post("/api/companies", json={"name": "Example Corp", "ticker": "exm",
                                            "searches": [{"q": "Example Corp earnings", "lang": "en"}],
                                            "next": [{"day": "2099-01-30", "kind": "Q4", "confirmed": False}]})
    assert r.status_code == 200 and r.json()["key"] == "example-corp" and r.json()["ticker"] == "EXM"
    crit = client.get("/api/monitors").json()[0]["criteria"]
    assert "- Example Corp (EXM): Q4 on 2099-01-30 (estimated)" in crit
    sources = client.get("/api/monitors/1/sources").json()
    assert sources[0]["title"] == "Example Corp · Google News"
    assert client.post("/api/companies", json={"name": "Bad", "searches": []}).status_code == 400
    assert client.delete("/api/companies/example-corp").status_code == 200
    assert "Example Corp" not in client.get("/api/monitors").json()[0]["criteria"]


def test_basic_auth(monkeypatch):
    from earniq import config
    monkeypatch.setattr(config, "BASIC_AUTH", "me:secret")
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/monitors", auth=("me", "secret")).status_code == 200
