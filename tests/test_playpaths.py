"""Tests for resources/lib/playpaths.py (no Kodi needed): python -m unittest discover tests"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resources", "lib"))

import playpaths  # noqa: E402

# Dex Hub's TMDb Helper player file (plugin.video.dexhub/resources/players/dexhub.json), trimmed.
DEXHUB = {
    "name": "Dex Hub",
    "plugin": "plugin.video.dexhub",
    "priority": 100,
    "play_movie": "plugin://plugin.video.dexhub/?action=tmdb_player&video_type=movie&title={title_url}"
                  "&ar_title={ar_title_url}&year={year}&imdb_id={imdb}&tmdb_id={tmdb}&poster={poster_url}"
                  "&fanart={fanart_url}",
    "play_episode": "plugin://plugin.video.dexhub/?action=tmdb_player&video_type=episode"
                    "&showname={showname_url}&ar_showname={ar_showname_url}&title={title_url}&season={season}"
                    "&episode={episode}&tvdb_id={tvdb}&tmdb_id={tmdb}&imdb_id={imdb}&poster={poster_url}"
                    "&fanart={fanart_url}",
    "is_resolvable": "false",
}

MOVIE = {"kind": "movie", "tmdb": "27205", "imdb": "tt1375666", "title": "Inception: The Dream",
         "year": 2010, "poster": "https://image.tmdb.org/t/p/w500/a b.jpg"}
EPISODE = {"kind": "episode", "tmdb": "1396", "tvdb": "81189", "title": "Cat's in the Bag...",
           "show_title": "Breaking Bad", "season": 1, "episode": 2, "year": 2008,
           "imdb": "tt1054724", "show_imdb": "tt0903747"}


class FillTest(unittest.TestCase):
    def test_dexhub_movie(self):
        url = playpaths.fill(playpaths.player_template(DEXHUB, "movie"), playpaths.values(MOVIE))
        self.assertEqual(
            url,
            "plugin://plugin.video.dexhub/?action=tmdb_player&video_type=movie"
            "&title=Inception%3A%20The%20Dream&ar_title=&year=2010&imdb_id=tt1375666&tmdb_id=27205"
            "&poster=https%3A//image.tmdb.org/t/p/w500/a%20b.jpg&fanart=")

    def test_dexhub_episode_title_is_the_episodes(self):
        template = playpaths.player_template(DEXHUB, "episode")
        self.assertIn("title={eptitle_url}", template)
        self.assertIn("showname={showname_url}", template)
        url = playpaths.fill(template, playpaths.values(EPISODE))
        self.assertIn("showname=Breaking%20Bad&ar_showname=&title=Cat%27s%20in%20the%20Bag...", url)
        # An episode's {imdb} is the show's, as in TMDb Helper.
        self.assertIn("&season=1&episode=2&tvdb_id=81189&tmdb_id=1396&imdb_id=tt0903747&", url)

    def test_encodings_and_aliases(self):
        vals = playpaths.values(EPISODE)
        got = playpaths.fill("plugin://x/?a={tvshowtitle_+}&b={clearname_-}&c={showname_url+}"
                             "&d={title_escaped}&e={showyear}&f={name}&g={nope}&h={eptitle}", vals)
        self.assertEqual(got, "plugin://x/?a=Breaking+Bad&b=Breaking-Bad&c=Breaking+Bad"
                              "&d=Breaking%2520Bad&e=2008&f=Breaking Bad S01E02&g=&h=Cat's in the Bag...")
        self.assertEqual(playpaths.values(MOVIE)["name"], "Inception: The Dream (2010)")
        self.assertEqual(playpaths.values(MOVIE)["id"], "27205")

    def test_path_without_placeholders_gets_ids(self):
        got = playpaths.fill("plugin://p/play?tmdb=1&mode=x", playpaths.values(EPISODE))
        self.assertEqual(got, "plugin://p/play?tmdb=1396&mode=x&imdb_id=tt0903747&tvdb_id=81189&season=1&episode=2"
                              "&title=Breaking+Bad&year=2008")


class TemplatizeTest(unittest.TestCase):
    def test_values_become_placeholders(self):
        got = playpaths.templatize("plugin://p/?action=play&tmdb_id=27205&title=Inception&mode=movie")
        self.assertEqual(got, "plugin://p/?action=play&tmdb_id={tmdb}&title={title_url}&mode=movie")
        self.assertEqual(playpaths.fill(got, playpaths.values(MOVIE)),
                         "plugin://p/?action=play&tmdb_id=27205&title=Inception%3A%20The%20Dream&mode=movie")

    def test_no_query_unchanged(self):
        self.assertEqual(playpaths.templatize("plugin://p/movies/"), "plugin://p/movies/")


class PlayersTest(unittest.TestCase):
    def test_reads_installed_players_first_copy_wins(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            def write(folder, name, data):
                with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                    json.dump(data, f)
            write(first, "dexhub.json", DEXHUB)
            write(second, "dexhub.json", dict(DEXHUB, name="Old copy"))
            write(second, "other.json", {"name": "Other", "plugin": "plugin.video.other", "priority": 5,
                                         "play_movie": "plugin://plugin.video.other/?id={tmdb}"})
            write(second, "listform.json", {"name": "List", "plugin": "plugin.video.dexhub",
                                            "play_movie": [["plugin://x/", {}]]})
            write(second, "missing.json", {"name": "Missing", "plugin": "plugin.video.gone",
                                           "play_movie": "plugin://plugin.video.gone/"})
            installed = {"plugin.video.dexhub", "plugin.video.other"}.__contains__
            found = playpaths.players("movie", [first, second], installed)
            self.assertEqual([(f[0], f[3]) for f in found], [("Other", True), ("Dex Hub", False)])
            self.assertEqual([f[0] for f in playpaths.players("movie", [first, second], installed,
                                                              "plugin.video.dexhub")], ["Dex Hub"])

    def test_folders_include_add_ons_own_player_files(self):
        with tempfile.TemporaryDirectory() as addons:
            for addon in ("plugin.video.ultra.player", "plugin.video.themoviedb.helper", "skin.x"):
                os.makedirs(os.path.join(addons, addon, "resources", "players"))
            os.makedirs(os.path.join(addons, "plugin.video.none", "resources"))
            helper = os.path.join(addons, "plugin.video.themoviedb.helper", "resources", "players")
            folders = playpaths.player_folders(["/data/tmdbhelper/players", helper + os.sep], "/data/own/players",
                                               addons)
            self.assertEqual(folders, [
                "/data/tmdbhelper/players", helper + os.sep, "/data/own/players",  # TMDb Helper's once
                os.path.join(addons, "plugin.video.ultra.player", "resources", "players"),
                os.path.join(addons, "skin.x", "resources", "players"),
            ])


if __name__ == "__main__":
    unittest.main()
