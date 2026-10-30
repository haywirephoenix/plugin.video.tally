"""Tests for importer definitions and merging an export into the store (no Kodi needed).

python -m unittest discover tests
"""

import csv
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from resources.lib import importers, store  # noqa: E402
from resources.lib.store import Store  # noqa: E402

DEFINITIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resources", "importers")

# The header of a Floppy export, as it is (note the two "status" columns).
FLOPPY_HEADER = (
    "row_type,media_id,source,media_type,library_media_type,title,original_title,localized_title,synopsis,"
    "source_url,image,season_number,episode_number,provider_episode_id,runtime_minutes,number_of_pages,"
    "release_datetime,genres,implied_genres,country,languages,platforms,format,status,studios,themes,authors,"
    "publishers,isbn,source_material,creators,runtime,provider_popularity,provider_rating,provider_rating_count,"
    "trakt_rating,trakt_rating_count,imdb_rating,imdb_rating_count,mal_rating,mal_rating_count,igdb_user_rating,"
    "igdb_user_rating_count,opencritic_id,opencritic_score,opencritic_percent_recommended,opencritic_tier,"
    "opencritic_review_count,opencritic_url,opencritic_checked_at,trakt_popularity_score,trakt_popularity_rank,"
    "trakt_popularity_fetched_at,provider_keywords,provider_certification,provider_collection_id,"
    "provider_collection_name,provider_franchise_id,provider_franchise_name,metadata_refreshed_at,"
    "provider_external_ids,provider_game_lengths,provider_game_lengths_source,provider_game_lengths_match,"
    "provider_game_lengths_fetched_at,metadata_fetched_at,calendar_checked_at,manual_metadata,"
    "provider_metadata_status,local_season_episode_count,provider_episode_count,series_name,series_position,"
    "metadata_migration_pinned_at,episode_order_data,score,status,notes,entry_source,order_archived,"
    "rewatch_started_at,start_date,end_date,dropped,rating_only,progress,migrated_at,reading_time,origin_url,"
    "played_up_to_seconds,last_seen_status,position_updated_at,is_end_date_inferred,channel,watch_url,"
    "length_seconds,created_at,progressed_at,scored_at,active_episode_order_data,episode_order_history,list_uid,"
    "list_name,list_description,list_tags,list_visibility,list_allow_recommendations,list_include_notes,"
    "list_source,list_source_id,list_is_smart,list_smart_media_types,list_smart_excluded_media_types,"
    "list_smart_filters,list_item_date_added,collection_format,collection_resolution,collection_hdr,"
    "collection_is_3d,collection_audio_codec,collection_audio_channels,collection_bitrate,"
    "collection_collected_at,collection_purchase_price,collection_purchase_location,collection_custom_fields,"
    "collection_source_identity,item_tags").split(",")
STATUS_AT = FLOPPY_HEADER.index("status")
USER_STATUS_AT = FLOPPY_HEADER.index("status", STATUS_AT + 1)


