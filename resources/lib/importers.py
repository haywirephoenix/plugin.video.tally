"""Importing watch history from other trackers, each described by a JSON definition.

Plain Python and no Kodi imports, so it runs (and is tested) outside Kodi. A definition in
resources/importers/<id>.json (or the profile's importers folder) names the file format and how
each of Tally's fields is read from a row; ``Importer`` turns rows into ``Record`` dicts and
``apply`` merges them into the store. Formats other than CSV subclass ``Importer`` and register
in FORMATS.

A field spec is a column name, a dict, or a list of either (the first that gives a value wins):
    "title"                                       the column's text
    {"column": "x", "convert": "int"}             converted (int, float, minutes, year, time, json)
    {"columns": ["a", "b"], "convert": "time"}    the first of several columns
    {"column": "x", "convert": "json", "key": "tmdb_id"}   a key of a JSON object column
    {"column": "x", "map": {"a": "b"}}            looked up (lower-cased); unmapped gives nothing
    {"column": "x", "when": {"source": "tmdb"}}   only on rows where those columns match
A repeated column name is read as "name#2", "name#3", ...

Record fields: kind ("movie", "tv", "season" or "episode"; other rows are skipped), tmdb, imdb,
tvdb, title, year, poster, season, episode, status (planning, in_progress, paused, completed,
dropped), position, duration (seconds), watched_at, added_at, updated_at (epoch seconds).
Episode and season rows are identified by their show's ids. The definition's "statuses" maps
statuses Tally has no state for onto one it has (or null: the title keeps no status).
"""

import calendar
import csv
import io
import json
import os
import re
import time

from . import model

FIELDS = ("kind", "tmdb", "imdb", "tvdb", "title", "year", "poster", "season", "episode", "status",
          "position", "duration", "watched_at", "added_at", "updated_at")
KINDS = ("movie", "tv", "season", "episode")
TALLY_STATUSES = {"planning": model.STATUS_PLANNING, "in_progress": model.STATUS_IN_PROGRESS,
                  "completed": model.STATUS_COMPLETED}
_RANK = {model.STATUS_PLANNING: 0, model.STATUS_IN_PROGRESS: 1, model.STATUS_COMPLETED: 2}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?"
                  r"\s*(Z|[+-]\d{2}:?\d{2})?$")


# -- conversions --------------------------------------------------------------------------

def _to_int(value):
    return int(float(value))


def _to_time(value):
    """Epoch seconds from an ISO date/time (UTC unless it says otherwise) or a number."""
    try:
        number = float(value)
        return number / 1000 if number > 1e11 else number  # milliseconds
    except ValueError:
        pass
    match = _ISO.match(value.strip())
    if not match:
        return None
    y, mo, d, h, mi, s, zone = match.groups()
    stamp = calendar.timegm((int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0)))
    if zone and zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        stamp -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return float(stamp)


def _to_year(value):
    match = re.match(r"\s*(\d{4})", value)
    return int(match.group(1)) if match else None


CONVERT = {
    "text": lambda v: v,
    "int": _to_int,
    "float": float,
    "minutes": lambda v: float(v) * 60,
    "year": _to_year,
    "time": _to_time,
    "json": json.loads,
}


def _empty(value):
    return value is None or value == "" or value == [] or value == {}


def read_field(spec, row):
    """The value a field spec gives for a row, or None."""
    if isinstance(spec, list):
        for part in spec:
            value = read_field(part, row)
            if not _empty(value):
                return value
        return None
    if isinstance(spec, str):
        spec = {"column": spec}
    for column, wanted in (spec.get("when") or {}).items():
        if (row.get(column) or "").strip().lower() != str(wanted).lower():
            return None
    columns = spec.get("columns") or [spec.get("column")]
    for column in columns:
        value = _read_one(spec, (row.get(column) or "").strip())
        if not _empty(value):
            return value
    return None


