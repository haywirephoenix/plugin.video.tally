"""Tests for list routes, item properties, actions and playback saving, with Kodi and TMDb stubbed.

python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for name in ("xbmc", "xbmcaddon", "xbmcgui", "xbmcplugin", "xbmcvfs"):
    sys.modules.setdefault(name, mock.MagicMock())


class FakeListItem:
    def __init__(self, label="", label2="", offscreen=False):
        self.label = label
        self.props = {}
        self.art = {}
        self.menu = []
        self.path = ""
        self.tag = mock.MagicMock()

    def setProperty(self, key, value):  # noqa: N802 - Kodi's API
        self.props[key] = value

    def setArt(self, art):  # noqa: N802
        self.art.update(art)

    def setPath(self, path):  # noqa: N802
        self.path = path

    def setLabel2(self, label):  # noqa: N802
        pass

    def getVideoInfoTag(self):  # noqa: N802
        return self.tag

    def addContextMenuItems(self, items):  # noqa: N802
        self.menu.extend(items)


sys.modules["xbmcgui"].ListItem = FakeListItem
sys.modules["xbmc"].Player = object  # so TallyPlayer is a real class

from resources.lib import categories, common, listing, player_service, router, store, tmdb, tracking  # noqa: E402

SETTINGS = {"playback": "1", "episode_plugin_path": "plugin://plugin.video.ultra.player/?tmdb_id={tmdb}"
            "&season={season}&episode={episode}", "movie_plugin_path": "plugin://p/?tmdb_id={tmdb}"}
PROPS = {}


def run_route(query):
    """Run the router on a query; returns the entries it rendered (or None) and the built items."""
    rendered = {}

    def capture(self, entries, has_more, content, in_resume, next_params):
        rendered["entries"] = entries
        rendered["items"] = [listing.build(e, in_resume) for e in entries]

    with mock.patch.object(router.Router, "_render_entries", capture), \
            mock.patch.object(router.Router, "_finish", lambda *a, **k: None):
        router.Router(["plugin://plugin.video.tally/", "1", "?" + query]).__getattribute__(
            dict(p.split("=") for p in query.split("&"))["action"])()
    return rendered.get("entries"), rendered.get("items")


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = store.Store(os.path.join(self.dir.name, "tally.db"))
        PROPS.clear()
        patches = [
            mock.patch.object(common, "store", lambda: self.db),
            mock.patch.object(common, "setting", lambda key, default="": SETTINGS.get(key, default)),
            mock.patch.object(common, "setting_int",
                              lambda key, default=0: int(SETTINGS.get(key, default))),
            mock.patch.object(common, "setting_bool", lambda key, default=False: default),
            mock.patch.object(common, "home_property",
                              lambda name, value=None: PROPS.get(name, "") if value is None
                              else PROPS.__setitem__(name, value)),
            mock.patch.object(common, "set_play_handoff", lambda **v: PROPS.update(
                {"tally.play." + k: str(x) for k, x in v.items() if x not in (None, "")})),
            mock.patch.object(common, "notify", lambda *a, **k: None),
            mock.patch.object(tmdb, "_key", lambda: ""),  # no TMDb: nothing reaches the network
            mock.patch.object(listing, "is_widget_context", lambda: True),
            mock.patch.object(tracking, "title_meta", lambda kind, tmdb_id: {"title": "T" + tmdb_id}),
            mock.patch.object(tracking, "after_episode_change", lambda tmdb_id: None),
            mock.patch.object(tracking, "fill_next", lambda tmdb_id: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()


class ListTest(RouteTestCase):
    def test_continue_watching_widget(self):
        self.db.save_progress("movie", "603", 1200, 8000, meta={"title": "The Matrix", "imdb": "tt0133093"})
        self.db.save_progress("episode", "1396", 300, 2800, season=1, episode=2, title="Cat's in the Bag...",
                              meta={"title": "Breaking Bad", "imdb": "tt0903747"})
        entries, items = run_route("action=list&cat=continue_watching&widget=1&limit=10")
        self.assertEqual([(e["kind"], e["tmdb"]) for e in entries], [("episode", "1396"), ("movie", "603")])
        path, li, folder = items[0]
        self.assertFalse(folder)
        self.assertEqual(li.label, "1x02. Cat's in the Bag...")
        self.assertEqual(li.props["tvshow.tmdb_id"], "1396")
        self.assertEqual(li.props["tvshow.imdb_id"], "tt0903747")
        self.assertIn("action=play&kind=episode&tmdb=1396", li.props["tally.play_path"])
        self.assertIn("resume=300", li.props["tally.play_path"])
        self.assertIn(("Tally: Remove from Continue Watching"), [label for label, _ in li.menu])
        movie = items[1][1]
        self.assertEqual(movie.props["tmdb_id"], "603")
        self.assertEqual(movie.props["tally.status"], "1")

    def test_next_up_and_show_properties(self):
        self.db.add_play("episode", "1396", 1, 1, meta={"title": "Breaking Bad"})
        self.db.set_show_structure("1396", [[1, 7], [2, 13]], [2, 13])
        entries, items = run_route("action=list&cat=tv_next_up&widget=1&limit=10")
        self.assertEqual([(e["season"], e["episode"]) for e in entries], [(1, 2)])
        self.assertIn("season=1&episode=2", items[0][1].props["tally.play_path"])
        entries, items = run_route("action=list&cat=tv_watching&widget=1")
        li = items[0][1]
        self.assertEqual(li.props["tally.status"], "1")
        self.assertEqual(li.props["tally.next_label"], "S01E02")
        self.assertIn("action=seasons&tmdb=1396", li.props["tally.seasons_path"])
        self.assertIn("season=1&episode=2", li.props["tally.next_play_path"])
        self.assertEqual(li.props["tally.episodes_left"], "19")

    def test_unknown_category(self):
        self.assertEqual(run_route("action=list&cat=nope"), (None, None))

    def test_art_at_picked_sizes(self):
        img = "https://image.tmdb.org/t/p/"
        extra = {"poster": img + "w500/d.jpg", "fanart": img + "w1280/f.jpg",
                 "clearlogo": img + "w500/l.png"}
        entry = {"kind": "episode", "tmdb": "1396", "season": 1, "episode": 2, "title": "E",
                 "poster": img + "w500/p.jpg", "thumb": img + "w780/s.jpg"}
        with mock.patch.object(tmdb, "details", lambda *a, **k: extra):
            art = listing.build(entry)[1].art
            self.assertEqual(art["poster"], img + "w342/p.jpg")
            self.assertEqual(art["tvshow.fanart"], img + "w1280/f.jpg")
            self.assertEqual(art["landscape"], img + "w1280/f.jpg")
            self.assertEqual(art["clearlogo"], img + "w500/l.png")
            self.assertEqual(art["thumb"], img + "w780/s.jpg")
            with mock.patch.dict(SETTINGS, {"image_poster": "3", "image_backdrop": "0",
                                            "image_still": "0"}):
                art = listing.build(entry)[1].art
            self.assertEqual(art["poster"], img + "w780/p.jpg")
            self.assertEqual(art["fanart"], img + "w780/f.jpg")
            self.assertEqual(art["thumb"], img + "w300/s.jpg")


class ActionTest(RouteTestCase):
    def test_watchlist_add_remove(self):
        run_route("action=add_watchlist&media_type=movie&tmdb=603")
        self.assertEqual(self.db.get("movie", "603")["title"], "T603")
        reload_after_add = PROPS.get(common.PROP_RELOAD)
        self.assertTrue(reload_after_add)
        entries, items = run_route("action=list&cat=movies_watchlist&widget=1")
        li = items[0][1]
        self.assertEqual(li.props["tally.status"], "0")
        self.assertIn("action=remove_watchlist&media_type=movie&tmdb=603", li.props["tally.remove_watchlist_path"])
        run_route("action=remove_watchlist&media_type=movie&tmdb=603")
        self.assertIsNone(self.db.get("movie", "603"))

    def test_mark_watched_and_unwatch(self):
        run_route("action=mark_watched&kind=episode&tmdb=1396&season=1&episode=1")
        self.assertEqual(self.db.episode_playcounts("1396"), {(1, 1): 1})
        run_route("action=unwatch&kind=episode&tmdb=1396&season=1&episode=1")
        self.assertEqual(self.db.episode_playcounts("1396"), {})

    def test_play_hands_off(self):
        with mock.patch.object(router.xbmc, "executebuiltin") as builtin:
            run_route("action=play&kind=episode&tmdb=1396&season=1&episode=2&title=Cat&show=Breaking+Bad")
        self.assertEqual(PROPS["tally.play.kind"], "episode")
        self.assertEqual((PROPS["tally.play.tmdb"], PROPS["tally.play.season"], PROPS["tally.play.episode"]),
                         ("1396", "1", "2"))
        builtin.assert_called_once_with(
            'PlayMedia("plugin://plugin.video.ultra.player/?tmdb_id=1396&season=1&episode=2")')


class PlaybackSaveTest(RouteTestCase):
    def test_progress_then_watched(self):
        write = player_service.TallyPlayer._write
        write("episode", "1396", 1, 2, "Cat's in the Bag...", 600, 2800, False, False)
        self.assertEqual(self.db.get_progress("episode", "1396", 1, 2), (600, 2800))
        self.assertEqual(self.db.get("tv", "1396")["title"], "T1396")
        write("episode", "1396", 1, 2, "Cat's in the Bag...", 2300, 2800, True, True)
        self.assertEqual(self.db.get_progress("episode", "1396", 1, 2), (0, 0))
        self.assertEqual(self.db.episode_playcounts("1396"), {(1, 2): 1})
        self.assertTrue(PROPS.get(common.PROP_RELOAD))



class ResumeFillTest(RouteTestCase):
    def test_lists_carry_resume_points(self):
        # Any list's items get their resume point (the skin's Resume / Play from beginning)
        self.db.save_progress("movie", "155", 600, 9000)
        self.db.save_progress("episode", "1399", 300, 3000, season=1, episode=2)
        entries = [{"kind": "movie", "tmdb": "155"}, {"kind": "movie", "tmdb": "11"},
                   {"kind": "episode", "tmdb": "1399", "season": 1, "episode": 2}]
        router.Router._fill_resume(entries)
        self.assertEqual((entries[0]["resume_pos"], entries[0]["resume_total"]), (600, 9000))
        self.assertNotIn("resume_pos", entries[1])
        self.assertEqual(entries[2]["resume_pos"], 300)


if __name__ == "__main__":
    unittest.main()
