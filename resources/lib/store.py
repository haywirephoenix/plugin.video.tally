"""Tally's own tracking database: watchlist, plays, resume points and show progress.

Plain sqlite3 and no Kodi imports, so it runs (and is tested) outside Kodi. The schema is
versioned with PRAGMA user_version; MIGRATIONS[n] takes a database from version n to n + 1,
so later features (recommendations) add their tables with a new entry.

Identity is TMDb: movies by their id, shows and their episodes by the show's id.
``kind`` is "movie" or "tv" for titles; plays and resume points use "movie" or "episode".
Movies have season = episode = -1 in tables keyed on them, so the keys have no NULLs.
"""

import json
import sqlite3
import threading
import time

from . import model

STATUS_PLANNING = model.STATUS_PLANNING
STATUS_IN_PROGRESS = model.STATUS_IN_PROGRESS
STATUS_COMPLETED = model.STATUS_COMPLETED

# A saved position under this is an accidental start; at or past FINISHED of the length it is done.
MIN_POSITION = 60
FINISHED = 0.95

MIGRATIONS = [
    # 0 -> 1
    """
    CREATE TABLE media (
        kind TEXT NOT NULL,
        tmdb TEXT NOT NULL,
        imdb TEXT NOT NULL DEFAULT '',
        tvdb TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        year INTEGER,
        poster TEXT NOT NULL DEFAULT '',
        status INTEGER,
        added_at REAL NOT NULL,
        activity_at REAL NOT NULL DEFAULT 0,
        next_season INTEGER,
        next_episode INTEGER,
        next_title TEXT NOT NULL DEFAULT '',
        next_thumb TEXT NOT NULL DEFAULT '',
        next_plot TEXT NOT NULL DEFAULT '',
        next_aired TEXT NOT NULL DEFAULT '',
        episodes_left INTEGER,
        PRIMARY KEY (kind, tmdb)
    );
    CREATE INDEX media_status ON media (kind, status, activity_at);
    CREATE TABLE plays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        tmdb TEXT NOT NULL,
        season INTEGER NOT NULL DEFAULT -1,
        episode INTEGER NOT NULL DEFAULT -1,
        watched_at REAL NOT NULL
    );
    CREATE INDEX plays_item ON plays (kind, tmdb, season, episode);
    CREATE INDEX plays_time ON plays (kind, watched_at);
    CREATE TABLE progress (
        kind TEXT NOT NULL,
        tmdb TEXT NOT NULL,
        season INTEGER NOT NULL DEFAULT -1,
        episode INTEGER NOT NULL DEFAULT -1,
        position REAL NOT NULL,
        duration REAL NOT NULL DEFAULT 0,
        title TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL,
        PRIMARY KEY (kind, tmdb, season, episode)
    );
    CREATE INDEX progress_time ON progress (updated_at);
    CREATE TABLE shows (
        tmdb TEXT PRIMARY KEY,
        seasons TEXT NOT NULL DEFAULT '[]',
        last_aired TEXT NOT NULL DEFAULT '',
        ended INTEGER NOT NULL DEFAULT 0,
        refreshed_at REAL NOT NULL DEFAULT 0
    );
    """,
    # 1 -> 2
    """
    CREATE TABLE imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        importer TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        imported_at REAL NOT NULL,
        summary TEXT NOT NULL DEFAULT '{}'
    );
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)

# An imported play this close to one already stored is the same play (an import run twice).
SAME_PLAY = 60


def _key(season, episode):
    return (-1 if season is None else int(season), -1 if episode is None else int(episode))


def _play_kind(kind):
    return "movie" if kind == "movie" else "episode"


def _title_kind(kind):
    return "movie" if kind == "movie" else "tv"


class Store:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def close(self):
        with self._lock:
            self._conn.close()

    def _migrate(self):
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for target in range(version, SCHEMA_VERSION):
                # executescript commits first; the version bump goes with the migration's own statements.
                self._conn.executescript(
                    "BEGIN;\n{}\nPRAGMA user_version = {};\nCOMMIT;".format(MIGRATIONS[target], target + 1))

    @property
    def version(self):
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _write(self, fn):
        with self._lock:
            try:
                result = fn(self._conn)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    # -- titles -----------------------------------------------------------------------

    @staticmethod
    def _ensure(conn, kind, tmdb, meta, now):
        """Insert a title row if missing (untracked: status NULL), and fill in what meta knows."""
        conn.execute("INSERT OR IGNORE INTO media (kind, tmdb, added_at) VALUES (?, ?, ?)",
                     (kind, str(tmdb), now))
        fields = {k: v for k, v in (meta or {}).items()
                  if k in ("imdb", "tvdb", "title", "year", "poster") and v not in (None, "")}
        if fields:
            conn.execute("UPDATE media SET {} WHERE kind = ? AND tmdb = ?".format(
                ", ".join("{} = ?".format(k) for k in fields)),
                list(fields.values()) + [kind, str(tmdb)])

    def update_meta(self, kind, tmdb, meta):
        """Fill in ids, title, year or poster of a tracked title (never adds one)."""
        kind = _title_kind(kind)
        if not self.get(kind, tmdb):
            return
        self._write(lambda c: self._ensure(c, kind, tmdb, meta, time.time()))

    def get(self, kind, tmdb):
        return self._one("SELECT * FROM media WHERE kind = ? AND tmdb = ? AND status IS NOT NULL",
                         (_title_kind(kind), str(tmdb)))

    def add_watchlist(self, kind, tmdb, meta=None):
        """Put an untracked title on the watchlist. False when it is already tracked."""
        kind = _title_kind(kind)
        now = time.time()

        def run(conn):
            self._ensure(conn, kind, tmdb, meta, now)
            cur = conn.execute("UPDATE media SET status = ?, added_at = ? "
                               "WHERE kind = ? AND tmdb = ? AND status IS NULL",
                               (STATUS_PLANNING, now, kind, str(tmdb)))
            return cur.rowcount > 0
        return self._write(run)

    def remove_watchlist(self, kind, tmdb):
        """Take a title off the watchlist; only one with nothing watched (status Planning)."""
        cur = self._write(lambda c: c.execute(
            "UPDATE media SET status = NULL WHERE kind = ? AND tmdb = ? AND status = ?",
            (_title_kind(kind), str(tmdb), STATUS_PLANNING)))
        return cur.rowcount > 0

    def untrack(self, kind, tmdb):
        """Forget a title: its status, plays and resume points."""
        kind, tmdb = _title_kind(kind), str(tmdb)

        def run(conn):
            conn.execute("DELETE FROM media WHERE kind = ? AND tmdb = ?", (kind, tmdb))
            conn.execute("DELETE FROM plays WHERE kind = ? AND tmdb = ?", (_play_kind(kind), tmdb))
            conn.execute("DELETE FROM progress WHERE kind = ? AND tmdb = ?", (_play_kind(kind), tmdb))
            if kind == "tv":
                conn.execute("DELETE FROM shows WHERE tmdb = ?", (tmdb,))
        self._write(run)

    # -- plays --------------------------------------------------------------------------

    def add_play(self, kind, tmdb, season=None, episode=None, meta=None, when=None):
        """Record a finished play of a movie or episode; it also clears its resume point."""
        play_kind = _play_kind(kind)
        title_kind = _title_kind(kind)
        tmdb = str(tmdb)
        season, episode = _key(season, episode) if play_kind == "episode" else (-1, -1)
        now = when or time.time()

        def run(conn):
            self._ensure(conn, title_kind, tmdb, meta, now)
            conn.execute("INSERT INTO plays (kind, tmdb, season, episode, watched_at) VALUES (?, ?, ?, ?, ?)",
                         (play_kind, tmdb, season, episode, now))
            conn.execute("DELETE FROM progress WHERE kind = ? AND tmdb = ? AND season = ? AND episode = ?",
                         (play_kind, tmdb, season, episode))
            conn.execute("UPDATE media SET activity_at = MAX(activity_at, ?) WHERE kind = ? AND tmdb = ?",
                         (now, title_kind, tmdb))
            if title_kind == "movie":
                conn.execute("UPDATE media SET status = ? WHERE kind = 'movie' AND tmdb = ?",
                             (STATUS_COMPLETED, tmdb))
            else:
                self._recompute(conn, tmdb)
        self._write(run)

    def add_plays(self, tmdb, episodes, meta=None):
        """Mark many episodes of a show watched at once (season/show "mark watched")."""
        now = time.time()
        tmdb = str(tmdb)

        def run(conn):
            self._ensure(conn, "tv", tmdb, meta, now)
            for season, episode in episodes:
                conn.execute("INSERT INTO plays (kind, tmdb, season, episode, watched_at) "
                             "VALUES ('episode', ?, ?, ?, ?)", (tmdb, int(season), int(episode), now))
                conn.execute("DELETE FROM progress WHERE kind = 'episode' AND tmdb = ? "
                             "AND season = ? AND episode = ?", (tmdb, int(season), int(episode)))
            conn.execute("UPDATE media SET activity_at = MAX(activity_at, ?) WHERE kind = 'tv' AND tmdb = ?",
                         (now, tmdb))
            self._recompute(conn, tmdb)
        self._write(run)

    def remove_play(self, kind, tmdb, season=None, episode=None):
        """Remove every play of a movie or episode (mark unwatched)."""
        play_kind = _play_kind(kind)
        tmdb = str(tmdb)
        season, episode = _key(season, episode) if play_kind == "episode" else (-1, -1)

        def run(conn):
            cur = conn.execute("DELETE FROM plays WHERE kind = ? AND tmdb = ? AND season = ? AND episode = ?",
                               (play_kind, tmdb, season, episode))
            if play_kind == "movie":
                left = conn.execute("SELECT 1 FROM progress WHERE kind = 'movie' AND tmdb = ?",
                                    (tmdb,)).fetchone()
                conn.execute("UPDATE media SET status = ? WHERE kind = 'movie' AND tmdb = ? "
                             "AND status = ?", (STATUS_IN_PROGRESS if left else STATUS_PLANNING,
                                                tmdb, STATUS_COMPLETED))
            else:
                self._recompute(conn, tmdb)
            return cur.rowcount
        return self._write(run)

    def playcount(self, kind, tmdb, season=None, episode=None):
        season, episode = _key(season, episode) if _play_kind(kind) == "episode" else (-1, -1)
        row = self._one("SELECT COUNT(*) AS n FROM plays WHERE kind = ? AND tmdb = ? AND season = ? "
                        "AND episode = ?", (_play_kind(kind), str(tmdb), season, episode))
        return row["n"]

    def episode_playcounts(self, tmdb, season=None):
        """{(season, episode): plays} for a show, or one of its seasons."""
        sql = "SELECT season, episode, COUNT(*) AS n FROM plays WHERE kind = 'episode' AND tmdb = ?"
        args = [str(tmdb)]
        if season is not None:
            sql += " AND season = ?"
            args.append(int(season))
        rows = self._rows(sql + " GROUP BY season, episode", args)
        return {(r["season"], r["episode"]): r["n"] for r in rows}

    # -- resume points ----------------------------------------------------------------

    def save_progress(self, kind, tmdb, position, duration=0, season=None, episode=None,
                      title="", meta=None):
        """Save a resume point. Under MIN_POSITION nothing is kept; past FINISHED it counts as a play."""
        play_kind = _play_kind(kind)
        if duration and position >= duration * FINISHED:
            self.add_play(kind, tmdb, season, episode, meta=meta)
            return "played"
        if position < MIN_POSITION:
            return None
        title_kind = _title_kind(kind)
        tmdb = str(tmdb)
        season, episode = _key(season, episode) if play_kind == "episode" else (-1, -1)
        now = time.time()

        def run(conn):
            self._ensure(conn, title_kind, tmdb, meta, now)
            conn.execute(
                "INSERT OR REPLACE INTO progress (kind, tmdb, season, episode, position, duration, title, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (play_kind, tmdb, season, episode, float(position), float(duration or 0), title or "", now))
            conn.execute("UPDATE media SET activity_at = MAX(activity_at, ?) WHERE kind = ? AND tmdb = ?",
                         (now, title_kind, tmdb))
            # A movie being watched is in progress unless it was watched before (a rewatch).
            if title_kind == "movie":
                conn.execute("UPDATE media SET status = ? WHERE kind = 'movie' AND tmdb = ? "
                             "AND (status IS NULL OR status = ?)",
                             (STATUS_IN_PROGRESS, tmdb, STATUS_PLANNING))
            else:
                conn.execute("UPDATE media SET status = ? WHERE kind = 'tv' AND tmdb = ? "
                             "AND (status IS NULL OR status = ?)",
                             (STATUS_IN_PROGRESS, tmdb, STATUS_PLANNING))
        self._write(run)
        return "saved"

    def get_progress(self, kind, tmdb, season=None, episode=None):
        """(position, duration) of a resume point, or (0, 0)."""
        season, episode = _key(season, episode) if _play_kind(kind) == "episode" else (-1, -1)
        row = self._one("SELECT position, duration FROM progress WHERE kind = ? AND tmdb = ? "
                        "AND season = ? AND episode = ?", (_play_kind(kind), str(tmdb), season, episode))
        return (row["position"], row["duration"]) if row else (0, 0)

    def clear_progress(self, kind, tmdb, season=None, episode=None):
        play_kind = _play_kind(kind)
        season, episode = _key(season, episode) if play_kind == "episode" else (-1, -1)
        tmdb = str(tmdb)

        def run(conn):
            conn.execute("DELETE FROM progress WHERE kind = ? AND tmdb = ? AND season = ? AND episode = ?",
                         (play_kind, tmdb, season, episode))
            if play_kind == "movie":
                # Nothing left in progress and never finished: back to the watchlist state it came from.
                conn.execute("UPDATE media SET status = NULL WHERE kind = 'movie' AND tmdb = ? AND status = ?",
                             (tmdb, STATUS_IN_PROGRESS))
        self._write(run)

    def movie_progress(self):
        """{tmdb: (position, duration)} of the movies' resume points."""
        rows = self._rows("SELECT tmdb, position, duration FROM progress WHERE kind = 'movie' AND position >= ?",
                          (MIN_POSITION,))
        return {r["tmdb"]: (r["position"], r["duration"]) for r in rows}

    def season_progress(self, tmdb, season):
        """{episode: (position, duration)} of a show season's resume points."""
        rows = self._rows("SELECT episode, position, duration FROM progress WHERE kind = 'episode' "
                          "AND tmdb = ? AND season = ?", (str(tmdb), int(season)))
        return {r["episode"]: (r["position"], r["duration"]) for r in rows}

    def resume_list(self, kinds=("movie", "episode"), limit=20, offset=0):
        """Resume points, most recent first, with the title's stored details."""
        marks = ",".join("?" * len(kinds))
        return self._rows(
            "SELECT p.*, m.title AS media_title, m.imdb, m.tvdb, m.poster, m.year FROM progress p "
            "LEFT JOIN media m ON m.tmdb = p.tmdb AND m.kind = (CASE p.kind WHEN 'movie' THEN 'movie' "
            "ELSE 'tv' END) WHERE p.kind IN ({}) AND p.position >= ? "
            "ORDER BY p.updated_at DESC LIMIT ? OFFSET ?".format(marks),
            list(kinds) + [MIN_POSITION, limit, offset])

    # -- show progress ------------------------------------------------------------------

    def set_show_structure(self, tmdb, seasons, last_aired, ended=False):
        """Store a show's seasons ([[season, episode count], ...]) and last aired (season, episode)."""
        tmdb = str(tmdb)

        def run(conn):
            conn.execute(
                "INSERT OR REPLACE INTO shows (tmdb, seasons, last_aired, ended, refreshed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tmdb, json.dumps([[int(s), int(n)] for s, n in seasons]),
                 json.dumps(list(last_aired)) if last_aired else "", 1 if ended else 0, time.time()))
            self._recompute(conn, tmdb)
        self._write(run)

    def show_structure(self, tmdb):
        row = self._one("SELECT * FROM shows WHERE tmdb = ?", (str(tmdb),))
        if not row:
            return None
        return {"seasons": json.loads(row["seasons"] or "[]"),
                "last_aired": json.loads(row["last_aired"]) if row["last_aired"] else None,
                "ended": bool(row["ended"]), "refreshed_at": row["refreshed_at"]}

    def _recompute(self, conn, tmdb):
        """Work out a show's next episode, episodes left and status from its plays."""
        watched = {(r[0], r[1]) for r in conn.execute(
            "SELECT DISTINCT season, episode FROM plays WHERE kind = 'episode' AND tmdb = ?", (tmdb,))}
        row = conn.execute("SELECT seasons, last_aired FROM shows WHERE tmdb = ?", (tmdb,)).fetchone()
        seasons = json.loads(row[0] or "[]") if row else None
        last_aired = json.loads(row[1]) if row and row[1] else None
        nxt, left = model.next_episode(watched, seasons, last_aired)
        current = conn.execute("SELECT status, next_season, next_episode FROM media "
                               "WHERE kind = 'tv' AND tmdb = ?", (tmdb,)).fetchone()
        if current is None:
            return
        status = current[0]
        if watched:
            status = STATUS_COMPLETED if (seasons and left == 0) else STATUS_IN_PROGRESS
        elif status in (STATUS_IN_PROGRESS, STATUS_COMPLETED):
            started = conn.execute("SELECT 1 FROM progress WHERE kind = 'episode' AND tmdb = ?",
                                   (tmdb,)).fetchone()
            status = STATUS_IN_PROGRESS if started else STATUS_PLANNING
        same = nxt and (current[1], current[2]) == tuple(nxt)
        conn.execute(
            "UPDATE media SET status = ?, next_season = ?, next_episode = ?, episodes_left = ?{} "
            "WHERE kind = 'tv' AND tmdb = ?".format(
                "" if same else ", next_title = '', next_thumb = '', next_plot = '', next_aired = ''"),
            (status, nxt[0] if nxt else None, nxt[1] if nxt else None, left if seasons else None, tmdb))

    def set_next_meta(self, tmdb, season, episode, title="", thumb="", plot="", aired=""):
        """Details of a show's next episode, kept only while it is still the next one."""
        self._write(lambda c: c.execute(
            "UPDATE media SET next_title = ?, next_thumb = ?, next_plot = ?, next_aired = ? "
            "WHERE kind = 'tv' AND tmdb = ? AND next_season = ? AND next_episode = ?",
            (title or "", thumb or "", plot or "", aired or "", str(tmdb), int(season), int(episode))))

    def shows_to_refresh(self, older_than, limit=50):
        """Watched shows whose seasons were last read before ``older_than`` (or never)."""
        return [r["tmdb"] for r in self._rows(
            "SELECT m.tmdb FROM media m LEFT JOIN shows s ON s.tmdb = m.tmdb "
            "WHERE m.kind = 'tv' AND m.status IN (?, ?) AND COALESCE(s.refreshed_at, 0) < ? "
            "ORDER BY m.activity_at DESC LIMIT ?",
            (STATUS_IN_PROGRESS, STATUS_COMPLETED, older_than, limit))]

    def next_missing_meta(self, limit=50):
        """(tmdb, season, episode) of next episodes whose details are not stored yet."""
        return [(r["tmdb"], r["next_season"], r["next_episode"]) for r in self._rows(
            "SELECT tmdb, next_season, next_episode FROM media WHERE kind = 'tv' AND status = ? "
            "AND next_season IS NOT NULL AND next_title = '' ORDER BY activity_at DESC LIMIT ?",
            (STATUS_IN_PROGRESS, limit))]

    # -- imports ----------------------------------------------------------------------

    def import_history(self, titles, plays, progress, now=None):
        """Merge imported history in one transaction; running the same import twice adds nothing.

        titles: dicts of kind ("movie"/"tv"), tmdb, meta, status (or None), added_at, activity_at.
        A title's status only moves forward (Planning, In progress, Completed); a show's then
        follows from its plays. plays: (kind, tmdb, season, episode, watched_at or None).
        progress: (kind, tmdb, season, episode, position, duration, updated_at), kept only when
        newer than the stored resume point and not followed by a play.
        """
        now = now or time.time()
        counts = {"titles_added": 0, "plays_added": 0, "progress_added": 0}

        def run(conn):
            shows = set()
            for t in titles:
                kind, tmdb = _title_kind(t["kind"]), str(t["tmdb"])
                had = conn.execute("SELECT status FROM media WHERE kind = ? AND tmdb = ?",
                                   (kind, tmdb)).fetchone()
                old = had[0] if had else None
                self._ensure(conn, kind, tmdb, t.get("meta"), t.get("added_at") or now)
                status = t.get("status")
                if status is not None and (old is None or status > old):
                    conn.execute("UPDATE media SET status = ? WHERE kind = ? AND tmdb = ?", (status, kind, tmdb))
                    if old is None:
                        counts["titles_added"] += 1
                conn.execute("UPDATE media SET added_at = MIN(added_at, ?), activity_at = MAX(activity_at, ?) "
                             "WHERE kind = ? AND tmdb = ?",
                             (t.get("added_at") or now, t.get("activity_at") or 0, kind, tmdb))
            for kind, tmdb, season, episode, when in plays:
                play_kind, tmdb = _play_kind(kind), str(tmdb)
                season, episode = _key(season, episode) if play_kind == "episode" else (-1, -1)
                key = (play_kind, tmdb, season, episode)
                match = "kind = ? AND tmdb = ? AND season = ? AND episode = ?"
                if when:
                    same = conn.execute("SELECT 1 FROM plays WHERE {} AND ABS(watched_at - ?) < ?".format(match),
                                        key + (when, SAME_PLAY)).fetchone()
                else:
                    same = conn.execute("SELECT 1 FROM plays WHERE " + match, key).fetchone()
                if same:
                    continue
                when = when or now
                self._ensure(conn, _title_kind(kind), tmdb, None, when)
                conn.execute("INSERT INTO plays (kind, tmdb, season, episode, watched_at) VALUES (?, ?, ?, ?, ?)",
                             key + (when,))
                conn.execute("DELETE FROM progress WHERE {} AND updated_at <= ?".format(match), key + (when,))
                conn.execute("UPDATE media SET activity_at = MAX(activity_at, ?) WHERE kind = ? AND tmdb = ?",
                             (when, _title_kind(kind), tmdb))
                counts["plays_added"] += 1
                if play_kind == "movie":
                    conn.execute("UPDATE media SET status = ? WHERE kind = 'movie' AND tmdb = ?",
                                 (STATUS_COMPLETED, tmdb))
                else:
                    shows.add(tmdb)
            for kind, tmdb, season, episode, position, duration, updated in progress:
                play_kind, tmdb = _play_kind(kind), str(tmdb)
                season, episode = _key(season, episode) if play_kind == "episode" else (-1, -1)
                key = (play_kind, tmdb, season, episode)
                if position < MIN_POSITION or (duration and position >= duration * FINISHED):
                    continue
                newer = conn.execute(
                    "SELECT 1 FROM progress WHERE kind = ? AND tmdb = ? AND season = ? AND episode = ? "
                    "AND updated_at >= ? UNION SELECT 1 FROM plays WHERE kind = ? AND tmdb = ? AND season = ? "
                    "AND episode = ? AND watched_at >= ?", key + (updated,) + key + (updated,)).fetchone()
                if newer:
                    continue
                self._ensure(conn, _title_kind(kind), tmdb, None, updated)
                conn.execute("INSERT OR REPLACE INTO progress (kind, tmdb, season, episode, position, duration, "
                             "title, updated_at) VALUES (?, ?, ?, ?, ?, ?, '', ?)",
                             key + (float(position), float(duration or 0), updated))
                conn.execute("UPDATE media SET status = ? WHERE kind = ? AND tmdb = ? "
                             "AND (status IS NULL OR status = ?)",
                             (STATUS_IN_PROGRESS, _title_kind(kind), tmdb, STATUS_PLANNING))
                counts["progress_added"] += 1
            # Shows with new plays work out their next episode and status; the others keep theirs.
            for tmdb in shows:
                self._recompute(conn, tmdb)
        self._write(run)
        return counts

    def log_import(self, importer, source, summary):
        self._write(lambda c: c.execute(
            "INSERT INTO imports (importer, source, imported_at, summary) VALUES (?, ?, ?, ?)",
            (importer, source or "", time.time(), json.dumps(summary))))

    def imports(self, limit=20):
        rows = self._rows("SELECT * FROM imports ORDER BY imported_at DESC LIMIT ?", (limit,))
        for row in rows:
            row["summary"] = json.loads(row["summary"] or "{}")
        return rows

    # -- lists ------------------------------------------------------------------------

    def titles(self, kind, statuses=None, order="activity", limit=20, offset=0):
        """Tracked movies or shows, optionally only some statuses."""
        sql = "SELECT * FROM media WHERE kind = ? AND status IS NOT NULL"
        args = [_title_kind(kind)]
        if statuses is not None:
            sql += " AND status IN ({})".format(",".join("?" * len(statuses)))
            args += list(statuses)
        sql += {"activity": " ORDER BY activity_at DESC, added_at DESC",
                "added": " ORDER BY added_at DESC",
                "title": " ORDER BY title COLLATE NOCASE"}.get(order, " ORDER BY activity_at DESC")
        return self._rows(sql + " LIMIT ? OFFSET ?", args + [limit, offset])

    def next_up(self, limit=20, offset=0, order="activity"):
        """Shows in progress whose next episode has aired, with that episode's details."""
        sql = ("SELECT * FROM media WHERE kind = 'tv' AND status = ? AND next_season IS NOT NULL")
        sql += {"title": " ORDER BY title COLLATE NOCASE",
                "aired": " ORDER BY next_aired DESC, activity_at DESC"}.get(order, " ORDER BY activity_at DESC")
        return self._rows(sql + " LIMIT ? OFFSET ?", (STATUS_IN_PROGRESS, limit, offset))

    def history(self, kind, limit=20, offset=0):
        """Latest plays of movies or episodes, newest first, one row per play."""
        play_kind = _play_kind(kind)
        return self._rows(
            "SELECT p.*, m.title AS media_title, m.imdb, m.tvdb, m.poster, m.year FROM plays p "
            "LEFT JOIN media m ON m.tmdb = p.tmdb AND m.kind = ? WHERE p.kind = ? "
            "ORDER BY p.watched_at DESC LIMIT ? OFFSET ?",
            (_title_kind(kind), play_kind, limit, offset))

    def states(self, kind, tmdb_ids):
        """{tmdb: row} for those of ``tmdb_ids`` that are tracked, with a movie's playcount."""
        tmdb_ids = [str(t) for t in tmdb_ids if t]
        if not tmdb_ids:
            return {}
        kind = _title_kind(kind)
        found = {}
        for start in range(0, len(tmdb_ids), 500):
            chunk = tmdb_ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._rows(
                "SELECT m.*, (SELECT COUNT(*) FROM plays p WHERE p.kind = 'movie' AND p.tmdb = m.tmdb "
                "AND m.kind = 'movie') AS playcount FROM media m "
                "WHERE m.kind = ? AND m.status IS NOT NULL AND m.tmdb IN ({})".format(marks),
                [kind] + chunk)
            found.update((r["tmdb"], r) for r in rows)
        return found

    def watched_ids(self, kind):
        """TMDb ids of movies or shows with any play."""
        return {r["tmdb"] for r in self._rows("SELECT DISTINCT tmdb FROM plays WHERE kind = ?",
                                              (_play_kind(kind),))}