def floppy_csv(*rows):
    """A Floppy export with these rows; "status" is the media's status, "user_status" the entry's."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(FLOPPY_HEADER)
    for row in rows:
        values = [""] * len(FLOPPY_HEADER)
        for key, value in row.items():
            if key == "user_status":
                values[USER_STATUS_AT] = value
            else:
                values[FLOPPY_HEADER.index(key)] = value
        writer.writerow(values)
    return "\ufeff" + out.getvalue()


MATRIX = {"row_type": "media", "media_id": "603", "source": "tmdb", "media_type": "movie",
          "title": "The Matrix", "release_datetime": "1999-03-30T00:00:00Z", "image": "http://img/603.jpg",
          "status": "Released", "user_status": "3", "end_date": "2024-05-01T20:00:00Z",
          "provider_external_ids": '{"imdb_id": "tt0133093"}'}
BREAKING_BAD = {"row_type": "media", "media_id": "1396", "source": "tmdb", "media_type": "tv",
                "title": "Breaking Bad", "status": "Ended", "user_status": "1",
                "created_at": "2024-01-01T00:00:00Z"}


def bb_episode(season, episode, **extra):
    row = {"row_type": "episode", "media_id": "1396", "source": "tmdb", "media_type": "episode",
           "title": "Episode {}".format(episode), "season_number": str(season), "episode_number": str(episode),
           "user_status": "3", "end_date": "2024-02-0{}T21:00:00+01:00".format(episode)}
    row.update(extra)
    return row


class FieldTest(unittest.TestCase):
    def test_specs(self):
        row = {"a": "", "b": " 12 ", "ids": '{"tmdb_id": 5}', "src": "TMDB", "d": "2024-05-01"}
        self.assertEqual(importers.read_field("b", row), "12")
        self.assertEqual(importers.read_field(["a", "b"], row), "12")
        self.assertEqual(importers.read_field({"column": "b", "convert": "int"}, row), 12)
        self.assertEqual(importers.read_field({"column": "b", "convert": "minutes"}, row), 720)
        self.assertEqual(importers.read_field({"column": "ids", "convert": "json", "key": "tmdb_id"}, row), 5)
        self.assertEqual(importers.read_field({"column": "b", "when": {"src": "tmdb"}}, row), "12")
        self.assertIsNone(importers.read_field({"column": "b", "when": {"src": "tvdb"}}, row))
        self.assertIsNone(importers.read_field({"column": "src", "map": {"imdb": "x"}}, row))
        self.assertEqual(importers.read_field({"columns": ["a", "d"], "convert": "time"}, row), 1714521600)
        self.assertIsNone(importers.read_field({"column": "src", "convert": "int"}, row))  # bad value

    def test_times(self):
        self.assertEqual(importers._to_time("2024-05-01T20:00:00Z"), 1714593600)
        self.assertEqual(importers._to_time("2024-05-01 21:00:00.123+01:00"), 1714593600)
        self.assertEqual(importers._to_time("1714593600000"), 1714593600)
        self.assertIsNone(importers._to_time("yesterday"))


class FloppyRecordTest(unittest.TestCase):
    def setUp(self):
        self.importer = importers.create(importers.definitions(DEFINITIONS)["floppy"])

    def test_detect_and_duplicate_status_column(self):
        text = floppy_csv(MATRIX)
        self.assertTrue(self.importer.detects(text))
        self.assertFalse(self.importer.detects("title,year\nX,1999\n"))
        self.assertIn("status#2", self.importer.columns(text))
        rec = list(self.importer.records(text))[0]
        self.assertEqual((rec["kind"], rec["tmdb"], rec["imdb"], rec["title"], rec["year"], rec["status"]),
                         ("movie", "603", "tt0133093", "The Matrix", 1999, "completed"))
        self.assertEqual(rec["watched_at"], 1714593600)

    def test_kinds_statuses_and_ids(self):
        text = floppy_csv(
            {"media_type": "book", "media_id": "1", "source": "openlibrary", "user_status": "1"},
            {"media_type": "anime", "media_id": "77", "source": "mal", "user_status": "Paused",
             "provider_external_ids": '{"tmdb_id": 31910, "tvdb_id": 79824}'},
            {"media_type": "movie", "media_id": "9", "source": "tmdb", "user_status": "4"},
            {"media_type": "movie", "media_id": "10", "source": "tmdb", "list_name": "Watchlist"})
        anime, dropped, listed = list(self.importer.records(text))  # the book is skipped
        self.assertEqual((anime["kind"], anime["tmdb"], anime["tvdb"], anime["status"]),
                         ("tv", "31910", "79824", "in_progress"))
        self.assertIsNone(dropped["status"])
        self.assertEqual(listed["status"], "planning")


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Store(os.path.join(self.dir.name, "tally.db"))
        self.importer = importers.create(importers.definitions(DEFINITIONS)["floppy"])

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def run_import(self, *rows, **kwargs):
        return importers.apply(self.db, self.importer.records(floppy_csv(*rows)), **kwargs)

    def test_movies_shows_episodes_resume(self):
        heat = {"media_id": "949", "source": "tmdb", "media_type": "movie", "title": "Heat",
                "user_status": "1", "played_up_to_seconds": "3600", "length_seconds": "10200",
                "position_updated_at": "2024-06-01T10:00:00Z"}
        dune = {"media_id": "438631", "source": "tmdb", "media_type": "movie", "title": "Dune", "user_status": "0"}
        counts = self.run_import(MATRIX, heat, dune, BREAKING_BAD, bb_episode(1, 1), bb_episode(1, 2),
                                 bb_episode(1, 3, user_status="1", end_date="", played_up_to_seconds="900",
                                            length_seconds="2800"))
        self.assertEqual((counts["titles_added"], counts["plays_added"], counts["progress_added"]), (4, 3, 2))
        self.assertEqual(self.db.get("movie", "603")["status"], store.STATUS_COMPLETED)
        self.assertEqual(self.db.history("movie")[0]["watched_at"], 1714593600)
        self.assertEqual(self.db.get("movie", "438631")["status"], store.STATUS_PLANNING)
        self.assertEqual(self.db.get_progress("movie", "949"), (3600, 10200))
        show = self.db.get("tv", "1396")
        self.assertEqual((show["title"], show["status"], show["next_season"], show["next_episode"]),
                         ("Breaking Bad", store.STATUS_IN_PROGRESS, 1, 3))
        self.assertEqual(self.db.get_progress("episode", "1396", 1, 3), (900, 2800))
        self.assertEqual(self.db.episode_playcounts("1396"), {(1, 1): 1, (1, 2): 1})

    def test_twice_adds_nothing(self):
        rows = (MATRIX, BREAKING_BAD, bb_episode(1, 1))
        self.run_import(*rows)
        again = self.run_import(*rows)
        self.assertEqual((again["titles_added"], again["plays_added"], again["progress_added"]), (0, 0, 0))
        self.assertEqual(self.db.playcount("movie", "603"), 1)

    def test_status_only_moves_forward(self):
        self.db.add_play("movie", "603")
        self.run_import(dict(MATRIX, user_status="0"))
        self.assertEqual(self.db.get("movie", "603")["status"], store.STATUS_COMPLETED)

    def test_older_resume_point_skipped(self):
        self.db.save_progress("movie", "949", 5000, 10200)
        self.run_import({"media_id": "949", "source": "tmdb", "media_type": "movie", "user_status": "1",
                         "played_up_to_seconds": "3600", "position_updated_at": "2024-06-01T10:00:00Z"})
        self.assertEqual(self.db.get_progress("movie", "949"), (5000, 10200))

    def test_resolve_and_unmatched(self):
        asked = []

        def resolve(kind, imdb, tvdb, title, year):
            asked.append((kind, imdb, title))
            return "603" if imdb == "tt0133093" else ""

        counts = self.run_import(
            {"media_id": "x1", "source": "imdb", "media_type": "movie", "user_status": "0",
             "provider_external_ids": '{"imdb_id": "tt0133093"}'},
            {"media_id": "x1", "source": "imdb", "media_type": "movie", "user_status": "0",
             "provider_external_ids": '{"imdb_id": "tt0133093"}'},
            {"media_id": "x2", "source": "imdb", "media_type": "movie", "title": "Nothing", "user_status": "0"},
            resolve=resolve)
        self.assertEqual(len(asked), 2)  # each title asked once
        self.assertEqual(counts["unmatched"], 1)
        self.assertEqual(self.db.get("movie", "603")["status"], store.STATUS_PLANNING)

    def test_completed_show_and_season_get_aired_episodes(self):
        shape = ([[0, 1], [1, 3], [2, 3]], [2, 2])
        counts = self.run_import(
            dict(BREAKING_BAD, user_status="3"),
            {"media_id": "1399", "source": "tmdb", "media_type": "season", "season_number": "1",
             "user_status": "3"},
            structure=lambda tmdb: shape)
        self.assertEqual(counts["plays_added"], 5 + 3)  # specials and unaired left out
        self.assertEqual(sorted(self.db.episode_playcounts("1399")), [(1, 1), (1, 2), (1, 3)])
        self.db.set_show_structure("1396", *shape)
        self.assertEqual(self.db.get("tv", "1396")["status"], store.STATUS_COMPLETED)

    def test_import_log(self):
        self.db.log_import("floppy", "C:/export.csv", {"plays_added": 2})
        row = self.db.imports()[0]
        self.assertEqual((row["importer"], row["summary"]), ("floppy", {"plays_added": 2}))


if __name__ == "__main__":
    unittest.main()
