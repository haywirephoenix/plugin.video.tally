"""Keeps the store's show progress in step with TMDb: seasons, aired episodes, next episode details.

Used by actions that change what you watched and by the service; widgets only read the store.
"""

from . import common, tmdb


def title_meta(kind, tmdb_id):
    """Ids, title, year and poster of a movie or show from TMDb (cached), for the store."""
    if kind == "movie":
        return tmdb.movie_meta(tmdb_id)
    info = tmdb.show_info(tmdb_id) or {}
    return {k: info.get(k) for k in ("title", "year", "poster", "imdb", "tvdb")}


def refresh_show(tmdb_id, refresh=False):
    """Read a show's seasons from TMDb into the store, then store its next episode's details.

    False when TMDb couldn't be asked (no key, offline); the store then guesses the next episode.
    """
    info = tmdb.show_info(tmdb_id, refresh=refresh)
    if not info:
        return False
    store = common.store()
    seasons = [[s["season"], s["episodes"]] for s in info["seasons"]]
    store.set_show_structure(tmdb_id, seasons, info.get("last_aired"), info.get("ended"))
    store.update_meta("tv", tmdb_id, {k: info.get(k) for k in ("title", "year", "poster", "imdb", "tvdb")})
    fill_next(tmdb_id)
    return True


def fill_next(tmdb_id):
    """Store the title, still and plot of a show's next episode (one cached TMDb request per season)."""
    row = common.store().get("tv", tmdb_id)
    if not row or row.get("next_season") is None:
        return
    season, episode = row["next_season"], row["next_episode"]
    found = tmdb.season_episodes(tmdb_id, season).get(int(episode)) or {}
    common.store().set_next_meta(tmdb_id, season, episode, found.get("title") or "",
                                 found.get("thumb") or "", found.get("plot") or "",
                                 found.get("premiered") or "")


def after_episode_change(tmdb_id):
    """After plays of a show changed: make sure its seasons are known, then fill the next episode."""
    if common.store().show_structure(tmdb_id) is None:
        refresh_show(tmdb_id)
    else:
        fill_next(tmdb_id)
