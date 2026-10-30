"""Small SQLite key/value cache for TMDb answers, with per-entry expiry, shared by plugin and service.

Kept apart from the tracking database (tally.db) so clearing it never touches what you watched.
``tmdb_pending`` holds artwork a widget could not wait for; the service fetches it.
"""

import json
import os
import sqlite3
import threading
import time

from . import common, perf

_LOCK = threading.Lock()
_DB_NAME = "cache.db"
_conn = None


def _connect():
    global _conn
    if _conn is not None:
        return _conn
    path = os.path.join(common.ensure_profile_dir(), _DB_NAME)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        " key TEXT PRIMARY KEY,"
        " expires REAL NOT NULL,"
        " value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tmdb_pending ("
        " kind TEXT NOT NULL,"
        " tmdb_id TEXT NOT NULL,"
        " queued REAL NOT NULL,"
        " PRIMARY KEY (kind, tmdb_id))"
    )
    conn.commit()
    _conn = conn
    return conn


def get(key):
    """Cached value, or None if missing or expired."""
    with _LOCK, perf.span("sqlite_get"):
        row = _connect().execute(
            "SELECT expires, value FROM cache WHERE key = ?", (key,)).fetchone()
    if not row or row[0] < time.time():
        return None
    try:
        return json.loads(row[1])
    except ValueError:
        return None


def get_many(keys):
    """{key: value} for the keys that are cached and not expired, in as few queries as possible."""
    keys = list(keys)
    found = {}
    now = time.time()
    with _LOCK, perf.span("sqlite_get_many"):
        conn = _connect()
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            rows = conn.execute(
                "SELECT key, value FROM cache WHERE expires >= ? AND key IN ({})".format(
                    ",".join("?" * len(chunk))),
                [now] + chunk,
            ).fetchall()
            for key, value in rows:
                try:
                    found[key] = json.loads(value)
                except ValueError:
                    pass
    return found


def set(key, value, ttl_seconds):  # noqa: A001 - mirrors dict-like API
    if ttl_seconds <= 0:
        return
    payload = json.dumps(value)
    with _LOCK, perf.span("sqlite_set"):
        conn = _connect()
        conn.execute("INSERT OR REPLACE INTO cache (key, expires, value) VALUES (?, ?, ?)",
                     (key, time.time() + ttl_seconds, payload))
        conn.commit()


def delete_prefix(prefix):
    with _LOCK:
        conn = _connect()
        conn.execute("DELETE FROM cache WHERE key LIKE ?", (prefix + "%",))
        conn.commit()


def purge_expired():
    now = time.time()
    with _LOCK:
        conn = _connect()
        conn.execute("DELETE FROM cache WHERE expires < ?", (now,))
        conn.execute("DELETE FROM tmdb_pending WHERE queued < ?", (now - 24 * 3600,))
        conn.commit()


def clear_all():
    with _LOCK:
        conn = _connect()
        conn.execute("DELETE FROM cache")
        conn.execute("DELETE FROM tmdb_pending")
        conn.commit()
        conn.execute("VACUUM")


# -- artwork handed from widgets to the service ----------------------------------------

def queue_tmdb(pairs):
    """Artwork lookups a widget did not wait for; the service fetches them."""
    pairs = list(pairs)
    if not pairs:
        return
    now = time.time()
    with _LOCK, perf.span("sqlite_queue"):
        conn = _connect()
        conn.executemany(
            "INSERT OR IGNORE INTO tmdb_pending (kind, tmdb_id, queued) VALUES (?, ?, ?)",
            [(kind, str(tmdb_id), now) for kind, tmdb_id in pairs],
        )
        conn.commit()


def pending_newest():
    """When the latest artwork lookup was queued (epoch seconds), or None when none are."""
    with _LOCK:
        return _connect().execute("SELECT MAX(queued) FROM tmdb_pending").fetchone()[0]


def pending_tmdb(limit=100):
    with _LOCK:
        return _connect().execute(
            "SELECT kind, tmdb_id FROM tmdb_pending ORDER BY queued LIMIT ?", (limit,)
        ).fetchall()


def done_tmdb(pairs):
    with _LOCK:
        conn = _connect()
        conn.executemany("DELETE FROM tmdb_pending WHERE kind = ? AND tmdb_id = ?",
                         [(kind, str(tmdb_id)) for kind, tmdb_id in pairs])
        conn.commit()
