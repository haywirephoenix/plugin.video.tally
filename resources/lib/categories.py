"""Every list the add-on offers. Keys match Floppy's where the list is the same.

Each category's fetch(page, size) returns (entries, has_more). Lists of what you watched come
from the store, so they need no network; the TMDb lists need the TMDb API key.
"""

from . import common, model, tmdb

MOVIE, TV, MIXED, EPISODES = "movies", "tvshows", "videos", "episodes"
S = model


def _store():
    return common.store()


def _page(rows, size):
    """Rows were read with one extra to tell whether there is a next page."""
    return rows[:size], len(rows) > size


def annotate(entries):
    """Merge what you watched into entries from TMDb (status, playcount, next episode)."""
    for kind, entry_kind in (("movie", "movie"), ("tv", "tvshow")):
        wanted = [e for e in entries if e and e["kind"] == entry_kind and e.get("tmdb")]
        if not wanted:
            continue
        states = _store().states(kind, [e["tmdb"] for e in wanted])
        for entry in wanted:
            model.apply_state(entry, states.get(entry["tmdb"]))
    return entries


def fill_episodes(entries):
    """Episode titles, stills and plots from TMDb season data already cached (never fetched)."""
    seasons = {}
    for entry in entries:
        if entry["kind"] != "episode" or not entry.get("tmdb") or entry.get("season") is None:
            continue
        key = (entry["tmdb"], entry["season"])
        if key not in seasons:
            seasons[key] = tmdb.season_episodes(entry["tmdb"], entry["season"], fetch=False)
        found = seasons[key].get(int(entry["episode"])) or {}
        if found.get("title") and entry.get("title", "").startswith("Episode "):
            entry["title"] = found["title"]
        for field in ("thumb", "plot", "premiered"):
            if found.get(field) and not entry.get(field):
                entry[field] = found[field]
    return entries


# -- fetch builders ---------------------------------------------------------------

def titles(kind, statuses=None, order="activity"):
    def fetch(page, size):
        rows = _store().titles(kind, statuses, order, limit=size + 1, offset=page * size)
        rows, more = _page(rows, size)
        return [model.from_title(r) for r in rows], more
    return fetch


def in_progress_shows(caught_up=False):
    """Shows you're watching; with ``caught_up`` off, only those with an aired episode to watch."""
    def fetch(page, size):
        offset = page * size
        # Filtered after reading (one query: a household tracks tens of shows, not thousands).
        rows = _store().titles("tv", [S.STATUS_IN_PROGRESS], limit=1000)
        if not caught_up:
            rows = [r for r in rows if r.get("next_season") is not None]
        rows = rows[offset:offset + size + 1]
        rows, more = _page(rows, size)
        return [model.from_title(r) for r in rows], more
    return fetch


def resume(kinds):
    def fetch(page, size):
        rows = _store().resume_list(kinds, limit=size + 1, offset=page * size)
        rows, more = _page(rows, size)
        entries = []
        for row in rows:
            entry = model.from_progress(row)
            total = entry.get("resume_total") or 0
            if total and entry["resume_pos"] >= total * 0.95:
                continue
            entries.append(entry)
        return annotate(fill_episodes(entries)), more
    return fetch


def next_up(with_movies=False):
    def fetch(page, size):
        order = {1: "aired", 2: "title"}.get(common.setting_int("nextup_sort", 0), "activity")
        if not with_movies:
            rows = _store().next_up(limit=size + 1, offset=page * size, order=order)
            rows, more = _page(rows, size)
            return [model.next_up(r) for r in rows], more
        # Next episodes and movies in progress together, by last activity.
        count = (page + 1) * size + 1
        pool = [(r["activity_at"], model.next_up(r)) for r in _store().next_up(limit=count)]
        pool += [(r["activity_at"], model.from_title(r))
                 for r in _store().titles("movie", [S.STATUS_IN_PROGRESS], limit=count)]
        pool.sort(key=lambda p: p[0] or 0, reverse=True)
        entries = [e for _, e in pool]
        start = page * size
        return entries[start:start + size], len(entries) > start + size
    return fetch


def in_progress_all():
    def fetch(page, size):
        count = (page + 1) * size + 1
        pool = _store().titles("movie", [S.STATUS_IN_PROGRESS], limit=count)
        pool += _store().titles("tv", [S.STATUS_IN_PROGRESS], limit=count)
        pool.sort(key=lambda r: r.get("activity_at") or 0, reverse=True)
        entries = [model.from_title(r) for r in pool]
        start = page * size
        return entries[start:start + size], len(entries) > start + size
    return fetch


def history(kind):
    def fetch(page, size):
        rows = _store().history(kind, limit=size + 1, offset=page * size)
        rows, more = _page(rows, size)
        return fill_episodes([model.from_play(r) for r in rows]), more
    return fetch


