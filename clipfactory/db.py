import json
import sqlite3
import threading

from . import config
from .util import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT, rate_per_1k REAL DEFAULT 0, budget_left REAL,
  max_payout_per_clip REAL, min_views INTEGER DEFAULT 0, ends TEXT, daily_clip_quota INTEGER DEFAULT 3,
  platforms TEXT DEFAULT '["youtube"]', accounts TEXT DEFAULT '{}', required_hashtags TEXT DEFAULT '[]',
  credit TEXT DEFAULT '', min_len INTEGER DEFAULT 20, max_len INTEGER DEFAULT 55, min_score REAL DEFAULT 7,
  max_moments_per_video INTEGER DEFAULT 8, priority INTEGER DEFAULT 5, paused INTEGER DEFAULT 0,
  demo INTEGER DEFAULT 0, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  name TEXT, channel_id TEXT, streams_only INTEGER DEFAULT 0, layout TEXT DEFAULT 'blur', facecam TEXT,
  last_checked TEXT, UNIQUE(campaign_id, channel_id));
CREATE TABLE IF NOT EXISTS videos(
  id TEXT NOT NULL, campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE, source_id INTEGER,
  title TEXT, url TEXT, local_path TEXT, is_stream INTEGER DEFAULT 0, duration_s REAL, published_at TEXT,
  status TEXT DEFAULT 'new', route TEXT, error TEXT, created_at TEXT, analyzed_at TEXT,
  PRIMARY KEY(id, campaign_id));
CREATE TABLE IF NOT EXISTS moments(
  id INTEGER PRIMARY KEY, video_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  start_s REAL, end_s REAL, score REAL, category TEXT, hook TEXT, why TEXT,
  status TEXT DEFAULT 'found', error TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY, moment_id INTEGER REFERENCES moments(id) ON DELETE CASCADE,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  raw_path TEXT, final_path TEXT, duration_s REAL, words TEXT, titles TEXT, title TEXT, description TEXT,
  hashtags TEXT, status TEXT DEFAULT 'downloaded', reject_reason TEXT, error TEXT, created_at TEXT,
  reviewed_at TEXT, tg_message_id INTEGER);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY, clip_id INTEGER REFERENCES clips(id) ON DELETE CASCADE,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE, platform TEXT, account TEXT,
  scheduled_at TEXT, posted_at TEXT, external_id TEXT, url TEXT, status TEXT DEFAULT 'queued', error TEXT,
  views INTEGER DEFAULT 0, likes INTEGER DEFAULT 0, us_share REAL, stats_at TEXT,
  submitted INTEGER DEFAULT 0, submitted_at TEXT);
CREATE TABLE IF NOT EXISTS llm_cache(key TEXT PRIMARY KEY, provider TEXT, response TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT, campaign_id TEXT, level TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, kind TEXT, campaign_id TEXT, clip_id INTEGER, post_id INTEGER, text TEXT,
  file_path TEXT, buttons TEXT, created_at TEXT, sent_at TEXT, message_id TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS gmail_seen(
  id TEXT PRIMARY KEY, subject TEXT, date TEXT, internal_ms INTEGER, summary TEXT, action TEXT, deadline TEXT,
  seen_at TEXT);
CREATE TABLE IF NOT EXISTS discovered(key TEXT PRIMARY KEY, campaign_id TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_moments_status ON moments(status, campaign_id);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status, campaign_id);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, scheduled_at);
"""

MIGRATIONS = [
    ("campaigns", "platform", "TEXT DEFAULT ''"),
    ("campaigns", "creator", "TEXT DEFAULT ''"),
    ("campaigns", "topic", "TEXT DEFAULT 'gaming'"),
    ("campaigns", "audience", "TEXT DEFAULT 'US viewers aged 16 to 34'"),
    ("campaigns", "rules", "TEXT DEFAULT '[]'"),
    ("campaigns", "brief", "TEXT DEFAULT ''"),
    ("campaigns", "origin", "TEXT DEFAULT 'yaml'"),
    ("sources", "kind", "TEXT DEFAULT 'youtube_channel'"),
    ("videos", "settle", "TEXT"),
    ("campaigns", "stage", "TEXT DEFAULT 'active'"),
    ("campaigns", "auto_approve_score", "REAL"),
    ("campaigns", "found_at", "TEXT"),
    ("posts", "mode", "TEXT DEFAULT 'auto'"),
]

_local = threading.local()


def _migrate(c):
    for table, col, decl in MIGRATIONS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(config.data_dir() / "clipfactory.db", timeout=30, isolation_level=None,
                            check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript(SCHEMA)
        _migrate(c)
        _local.c = c
    return c


def q(sql, *args):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, *args):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def x(sql, *args):
    return conn().execute(sql, args).lastrowid


def kv_get(k, default=None):
    r = one("SELECT v FROM kv WHERE k=?", k)
    return json.loads(r["v"]) if r else default


def kv_set(k, v):
    x("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, json.dumps(v))


def log(campaign_id, msg, level="info"):
    x("INSERT INTO events(at, campaign_id, level, msg) VALUES(?,?,?,?)", iso(utcnow()), campaign_id, level, msg)
    print(f"[{level}] {campaign_id or '-'}: {msg}", flush=True)
    if level == "error":
        _alert(campaign_id, msg)


def _alert(campaign_id, msg):
    """Errors also go to Discord, at most once a day per kind of error, so a broken step is never silent."""
    import hashlib
    key = "alert_" + utcnow().date().isoformat() + "_" + hashlib.sha1(f"{campaign_id}{msg[:60]}".encode()).hexdigest()[:12]
    if kv_get(key):
        return
    kv_set(key, True)
    from . import notify
    notify.send("error", f"Problem{f' in {campaign_id}' if campaign_id else ''}: {msg[:600]}\n"
                "Details are in Clipping Tools\\logs. Send this to Claude if it keeps happening.",
                campaign_id=campaign_id)
