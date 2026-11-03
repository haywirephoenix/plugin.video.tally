"""Tally's own tracking database: watchlist, plays, resume points and show progress.

Plain sqlite3 and no Kodi imports, so it runs (and is tested) outside Kodi. The schema is
versioned with PRAGMA user_version; MIGRATIONS[n] takes a database from version n to n + 1,
so later features (recommendations) add their tables with a new entry.

Identity is TMDb: movies by their id, shows and their episodes by the show's id.
``kind`` is "movie" or "tv" for titles; plays and resume points use "movie" or "episode".
Movies have season = episode = -1 in tables keyed on them, so the keys have no NULLs.

Sync: every change to what is watched or listed is made as an op (a name and its JSON data:
OPS) and kept in the changes log, in the same transaction. Paired devices pull each other's log
(changes) and apply what is new to them (apply_changes), keeping it in their own log under its
first uid so it reaches the devices they are paired with too. Applying an op is idempotent and
order-independent: plays keep their uid, a resume point keeps the newest, and a removal leaves
its time in the removed table, so nothing older it removed comes back from a peer. The shows
table is each device's own TMDb cache and is not synced; titles' status and next episode follow
from the plays.
"""

import json
import sqlite3
import threading
import time
import uuid

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
    # 2 -> 3: when the last episode came out and the next is due (cards' Coming soon / New episodes)
    """
    ALTER TABLE shows ADD COLUMN last_air_date TEXT NOT NULL DEFAULT '';
    ALTER TABLE shows ADD COLUMN next_air_date TEXT NOT NULL DEFAULT '';
    """,
    # 3 -> 4: sync. Each play gets a uid; the changes log (seq: this device's order, a peer's
    # cursor), removals' times, this device's id and its paired devices (peers: the token both
    # send, where it was last reached, and how far its log was pulled). The log is seeded with
    # what is stored (_seed_changes), so a peer's first sync gets the whole history.
    """
    ALTER TABLE plays ADD COLUMN uid TEXT;
    UPDATE plays SET uid = lower(hex(randomblob(16)));
    CREATE UNIQUE INDEX plays_uid ON plays (uid);
    CREATE TABLE changes (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        uid TEXT NOT NULL UNIQUE,
        op TEXT NOT NULL,
        data TEXT NOT NULL,
        at REAL NOT NULL,
        item TEXT
    );
    CREATE INDEX changes_item ON changes (op, item);
    CREATE TABLE removed (
        scope TEXT NOT NULL,
        kind TEXT NOT NULL,
        tmdb TEXT NOT NULL,
        season INTEGER NOT NULL DEFAULT -1,
        episode INTEGER NOT NULL DEFAULT -1,
        removed_at REAL NOT NULL,
        PRIMARY KEY (scope, kind, tmdb, season, episode)
    );
    CREATE TABLE sync (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    INSERT INTO sync (key, value) VALUES ('device', lower(hex(randomblob(8))));
    CREATE TABLE peers (
        device TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        token TEXT NOT NULL,
        address TEXT NOT NULL DEFAULT '',
        port INTEGER NOT NULL DEFAULT 0,
        cursor INTEGER NOT NULL DEFAULT 0,
        paired_at REAL NOT NULL,
        synced_at REAL NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT ''
    );
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)
# Migrations followed by a Store method, in the same transaction: {from version: method name}
MIGRATION_HOOKS = {3: "_seed_changes"}

# Ops (OPS: the changes log's op names) and their data. kind is a title's ("movie"/"tv") for
# title, unlist and forget, a play's ("movie"/"episode") for the rest; season/episode -1 for movies.
#   title     {kind, tmdb, meta, added_at}: on the watchlist (unless removed since added_at)
#   unlist    {kind, tmdb, at}: off the watchlist
#   play      {uid, kind, tmdb, season, episode, watched_at, meta}
#   unplay    {kind, tmdb, season, episode, at}: plays up to at removed (mark unwatched)
#   progress  {kind, tmdb, season, episode, position, duration, title, updated_at, meta}
#   unprogress {kind, tmdb, season, episode, at}: a resume point up to at removed
#   forget    {kind, tmdb, at}: a title's plays and resume points up to at, and the title
OPS = ("title", "unlist", "play", "unplay", "progress", "unprogress", "forget")
META_FIELDS = ("imdb", "tvdb", "title", "year", "poster")

# An imported play this close to one already stored is the same play (an import run twice).
SAME_PLAY = 60


def _key(season, episode):
    return (-1 if season is None else int(season), -1 if episode is None else int(episode))


def _play_kind(kind):
    return "movie" if kind == "movie" else "episode"


def _title_kind(kind):
    return "movie" if kind == "movie" else "tv"


def _item(kind, tmdb, season=None, episode=None):
    """An op's movie or episode: {kind, tmdb, season, episode}, a movie's season/episode -1."""
    kind = _play_kind(kind)
    season, episode = _key(season, episode) if kind == "episode" else (-1, -1)
    return {"kind": kind, "tmdb": str(tmdb), "season": season, "episode": episode}


def _play(kind, tmdb, season, episode, watched_at, meta=None):
    return dict(_item(kind, tmdb, season, episode), uid=uuid.uuid4().hex, watched_at=watched_at, meta=meta or {})


def _progress(kind, tmdb, season, episode, position, duration, title, updated_at, meta=None):
    return dict(_item(kind, tmdb, season, episode), position=float(position), duration=float(duration or 0),
                title=title or "", updated_at=updated_at, meta=meta or {})


def _where(item):
    """A WHERE clause and its args for an op's movie or episode, in plays or progress."""
    return ("kind = ? AND tmdb = ? AND season = ? AND episode = ?",
            (item["kind"], item["tmdb"], item["season"], item["episode"]))


