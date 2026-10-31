"""Tests for the tracking store and model (no Kodi needed): python -m unittest discover tests"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from resources.lib import model, store  # noqa: E402
from resources.lib.store import Store  # noqa: E402

# Two seasons of 3 episodes, specials too; everything up to S02E02 has aired.
SEASONS = [[0, 2], [1, 3], [2, 3]]


class NextEpisodeTest(unittest.TestCase):
    def test_after_furthest_watched(self):
        self.assertEqual(model.next_episode({(1, 1)}, SEASONS, [2, 2]), ((1, 2), 4))
        # Season rollover; an earlier gap doesn't pull Next Up back.
        self.assertEqual(model.next_episode({(1, 1), (1, 3)}, SEASONS, [2, 2]), ((2, 1), 3))

    def test_caught_up_and_specials(self):
        watched = {(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (0, 1)}
        self.assertEqual(model.next_episode(watched, SEASONS, [2, 2]), (None, 0))
        # Specials never count: watching only a special starts nothing.
        self.assertEqual(model.next_episode({(0, 1)}, SEASONS, [2, 2]), (None, 5))

    def test_unaired_not_next(self):
        self.assertEqual(model.next_episode({(1, 1), (1, 2), (1, 3)}, SEASONS, [1, 3]), (None, 0))

    def test_without_tmdb_guesses(self):
        self.assertEqual(model.next_episode({(3, 4), (1, 1)}), ((3, 5), None))
        self.assertEqual(model.next_episode(set()), (None, None))


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "tally.db")
        self.db = Store(self.path)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()


class MigrationTest(StoreTestCase):
    def test_version_and_reopen(self):
        self.assertEqual(self.db.version, store.SCHEMA_VERSION)
        self.db.add_watchlist("movie", "603", {"title": "The Matrix"})
        self.db.close()
        self.db = Store(self.path)  # migrations don't run twice; data is kept
        self.assertEqual(self.db.get("movie", "603")["title"], "The Matrix")

    def test_new_migration_runs_on_old_database(self):
        self.db.close()
        extra = "CREATE TABLE picks (tmdb TEXT PRIMARY KEY, score REAL);"
        store.MIGRATIONS.append(extra)
        store.SCHEMA_VERSION = len(store.MIGRATIONS)
        try:
            self.db = Store(self.path)
            self.assertEqual(self.db.version, store.SCHEMA_VERSION)
            conn = sqlite3.connect(self.path)
            self.assertTrue(conn.execute("SELECT name FROM sqlite_master WHERE name = 'picks'").fetchone())
            conn.close()
        finally:
            store.MIGRATIONS.pop()
            store.SCHEMA_VERSION = len(store.MIGRATIONS)


class WatchlistTest(StoreTestCase):
    def test_add_remove(self):
        self.assertTrue(self.db.add_watchlist("movie", 603, {"title": "The Matrix", "year": 1999}))
        self.assertFalse(self.db.add_watchlist("movie", "603"))  # already tracked
        row = self.db.titles("movie", [store.STATUS_PLANNING])[0]
        self.assertEqual((row["tmdb"], row["title"], row["year"]), ("603", "The Matrix", 1999))
        self.assertEqual(self.db.states("movie", ["603", "1"])["603"]["status"], 0)
        self.assertTrue(self.db.remove_watchlist("movie", "603"))
        self.assertEqual(self.db.titles("movie"), [])
        self.assertEqual(self.db.states("movie", ["603"]), {})
        self.assertTrue(self.db.add_watchlist("movie", "603"))  # can be added again

    def test_watched_title_is_not_removed(self):
        self.db.add_watchlist("movie", "603")
        self.db.add_play("movie", "603")
        self.assertFalse(self.db.remove_watchlist("movie", "603"))
        self.assertEqual(self.db.get("movie", "603")["status"], store.STATUS_COMPLETED)

    def test_shows_and_movies_apart(self):
        self.db.add_watchlist("tv", "603")
        self.assertIsNone(self.db.get("movie", "603"))
        self.assertEqual(self.db.get("tv", "603")["status"], 0)


class MovieProgressTest(StoreTestCase):
    def test_resume_then_finish(self):
        self.assertIsNone(self.db.save_progress("movie", "603", 30, 8000))  # accidental start
        self.assertEqual(self.db.get_progress("movie", "603"), (0, 0))
        self.assertEqual(self.db.save_progress("movie", "603", 1200, 8000, meta={"title": "The Matrix"}),
                         "saved")
        self.assertEqual(self.db.get_progress("movie", "603"), (1200, 8000))
        self.assertEqual(self.db.get("movie", "603")["status"], store.STATUS_IN_PROGRESS)
        rows = self.db.resume_list(["movie"])
        entry = model.from_progress(rows[0])
        self.assertEqual((entry["kind"], entry["tmdb"], entry["title"], entry["resume_pos"]),
                         ("movie", "603", "The Matrix", 1200))
        # Past 95% counts as a play and the resume point goes.
        self.assertEqual(self.db.save_progress("movie", "603", 7700, 8000), "played")
        self.assertEqual(self.db.resume_list(), [])
        self.assertEqual(self.db.playcount("movie", "603"), 1)
        self.assertEqual(self.db.get("movie", "603")["status"], store.STATUS_COMPLETED)

    def test_unwatch_and_clear(self):
        self.db.add_play("movie", "603", meta={"title": "The Matrix"})
        self.assertEqual(self.db.history("movie")[0]["media_title"], "The Matrix")
        self.assertEqual(self.db.remove_play("movie", "603"), 1)
        self.assertEqual(self.db.get("movie", "603")["status"], store.STATUS_PLANNING)
        self.db.save_progress("movie", "604", 600, 6000)
        self.db.clear_progress("movie", "604")
        self.assertEqual(self.db.resume_list(), [])
        self.assertIsNone(self.db.get("movie", "604"))

    def test_rewatch_keeps_completed(self):
        self.db.add_play("movie", "603")
        self.db.save_progress("movie", "603", 600, 8000)
        self.assertEqual(self.db.get("movie", "603")["status"], store.STATUS_COMPLETED)
        self.assertEqual(len(self.db.resume_list()), 1)

    def test_resume_order_and_episodes(self):
        self.db.save_progress("movie", "603", 600, 8000)
        time.sleep(0.01)
        self.db.save_progress("episode", "1396", 300, 2800, season=1, episode=2, title="Cat's in the Bag...",
                              meta={"title": "Breaking Bad", "imdb": "tt0903747"})
        rows = self.db.resume_list()
        self.assertEqual([r["kind"] for r in rows], ["episode", "movie"])
        entry = model.from_progress(rows[0])
        self.assertEqual((entry["show_title"], entry["title"], entry["season"], entry["episode"],
                          entry["show_imdb"], entry["imdb"]),
                         ("Breaking Bad", "Cat's in the Bag...", 1, 2, "tt0903747", ""))
        self.assertEqual(self.db.season_progress("1396", 1), {2: (300, 2800)})
        self.assertEqual(self.db.get("tv", "1396")["status"], store.STATUS_IN_PROGRESS)


class ShowProgressTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.db.add_watchlist("tv", "1396", {"title": "Breaking Bad"})
        self.db.set_show_structure("1396", SEASONS, [2, 2])

    def test_next_up_follows_plays(self):
        self.assertEqual(self.db.next_up(), [])  # on the watchlist, nothing watched
        self.db.add_play("episode", "1396", 1, 1)
        row = self.db.get("tv", "1396")
        self.assertEqual((row["status"], row["next_season"], row["next_episode"], row["episodes_left"]),
                         (store.STATUS_IN_PROGRESS, 1, 2, 4))
        entry = model.next_up(self.db.next_up()[0])
        self.assertEqual((entry["kind"], entry["tmdb"], entry["show_title"], entry["season"], entry["episode"],
                          entry["title"]), ("episode", "1396", "Breaking Bad", 1, 2, "Episode 2"))
        self.db.set_next_meta("1396", 1, 2, "Cat's in the Bag...", "thumb.jpg", "Plot", "2008-01-27")
        self.assertEqual(model.next_up(self.db.next_up()[0])["title"], "Cat's in the Bag...")
        # Stale details for an episode that is no longer next are dropped and not written.
        self.db.add_play("episode", "1396", 1, 2)
        row = self.db.get("tv", "1396")
        self.assertEqual((row["next_season"], row["next_episode"], row["next_title"]), (1, 3, ""))
        self.db.set_next_meta("1396", 1, 2, "Old")
        self.assertEqual(self.db.get("tv", "1396")["next_title"], "")

    def test_caught_up_then_new_episode(self):
        self.db.add_plays("1396", [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)])
        row = self.db.get("tv", "1396")
        self.assertEqual((row["status"], row["next_season"], row["episodes_left"]),
                         (store.STATUS_COMPLETED, None, 0))
        self.assertEqual(self.db.next_up(), [])
        self.assertEqual(model.from_title(row)["playcount"], 1)
        # S02E03 airs: back in progress, and it is next.
        self.db.set_show_structure("1396", SEASONS, [2, 3])
        row = self.db.get("tv", "1396")
        self.assertEqual((row["status"], row["next_season"], row["next_episode"], row["episodes_left"]),
                         (store.STATUS_IN_PROGRESS, 2, 3, 1))

    def test_unwatch_episode(self):
        self.db.add_plays("1396", [(1, 1), (1, 2)])
        self.db.remove_play("episode", "1396", 1, 2)
        row = self.db.get("tv", "1396")
        self.assertEqual((row["next_season"], row["next_episode"]), (1, 2))
        self.assertEqual(self.db.episode_playcounts("1396"), {(1, 1): 1})
        self.db.remove_play("episode", "1396", 1, 1)
        row = self.db.get("tv", "1396")
        self.assertEqual((row["status"], row["next_season"]), (store.STATUS_PLANNING, None))

    def test_play_clears_resume_and_history(self):
        self.db.save_progress("episode", "1396", 900, 2800, season=1, episode=1)
        self.db.add_play("episode", "1396", 1, 1)
        self.assertEqual(self.db.resume_list(), [])
        entry = model.from_play(self.db.history("episode")[0])
        self.assertEqual((entry["kind"], entry["show_title"], entry["season"], entry["episode"],
                          entry["playcount"]), ("episode", "Breaking Bad", 1, 1, 1))
        self.assertEqual(self.db.watched_ids("tv"), {"1396"})

    def test_untracked_show_without_seasons_guesses(self):
        self.db.add_play("episode", "999", 3, 4, meta={"title": "Unknown"})
        row = self.db.get("tv", "999")
        self.assertEqual((row["status"], row["next_season"], row["next_episode"], row["episodes_left"]),
                         (store.STATUS_IN_PROGRESS, 3, 5, None))
        # Shows only on the watchlist (1396) are never refreshed.
        self.assertEqual(self.db.shows_to_refresh(time.time() + 1), ["999"])

    def test_refresh_and_missing_meta(self):
        self.db.add_play("episode", "1396", 1, 1)
        self.assertEqual(self.db.shows_to_refresh(time.time() - 60), [])
        self.assertEqual(self.db.shows_to_refresh(time.time() + 1), ["1396"])
        self.assertEqual(self.db.next_missing_meta(), [("1396", 1, 2)])

    def test_untrack(self):
        self.db.add_play("episode", "1396", 1, 1)
        self.db.save_progress("episode", "1396", 900, 2800, season=1, episode=2)
        self.db.untrack("tv", "1396")
        self.assertIsNone(self.db.get("tv", "1396"))
        self.assertEqual((self.db.history("episode"), self.db.resume_list(), self.db.show_structure("1396")),
                         ([], [], None))


class SyncReadTest(StoreTestCase):
    def test_play_counts(self):
        self.db.add_play("movie", "1", when=100)
        self.db.add_play("movie", "1", when=200)
        self.db.add_play("episode", "3", 1, 2, when=150)
        self.assertEqual(self.db.play_counts("movie"), [
            {"tmdb": "1", "season": -1, "episode": -1, "plays": 2, "last_watched_at": 200}])
        self.assertEqual(self.db.play_counts("episode"), [
            {"tmdb": "3", "season": 1, "episode": 2, "plays": 1, "last_watched_at": 150}])

    def test_activity_changes_on_removal(self):
        before = self.db.activity()
        self.assertEqual(before["watchlist"], "0|None")
        self.db.add_watchlist("movie", "1")
        added = self.db.activity()
        self.assertNotEqual(added["watchlist"], before["watchlist"])
        self.db.remove_watchlist("movie", "1")
        self.assertNotEqual(self.db.activity()["watchlist"], added["watchlist"])
        self.db.add_play("episode", "3", 1, 1)
        played = self.db.activity()
        self.assertEqual(played["movie_plays"], before["movie_plays"])
        self.db.remove_play("episode", "3", 1, 1)
        self.assertNotEqual(self.db.activity()["episode_plays"], played["episode_plays"])


class ApplyStateTest(StoreTestCase):
    def test_tmdb_entries_get_state(self):
        self.db.add_watchlist("movie", "1")
        self.db.add_play("movie", "2")
        self.db.add_play("episode", "3", 1, 1)
        entries = [{"kind": "movie", "tmdb": "1"}, {"kind": "movie", "tmdb": "2"}, {"kind": "movie", "tmdb": "9"}]
        states = self.db.states("movie", [e["tmdb"] for e in entries])
        for entry in entries:
            model.apply_state(entry, states.get(entry["tmdb"]))
        self.assertEqual([(e.get("status"), e.get("playcount")) for e in entries],
                         [(0, 0), (3, 1), (None, None)])
        show = model.apply_state({"kind": "tvshow", "tmdb": "3"}, self.db.states("tv", ["3"])["3"])
        self.assertEqual((show["status"], show["next_season"], show["next_episode"]), (1, 1, 2))


if __name__ == "__main__":
    unittest.main()
