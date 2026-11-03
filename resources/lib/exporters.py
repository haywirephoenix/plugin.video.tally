"""Exporting Tally's history for other trackers. Plain Python and no Kodi imports, so it runs (and
is tested) outside Kodi; export_ui.py writes the files.

An exporter (EXPORTERS) takes the store and episode_ids(show tmdb, season) -> {episode: its own
TMDb id} (None without TMDb), and gives ({file name: text}, [notes for the user]).

trakt: one CSV for Trakt's import (trakt.tv/settings/data): a flat list of rows, each an id column
named for its service, the type it is the id of (movie, show, season, episode), and watched_at or
watchlisted_at (ISO 8601). Every Tally title has a TMDb id, so every row uses tmdb_id; an episode's
row needs the episode's own id (Trakt has no season / episode columns), which TMDb's season data
gives: an episode it can't find is left out and counted in the notes.
"""

import csv
import io
import time

from . import model

TRAKT_FILE = "tally-trakt.csv"
TRAKT_COLUMNS = ("tmdb_id", "type", "watched_at", "watchlisted_at")
ALL = 1000000


def _iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds)) if seconds else ""


def _csv(columns, rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def trakt(store, episode_ids=None):
    rows = []
    for play in store.history("movie", limit=ALL):
        rows.append({"tmdb_id": play["tmdb"], "type": "movie", "watched_at": _iso(play["watched_at"])})
    seasons, missing = {}, []
    for play in store.history("episode", limit=ALL):
        key = (play["tmdb"], play["season"])
        if key not in seasons:
            seasons[key] = (episode_ids(*key) if episode_ids else None) or {}
        episode_id = seasons[key].get(play["episode"])
        if not episode_id:
            missing.append("{} S{:02d}E{:02d}".format(play.get("media_title") or "TMDb " + play["tmdb"],
                                                     play["season"], play["episode"]))
            continue
        rows.append({"tmdb_id": episode_id, "type": "episode", "watched_at": _iso(play["watched_at"])})
    for kind, trakt_type in (("movie", "movie"), ("tv", "show")):
        for title in store.titles(kind, [model.STATUS_PLANNING], order="added", limit=ALL):
            rows.append({"tmdb_id": title["tmdb"], "type": trakt_type, "watchlisted_at": _iso(title["added_at"])})
    rows.sort(key=lambda r: r.get("watched_at") or r.get("watchlisted_at"))
    notes = []
    if missing:
        notes.append("{} episode plays left out: TMDb has no id for them{}".format(
            len(missing), "" if episode_ids else " (no TMDb key)"))
        notes += sorted(set(missing))[:20]
    return {TRAKT_FILE: _csv(TRAKT_COLUMNS, rows)}, notes


# id: (name, function(store, episode_ids) -> ({file name: text}, notes), what it holds)
EXPORTERS = {
    "trakt": ("Trakt (CSV)", trakt, "Watched movies and episodes, and the watchlist, for Trakt's CSV import"),
}