class Store:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._last = 0
        self._conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _now(self):
        """The time of a change made here: later than the last one's, even within one clock tick
        (a resume point saved right after a play is newer than it), and than every change a peer
        sent (apply_changes): what is done here after seeing one wins over it, whatever the two
        clocks say."""
        with self._lock:
            self._last = max(time.time(), self._last + 0.001)
            return self._last

    def close(self):
        with self._lock:
            self._conn.close()

    def _migrate(self):
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for target in range(version, SCHEMA_VERSION):
                # executescript commits first; the version bump goes with the migration's own
                # statements, and its hook's, all or nothing.
                self._conn.executescript("BEGIN;\n{}".format(MIGRATIONS[target]))
                try:
                    if target in MIGRATION_HOOKS:
                        getattr(self, MIGRATION_HOOKS[target])(self._conn)
                    self._conn.execute("PRAGMA user_version = {}".format(target + 1))
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise

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
        return self._change("title", {"kind": _title_kind(kind), "tmdb": str(tmdb), "meta": meta or {},
                                      "added_at": self._now()})

    def remove_watchlist(self, kind, tmdb):
        """Take a title off the watchlist; only one with nothing watched (status Planning)."""
        return self._change("unlist", {"kind": _title_kind(kind), "tmdb": str(tmdb), "at": self._now()})

    def untrack(self, kind, tmdb):
        """Forget a title: its status, plays and resume points."""
        self._change("forget", {"kind": _title_kind(kind), "tmdb": str(tmdb), "at": self._now()})

    # -- plays --------------------------------------------------------------------------

    def add_play(self, kind, tmdb, season=None, episode=None, meta=None, when=None):
        """Record a finished play of a movie or episode; it also clears its resume point."""
        self._change("play", _play(kind, tmdb, season, episode, when or self._now(), meta))

    def add_plays(self, tmdb, episodes, meta=None):
        """Mark many episodes of a show watched at once (season/show "mark watched")."""
        now = self._now()
        self._changes([("play", _play("episode", tmdb, season, episode, now, meta)) for season, episode in episodes])

    def remove_play(self, kind, tmdb, season=None, episode=None):
        """Remove every play of a movie or episode (mark unwatched)."""
        return self._change("unplay", dict(_item(kind, tmdb, season, episode), at=self._now()))

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

    def show_air_dates(self, tmdbs):
        """{tmdb: (ended, last_air_date, next_air_date)} for the shows the store has read."""
        tmdbs = sorted({str(t) for t in tmdbs if t})
        if not tmdbs:
            return {}
        rows = self._rows("SELECT tmdb, ended, last_air_date, next_air_date FROM shows WHERE tmdb IN ({})".format(
            ", ".join("?" * len(tmdbs))), tmdbs)
        return {str(r["tmdb"]): (bool(r["ended"]), r["last_air_date"], r["next_air_date"]) for r in rows}

    def watched_episode_counts(self, tmdbs):
        """{tmdb: episodes watched} for shows (each episode once; specials left out, as the
        episodes left are counted without them)."""
        tmdbs = sorted({str(t) for t in tmdbs if t})
        if not tmdbs:
            return {}
        rows = self._rows("SELECT tmdb, COUNT(DISTINCT season * 10000 + episode) AS n FROM plays "
                          "WHERE kind = 'episode' AND season > 0 AND tmdb IN ({}) GROUP BY tmdb".format(
                              ", ".join("?" * len(tmdbs))), tmdbs)
        return {str(r["tmdb"]): r["n"] for r in rows}

    # -- resume points ----------------------------------------------------------------

    def save_progress(self, kind, tmdb, position, duration=0, season=None, episode=None,
                      title="", meta=None):
        """Save a resume point. Under MIN_POSITION nothing is kept; past FINISHED it counts as a play."""
        if duration and position >= duration * FINISHED:
            self.add_play(kind, tmdb, season, episode, meta=meta)
            return "played"
        if position < MIN_POSITION:
            return None
        self._change("progress", _progress(kind, tmdb, season, episode, position, duration, title,
                                           self._now(), meta))
        return "saved"

    def get_progress(self, kind, tmdb, season=None, episode=None):
        """(position, duration) of a resume point, or (0, 0)."""
        season, episode = _key(season, episode) if _play_kind(kind) == "episode" else (-1, -1)
        row = self._one("SELECT position, duration FROM progress WHERE kind = ? AND tmdb = ? "
                        "AND season = ? AND episode = ?", (_play_kind(kind), str(tmdb), season, episode))
        return (row["position"], row["duration"]) if row else (0, 0)

    def clear_progress(self, kind, tmdb, season=None, episode=None):
        return self._change("unprogress", dict(_item(kind, tmdb, season, episode), at=self._now()))

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

    def set_show_structure(self, tmdb, seasons, last_aired, ended=False, last_air_date="", next_air_date=""):
        """Store a show's seasons ([[season, episode count], ...]), last aired (season, episode), and
        when the last episode came out and the next is due."""
        tmdb = str(tmdb)

        def run(conn):
            conn.execute(
                "INSERT OR REPLACE INTO shows (tmdb, seasons, last_aired, ended, refreshed_at, last_air_date, "
                "next_air_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tmdb, json.dumps([[int(s), int(n)] for s, n in seasons]),
                 json.dumps(list(last_aired)) if last_aired else "", 1 if ended else 0, time.time(),
                 last_air_date or "", next_air_date or ""))
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
                added = t.get("added_at") or now
                self._ensure(conn, kind, tmdb, t.get("meta"), added)
                status = t.get("status")
                if status is not None and (old is None or status > old):
                    conn.execute("UPDATE media SET status = ? WHERE kind = ? AND tmdb = ?", (status, kind, tmdb))
                    if old is None:
                        counts["titles_added"] += 1
                        # Peers list it; its status there follows from the plays they get with it.
                        self._log(conn, "title", {"kind": kind, "tmdb": tmdb, "added_at": added})
                conn.execute("UPDATE media SET added_at = MIN(added_at, ?), activity_at = MAX(activity_at, ?) "
                             "WHERE kind = ? AND tmdb = ?", (added, t.get("activity_at") or 0, kind, tmdb))
            for kind, tmdb, season, episode, when in plays:
                if not when:
                    # Without a time, any play of it already stored is this one.
                    match, args = _where(_item(kind, tmdb, season, episode))
                    if conn.execute("SELECT 1 FROM plays WHERE " + match, args).fetchone():
                        continue
                data = _play(kind, tmdb, season, episode, when or now)
                if self._apply(conn, "play", data, shows):
                    self._log(conn, "play", data)
                    counts["plays_added"] += 1
            for kind, tmdb, season, episode, position, duration, updated in progress:
                if position < MIN_POSITION or (duration and position >= duration * FINISHED):
                    continue
                data = _progress(kind, tmdb, season, episode, position, duration, "", updated)
                if self._apply(conn, "progress", data, shows):
                    self._log(conn, "progress", data)
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

    # -- sync between devices --------------------------------------------------------

    @property
    def device_id(self):
        return self._one("SELECT value FROM sync WHERE key = 'device'")["value"]

    def last_change(self):
        """The seq of the log's latest change (0 when it is empty)."""
        return self._one("SELECT COALESCE(MAX(seq), 0) AS seq FROM changes")["seq"]

    def changes(self, after=0, limit=500):
        """The log's changes after seq ``after``, oldest first: [{seq, uid, op, data, at}]."""
        rows = self._rows("SELECT seq, uid, op, data, at FROM changes WHERE seq > ? ORDER BY seq LIMIT ?",
                          (int(after), int(limit)))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    def peers(self):
        """The paired devices: [{device, name, token, address, port, cursor, paired_at, synced_at, error}]."""
        return self._rows("SELECT * FROM peers ORDER BY name COLLATE NOCASE")

    def peer(self, device):
        return self._one("SELECT * FROM peers WHERE device = ?", (str(device),))

    def save_peer(self, device, name, token, address, port):
        """Pair with a device (or pair again: a new token, how far its log was pulled kept)."""
        self._write(lambda c: c.execute(
            "INSERT INTO peers (device, name, token, address, port, paired_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (device) DO UPDATE SET name = excluded.name, token = excluded.token, "
            "address = excluded.address, port = excluded.port, paired_at = excluded.paired_at, error = ''",
            (str(device), name or "", token, address or "", int(port or 0), time.time())))

    def update_peer(self, device, **fields):
        """Change a paired device's name, address, port, cursor, synced_at or error."""
        fields = {k: v for k, v in fields.items() if k in ("name", "address", "port", "cursor", "synced_at", "error")}
        if fields:
            self._write(lambda c: c.execute("UPDATE peers SET {} WHERE device = ?".format(
                ", ".join("{} = ?".format(k) for k in fields)), list(fields.values()) + [str(device)]))

    def remove_peer(self, device):
        return self._write(lambda c: c.execute("DELETE FROM peers WHERE device = ?", (str(device),))).rowcount > 0

    def apply_changes(self, changes):
        """Apply a peer's changes ({uid, op, data, at}, as changes returns them) and keep them in
        the log, all in one transaction; those already in it are skipped. One this version can't
        apply (a newer Tally's op, or data it can't read) is kept, to pass on, but not applied.
        Returns how many were new."""
        def run(conn):
            shows, new = set(), 0
            for change in changes:
                op, data = str(change["op"]), change["data"]
                if not self._insert_change(conn, str(change["uid"]), op, data, float(change["at"])):
                    continue
                new += 1
                self._last = max(self._last, float(change["at"]))
                if op not in OPS:
                    continue
                conn.execute("SAVEPOINT change")
                try:
                    self._apply(conn, op, data, shows)
                except (KeyError, TypeError, ValueError, AttributeError):
                    conn.execute("ROLLBACK TO change")
                conn.execute("RELEASE change")
            for tmdb in shows:
                self._recompute(conn, tmdb)
            return new
        return self._write(run)

    def _change(self, op, data):
        """Make a change here: apply it and log it for peers when it changed anything."""
        return self._changes([(op, data)])[0]

    def _changes(self, ops):
        def run(conn):
            shows, results = set(), []
            for op, data in ops:
                result = self._apply(conn, op, data, shows)
                if result:
                    self._log(conn, op, data)
                results.append(result)
            for tmdb in shows:
                self._recompute(conn, tmdb)
            return results
        return self._write(run)

    def _log(self, conn, op, data):
        """Log a change made here, with what the store knows of its title (a peer may not have it)."""
        if op in ("title", "play", "progress"):
            row = conn.execute("SELECT * FROM media WHERE kind = ? AND tmdb = ?",
                               (_title_kind(data["kind"]), data["tmdb"])).fetchone()
            if row:
                data = dict(data, meta={k: row[k] for k in META_FIELDS if row[k] not in (None, "")})
        at = next(data[k] for k in ("updated_at", "watched_at", "added_at", "at") if k in data)
        self._insert_change(conn, uuid.uuid4().hex, op, data, at)

    @staticmethod
    def _insert_change(conn, uid, op, data, at):
        """Add a change to the log; False when it is there already. Only the latest resume point
        of a movie or episode is kept: an older one would change nothing where it arrives."""
        item = ":".join(str(data.get(k)) for k in ("kind", "tmdb", "season", "episode")) if op == "progress" else None
        cur = conn.execute("INSERT OR IGNORE INTO changes (uid, op, data, at, item) VALUES (?, ?, ?, ?, ?)",
                           (uid, op, json.dumps(data), at, item))
        if item and cur.rowcount:
            conn.execute("DELETE FROM changes WHERE op = 'progress' AND item = ? AND seq != (SELECT seq FROM "
                         "changes WHERE op = 'progress' AND item = ? ORDER BY at DESC, seq DESC LIMIT 1)",
                         (item, item))
        return cur.rowcount > 0

    def _seed_changes(self, conn):
        """Log what is stored already (migration 3 -> 4): listed titles, then plays and resume
        points, each at the time it happened."""
        for row in conn.execute("SELECT kind, tmdb, added_at FROM media WHERE status IS NOT NULL "
                                "ORDER BY added_at").fetchall():
            self._log(conn, "title", {"kind": row["kind"], "tmdb": row["tmdb"], "added_at": row["added_at"]})
        for row in conn.execute("SELECT * FROM plays ORDER BY watched_at").fetchall():
            self._log(conn, "play", dict(_item(row["kind"], row["tmdb"], row["season"], row["episode"]),
                                         uid=row["uid"], watched_at=row["watched_at"]))
        for row in conn.execute("SELECT * FROM progress ORDER BY updated_at").fetchall():
            self._log(conn, "progress", _progress(row["kind"], row["tmdb"], row["season"], row["episode"],
                                                  row["position"], row["duration"], row["title"], row["updated_at"]))

    # Applying ops: each returns whether it changed anything (a count for removals), and adds
    # to shows the shows whose next episode and status need working out again.

    def _apply(self, conn, op, data, shows):
        return getattr(self, "_apply_" + op)(conn, data, shows)

    @staticmethod
    def _remove(conn, scope, kind, tmdb, at, season=-1, episode=-1):
        """Note a removal's time (the latest kept): scope list, plays, progress or forget."""
        conn.execute("INSERT INTO removed (scope, kind, tmdb, season, episode, removed_at) VALUES (?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT (scope, kind, tmdb, season, episode) "
                     "DO UPDATE SET removed_at = MAX(removed_at, excluded.removed_at)",
                     (scope, kind, tmdb, season, episode, at))

    @staticmethod
    def _removed(conn, item, scope, when):
        """Whether an op's movie or episode (scope plays or progress), or its title, was removed
        at or after ``when``: what it would add is older than the removal."""
        return conn.execute(
            "SELECT 1 FROM removed WHERE removed_at >= ? AND ((scope = ? AND kind = ? AND tmdb = ? "
            "AND season = ? AND episode = ?) OR (scope = 'forget' AND kind = ? AND tmdb = ?))",
            (when, scope, item["kind"], item["tmdb"], item["season"], item["episode"],
             _title_kind(item["kind"]), item["tmdb"])).fetchone() is not None

    def _apply_title(self, conn, d, shows):
        kind, tmdb, added = _title_kind(d["kind"]), str(d["tmdb"]), float(d["added_at"])
        if conn.execute("SELECT 1 FROM removed WHERE scope IN ('list', 'forget') AND kind = ? AND tmdb = ? "
                        "AND removed_at >= ?", (kind, tmdb, added)).fetchone():
            return False
        self._ensure(conn, kind, tmdb, d.get("meta"), added)
        return conn.execute("UPDATE media SET status = ?, added_at = ? WHERE kind = ? AND tmdb = ? "
                            "AND status IS NULL", (STATUS_PLANNING, added, kind, tmdb)).rowcount > 0

    def _apply_unlist(self, conn, d, shows):
        kind, tmdb, at = _title_kind(d["kind"]), str(d["tmdb"]), float(d["at"])
        self._remove(conn, "list", kind, tmdb, at)
        return conn.execute("UPDATE media SET status = NULL WHERE kind = ? AND tmdb = ? AND status = ? "
                            "AND added_at <= ?", (kind, tmdb, STATUS_PLANNING, at)).rowcount > 0

    def _apply_play(self, conn, d, shows):
        item, uid, when = _item(d["kind"], d["tmdb"], d["season"], d["episode"]), str(d["uid"]), float(d["watched_at"])
        match, args = _where(item)
        # A play already here, removed since, or one within SAME_PLAY of it (the same watch,
        # recorded on two devices before they synced) adds nothing.
        if (conn.execute("SELECT 1 FROM plays WHERE uid = ?", (uid,)).fetchone()
                or self._removed(conn, item, "plays", when)
                or conn.execute("SELECT 1 FROM plays WHERE {} AND ABS(watched_at - ?) < ?".format(match),
                                args + (when, SAME_PLAY)).fetchone()):
            return False
        kind, tmdb = _title_kind(item["kind"]), item["tmdb"]
        self._ensure(conn, kind, tmdb, d.get("meta"), when)
        conn.execute("INSERT INTO plays (uid, kind, tmdb, season, episode, watched_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (uid,) + args + (when,))
        conn.execute("DELETE FROM progress WHERE {} AND updated_at <= ?".format(match), args + (when,))
        conn.execute("UPDATE media SET activity_at = MAX(activity_at, ?) WHERE kind = ? AND tmdb = ?",
                     (when, kind, tmdb))
        if kind == "movie":
            conn.execute("UPDATE media SET status = ? WHERE kind = 'movie' AND tmdb = ?", (STATUS_COMPLETED, tmdb))
        else:
            shows.add(tmdb)
        return True

    def _apply_unplay(self, conn, d, shows):
        item, at = _item(d["kind"], d["tmdb"], d["season"], d["episode"]), float(d["at"])
        match, args = _where(item)
        self._remove(conn, "plays", item["kind"], item["tmdb"], at, item["season"], item["episode"])
        removed = conn.execute("DELETE FROM plays WHERE {} AND watched_at <= ?".format(match), args + (at,)).rowcount
        tmdb = item["tmdb"]
        if item["kind"] == "episode":
            shows.add(tmdb)
        elif not conn.execute("SELECT 1 FROM plays WHERE kind = 'movie' AND tmdb = ?", (tmdb,)).fetchone():
            left = conn.execute("SELECT 1 FROM progress WHERE kind = 'movie' AND tmdb = ?", (tmdb,)).fetchone()
            conn.execute("UPDATE media SET status = ? WHERE kind = 'movie' AND tmdb = ? AND status = ?",
                         (STATUS_IN_PROGRESS if left else STATUS_PLANNING, tmdb, STATUS_COMPLETED))
        return removed

    def _apply_progress(self, conn, d, shows):
        item, when = _item(d["kind"], d["tmdb"], d["season"], d["episode"]), float(d["updated_at"])
        match, args = _where(item)
        # Kept only when newer than the resume point here, any play of it and its removal.
        if (self._removed(conn, item, "progress", when)
                or conn.execute("SELECT 1 FROM progress WHERE {0} AND updated_at >= ? UNION ALL "
                                "SELECT 1 FROM plays WHERE {0} AND watched_at >= ?".format(match),
                                args + (when,) + args + (when,)).fetchone()):
            return False
        kind, tmdb = _title_kind(item["kind"]), item["tmdb"]
        self._ensure(conn, kind, tmdb, d.get("meta"), when)
        conn.execute("INSERT OR REPLACE INTO progress (kind, tmdb, season, episode, position, duration, title, "
                     "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     args + (float(d["position"]), float(d.get("duration") or 0), d.get("title") or "", when))
        conn.execute("UPDATE media SET activity_at = MAX(activity_at, ?) WHERE kind = ? AND tmdb = ?",
                     (when, kind, tmdb))
        # Being watched is in progress, unless it was watched before (a rewatch).
        conn.execute("UPDATE media SET status = ? WHERE kind = ? AND tmdb = ? AND (status IS NULL OR status = ?)",
                     (STATUS_IN_PROGRESS, kind, tmdb, STATUS_PLANNING))
        return True

    def _apply_unprogress(self, conn, d, shows):
        item, at = _item(d["kind"], d["tmdb"], d["season"], d["episode"]), float(d["at"])
        match, args = _where(item)
        self._remove(conn, "progress", item["kind"], item["tmdb"], at, item["season"], item["episode"])
        removed = conn.execute("DELETE FROM progress WHERE {} AND updated_at <= ?".format(match),
                               args + (at,)).rowcount
        if item["kind"] == "movie" and not conn.execute(
                "SELECT 1 FROM progress WHERE kind = 'movie' AND tmdb = ?", (item["tmdb"],)).fetchone():
            # Nothing left in progress and never finished: back to the watchlist state it came from.
            conn.execute("UPDATE media SET status = NULL WHERE kind = 'movie' AND tmdb = ? AND status = ?",
                         (item["tmdb"], STATUS_IN_PROGRESS))
        return removed

    def _apply_forget(self, conn, d, shows):
        kind, tmdb, at = _title_kind(d["kind"]), str(d["tmdb"]), float(d["at"])
        play_kind = _play_kind(kind)
        self._remove(conn, "forget", kind, tmdb, at)
        conn.execute("DELETE FROM plays WHERE kind = ? AND tmdb = ? AND watched_at <= ?", (play_kind, tmdb, at))
        conn.execute("DELETE FROM progress WHERE kind = ? AND tmdb = ? AND updated_at <= ?", (play_kind, tmdb, at))
        # What a peer watched after it was forgotten (before they synced) stays.
        played = conn.execute("SELECT 1 FROM plays WHERE kind = ? AND tmdb = ?", (play_kind, tmdb)).fetchone()
        started = conn.execute("SELECT 1 FROM progress WHERE kind = ? AND tmdb = ?", (play_kind, tmdb)).fetchone()
        if not (played or started):
            conn.execute("DELETE FROM media WHERE kind = ? AND tmdb = ? AND added_at <= ?", (kind, tmdb, at))
            if kind == "tv":
                conn.execute("DELETE FROM shows WHERE tmdb = ?", (tmdb,))
        elif kind == "tv":
            shows.add(tmdb)
        else:
            conn.execute("UPDATE media SET status = ? WHERE kind = 'movie' AND tmdb = ?",
                         (STATUS_COMPLETED if played else STATUS_IN_PROGRESS, tmdb))
        return True

    # -- TMDb Helper sync (script.module.tmdbhelper.sync.tally) ------------------------

    def play_counts(self, kind):
        """Plays of every movie or episode: [{tmdb, season, episode, plays, last_watched_at}]."""
        return self._rows(
            "SELECT tmdb, season, episode, COUNT(*) AS plays, MAX(watched_at) AS last_watched_at "
            "FROM plays WHERE kind = ? GROUP BY tmdb, season, episode", (_play_kind(kind),))

    def activity(self):
        """{list: "count|newest"} for movie_plays, episode_plays, progress and watchlist.

        Each value changes whenever its list does, removals included.
        """
        queries = {
            "movie_plays": ("SELECT COUNT(*), MAX(watched_at) FROM plays WHERE kind = 'movie'", ()),
            "episode_plays": ("SELECT COUNT(*), MAX(watched_at) FROM plays WHERE kind = 'episode'", ()),
            "progress": ("SELECT COUNT(*), MAX(updated_at) FROM progress", ()),
            "watchlist": ("SELECT COUNT(*), MAX(added_at) FROM media WHERE status = ?", (STATUS_PLANNING,)),
        }
        with self._lock:
            return {name: "{}|{}".format(*self._conn.execute(sql, args).fetchone())
                    for name, (sql, args) in queries.items()}

    def watched_ids(self, kind):
        """TMDb ids of movies or shows with any play."""
        return {r["tmdb"] for r in self._rows("SELECT DISTINCT tmdb FROM plays WHERE kind = ?",
                                              (_play_kind(kind),))}
