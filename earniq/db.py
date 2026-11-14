"""Schema and connection.

One SQLite file in WAL mode. Page texts grow fast, so they live in their own
table (`contents`) and age out on a schedule, while findings and scored
headlines stay forever: the time series is the product.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from . import config

DB = config.DATA_DIR / "earniq.db"

SCHEMA = """
-- ── Monitors: pages watched on a schedule and judged against criteria ──
CREATE TABLE IF NOT EXISTS monitors(
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL UNIQUE,
  description    TEXT    DEFAULT '',
  criteria       TEXT    DEFAULT '',     -- free text: what counts, what explicitly does not
  fields         TEXT    DEFAULT '[]',   -- [{"name":..,"description":..,"match":[..],"cap":{..}}]
  interval_min   INTEGER DEFAULT 45,
  mode           TEXT    DEFAULT 'auto', -- auto | http | chromium
  threshold      INTEGER DEFAULT 60,     -- score from which a finding counts as a hit
  only_on_change INTEGER DEFAULT 1,      -- do not re-judge an unchanged page
  active         INTEGER DEFAULT 1,
  created        TEXT
);

CREATE TABLE IF NOT EXISTS sources(
  id          INTEGER PRIMARY KEY,
  monitor_id  INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  url         TEXT NOT NULL,
  title       TEXT    DEFAULT '',
  note        TEXT    DEFAULT '',
  mode        TEXT    DEFAULT '',        -- empty = the monitor decides
  active      INTEGER DEFAULT 1,
  due         TEXT,                      -- next run, ISO
  last_run    TEXT,
  last_status TEXT    DEFAULT '',
  errors      INTEGER DEFAULT 0,         -- consecutive errors, drives the backoff
  created     TEXT,
  UNIQUE(monitor_id, url)
);
CREATE INDEX IF NOT EXISTS sources_due ON sources(active, due);