def tmdb_list(name, tmdb_type, unseen=False):
    def fetch(page, size):
        if unseen:
            # Read past what you've seen; a page can come up a little short.
            watched = _store().watched_ids(tmdb_type)
            results, _ = tmdb.tmdb_list(name, tmdb_type, limit=(page + 1) * size * 2 + 20)
            results = [r for r in results if str(r.get("id")) not in watched]
            start = page * size
            results, more = results[start:start + size], len(results) > start + size
        else:
            results, more = tmdb.tmdb_list(name, tmdb_type, limit=size, page=page)
        entries = [tmdb.to_entry(r, tmdb_type) for r in results]
        entries = [e for e in model.dedupe(entries) if model.playable(e)]
        return annotate(entries), more
    return fetch


# -- the menu -----------------------------------------------------------------------
# key: (label, content, fetch, description, in_resume_list)

_RESUME_MOVIES = resume(["movie"])
_RESUME_EPISODES = resume(["episode"])
_PROGRESS = [S.STATUS_IN_PROGRESS]

CATEGORIES = {
    # Mixed
    "continue_watching": ("Continue Watching", MIXED, resume(["movie", "episode"]),
                          "Movies and episodes you stopped part-way through.", True),
    "all_in_progress": ("In Progress", MIXED, in_progress_all(),
                        "Movies and shows you're part-way through, last watched first.", False),
    "all_next_up": ("Next Up", MIXED, next_up(with_movies=True),
                    "The next episode of every show, plus movies in progress, last watched first.", False),
    # Movies
    "movies_continue": ("Continue Watching", MOVIE, _RESUME_MOVIES,
                        "Movies you stopped part-way through.", True),
    "movies_trending": ("Trending", MOVIE, tmdb_list("trending", "movie"),
                        "What everyone has been watching this week.", False),
    "movies_popular": ("Popular", MOVIE, tmdb_list("popular", "movie"),
                       "Popular on TMDb right now.", False),
    "movies_top_rated": ("Top Rated", MOVIE, tmdb_list("top_rated", "movie"),
                         "TMDb's highest-rated movies.", False),
    "movies_greats": ("All-Time Greats You Haven't Seen", MOVIE,
                      tmdb_list("top_rated", "movie", unseen=True), "Top-rated movies you haven't watched.", False),
    "movies_coming_soon": ("Coming Soon", MOVIE, tmdb_list("upcoming", "movie"),
                           "Upcoming releases.", False),
    "movies_watchlist": ("Watchlist", MOVIE, titles("movie", [S.STATUS_PLANNING], "added"),
                         "Movies you plan to watch.", False),
    "movies_in_progress": ("In Progress", MOVIE, titles("movie", _PROGRESS),
                           "Movies you started.", False),
    "movies_history": ("Recently Watched", MOVIE, history("movie"),
                       "Your latest movie plays.", False),
    "movies_completed": ("Watched", MOVIE, titles("movie", [S.STATUS_COMPLETED]),
                         "Every movie you have finished.", False),
    # TV
    "tv_next_up": ("Next Up", EPISODES, next_up(),
                   "The next unwatched episode of every show you are watching.", False),
    "tv_continue": ("Continue Watching", EPISODES, _RESUME_EPISODES,
                    "Episodes you stopped part-way through.", True),
    "tv_trending": ("Trending", TV, tmdb_list("trending", "tv"),
                    "What everyone has been watching this week.", False),
    "tv_popular": ("Popular", TV, tmdb_list("popular", "tv"), "Popular on TMDb right now.", False),
    "tv_top_rated": ("Top Rated", TV, tmdb_list("top_rated", "tv"), "TMDb's highest-rated shows.", False),
    "tv_greats": ("All-Time Greats You Haven't Seen", TV, tmdb_list("top_rated", "tv", unseen=True),
                  "Top-rated shows you haven't started.", False),
    "tv_on_the_air": ("On The Air", TV, tmdb_list("on_the_air", "tv"),
                      "Shows with an episode airing this week.", False),
    "tv_watching": ("Watching", TV, in_progress_shows(caught_up=True), "Shows in progress.", False),
    "tv_in_progress": ("In Progress: Not Caught Up", TV, in_progress_shows(),
                       "Shows with unwatched episodes, most recently watched first.", False),
    "tv_watchlist": ("Watchlist", TV, titles("tv", [S.STATUS_PLANNING], "added"),
                     "Shows you plan to watch.", False),
    "tv_history": ("Recently Watched", EPISODES, history("episode"),
                   "Your latest episode plays.", False),
    "tv_completed": ("Watched", TV, titles("tv", [S.STATUS_COMPLETED]),
                     "Shows you have finished (every aired episode).", False),
}

MOVIE_MENU = [
    "movies_continue", "movies_trending", "movies_popular", "movies_top_rated", "movies_greats",
    "movies_coming_soon", "movies_watchlist", "movies_in_progress", "movies_history", "movies_completed",
]

TV_MENU = [
    "tv_next_up", "tv_continue", "tv_trending", "tv_popular", "tv_top_rated", "tv_greats", "tv_on_the_air",
    "tv_watching", "tv_in_progress", "tv_watchlist", "tv_history", "tv_completed",
]

ALL_MENU = ["continue_watching", "all_next_up", "all_in_progress"]