def _read_one(spec, raw):
    if raw == "":
        return None
    try:
        value = CONVERT[spec.get("convert", "text")](raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if "key" in spec:
        value = value.get(spec["key"]) if isinstance(value, dict) else None
    if "map" in spec:
        value = spec["map"].get(str(value).strip().lower()) if value is not None else None
    return value


# -- definitions --------------------------------------------------------------------------

def definitions(*folders):
    """{id: definition} of every importer JSON in the folders; later folders override earlier ones."""
    found = {}
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(".json"):
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                definition = json.load(handle)
            definition.setdefault("id", os.path.splitext(name)[0])
            definition.setdefault("name", definition["id"])
            found[definition["id"]] = definition
    return found


def create(definition):
    """The importer for a definition, by its format."""
    cls = FORMATS.get(definition.get("format", "csv"))
    if cls is None:
        raise ValueError("Unknown import format: {}".format(definition.get("format")))
    return cls(definition)


class Importer:
    """Turns an export into records. Subclasses read their format's rows (dicts of text)."""

    def __init__(self, definition):
        self.definition = definition
        self.id = definition["id"]
        self.name = definition.get("name", self.id)
        self.fields = definition.get("fields") or {}
        self.statuses = definition.get("statuses") or {}

    def rows(self, text):
        raise NotImplementedError

    def columns(self, text):
        """The column names an export has, for detection."""
        raise NotImplementedError

    def detects(self, text):
        """True when the export has every column the definition's "detect" names."""
        wanted = self.definition.get("detect") or []
        return bool(wanted) and set(wanted) <= set(self.columns(text))

    def record(self, row):
        """A Record dict for a row, or None when it is not a movie, show, season or episode."""
        rec = {name: read_field(spec, row) for name, spec in self.fields.items() if name in FIELDS}
        if rec.get("kind") not in KINDS:
            return None
        status = rec.get("status")
        if status in self.statuses:
            status = self.statuses[status]
        rec["status"] = status if status in TALLY_STATUSES else None
        for name in ("tmdb", "imdb", "tvdb"):
            rec[name] = str(rec[name]) if not _empty(rec.get(name)) else ""
        for name in ("season", "episode", "year"):
            if rec.get(name) is not None:
                try:
                    rec[name] = int(rec[name])
                except (ValueError, TypeError):
                    rec[name] = None
        return rec

    def records(self, text):
        for row in self.rows(text):
            rec = self.record(row)
            if rec is not None:
                yield rec


class CsvImporter(Importer):
    @staticmethod
    def _reader(text):
        csv.field_size_limit(16 * 1024 * 1024)
        return csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))

    @staticmethod
    def _names(header):
        seen = {}
        names = []
        for name in header:
            name = name.strip()
            seen[name] = seen.get(name, 0) + 1
            names.append(name if seen[name] == 1 else "{}#{}".format(name, seen[name]))
        return names

    def columns(self, text):
        for header in self._reader(text):
            return self._names(header)
        return []

    def rows(self, text):
        reader = self._reader(text)
        names = None
        for values in reader:
            if names is None:
                names = self._names(values)
                continue
            if any(values):
                yield dict(zip(names, values))


FORMATS = {"csv": CsvImporter}


# -- merging into the store -------------------------------------------------------------

def _better(old, new):
    if new is None:
        return old
    if old is None:
        return new
    return new if _RANK[new] > _RANK[old] else old


def _resume(kind, tmdb, season, episode, rec, now):
    return (kind, tmdb, season, episode, rec["position"], rec.get("duration") or 0,
            rec.get("updated_at") or rec.get("watched_at") or now)


