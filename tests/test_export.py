"""Tests for exporting Tally's history (resources/lib/exporters.py): python -m unittest discover tests"""

import csv
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from resources.lib import exporters  # noqa: E402
from resources.lib.store import Store  # noqa: E402

# TMDb's own ids of Breaking Bad's first episodes
BB_EPISODES = {("1396", 1): {1: 62085, 2: 62086}}


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class TraktExportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Store(os.path.join(self.dir.name, "tally.db"))
        self.seasons = []

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def episode_ids(self, show, season):
        self.seasons.append((show, season))
        return BB_EPISODES.get((show, season), {})

    def test_flat_list_by_tmdb_id(self):
        self.db.add_play("movie", "603", meta={"title": "The Matrix"}, when=1729886400)  # 2024-10-25T20:00:00Z
        self.db.add_play("movie", "603", when=1730000000)  # watched twice: two rows
        self.db.add_watchlist("movie", "438631", {"title": "Dune"})
        self.db.add_watchlist("tv", "95396", {"title": "Severance"})
        self.db.add_play("episode", "1396", 1, 2, meta={"title": "Breaking Bad"}, when=1706821200)
        self.db.add_play("episode", "1396", 1, 1, when=1706734800)
        self.db.add_play("episode", "1396", 1, 9, when=1706900000)  # TMDb doesn't know it: left out
        self.db.save_progress("movie", "949", 3600, 10200)  # a resume point: not exported

        files, notes = exporters.trakt(self.db, self.episode_ids)
        out = rows(files[exporters.TRAKT_FILE])
        self.assertEqual(list(out[0]), list(exporters.TRAKT_COLUMNS))
        watched = [(r["tmdb_id"], r["type"], r["watched_at"]) for r in out if r["watched_at"]]
        self.assertEqual(watched, [("62085", "episode", "2024-01-31T21:00:00Z"),
                                   ("62086", "episode", "2024-02-01T21:00:00Z"),
                                   ("603", "movie", "2024-10-25T20:00:00Z"),
                                   ("603", "movie", "2024-10-27T03:33:20Z")])
        listed = {(r["tmdb_id"], r["type"]) for r in out if r["watchlisted_at"]}
        self.assertEqual(listed, {("438631", "movie"), ("95396", "show")})
        self.assertEqual(len(out), 6)
        self.assertEqual(self.seasons, [("1396", 1)])  # one lookup a season
        self.assertEqual(notes, ["1 episode plays left out: TMDb has no id for them", "Breaking Bad S01E09"])

    def test_without_tmdb(self):
        self.db.add_play("episode", "1396", 1, 1, meta={"title": "Breaking Bad"}, when=1706734800)
        files, notes = exporters.trakt(self.db)
        self.assertEqual(files[exporters.TRAKT_FILE].splitlines(), [",".join(exporters.TRAKT_COLUMNS)])
        self.assertIn("(no TMDb key)", notes[0])


if __name__ == "__main__":
    unittest.main()
