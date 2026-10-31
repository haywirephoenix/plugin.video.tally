"""Plugin entry point: menus, lists and item actions."""

import itertools
import sys
import threading
from urllib.parse import parse_qsl

import xbmc
import xbmcgui
import xbmcplugin

from . import categories, common, listing, model, perf, tmdb


class Router:
    def __init__(self, argv):
        self.base = argv[0]
        self.handle = int(argv[1]) if len(argv) > 1 and argv[1].lstrip("-").isdigit() else -1
        query = argv[2][1:] if len(argv) > 2 and argv[2].startswith("?") else ""
        self.params = dict(parse_qsl(query))
        self.widget = self.params.get("widget") == "1" or listing.is_widget_context()

    # -- helpers --------------------------------------------------------------

    def _finish(self, items, content="videos", succeeded=True, cache_to_disc=False):
        if self.handle < 0:
            return
        xbmcplugin.setContent(self.handle, content)
        if items:
            xbmcplugin.addDirectoryItems(self.handle, items, len(items))
        xbmcplugin.addSortMethod(self.handle, xbmcplugin.SORT_METHOD_UNSORTED)
        perf.mark("add_items")
        xbmcplugin.endOfDirectory(self.handle, succeeded=succeeded, cacheToDisc=cache_to_disc)
        perf.mark("end_of_directory")

    def _need_tmdb(self, what):
        """True (and an empty listing) when there is no TMDb key; says so outside widgets."""
        if tmdb.enabled():
            return False
        if not self.widget:
            common.notify("Add a TMDb API key in Tally settings to {}".format(what))
        self._finish([])
        return True

    def _page_size(self):
        try:
            return max(1, int(self.params.get("limit") or common.setting_int("page_size", 20)))
        except ValueError:
            return 20

    def _show_next_page(self):
        if self.params.get("widget") == "1":
            return False
        return common.setting_bool("next_page", True) and not listing.is_widget_context()

    def _render_entries(self, entries, has_more, content, in_resume_list, next_params):
        perf.mark("fetch")
        tmdb.prefetch(
            (("movie" if e["kind"] == "movie" else "tv", e.get("tmdb")) for e in entries),
            deadline=1.0 if self.widget else None,
        )
        perf.mark("prefetch")
        self._fill_resume(entries)
        items = []
        for entry in entries:
            try:
                items.append(listing.build(entry, in_resume_list))
            except Exception as exc:  # noqa: BLE001 - one bad row must not empty a widget
                common.log("Skipping item {}: {}".format(entry.get("title"), exc), xbmc.LOGWARNING)
        perf.mark("build")
        if has_more and self._show_next_page():
            page = int(self.params.get("page") or 0) + 1
            url = common.plugin_url(**dict(next_params, page=page))
            label = "[B]Next page ({})[/B]".format(page + 1)
            items.append(listing.folder_item(label, url, icon="DefaultFolder.png"))
        self._finish(items, content)

    @staticmethod
    def _fill_resume(entries):
        """Resume points for the entries that came without one (lists other than Continue
        Watching), so every item shows it: a progress bar, the skin's Resume button."""
        missing = [e for e in entries if not e.get("resume_pos") and e.get("tmdb")
                   and (e["kind"] == "movie" or (e["kind"] == "episode" and e.get("season") is not None
                                                 and e.get("episode") is not None))]
        if not missing:
            return
        store = common.store()
        movies = store.movie_progress() if any(e["kind"] == "movie" for e in missing) else {}
        for entry in missing:
            if entry["kind"] == "movie":
                pos, total = movies.get(str(entry["tmdb"]), (0, 0))
            else:
                pos, total = store.get_progress("episode", entry["tmdb"], entry["season"], entry["episode"])
            total = total or entry.get("runtime") or 0
            if pos >= 60 and not (total and pos >= total * 0.95):
                entry["resume_pos"], entry["resume_total"] = int(pos), int(total)

    @staticmethod
    def _fill_ids(entry):
        """The ids TMDb's lists leave out, from the title's details (cached; one lookup if not):
        a movie's IMDb id, an episode's show IMDb and TVDb ids. Players such as Ultra Player's
        sources (Torrentio) find movies by IMDb id only."""
        if not entry.get("tmdb") or not tmdb.enabled():
            return
        if entry["kind"] == "movie" and not entry.get("imdb"):
            entry["imdb"] = tmdb.details("movie", entry["tmdb"]).get("imdb") or ""
        elif entry["kind"] == "episode" and not (entry.get("show_imdb") and entry.get("tvdb")):
            show = tmdb.details("tv", entry["tmdb"])
            entry["show_imdb"] = entry.get("show_imdb") or show.get("imdb") or ""
            entry["tvdb"] = entry.get("tvdb") or show.get("tvdb") or ""

    @staticmethod
    def _alternate(groups):
        """Interleave movie and show results so neither type buries the other."""
        entries = []
        for group in itertools.zip_longest(*groups):
            entries.extend(e for e in group if e)
        return [e for e in model.dedupe(entries) if model.playable(e)]

    # -- menus ----------------------------------------------------------------

    def root(self):
        menu = [
            ("Continue Watching", common.plugin_url(action="list", cat="continue_watching"),
             "DefaultInProgressShows.png", "Movies and episodes you stopped part-way through."),
            ("Next Up", common.plugin_url(action="list", cat="tv_next_up"),
             "DefaultRecentlyAddedEpisodes.png", "The next episode of every show you are watching."),
            ("All Videos", common.plugin_url(action="menu", menu="all"), "DefaultVideo.png",
             "Movies and shows together."),
            ("Movies", common.plugin_url(action="menu", menu="movies"), "DefaultMovies.png", ""),
            ("TV Shows", common.plugin_url(action="menu", menu="tv"), "DefaultTVShows.png", ""),
            ("Discover", common.plugin_url(action="discover"), "DefaultAddonVideo.png",
             "Trending, popular and top-rated titles, and genres, from TMDb."),
            ("Search", common.plugin_url(action="search"), "DefaultAddonsSearch.png",
             "Search movies and TV shows on TMDb."),
            ("Tools", common.plugin_url(action="menu", menu="tools"), "DefaultAddonProgram.png", ""),
        ]
        items = [listing.folder_item(label, url, icon, plot) for label, url, icon, plot in menu]
        self._finish(items, content="files")

    def menu(self):
        name = self.params.get("menu")
        if name == "tools":
            items = [
                listing.folder_item("Refresh widgets", common.plugin_url(action="refresh"),
                                    "DefaultAddonService.png", "Reload widgets and re-read your shows' episodes."),
                listing.folder_item("Show widget paths", common.plugin_url(action="widget_paths"),
                                    "DefaultAddonProgram.png", "Every list's path, for adding widgets by hand."),
                listing.folder_item("Clear cache", common.plugin_url(action="clear_cache"),
                                    "DefaultAddonProgram.png", "Clears cached TMDb data. What you watched is kept."),
                listing.folder_item("Settings", common.plugin_url(action="settings"),
                                    "DefaultAddonProgram.png", ""),
            ]
            # Tools are actions, not folders.
            items = [(url, li, False) for url, li, _ in items]
            self._finish(items, content="files")
            return
        if name == "all":
            keys, icon = categories.ALL_MENU, "DefaultVideo.png"
        elif name == "movies":
            keys, icon = categories.MOVIE_MENU, "DefaultMovies.png"
        else:
            keys, icon = categories.TV_MENU, "DefaultTVShows.png"
        items = []
        for key in keys:
            label, _, _, description, _ = categories.CATEGORIES[key]
            items.append(listing.folder_item(label, common.plugin_url(action="list", cat=key),
                                             icon, description))
        self._finish(items, content="files")

    def discover(self):
        items = []
        for key in ("movies_trending", "tv_trending", "movies_popular", "tv_popular",
                    "movies_top_rated", "tv_top_rated", "movies_coming_soon", "tv_on_the_air"):
            label, content, _, description, _ = categories.CATEGORIES[key]
            prefix = "Movies" if key.startswith("movies") else "TV"
            icon = "DefaultMovies.png" if prefix == "Movies" else "DefaultTVShows.png"
            items.append(listing.folder_item("{}: {}".format(prefix, label),
                                             common.plugin_url(action="list", cat=key), icon, description))
        items.append(listing.folder_item(
            "Genres", common.plugin_url(action="genres"), "DefaultGenre.png",
            "Popular movies and TV shows on TMDb by genre."))
        self._finish(items, content="files")

    # -- lists ----------------------------------------------------------------

    def list(self):
        key = self.params.get("cat")
        if key not in categories.CATEGORIES:
            self._finish([], succeeded=False)
            return
        _, content, fetch, _, in_resume = categories.CATEGORIES[key]
        page = int(self.params.get("page") or 0)
        try:
            entries, has_more = fetch(page, self._page_size())
        except Exception as exc:  # noqa: BLE001 - a broken list must still end the directory
            common.log("Could not list {}: {}".format(key, exc), xbmc.LOGWARNING)
            self._finish([], content, succeeded=False)
            return
        next_params = {k: v for k, v in self.params.items() if k != "page"}
        self._render_entries(entries, has_more, content, in_resume, next_params)

    # -- search ---------------------------------------------------------------

    def search(self):
        query = (self.params.get("query") or "").strip()
        if not query:
            if self.widget:
                self._finish([])
                return
            query = xbmcgui.Dialog().input("Search").strip()
            if not query:
                self._finish([], succeeded=False)
                return
        if self._need_tmdb("search"):
            return
        media_type = self.params.get("media_type", "both")
        if media_type in ("collection", "person"):
            self._search_people_or_sets(media_type, query)
            return
        types = ["movie", "tv"] if media_type == "both" else [media_type]
        results = {}

        def run(kind):
            results[kind] = tmdb.search(kind, query)

        threads = [threading.Thread(target=run, args=(kind,)) for kind in types]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        groups = [[tmdb.to_entry(r, kind) for r in results.get(kind) or []] for kind in types]
        entries = categories.annotate(self._alternate(groups)[: self._page_size()])
        content = "movies" if types == ["movie"] else "tvshows" if types == ["tv"] else "videos"
        self._render_entries(entries, False, content, False, {})

    def _search_people_or_sets(self, kind, query):
        items = []
        for r in tmdb.search(kind, query)[: self._page_size()]:
            if kind == "collection":
                url = common.plugin_url(action="collection", id=r.get("id"))
                art = tmdb._image(r.get("poster_path"), tmdb.image_size("poster")) or "DefaultSets.png"
                items.append(listing.folder_item(r.get("name") or "", url, art, r.get("overview") or "",
                                                 fanart=tmdb._image(r.get("backdrop_path"),
                                                                    tmdb.image_size("backdrop"))))
            else:
                url = common.plugin_url(action="person", id=r.get("id"))
                art = tmdb._image(r.get("profile_path"), tmdb.image_size("poster")) or "DefaultActor.png"
                known = ", ".join(k.get("title") or k.get("name") or "" for k in r.get("known_for") or [])
                items.append(listing.folder_item(r.get("name") or "", url, art, known))
        self._finish(items, content="files")

    def collection(self):
        entries = [tmdb.to_entry(p, "movie") for p in tmdb.collection_parts(self.params.get("id"))]
        entries.sort(key=lambda e: e.get("premiered") or "9999")
        self._render_entries(categories.annotate(entries), False, "movies", False, {})

    def person(self):
        credits = tmdb.person_credits(self.params.get("id"))
        entries = model.dedupe(tmdb.to_entry(c, c.get("media_type")) for c in credits
                               if c.get("media_type") in ("movie", "tv"))
        page = int(self.params.get("page") or 0)
        size = self._page_size()
        page_entries, has_more = entries[page * size:(page + 1) * size], len(entries) > (page + 1) * size
        next_params = {k: v for k, v in self.params.items() if k != "page"}
        self._render_entries(categories.annotate(page_entries), has_more, "videos", False, next_params)

    # -- genres --------------------------------------------------------------

    def genre(self):
        """Popular TMDb titles in any of ``genres`` (names joined by " / "), for search categories."""
        if self._need_tmdb("browse genres"):
            return
        media_type = self.params.get("media_type", "both")
        types = ["movie", "tv"] if media_type == "both" else [media_type]
        names = [n.strip().lower() for n in (self.params.get("genres") or "").split("/") if n.strip()]
        language = (self.params.get("language") or "").strip()
        keywords = (self.params.get("keywords") or "").strip()
        size = self._page_size()
        page = int(self.params.get("page") or 0)
        # Pages are cut from one merged list, so each type needs enough for every page so far
        # (plus one, to tell whether there is a next page). TMDb pages are cached.
        wanted_count = (page + 1) * size + 1
        if "movie" in types and categories.hide_unreleased():
            wanted_count *= 2  # room for the movies not out yet, which are left out

        queries = {}
        for kind in types:
            ids = tmdb.genre_ids(kind)
            wanted = sorted({ids[n] for n in names if n in ids})
            # Movie and TV genre names differ: a type with none of the asked genres is skipped.
            if wanted or (not names and (language or keywords)):
                queries[kind] = wanted

        results = {}

        def run(kind):
            results[kind] = tmdb.discover(kind, queries[kind], language, keywords, limit=wanted_count)

        threads = [threading.Thread(target=run, args=(kind,)) for kind in queries]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        groups = [[tmdb.to_entry(r, kind) for r in results.get(kind) or []] for kind in types]
        groups = [categories.released_only(group, 1.0 if self.widget else None) for group in groups]
        entries = self._alternate(groups)
        start = page * size
        page_entries, has_more = entries[start:start + size], len(entries) > start + size
        content = "movies" if types == ["movie"] else "tvshows" if types == ["tv"] else "videos"
        next_params = {k: v for k, v in self.params.items() if k != "page"}
        self._render_entries(categories.annotate(page_entries), has_more, content, False, next_params)

    def genres(self):
        """The genre categories as folders, each opening action=genre."""
        from . import genres

        if self._need_tmdb("browse genres"):
            return
        media_type = self.params.get("media_type", "both")
        types = ["movie", "tv"] if media_type == "both" else [media_type]
        known = set()
        for kind in types:
            known.update(tmdb.genre_ids(kind))
        icon = {"movie": "DefaultMovies.png", "tv": "DefaultTVShows.png"}.get(media_type, "DefaultGenre.png")
        items = []
        for label, params in genres.CATEGORIES:
            # Leave out categories none of these types has, like Reality TV under Movies.
            if params.get("genres") and not any(g.lower() in known for g in params["genres"]):
                continue
            url = common.plugin_url(action="genre", media_type=media_type, **genres.genre_params(params))
            items.append(listing.folder_item(label, url, icon))
        self._finish(items, content="files")

    # -- playback -------------------------------------------------------------

    def play(self):
        p = self.params
        entry = {
            "kind": p.get("kind", "movie"),
            "tmdb": p.get("tmdb", ""),
            "imdb": p.get("imdb", ""),
            "tvdb": p.get("tvdb", ""),
            "title": p.get("title", ""),
            "show_title": p.get("show", ""),
            "year": p.get("year", ""),
            "premiered": p.get("premiered", ""),
            "poster": p.get("poster", ""),
            "thumb": p.get("thumb", ""),
            "show_imdb": p.get("show_imdb", ""),
        }
        if entry["kind"] == "episode":
            try:
                entry["season"] = int(p.get("season"))
                entry["episode"] = int(p.get("episode"))
            except (TypeError, ValueError):
                common.notify("This episode has no season/episode number")
                return
        if not model.playable(entry):
            common.notify("No TMDb, IMDb or TVDb id for this item")
            return
        self._fill_ids(entry)
        url = listing.player_url(entry)
        if not url:
            common.notify("Choose a player in Tally settings > Playback")
            return
        # A known position (resume lists) or 0 for "from the start"; left empty, the
        # service looks the position up when the video starts.
        resume = "0" if p.get("noresume") == "1" else str(int(float(p.get("resume") or 0)) or "")
        common.set_play_handoff(
            kind=entry["kind"],
            tmdb=entry["tmdb"],
            imdb=entry["imdb"],
            tvdb=entry["tvdb"],
            season=entry.get("season"),
            episode=entry.get("episode"),
            title=entry["title"],
            resume=resume,
        )
        common.log("Handing playback to {}".format(url))
        # Player files marked is_resolvable=false start the video themselves.
        builtin = "PlayMedia" if listing.player_resolvable(entry) else "RunPlugin"
        xbmc.executebuiltin('{}("{}")'.format(builtin, url))

    # -- actions --------------------------------------------------------------

    def _after_change(self, message):
        common.bump_widget_reload()
        common.notify(message)
        xbmc.executebuiltin("Container.Refresh")

    def _item(self):
        """(kind, tmdb, season, episode) from the params, season/episode as ints for episodes."""
        p = self.params
        kind = "movie" if p.get("kind") == "movie" else "episode"
        season = episode = None
        if kind == "episode":
            season, episode = int(p.get("season")), int(p.get("episode"))
        return kind, p.get("tmdb"), season, episode

    def mark_watched(self):
        from . import tracking

        kind, tmdb_id, season, episode = self._item()
        if not tmdb_id:
            return
        meta = tracking.title_meta("movie" if kind == "movie" else "tv", tmdb_id)
        common.store().add_play(kind, tmdb_id, season, episode, meta=meta)
        if kind == "episode":
            tracking.after_episode_change(tmdb_id)
        self._after_change("Marked as watched")

    def unwatch(self):
        from . import tracking

        kind, tmdb_id, season, episode = self._item()
        if not tmdb_id:
            return
        common.store().remove_play(kind, tmdb_id, season, episode)
        if kind == "episode":
            tracking.fill_next(tmdb_id)
        self._after_change("Marked as unwatched")

    def clear_resume(self):
        kind, tmdb_id, season, episode = self._item()
        if not tmdb_id:
            return
        common.store().clear_progress(kind, tmdb_id, season, episode)
        self._after_change("Removed from Continue Watching")

    def add_watchlist(self):
        from . import tracking

        kind, tmdb_id = self.params.get("media_type"), self.params.get("tmdb")
        if kind not in ("movie", "tv") or not tmdb_id:
            return
        if common.store().add_watchlist(kind, tmdb_id, tracking.title_meta(kind, tmdb_id)):
            self._after_change("Added to your watchlist")
        else:
            common.notify("Already in your Tally library")

    def remove_watchlist(self):
        kind, tmdb_id = self.params.get("media_type"), self.params.get("tmdb")
        if kind not in ("movie", "tv") or not tmdb_id:
            return
        if common.store().remove_watchlist(kind, tmdb_id):
            self._after_change("Removed from your watchlist")
        else:
            common.notify("Not on your watchlist")

    def untrack(self):
        p = self.params
        title = p.get("title") or "this title"
        if not xbmcgui.Dialog().yesno(
            common.ADDON_NAME,
            "Forget {}?\n\nThis deletes its watch history and resume points in Tally "
            "and cannot be undone.".format(title),
            nolabel="Cancel",
            yeslabel="Forget",
        ):
            return
        common.store().untrack(p.get("media_type"), p.get("tmdb"))
        self._after_change("Forgot {}".format(title))

    def refresh(self):
        # The service re-reads every watched show's seasons from TMDb, then reloads the widgets.
        common.home_property("tally.refresh_shows", "1")
        common.bump_widget_reload()
        common.notify("Widgets will reload")
        xbmc.executebuiltin("Container.Refresh")

    def clear_cache(self):
        from . import cache

        cache.clear_all()
        common.bump_widget_reload()
        common.notify("Cache cleared")

    def widget_paths(self):
        lines = ["Add any of these as a widget source. To refresh automatically, add",
                 "&reload=$INFO[Window(Home).Property({})]".format(common.PROP_RELOAD),
                 "to the end of the path.", ""]
        sections = (("All Videos", categories.ALL_MENU), ("Movies", categories.MOVIE_MENU),
                    ("TV", categories.TV_MENU))
        for title, keys in sections:
            lines.append("[B]{}[/B]".format(title))
            for key in keys:
                label = categories.CATEGORIES[key][0]
                lines.append("{}: {}".format(label, common.plugin_url(action="list", cat=key)))
            lines.append("")
        xbmcgui.Dialog().textviewer("Tally widget paths", "\n".join(lines))

    # shows is imported only by the routes that use it, to keep widget start-up light.

    def seasons(self):
        from . import shows
        shows.list_seasons(self)

    def episodes(self):
        from . import shows
        shows.list_episodes(self)

    def mark_season_watched(self):
        from . import shows
        shows.mark_season_watched(self)

    def mark_show_watched(self):
        from . import shows
        shows.mark_show_watched(self)

    def import_history(self):
        from . import import_ui
        import_ui.run(self)

    def settings(self):
        common.addon().openSettings()

    def pick_play_path(self):
        from . import pathpicker
        pathpicker.pick(self.params.get("kind", "movie"))

    def noop(self):
        if self.params.get("reason") == "no_tmdb":
            common.notify("This show has no TMDb id")


def run(argv=None):
    argv = argv or sys.argv
    common.freeze_settings()
    router = Router(argv)
    perf.mark("init")
    action = router.params.get("action", "root")
    handler = getattr(router, action, None)
    if handler is None or action.startswith("_"):
        common.log("Unknown action {}".format(action), xbmc.LOGWARNING)
        router._finish([], succeeded=False)
        return
    handler()
    if common.setting_bool("debug"):
        detail = router.params.get("cat") or ""
        common.log(perf.summary("{} {}".format(action, detail).strip()), xbmc.LOGINFO)