def apply(store, records, resolve=None, structure=None, now=None):
    """Merge records into the store; returns counts of what was read and added.

    ``resolve(kind, imdb, tvdb, title, year)`` gives the TMDb id of a record that has none
    (kind "movie" or "tv"). ``structure(tmdb)`` gives a show's ([[season, count], ...],
    last aired (season, episode)), so a show or season marked completed without its episodes
    gets every aired episode as a play. Either may be None.
    """
    now = now or time.time()
    counts = {"rows": 0, "skipped": 0, "unmatched": 0}
    titles = {}
    plays = []
    progress = []
    completed = []  # (tmdb, season or None, when) of shows/seasons completed as a whole
    lookups = {}

    for rec in records:
        counts["rows"] += 1
        title_kind = "movie" if rec["kind"] == "movie" else "tv"
        tmdb = rec.get("tmdb")
        if not tmdb and resolve:
            key = (title_kind, rec.get("imdb"), rec.get("tvdb"), rec.get("title"), rec.get("year"))
            if key not in lookups:
                lookups[key] = resolve(*key) or ""
            tmdb = lookups[key]
        if not tmdb:
            counts["unmatched"] += 1
            continue
        status = TALLY_STATUSES.get(rec.get("status"))
        meta = {k: rec.get(k) for k in ("imdb", "tvdb", "title", "year", "poster")}
        if rec["kind"] in ("season", "episode"):
            meta = {}  # these rows' titles and art are the episode's or season's, not the show's
        title = titles.setdefault((title_kind, tmdb), {
            "kind": title_kind, "tmdb": tmdb, "meta": {}, "status": None,
            "added_at": rec.get("added_at") or now, "activity_at": 0})
        for k, v in meta.items():
            if not _empty(v) and _empty(title["meta"].get(k)):
                title["meta"][k] = v
        if rec.get("added_at"):
            title["added_at"] = min(title["added_at"], rec["added_at"])
        when = rec.get("watched_at") or rec.get("updated_at") or rec.get("added_at")

        if rec["kind"] == "movie":
            title["status"] = _better(title["status"], status)
            if status == model.STATUS_COMPLETED:
                plays.append(("movie", tmdb, None, None, when))
            elif rec.get("position"):
                progress.append(_resume("movie", tmdb, None, None, rec, now))
        elif rec["kind"] == "tv":
            title["status"] = _better(title["status"], status)
            if status == model.STATUS_COMPLETED:
                completed.append((tmdb, None, when))
        elif rec["kind"] == "season":
            if status == model.STATUS_COMPLETED and rec.get("season") is not None:
                completed.append((tmdb, rec["season"], when))
            elif status is not None:
                title["status"] = _better(title["status"], model.STATUS_IN_PROGRESS)
        else:
            if rec.get("season") is None or rec.get("episode") is None:
                counts["skipped"] += 1
                continue
            # An episode row is a play when it says completed, or says nothing but has a date.
            if status == model.STATUS_COMPLETED or (status is None and rec.get("watched_at")
                                                    and not rec.get("position")):
                plays.append(("episode", tmdb, rec["season"], rec["episode"], when))
            elif rec.get("position"):
                progress.append(_resume("episode", tmdb, rec["season"], rec["episode"], rec, now))
            else:
                counts["skipped"] += 1
                continue
            title["status"] = _better(title["status"], model.STATUS_IN_PROGRESS)
        if when:
            title["activity_at"] = max(title["activity_at"], when)

    # A show or season completed as a whole, with no episode plays imported for it: every aired one.
    if structure and completed:
        episode_plays = {(p[1], p[2]) for p in plays if p[0] == "episode"}
        episode_plays |= {(p[1], None) for p in plays if p[0] == "episode"}
        shapes = {}
        for tmdb, season, when in completed:
            if (tmdb, season) in episode_plays:
                continue
            if tmdb not in shapes:
                shapes[tmdb] = structure(tmdb)
            if not shapes[tmdb]:
                continue
            seasons, last_aired = shapes[tmdb]
            for s, e in model.aired_episodes(seasons, last_aired):
                if season is None or s == season:
                    plays.append(("episode", tmdb, s, e, when))

    added = store.import_history(list(titles.values()), plays, progress, now=now)
    counts.update(added)
    counts["titles"] = len(titles)
    return counts
