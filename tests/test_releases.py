"""Tests for leaving out movies not out to watch at home yet: python -m unittest discover tests"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for name in ("xbmc", "xbmcaddon", "xbmcgui", "xbmcplugin", "xbmcvfs"):
    sys.modules.setdefault(name, mock.MagicMock())

from resources.lib import categories, tmdb  # noqa: E402


def country(*releases):
    return {"iso_3166_1": "US", "release_dates": [{"type": t, "release_date": d + "T00:00:00.000Z"} for t, d in releases]}


class HomeReleaseTest(unittest.TestCase):
    def test_earliest_digital_physical_or_tv(self):
        dates = [country((3, "2025-03-01"), (4, "2025-05-10")), country((5, "2025-04-20"), (6, "2026-01-01"))]
        self.assertEqual(tmdb.home_release(dates), "2025-04-20")

    def test_cinema_only_counts_after_a_while(self):
        # An older movie with only cinema dates: at home HOME_AFTER_CINEMA days after.
        self.assertEqual(tmdb.home_release([country((1, "1994-09-10"), (3, "1994-09-23"))]), "1995-01-21")
        # A digital date sooner than that wins.
        self.assertEqual(tmdb.home_release([country((3, "2025-01-01"), (4, "2025-02-15"))]), "2025-02-15")

    def test_premiere_only_or_nothing(self):
        self.assertEqual(tmdb.home_release([country((1, "2026-12-01"))]), "")
        self.assertEqual(tmdb.home_release([]), "")
        # No release dates at all: the primary release date, as a cinema one.
        self.assertEqual(tmdb.home_release([], "2020-01-01"), "2020-04-30")

    def test_released_at_home(self):
        with mock.patch.object(tmdb, "details", lambda kind, tmdb_id, fetch=True: {
                "1": {"home_release": "2025-01-01"}, "2": {"home_release": "2030-01-01"},
                "3": {"home_release": ""}, "4": {"title": "old cache entry"}}.get(tmdb_id, {})):
            today = "2026-10-31"
            self.assertEqual([tmdb.released_at_home(i, today) for i in ("1", "2", "3", "4", "5")],
                             [True, False, False, None, None])


class ReleasedOnlyTest(unittest.TestCase):
    def entries(self):
        return [{"kind": "movie", "tmdb": "1"}, {"kind": "movie", "tmdb": "2"}, {"kind": "tvshow", "tmdb": "9"},
                {"kind": "movie", "tmdb": "4"}]

    def test_leaves_out_unreleased_movies_only(self):
        out = {"1": True, "2": False, "4": None}
        with mock.patch.object(categories, "hide_unreleased", return_value=True), \
                mock.patch.object(tmdb, "prefetch") as prefetch, \
                mock.patch.object(tmdb, "released_at_home", lambda tmdb_id, today=None: out[tmdb_id]):
            kept = categories.released_only(self.entries())
        # The show stays; a movie whose details didn't load in time (None) stays too.
        self.assertEqual([e["tmdb"] for e in kept], ["1", "9", "4"])
        self.assertEqual(sorted(p[1] for p in prefetch.call_args[0][0]), ["1", "2", "4"])

    def test_setting_off(self):
        with mock.patch.object(categories, "hide_unreleased", return_value=False), \
                mock.patch.object(tmdb, "prefetch") as prefetch:
            self.assertEqual(len(categories.released_only(self.entries())), 4)
        prefetch.assert_not_called()


class FillIdsTest(unittest.TestCase):
    """The play action adds the ids TMDb's lists leave out, so players find the title."""

    def fill(self, entry, details):
        from resources.lib import router
        with mock.patch.object(tmdb, "enabled", return_value=True),                 mock.patch.object(tmdb, "details", lambda kind, tmdb_id, fetch=True: details[kind]):
            router.Router._fill_ids(entry)
        return entry

    def test_movie_gets_its_imdb_id(self):
        entry = self.fill({"kind": "movie", "tmdb": "278", "imdb": ""}, {"movie": {"imdb": "tt0111161"}})
        self.assertEqual(entry["imdb"], "tt0111161")

    def test_episode_gets_its_shows_ids(self):
        entry = self.fill({"kind": "episode", "tmdb": "60625", "show_imdb": "", "tvdb": ""},
                          {"tv": {"imdb": "tt2861424", "tvdb": "275274"}})
        self.assertEqual((entry["show_imdb"], entry["tvdb"]), ("tt2861424", "275274"))

    def test_known_ids_kept(self):
        entry = self.fill({"kind": "movie", "tmdb": "278", "imdb": "tt9"}, {"movie": {"imdb": "tt0111161"}})
        self.assertEqual(entry["imdb"], "tt9")


if __name__ == "__main__":
    unittest.main()
