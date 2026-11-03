"""Tests for syncing stores between devices: the changes log and applying a peer's changes (no Kodi
or network needed).

python -m unittest discover tests
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from resources.lib import store  # noqa: E402
from resources.lib.store import Store  # noqa: E402


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.stores = []
        self.cursors = {}
        self.pc, self.tv = self.open("pc"), self.open("tv")

    def tearDown(self):
        for db in self.stores:
            db.close()
        self.dir.cleanup()

    def open(self, name):
        db = Store(os.path.join(self.dir.name, name + ".db"))
        self.stores.append(db)
        return db

    def pull(self, into, source):
        """into gets source's changes since it last asked; returns how many were new."""
        key = (id(into), id(source))
        changes = source.changes(self.cursors.get(key, 0), limit=10000)
        if changes:
            self.cursors[key] = changes[-1]["seq"]
        return into.apply_changes(changes)

    @staticmethod
    def later(db, other):
        """db's next change happens after other's last (two stores here can share a clock tick)."""
        db._last = other._now() + 1

    def sync(self, a, b):
        self.pull(a, b)
        self.pull(b, a)
        self.pull(a, b)


class LogTest(SyncTestCase):
    def test_device_ids_differ(self):
        self.assertNotEqual(self.pc.device_id, self.tv.device_id)
        self.assertEqual(len(self.pc.device_id), 16)

    def test_changes_logged(self):
        self.pc.add_watchlist("movie", "603", {"title": "The Matrix"})
        self.pc.add_play("episode", "1396", 1, 1)
        self.assertFalse(self.pc.add_watchlist("movie", "603"))  # nothing changed: not logged
        ops = [c["op"] for c in self.pc.changes()]
        self.assertEqual(ops, ["title", "play"])
        self.assertEqual(self.pc.changes()[0]["data"]["meta"], {"title": "The Matrix"})
        self.assertEqual(self.pc.changes(self.pc.last_change()), [])

    def test_only_latest_progress_kept(self):
        for position in (300, 600, 900):
            self.pc.save_progress("movie", "603", position, 8000)
        logged = [c for c in self.pc.changes() if c["op"] == "progress"]
        self.assertEqual([c["data"]["position"] for c in logged], [900])

    def test_migration_seeds_log(self):
        path = os.path.join(self.dir.name, "old.db")
        conn = sqlite3.connect(path)
        for sql in store.MIGRATIONS[:3]:
            conn.executescript(sql)
        conn.executescript(
            "PRAGMA user_version = 3;"
            "INSERT INTO media (kind, tmdb, title, status, added_at) VALUES ('movie', '603', 'The Matrix', 0, 10);"
            "INSERT INTO media (kind, tmdb, title, status, added_at) VALUES ('tv', '1396', 'Breaking Bad', 1, 20);"
            "INSERT INTO plays (kind, tmdb, season, episode, watched_at) VALUES ('episode', '1396', 1, 1, 30);"
            "INSERT INTO progress (kind, tmdb, season, episode, position, duration, updated_at) "
            "VALUES ('episode', '1396', 1, 2, 600, 2800, 40);")
        conn.close()
        old = self.open("old")
        self.assertEqual(old.version, store.SCHEMA_VERSION)
        self.assertEqual([(c["op"], c["at"]) for c in old.changes()],
                         [("title", 10), ("title", 20), ("play", 30), ("progress", 40)])
        self.assertTrue(old.history("episode")[0]["uid"])
        # A new device gets the whole history from it
        self.pull(self.tv, old)
        self.assertEqual(self.tv.get("movie", "603")["title"], "The Matrix")
        self.assertEqual(self.tv.playcount("episode", "1396", 1, 1), 1)
        self.assertEqual(self.tv.get_progress("episode", "1396", 1, 2), (600, 2800))
        self.assertEqual(self.tv.get("tv", "1396")["status"], store.STATUS_IN_PROGRESS)


class PlaysTest(SyncTestCase):
    def test_both_ways_once(self):
        self.pc.add_play("movie", "603", when=1000)
        self.tv.add_play("episode", "1396", 1, 1, when=2000)
        self.sync(self.pc, self.tv)
        self.sync(self.pc, self.tv)
        for db in (self.pc, self.tv):
            self.assertEqual(db.playcount("movie", "603"), 1)
            self.assertEqual(db.playcount("episode", "1396", 1, 1), 1)
            self.assertEqual(db.get("movie", "603")["status"], store.STATUS_COMPLETED)
            self.assertEqual(db.get("tv", "1396")["next_episode"], 2)
        self.assertEqual(self.pull(self.tv, self.pc), 0)

    def test_passed_on(self):
        phone = self.open("phone")
        phone.add_play("movie", "603", when=1000)
        self.sync(phone, self.tv)
        self.sync(self.tv, self.pc)  # the PC never pairs with the phone
        self.assertEqual(self.pc.playcount("movie", "603"), 1)
        self.sync(self.pc, phone)  # and when it does, nothing is doubled
        self.assertEqual(phone.playcount("movie", "603"), 1)

    def test_same_watch_on_both(self):
        # Both devices recorded one watch (an import of the same history) before they paired.
        self.pc.add_play("movie", "603", when=1000)
        self.tv.add_play("movie", "603", when=1030)
        self.sync(self.pc, self.tv)
        self.assertEqual((self.pc.playcount("movie", "603"), self.tv.playcount("movie", "603")), (1, 1))

    def test_unwatched(self):
        self.pc.add_play("episode", "1396", 1, 1, when=1000)
        self.sync(self.pc, self.tv)
        self.tv.remove_play("episode", "1396", 1, 1)
        self.sync(self.pc, self.tv)
        self.assertEqual(self.pc.playcount("episode", "1396", 1, 1), 0)
        # A copy of the old play arriving later (from a third device) doesn't bring it back
        phone = self.open("phone")
        self.pull(phone, self.pc)
        self.assertEqual(phone.playcount("episode", "1396", 1, 1), 0)
        stale = Store(":memory:")
        stale.add_play("episode", "1396", 1, 1, when=1000)
        self.pc.apply_changes(stale.changes())
        stale.close()
        self.assertEqual(self.pc.playcount("episode", "1396", 1, 1), 0)
        # ...but a watch after it does
        self.pc.add_play("episode", "1396", 1, 1)
        self.sync(self.pc, self.tv)
        self.assertEqual(self.tv.playcount("episode", "1396", 1, 1), 1)


