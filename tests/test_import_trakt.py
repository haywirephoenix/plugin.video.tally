"""Tests for the Trakt importer (resources/importers/trakt.json) and the JSON format it uses: Trakt
data as its API gives it, as one file, an export tool's object of sections, or a zip of files.

python -m unittest discover tests
"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from resources.lib import importers, store  # noqa: E402
from resources.lib.store import Store  # noqa: E402

DEFINITIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resources", "importers")

MATRIX = {"title": "The Matrix", "year": 1999,
          "ids": {"trakt": 481, "slug": "the-matrix-1999", "imdb": "tt0133093", "tmdb": 603}}
HEAT = {"title": "Heat", "year": 1995, "runtime": 170, "ids": {"trakt": 9, "imdb": "tt0113277", "tmdb": 949}}
DUNE = {"title": "Dune", "year": 2021, "ids": {"trakt": 287071, "imdb": "tt1160419", "tmdb": 438631}}
BREAKING_BAD = {"title": "Breaking Bad", "year": 2008,
                "ids": {"trakt": 1388, "slug": "breaking-bad", "tvdb": 81189, "imdb": "tt0903747", "tmdb": 1396}}
SEVERANCE = {"title": "Severance", "year": 2022, "ids": {"trakt": 154997, "tvdb": 371980, "tmdb": 95396}}


def episode(season, number, **extra):
    return dict({"season": season, "number": number, "title": "Episode {}".format(number),
                 "ids": {"trakt": season * 100 + number}}, **extra)


HISTORY = [
    {"id": 1, "watched_at": "2024-05-01T20:00:00.000Z", "action": "watch", "type": "movie", "movie": MATRIX},
    {"id": 2, "watched_at": "2024-02-01T21:00:00.000Z", "action": "scrobble", "type": "episode",
     "episode": episode(1, 1), "show": BREAKING_BAD},
    {"id": 3, "watched_at": "2024-02-02T21:00:00.000Z", "action": "checkin", "type": "episode",
     "episode": episode(1, 2), "show": BREAKING_BAD},
]
WATCHLIST = [
    {"rank": 1, "id": 10, "listed_at": "2024-03-01T10:00:00.000Z", "notes": None, "type": "movie", "movie": DUNE},
    {"rank": 2, "id": 11, "listed_at": "2024-03-02T10:00:00.000Z", "notes": None, "type": "season",
     "season": {"number": 1, "ids": {"trakt": 5}}, "show": SEVERANCE},
    {"rank": 3, "id": 12, "listed_at": "2024-03-03T10:00:00.000Z", "type": "episode",
     "episode": episode(2, 1), "show": SEVERANCE},  # Tally has no episode watchlist: skipped
]
PLAYBACK = [
    {"progress": 30.0, "paused_at": "2024-06-01T10:00:00.000Z", "id": 20, "type": "movie", "movie": HEAT},
    {"progress": 32.1, "paused_at": "2024-06-02T10:00:00.000Z", "id": 21, "type": "episode",
     "episode": episode(1, 3, runtime=47), "show": BREAKING_BAD},
    {"progress": 50.0, "paused_at": "2024-06-03T10:00:00.000Z", "id": 22, "type": "movie", "movie": MATRIX},  # no runtime
]
WATCHED_MOVIES = [
    {"plays": 2, "last_watched_at": "2024-05-01T20:00:30.000Z", "last_updated_at": "2024-05-01T20:00:30.000Z",
     "movie": MATRIX},
]
WATCHED_SHOWS = [
    {"plays": 3, "last_watched_at": "2024-02-03T21:00:00.000Z", "show": BREAKING_BAD,
     "seasons": [{"number": 1, "episodes": [
         {"number": 1, "plays": 1, "last_watched_at": "2024-02-01T21:00:00.000Z"},
         {"number": 2, "plays": 1, "last_watched_at": "2024-02-02T21:00:00.000Z"},
         {"number": 4, "plays": 1, "last_watched_at": "2024-02-03T21:00:00.000Z"}]}]},
]
RATINGS = [{"rated_at": "2024-05-02T10:00:00.000Z", "rating": 10, "type": "movie", "movie": DUNE}]  # not imported


class TraktTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Store(os.path.join(self.dir.name, "tally.db"))
        self.found = {k: importers.create(d) for k, d in importers.definitions(DEFINITIONS).items()}
        self.trakt = self.found["trakt"]

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def run_import(self, text):
        return importers.apply(self.db, self.trakt.records(text))

    def everything(self):
        return {"history": HISTORY, "watchlist": WATCHLIST, "playback": PLAYBACK, "ratings": RATINGS,
                "watched": {"movies": WATCHED_MOVIES, "shows": WATCHED_SHOWS}}

    def check_everything(self, counts):
        self.assertEqual(self.db.get("movie", "603")["status"], store.STATUS_COMPLETED)
        self.assertEqual(self.db.playcount("movie", "603"), 1)  # history's and watched's are one watch
        self.assertEqual(self.db.get("movie", "438631")["status"], store.STATUS_PLANNING)  # rated too: no play
        self.assertEqual(self.db.playcount("movie", "438631"), 0)
        self.assertEqual(self.db.get_progress("movie", "949"), (3060, 10200))  # 30% of 170 minutes
        self.assertEqual(self.db.get("tv", "95396")["status"], store.STATUS_PLANNING)  # its season listed
        self.assertEqual(self.db.episode_playcounts("1396"), {(1, 1): 1, (1, 2): 1, (1, 4): 1})
        position, duration = self.db.get_progress("episode", "1396", 1, 3)
        self.assertEqual((round(position), duration), (905, 2820))
        show = self.db.get("tv", "1396")
        self.assertEqual((show["title"], show["status"]), ("Breaking Bad", store.STATUS_IN_PROGRESS))
        self.assertEqual(counts["unmatched"], 0)

    def test_detected(self):
        text = json.dumps(HISTORY)
        self.assertEqual([k for k, imp in self.found.items() if imp.detects(text)], ["trakt"])
        self.assertFalse(self.trakt.detects("row_type,media_id\nmedia,603\n"))  # a CSV

    def test_one_object_of_sections(self):
        self.check_everything(self.run_import(json.dumps(self.everything())))

    def test_files_one_by_one(self):
        counts = {}
        for part in (HISTORY, WATCHLIST, PLAYBACK, RATINGS, WATCHED_MOVIES, WATCHED_SHOWS):
            counts = self.run_import(json.dumps(part))
        self.check_everything(counts)

    def test_zip(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for name, part in (("history.json", HISTORY), ("watchlist.json", WATCHLIST),
                               ("playback.json", PLAYBACK), ("ratings.json", RATINGS),
                               ("watched-movies.json", WATCHED_MOVIES), ("watched-shows.json", WATCHED_SHOWS),
                               ("readme.txt", "not JSON")):
                archive.writestr(name, part if isinstance(part, str) else json.dumps(part))
        text = importers.export_text(data.getvalue())
        self.assertTrue(self.trakt.detects(text))
        self.check_everything(self.run_import(text))

    def test_twice_adds_nothing(self):
        text = json.dumps(self.everything())
        self.run_import(text)
        again = self.run_import(text)
        self.assertEqual((again["titles_added"], again["plays_added"], again["progress_added"]), (0, 0, 0))

    def test_without_tmdb_id(self):
        old = dict(MATRIX, ids={"trakt": 481, "imdb": "tt0133093"})
        text = json.dumps([dict(HISTORY[0], movie=old)])
        lookups = []

        def resolve(kind, imdb, tvdb, title, year):
            lookups.append((kind, imdb, title, year))
            return "603"
        importers.apply(self.db, self.trakt.records(text), resolve=resolve)
        self.assertEqual(lookups, [("movie", "tt0133093", "The Matrix", 1999)])
        self.assertEqual(self.db.playcount("movie", "603"), 1)


class JsonFormatTest(unittest.TestCase):
    def test_flatten_and_expand(self):
        imp = importers.create({"id": "x", "format": "json", "expand": ["a"]})
        rows = list(imp.rows(json.dumps({"x": [{"n": {"v": 1, "ok": True, "none": None}, "a": [{"b": 1}, {"b": 2}],
                                                "tags": ["t"]}]})))
        self.assertEqual(rows, [{"n.v": "1", "n.ok": "true", "n.none": "", "a.b": "1", "tags": '["t"]'},
                                {"n.v": "1", "n.ok": "true", "n.none": "", "a.b": "2", "tags": '["t"]'}])

    def test_export_text(self):
        self.assertEqual(importers.export_text("﻿a,b".encode("utf-8")), "a,b")
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("notes.txt", "x")
        with self.assertRaises(ValueError):
            importers.export_text(data.getvalue())


if __name__ == "__main__":
    unittest.main()