CREATE TABLE IF NOT EXISTS fetches(
  id          INTEGER PRIMARY KEY,
  source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  time        TEXT,
  mode        TEXT,                      -- http | chromium
  http_code   INTEGER DEFAULT 0,
  duration_ms INTEGER DEFAULT 0,
  chars       INTEGER DEFAULT 0,
  hash        TEXT    DEFAULT '',
  changed     INTEGER DEFAULT 0,
  diff        TEXT    DEFAULT '',
  title       TEXT    DEFAULT '',
  error       TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS fetches_source ON fetches(source_id, id DESC);

-- Page text is kept apart so list queries over fetches never touch megabytes,
-- and cleanup can drop it alone.
CREATE TABLE IF NOT EXISTS contents(
  fetch_id INTEGER PRIMARY KEY REFERENCES fetches(id) ON DELETE CASCADE,
  text     TEXT
);

CREATE TABLE IF NOT EXISTS findings(
  id          INTEGER PRIMARY KEY,
  monitor_id  INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  fetch_id    INTEGER REFERENCES fetches(id) ON DELETE SET NULL,
  time        TEXT,
  score       INTEGER DEFAULT 0,
  hit         INTEGER DEFAULT 0,         -- score >= threshold of the monitor
  summary     TEXT DEFAULT '',
  reasoning   TEXT DEFAULT '',
  quote       TEXT DEFAULT '',
  fields      TEXT DEFAULT '{}',
  changed     INTEGER DEFAULT 0,         -- page differed from the previous fetch
  model       TEXT DEFAULT '',
  duration_ms INTEGER DEFAULT 0,
  seen        INTEGER DEFAULT 0,
  starred     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS findings_time ON findings(time DESC);
CREATE INDEX IF NOT EXISTS findings_monitor ON findings(monitor_id, time DESC);

CREATE TABLE IF NOT EXISTS log(
  id    INTEGER PRIMARY KEY,
  time  TEXT,
  level TEXT DEFAULT 'info',
  text  TEXT
);
CREATE INDEX IF NOT EXISTS log_time ON log(id DESC);

CREATE TABLE IF NOT EXISTS settings(
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- ── Signals: every headline about a company, scored one by one ──
CREATE TABLE IF NOT EXISTS headlines(
  id          INTEGER PRIMARY KEY,
  company     TEXT NOT NULL,
  key         TEXT NOT NULL,             -- guid from the feed
  title       TEXT NOT NULL,
  publisher   TEXT DEFAULT '',
  link        TEXT DEFAULT '',
  time        TEXT,                      -- published, UTC ISO
  day         TEXT,                      -- published, date (UTC)
  captured    TEXT,
  backfilled  INTEGER DEFAULT 0,         -- loaded from the archive, not captured live
  sentiment   INTEGER,                   -- -2..2, NULL = not scored yet
  relevance   INTEGER,                   -- 0..3
  topic       TEXT DEFAULT '',
  key_point   TEXT DEFAULT '',
  article_id  INTEGER,                   -- set when the full article was read
  model       TEXT DEFAULT '',
  scored      TEXT,
  UNIQUE(company, key)
);
CREATE INDEX IF NOT EXISTS headlines_company_day ON headlines(company, day);
CREATE INDEX IF NOT EXISTS headlines_open ON headlines(sentiment) WHERE sentiment IS NULL;

CREATE TABLE IF NOT EXISTS prices(
  company  TEXT NOT NULL,
  day      TEXT NOT NULL,
  close    REAL,
  currency TEXT DEFAULT '',
  PRIMARY KEY(company, day)
);

CREATE TABLE IF NOT EXISTS earnings(
  company TEXT NOT NULL,
  day     TEXT NOT NULL,
  kind    TEXT DEFAULT '',
  source  TEXT DEFAULT '',
  PRIMARY KEY(company, day)
);

-- ── Articles: full texts read for watched companies and market searches ──
CREATE TABLE IF NOT EXISTS articles(
  id          INTEGER PRIMARY KEY,
  company     TEXT NOT NULL,             -- company key, or '_market'
  url         TEXT NOT NULL,
  title       TEXT,
  publisher   TEXT DEFAULT '',
  description TEXT DEFAULT '',
  time        TEXT,
  day         TEXT,
  captured    TEXT,
  state       TEXT DEFAULT 'new',        -- new | read | unreadable | scored | failed | skipped
  chars       INTEGER DEFAULT 0,
  text        TEXT,
  reason      TEXT DEFAULT '',
  sentiment   INTEGER,
  relevance   INTEGER,
  topic       TEXT DEFAULT '',
  key_point   TEXT DEFAULT '',
  quote       TEXT DEFAULT '',
  companies   TEXT DEFAULT '[]',
  headline_id INTEGER,
  model       TEXT DEFAULT '',
  scored      TEXT,
  UNIQUE(company, url)
);
CREATE INDEX IF NOT EXISTS articles_state ON articles(state);

-- ── Discovery: companies that stand out in the news ──
CREATE TABLE IF NOT EXISTS mentions(
  id         INTEGER PRIMARY KEY,
  key        TEXT NOT NULL,
  name       TEXT,
  direction  INTEGER,
  signal     TEXT DEFAULT '',
  reason     TEXT DEFAULT '',
  origin     TEXT DEFAULT '',            -- market | article:<company>
  publisher  TEXT DEFAULT '',
  url        TEXT DEFAULT '',
  title      TEXT DEFAULT '',
  day        TEXT,
  captured   TEXT,
  UNIQUE(key, url)
);
CREATE INDEX IF NOT EXISTS mentions_day ON mentions(day);

CREATE TABLE IF NOT EXISTS candidates(
  key        TEXT PRIMARY KEY,
  name       TEXT,
  ticker     TEXT DEFAULT '',
  exchange   TEXT DEFAULT '',
  long_name  TEXT DEFAULT '',
  status     TEXT DEFAULT 'new',         -- new | watched | hidden | unlisted
  checked    TEXT,
  attempted  TEXT,
  created    TEXT
);

-- ── Alerts and sentiment trends ──
CREATE TABLE IF NOT EXISTS alerts(
  id          INTEGER PRIMARY KEY,
  company     TEXT,
  kind        TEXT DEFAULT 'event',      -- event | trend
  topic       TEXT,
  direction   INTEGER,
  time        TEXT,
  headline_id INTEGER,
  text        TEXT,
  link        TEXT,
  result      TEXT
);
CREATE INDEX IF NOT EXISTS alerts_time ON alerts(time);

CREATE TABLE IF NOT EXISTS trends(
  id INTEGER PRIMARY KEY, company TEXT, grp TEXT, direction INTEGER, net7 REAL, baseline REAL, delta REAL,
  n7 INTEGER, factor REAL, price_pct REAL, rule TEXT, time TEXT, day TEXT, result TEXT
);
CREATE INDEX IF NOT EXISTS trends_day ON trends(day);
"""

DEFAULTS = {
    "retention_days": "60",     # page texts older than this are deleted
    "max_chars": "12000",       # per page sent to the model
}


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init() -> None:
    with closing(connect()) as c:
        c.executescript(SCHEMA)
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (k, v))
        c.commit()


def get(c: sqlite3.Connection, key: str, default: str = "") -> str:
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else DEFAULTS.get(key, default)


def get_int(c: sqlite3.Connection, key: str, default: int) -> int:
    try:
        return int(get(c, key, str(default)))
    except (TypeError, ValueError):
        return default


def put(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute("INSERT INTO settings(key, value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
