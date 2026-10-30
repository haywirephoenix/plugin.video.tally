"""Season and episode browsing: TMDb's seasons and episodes with Tally's watched state."""

from datetime import date

import xbmcgui

from . import common, listing, tmdb, tracking


def _aired(day):
    return bool(day) and day <= date.today().isoformat()


def _need_tmdb(router):
    if tmdb.enabled():
        return False
    common.notify("Add a TMDb API key in Tally settings to browse seasons")
    router._finish([], succeeded=False)
    return True


def list_seasons(router):
    if _need_tmdb(router):
        return
    tmdb_id = router.params.get("tmdb")
    info = tmdb.show_info(tmdb_id)
    if not info:
        router._finish([], "seasons", succeeded=False)
        return
    extra = tmdb.details("tv", tmdb_id)
    title = info.get("title") or router.params.get("show", "")
    poster = tmdb.art_url(info.get("poster") or extra.get("poster", ""), "poster")
    fanart = tmdb.art_url(extra.get("fanart") or info.get("fanart") or "", "backdrop") or poster
    landscape = tmdb.art_url(extra.get("landscape") or "", "backdrop") or fanart
    clearlogo = tmdb.art_url(extra.get("clearlogo", ""), "clearlogo")
    counts = common.store().episode_playcounts(tmdb_id)
    last = info.get("last_aired")

    seasons = sorted(info["seasons"], key=lambda s: (s["season"] == 0, s["season"]))
    items = []
    for season in seasons:
        number = season["season"]
        label = season.get("name") or ("Specials" if number == 0 else "Season {}".format(number))
        if label == title:
            label = "Specials" if number == 0 else "Season {}".format(number)
        total = season["episodes"]
        if last and number == last[0]:
            total = min(total, int(last[1]))  # episodes aired so far
        elif last and number > last[0] and number != 0:
            total = 0
        seen = len([1 for (s, e) in counts if s == number and e <= total])

        li = xbmcgui.ListItem(label=label, offscreen=True)
        tag = li.getVideoInfoTag()
        tag.setMediaType("season")
        tag.setTitle(label)
        tag.setTvShowTitle(title)
        tag.setSeason(int(number))
        if season.get("premiered"):
            tag.setPremiered(season["premiered"])
            try:
                tag.setYear(int(season["premiered"][:4]))
            except ValueError:
                pass
        if total and seen >= total:
            tag.setPlaycount(1)
        li.setProperty("TotalEpisodes", str(total))
        li.setProperty("WatchedEpisodes", str(seen))
        li.setProperty("UnWatchedEpisodes", str(max(total - seen, 0)))
        li.setProperty("tvshow.tmdb_id", str(tmdb_id))
        art = {
            "poster": tmdb.art_url(season.get("poster") or "", "poster") or poster,
            "fanart": fanart,
            "landscape": landscape,
            "clearlogo": clearlogo,
            "tvshow.poster": poster,
            "tvshow.fanart": fanart,
            "tvshow.clearlogo": clearlogo,
        }
        li.setArt({k: v for k, v in art.items() if v})
        li.addContextMenuItems([
            ("Tally: Mark season watched", "RunPlugin({})".format(common.plugin_url(
                action="mark_season_watched", tmdb=tmdb_id, season=number))),
            ("Tally: Refresh widgets", "RunPlugin({})".format(common.plugin_url(action="refresh"))),
        ])
        items.append((listing.episodes_url(tmdb_id, number, title, poster), li, True))
    router._finish(items, "seasons")


def list_episodes(router):
    if _need_tmdb(router):
        return
    p = router.params
    tmdb_id = p.get("tmdb")
    season = int(p.get("season") or 1)
    info = tmdb.show_info(tmdb_id) or {}
    episodes = tmdb.season_episodes(tmdb_id, season)
    store = common.store()
    counts = store.episode_playcounts(tmdb_id, season)
    resume = store.season_progress(tmdb_id, season)

    entries = []
    for number in sorted(episodes):
        ep = episodes[number]
        aired = ep.get("premiered") or ""
        entry = {
            "kind": "episode",
            "tmdb": str(tmdb_id),
            "imdb": "",
            "tvdb": "",
            "show_imdb": info.get("imdb") or "",
            "show_title": p.get("show") or info.get("title", ""),
            "title": ep.get("title") or "Episode {}".format(number),
            "season": season,
            "episode": number,
            "thumb": ep.get("thumb", ""),
            "poster": p.get("poster") or info.get("poster", ""),
            "premiered": aired,
            "plot": ep.get("plot", ""),
            "playcount": counts.get((season, number), 0),
            "runtime": int(ep.get("runtime") or 0) * 60,
        }
        pos, total = resume.get(number, (0, 0))
        if pos >= 60 and not (total and pos >= total * 0.95):
            entry["resume_pos"], entry["resume_total"] = int(pos), int(total)
        if aired and not _aired(aired):
            entry["label2"] = "Airs {}".format(aired)
        entries.append(entry)
    router._render_entries(entries, False, "episodes", False, {})


def _unwatched_aired(tmdb_id, season):
    """(season, episode) pairs of a season that have aired and aren't watched."""
    counts = common.store().episode_playcounts(tmdb_id, season)
    return [(season, number) for number, ep in sorted(tmdb.season_episodes(tmdb_id, season).items())
            if (season, number) not in counts and _aired(ep.get("premiered"))]


def _mark(router, tmdb_id, todo, question):
    if not todo:
        common.notify("Nothing left to mark")
        return
    if not xbmcgui.Dialog().yesno(common.ADDON_NAME, question(len(todo))):
        return
    common.store().add_plays(tmdb_id, todo, meta=tracking.title_meta("tv", tmdb_id))
    tracking.after_episode_change(tmdb_id)
    router._after_change("Marked {} episode(s) as watched".format(len(todo)))


def mark_season_watched(router):
    if _need_tmdb(router):
        return
    tmdb_id, season = router.params.get("tmdb"), int(router.params.get("season"))
    _mark(router, tmdb_id, _unwatched_aired(tmdb_id, season),
          lambda n: "Mark {} episode(s) of season {} as watched?".format(n, season))


def mark_show_watched(router):
    if _need_tmdb(router):
        return
    tmdb_id = router.params.get("tmdb")
    info = tmdb.show_info(tmdb_id) or {}
    todo = []
    for season in info.get("seasons") or []:
        if season["season"] > 0:  # specials left out
            todo.extend(_unwatched_aired(tmdb_id, season["season"]))
    title = router.params.get("title") or info.get("title") or "this show"
    _mark(router, tmdb_id, todo,
          lambda n: "Mark {} unwatched episode(s) of {} as watched?\n\nSpecials are not included.".format(n, title))
