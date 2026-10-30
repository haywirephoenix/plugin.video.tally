"""Tests for TMDb image sizing and the service's artwork queue: python -m unittest discover tests"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for name in ("xbmc", "xbmcaddon", "xbmcgui", "xbmcplugin", "xbmcvfs"):
    sys.modules.setdefault(name, mock.MagicMock())

from resources.lib import cache, common, tmdb  # noqa: E402

IMG = "https://image.tmdb.org/t/p/"


class SizedTest(unittest.TestCase):
    def test_resizes_tmdb_urls(self):
        self.assertEqual(tmdb.sized(IMG + "w500/abc.jpg", "w342"), IMG + "w342/abc.jpg")
        self.assertEqual(tmdb.sized(IMG + "original/abc.png", "w500"), IMG + "w500/abc.png")
        self.assertEqual(tmdb.sized("http://image.tmdb.org/t/p/w1280/x.jpg", "original"),
                         "http://image.tmdb.org/t/p/original/x.jpg")

    def test_leaves_other_urls(self):
        for url in ("", "DefaultSets.png", "https://example.com/t/p/w500/abc.jpg",
                    "special://home/addons/x/icon.png"):
            self.assertEqual(tmdb.sized(url, "w342"), url)

    def test_same_url_whatever_stored_size(self):
        stored = [IMG + "w500/p.jpg", IMG + "w342/p.jpg", IMG + "original/p.jpg"]
        self.assertEqual({tmdb.sized(u, "w342") for u in stored}, {IMG + "w342/p.jpg"})


class ImageSizeTest(unittest.TestCase):
    def sizes(self, settings):
        with mock.patch.object(common, "setting_int",
                               lambda key, default=0: int(settings.get(key, default))):
            return {kind: tmdb.image_size(kind) for kind in tmdb.IMAGE_SIZES}

    def test_defaults(self):
        self.assertEqual(self.sizes({}), {"poster": "w342", "backdrop": "w1280",
                                          "clearlogo": "w500", "still": "w780"})

    def test_picked_and_out_of_range(self):
        found = self.sizes({"image_poster": 0, "image_backdrop": 2, "image_clearlogo": 9,
                            "image_still": -1})
        self.assertEqual(found, {"poster": "w185", "backdrop": "original",
                                 "clearlogo": "w500", "still": "w780"})

    def test_defaults_are_offered(self):
        for kind, default in tmdb.DEFAULT_IMAGE_SIZE.items():
            self.assertIn(default, tmdb.IMAGE_SIZES[kind])


class PendingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(common, "ensure_profile_dir", lambda: self.dir.name),
            mock.patch.object(cache, "_conn", None),
            mock.patch.object(tmdb, "_key", lambda: "k" * 32),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        cache._connect().close()
        cache._conn = None
        self.dir.cleanup()

    def test_settled(self):
        self.assertFalse(tmdb.pending_settled(None, 100))
        self.assertFalse(tmdb.pending_settled(99, 100, quiet=3))
        self.assertTrue(tmdb.pending_settled(97, 100, quiet=3))

    def test_waits_for_quiet_queue(self):
        cache.queue_tmdb([("movie", "1")])
        with mock.patch.object(tmdb, "details") as details:
            self.assertFalse(tmdb.fetch_pending())
            details.assert_not_called()
        self.assertEqual(cache.pending_tmdb(), [("movie", "1")])

    def test_fetches_whole_queue_once_quiet(self):
        cache.queue_tmdb([("movie", str(n)) for n in range(150)] + [("tv", "7")])
        later = time.time() + tmdb.PENDING_QUIET + 1
        found = {("tv", "7"): {"fanart": IMG + "w1280/f.jpg"}}
        with mock.patch.object(tmdb.time, "time", lambda: later), \
                mock.patch.object(tmdb, "details", side_effect=lambda kind, i: found.get((kind, i), {})):
            self.assertTrue(tmdb.fetch_pending())
        self.assertEqual(cache.pending_tmdb(), [])
        self.assertIsNone(cache.pending_newest())

    def test_lookups_that_find_nothing_need_no_reload(self):
        cache.queue_tmdb([("movie", "1")])
        later = time.time() + tmdb.PENDING_QUIET + 1
        with mock.patch.object(tmdb.time, "time", lambda: later), \
                mock.patch.object(tmdb, "details", return_value={}):
            self.assertFalse(tmdb.fetch_pending())
        self.assertEqual(cache.pending_tmdb(), [])


if __name__ == "__main__":
    unittest.main()