class ProgressTest(SyncTestCase):
    def test_newest_wins_either_order(self):
        self.tv.save_progress("movie", "603", 600, 8000)
        self.later(self.pc, self.tv)
        self.pc.save_progress("movie", "603", 1200, 8000)
        self.sync(self.tv, self.pc)
        for db in (self.pc, self.tv):
            self.assertEqual(db.get_progress("movie", "603"), (1200, 8000))
            self.assertEqual(db.get("movie", "603")["status"], store.STATUS_IN_PROGRESS)

    def test_finished_elsewhere(self):
        self.pc.save_progress("episode", "1396", 900, 2800, season=1, episode=1)
        self.sync(self.pc, self.tv)
        self.tv.save_progress("episode", "1396", 2790, 2800, season=1, episode=1)  # a play
        self.sync(self.pc, self.tv)
        self.assertEqual(self.pc.get_progress("episode", "1396", 1, 1), (0, 0))
        self.assertEqual(self.pc.playcount("episode", "1396", 1, 1), 1)

    def test_cleared(self):
        self.pc.save_progress("movie", "603", 600, 8000)
        self.sync(self.pc, self.tv)
        self.tv.clear_progress("movie", "603")
        self.sync(self.pc, self.tv)
        self.assertEqual(self.pc.resume_list(), [])
        self.assertIsNone(self.pc.get("movie", "603"))


class TitlesTest(SyncTestCase):
    def test_watchlist(self):
        self.pc.add_watchlist("movie", "603", {"title": "The Matrix"})
        self.sync(self.pc, self.tv)
        self.assertEqual(self.tv.get("movie", "603")["status"], store.STATUS_PLANNING)
        self.tv.remove_watchlist("movie", "603")
        self.sync(self.pc, self.tv)
        self.assertIsNone(self.pc.get("movie", "603"))
        self.pc.add_watchlist("movie", "603")  # added again, later
        self.sync(self.pc, self.tv)
        self.assertEqual(self.tv.get("movie", "603")["status"], store.STATUS_PLANNING)

    def test_forget(self):
        self.pc.add_play("episode", "1396", 1, 1, when=1000)
        self.pc.save_progress("episode", "1396", 900, 2800, season=1, episode=2)
        self.sync(self.pc, self.tv)
        self.tv.untrack("tv", "1396")
        self.sync(self.pc, self.tv)
        for db in (self.pc, self.tv):
            self.assertIsNone(db.get("tv", "1396"))
            self.assertEqual((db.history("episode"), db.resume_list()), ([], []))

    def test_seen_change_is_older(self):
        # The TV's clock runs ahead; what the PC does after seeing the TV's change still wins.
        self.tv._last = self.pc._now() + 3600
        self.tv.add_watchlist("movie", "603")
        self.sync(self.pc, self.tv)
        self.assertTrue(self.pc.remove_watchlist("movie", "603"))
        self.sync(self.pc, self.tv)
        self.assertIsNone(self.tv.get("movie", "603"))

    def test_forget_keeps_later_watch(self):
        self.pc.add_play("movie", "603", when=1000)
        self.sync(self.pc, self.tv)
        self.tv.untrack("movie", "603")
        self.later(self.pc, self.tv)
        self.pc.add_play("movie", "603")  # watched again before they synced
        self.sync(self.pc, self.tv)
        for db in (self.pc, self.tv):
            self.assertEqual(db.playcount("movie", "603"), 1)
            self.assertEqual(db.get("movie", "603")["status"], store.STATUS_COMPLETED)


class ForeignChangesTest(SyncTestCase):
    def test_unknown_and_broken_kept_not_applied(self):
        changes = [{"uid": "a1", "op": "rating", "data": {"tmdb": "603", "stars": 5}, "at": 1},
                   {"uid": "a2", "op": "play", "data": {"kind": "movie"}, "at": 2},
                   {"uid": "a3", "op": "play", "at": 3, "data": dict(
                       store._play("movie", "603", None, None, 3000), meta={"title": "The Matrix"})}]
        self.assertEqual(self.pc.apply_changes(changes), 3)
        self.assertEqual(self.pc.playcount("movie", "603"), 1)
        self.assertEqual(self.pc.get("movie", "603")["title"], "The Matrix")
        self.pull(self.tv, self.pc)  # passed on, for a Tally that knows them
        self.assertEqual([c["uid"] for c in self.tv.changes()], ["a1", "a2", "a3"])
        self.assertEqual(self.pc.apply_changes(changes), 0)


if __name__ == "__main__":
    unittest.main()
