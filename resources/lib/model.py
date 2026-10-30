"""Turn store rows and TMDb results into one simple entry dict, and work out next episodes.

Every entry has:
    kind        "movie", "tvshow" or "episode"
    tmdb        TMDb id of the movie, or of the show for tvshow/episode entries
    imdb, tvdb  extra ids when known
plus optional display fields (title, show_title, season, episode, plot, poster,
thumb, premiered, genres, rating, runtime, resume_pos, resume_total, status,
playcount, label_prefix, episodes_left, next_season, next_episode).

No Kodi imports, so this runs outside Kodi too.
"""

import time

STATUS_PLANNING = 0
STATUS_IN_PROGRESS = 1
STATUS_PAUSED = 2  # unused for now; kept so status values match Floppy's
STATUS_COMPLETED = 3
STATUS_DROPPED = 4


def _date(timestamp):
    return time.strftime("%Y-%m-%d", time.localtime(timestamp)) if timestamp else ""


def aired_episodes(seasons, last_aired):
    """Every aired (season, episode), specials left out, in order.

    ``seasons`` is [[season, episode count], ...]; ``last_aired`` the last aired (season, episode),
    or None when nothing has aired.
    """
    if not last_aired:
        return []
    last = (int(last_aired[0]), int(last_aired[1]))
    out = []
    for season, count in sorted((int(s), int(n)) for s, n in seasons):
        if season <= 0 or season > last[0]:
            continue
        top = last[1] if season == last[0] else count
        out.extend((season, e) for e in range(1, top + 1))
    return out


def next_episode(watched, seasons=None, last_aired=None):
    """(next (season, episode) or None, aired episodes left) for a show.

    The next episode is the first unwatched aired one after the furthest watched, so going back
    to an old episode doesn't send Next Up back too. Without the show's seasons (no TMDb data) it
    is the episode after the furthest watched, unchecked, and nothing is known to be left.
    """
    watched = {(int(s), int(e)) for s, e in watched if int(s) > 0}
    if seasons is None:
        if not watched:
            return None, None
        season, episode = max(watched)
        return (season, episode + 1), None
    aired = aired_episodes(seasons, last_aired)
    left = sum(1 for ep in aired if ep not in watched)
    if not watched:
        return None, left
    furthest = max(watched)
    for ep in aired:
        if ep > furthest and ep not in watched:
            return ep, left
    return None, left


# -- entries -----------------------------------------------------------------------

def from_title(row):
    """A tracked movie or show from the store's media table."""
    kind = "movie" if row["kind"] == "movie" else "tvshow"
    entry = {
        "kind": kind,
        "tmdb": str(row["tmdb"]),
        "imdb": row.get("imdb") or "",
        "tvdb": row.get("tvdb") or "",
        "title": row.get("title") or "",
        "year": row.get("year"),
        "poster": row.get("poster") or "",
        "status": row.get("status"),
    }
    if kind == "movie":
        entry["playcount"] = 1 if row.get("status") == STATUS_COMPLETED or row.get("playcount") else 0
    else:
        entry["playcount"] = 1 if row.get("status") == STATUS_COMPLETED else 0
        if row.get("episodes_left") is not None:
            entry["episodes_left"] = row["episodes_left"]
        if row.get("next_season") is not None:
            entry["next_season"] = row["next_season"]
            entry["next_episode"] = row["next_episode"]
    return entry


def next_up(row):
    """The next episode of a show in progress, from the store's media table."""
    season, episode = row["next_season"], row["next_episode"]
    return {
        "kind": "episode",
        "tmdb": str(row["tmdb"]),
        "imdb": "",
        "tvdb": row.get("tvdb") or "",
        "show_imdb": row.get("imdb") or "",
        "show_title": row.get("title") or "",
        "title": row.get("next_title") or "Episode {}".format(episode),
        "season": season,
        "episode": episode,
        "thumb": row.get("next_thumb") or "",
        "poster": row.get("poster") or "",
        "plot": row.get("next_plot") or "",
        "premiered": row.get("next_aired") or "",
        "playcount": 0,
        "episodes_left": row.get("episodes_left"),
    }


def from_progress(row):
    """A resume point from the store's progress table (joined with its title)."""
    episode = row["kind"] == "episode"
    entry = {
        "kind": "episode" if episode else "movie",
        "tmdb": str(row["tmdb"]),
        "imdb": "" if episode else (row.get("imdb") or ""),
        "tvdb": row.get("tvdb") or "",
        "poster": row.get("poster") or "",
        "year": None if episode else row.get("year"),
        "resume_pos": int(row["position"]),
        "resume_total": int(row.get("duration") or 0),
        "runtime": int(row.get("duration") or 0),
        "updated_at": row.get("updated_at") or 0,
    }
    if episode:
        entry.update({
            "show_title": row.get("media_title") or "",
            "show_imdb": row.get("imdb") or "",
            "title": row.get("title") or "Episode {}".format(row["episode"]),
            "season": row["season"],
            "episode": row["episode"],
        })
    else:
        entry["title"] = row.get("media_title") or row.get("title") or ""
    return entry


def from_play(row):
    """A play from the store's plays table (joined with its title)."""
    episode = row["kind"] == "episode"
    entry = {
        "kind": "episode" if episode else "movie",
        "tmdb": str(row["tmdb"]),
        "imdb": "" if episode else (row.get("imdb") or ""),
        "tvdb": row.get("tvdb") or "",
        "poster": row.get("poster") or "",
        "playcount": 1,
        "lastplayed": _date(row.get("watched_at")),
    }
    if episode:
        entry.update({
            "show_title": row.get("media_title") or "",
            "show_imdb": row.get("imdb") or "",
            "title": "Episode {}".format(row["episode"]),
            "season": row["season"],
            "episode": row["episode"],
        })
    else:
        entry.update({"title": row.get("media_title") or "", "year": row.get("year")})
    return entry


def apply_state(entry, row):
    """Merge a tracked title's state (store.states row) into an entry from TMDb."""
    if not row:
        return entry
    tracked = from_title(row)
    for field in ("status", "playcount", "episodes_left", "next_season", "next_episode"):
        if tracked.get(field) is not None:
            entry[field] = tracked[field]
    for field in ("imdb", "tvdb"):
        if tracked.get(field) and not entry.get(field):
            entry[field] = tracked[field]
    return entry


def entry_key(entry):
    return (
        entry.get("kind"),
        entry.get("tmdb") or entry.get("imdb") or entry.get("tvdb") or entry.get("title"),
        entry.get("season"),
        entry.get("episode"),
    )


def dedupe(entries):
    seen = set()
    out = []
    for entry in entries:
        if not entry:
            continue
        key = entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def playable(entry):
    """True when a player can be pointed at this entry."""
    return bool(entry.get("tmdb") or entry.get("imdb") or entry.get("tvdb"))
