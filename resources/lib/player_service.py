"""Background service: tracks playback into Tally's store, offers resume points, keeps shows current.

A play started by Tally's play route (or a skin) is named by the tally.play.* hand-off; any
other video is identified from what Kodi knows about it (ids, TMDb Helper, the library, the
show title). While it plays its position is saved; past the watched threshold it is recorded
as a play. Store writes run on a worker thread so the player callbacks never wait on TMDb.
"""

import json
import queue
import threading
import time

import xbmc
import xbmcgui

from . import cache, common, tmdb, tracking

PENDING_MAX_AGE = 180  # seconds between the tally.play.* hand-off and the video starting
MIN_POSITION = 60  # do not save accidental starts
PROP_TMDBHELPER_PLAYING = "TMDbHelper.PlayerInfoString"  # set by TMDb Helper's player
PROP_REFRESH_SHOWS = "tally.refresh_shows"  # set by the plugin's Refresh widgets
SHOW_REFRESH_AGE = 24 * 3600  # a watched show's seasons are re-read from TMDb this often
UPKEEP_EVERY = 3600


def completed_ratio():
    """Watched threshold from settings (default 80%)."""
    return max(50, min(100, common.setting_int("watched_percent", 80))) / 100.0


def _fmt(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return "{}:{:02d}:{:02d}".format(hours, minutes, secs) if hours else "{}:{:02d}".format(minutes, secs)


def _rpc(method, params):
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(request))).get("result") or {}
    except ValueError:
        return {}


class Playing:
    """What is playing: a movie, or an episode of a show, by TMDb id."""

    def __init__(self, kind, tmdb_id, season=None, episode=None, title="", resume=None):
        self.kind = kind
        self.tmdb = str(tmdb_id)
        self.season = season
        self.episode = episode
        self.title = title  # the movie's or the episode's
        self.resume = resume  # seconds; None = look it up when the video starts
        self.position = 0.0
        self.total = 0.0
        self.last_saved = time.time()


class TallyPlayer(xbmc.Player):
    def __init__(self):
        super().__init__()
        self.current = None
        self._handoff_seen = (None, 0.0)  # (tally.play.* values, when first seen)
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def shutdown(self, timeout=3):
        """Stop the worker; Kodi waits on every Python thread, daemon or not, before exiting."""
        self._queue.put(None)
        self._thread.join(timeout)

    # -- identify what started ---------------------------------------------------

    def _watch_handoff(self):
        """Note when tally.play.* appears, and drop it once too old to belong to a new video."""
        values = common.read_play_handoff()
        seen, since = self._handoff_seen
        if not values:
            self._handoff_seen = (None, 0.0)
        elif values != seen:
            self._handoff_seen = (values, time.time())
        elif time.time() - since > PENDING_MAX_AGE:
            common.log("Dropping a play hand-off that never started: {}".format(values))
            common.clear_play_handoff()
            self._handoff_seen = (None, 0.0)

    def _from_handoff(self):
        values = common.read_play_handoff()
        common.clear_play_handoff()
        seen, since = self._handoff_seen
        self._handoff_seen = (None, 0.0)
        if not values or (values == seen and time.time() - since > PENDING_MAX_AGE):
            return None
        kind = values.get("kind")
        if kind not in ("movie", "episode"):
            return None
        season = episode = None
        if kind == "episode":
            try:
                season, episode = int(values["season"]), int(values["episode"])
            except (KeyError, ValueError):
                return None
        tmdb_id = values.get("tmdb") or tmdb.find_id(
            "movie" if kind == "movie" else "tv", values.get("imdb", ""), values.get("tvdb", ""))
        if not tmdb_id:
            return None
        try:
            resume = int(float(values["resume"])) if "resume" in values else None
        except ValueError:
            resume = None
        return Playing(kind, tmdb_id, season, episode, values.get("title") or "", resume)

    @staticmethod
    def _from_tmdbhelper(kind, season=None, episode=None, tag_ids=None):
        """The TMDb id for playback started by TMDb Helper's player, if it is what is playing.

        TMDb Helper keeps its last played item in a Home window property; episodes carry the
        show's ids. The property is not cleared after playback, so it is only used when it
        matches what is playing now.
        """
        info = common.get_json_property(PROP_TMDBHELPER_PLAYING) or {}
        if not info.get("tmdb_id"):
            return ""
        if kind == "movie":
            if info.get("tmdb_type") != "movie" or any(
                value and info.get(key) and str(info[key]) != str(value)
                for key, value in (("tmdb_id", (tag_ids or {}).get("tmdb")),
                                   ("imdb_id", (tag_ids or {}).get("imdb")))
            ):
                return ""  # left over from an earlier TMDb Helper play
        elif (info.get("tmdb_type") not in ("episode", "tv") or info.get("season") != season
              or info.get("episode") != episode):
            return ""
        return str(info["tmdb_id"])

    @staticmethod
    def _library_show_tmdb():
        """The show's TMDb id when a library episode is playing."""
        players = _rpc("Player.GetActivePlayers", {})
        video = next((p for p in players or [] if p.get("type") == "video"), None)
        if not video:
            return ""
        item = _rpc("Player.GetItem", {"playerid": video["playerid"], "properties": ["tvshowid"]}).get("item") or {}
        if not item.get("tvshowid") or item["tvshowid"] < 0:
            return ""
        show = _rpc("VideoLibrary.GetTVShowDetails",
                    {"tvshowid": item["tvshowid"], "properties": ["uniqueid"]}).get("tvshowdetails") or {}
        return str((show.get("uniqueid") or {}).get("tmdb") or "")

    def _from_player(self):
        if not common.setting_bool("track_external", True):
            return None
        try:
            tag = self.getVideoInfoTag()
        except RuntimeError:
            return None
        media_type = tag.getMediaType()
        if media_type == "movie":
            ids = {"tmdb": tag.getUniqueID("tmdb"), "imdb": tag.getUniqueID("imdb") or tag.getIMDBNumber()}
            tmdb_id = (self._from_tmdbhelper("movie", tag_ids=ids) or ids["tmdb"]
                       or tmdb.find_id("movie", ids["imdb"])
                       or tmdb.find_by_title("movie", tag.getTitle(), tag.getYear()))
            return Playing("movie", tmdb_id, title=tag.getTitle()) if tmdb_id else None
        if media_type == "episode":
            season, episode = tag.getSeason(), tag.getEpisode()
            if season < 0 or episode < 0:
                return None
            # A playing episode's tmdb uniqueid may be the episode's own id, never used as the show's.
            # Its imdb/tvdb ids find the show whether they are the episode's or the show's.
            tmdb_id = (self._from_tmdbhelper("episode", season, episode) or self._library_show_tmdb()
                       or tmdb.find_id("tv", tag.getUniqueID("imdb") or tag.getIMDBNumber(),
                                       tag.getUniqueID("tvdb"))
                       or tmdb.find_by_title("tv", tag.getTVShowTitle()))
            return Playing("episode", tmdb_id, season, episode, tag.getTitle()) if tmdb_id else None
        return None

    # -- callbacks -----------------------------------------------------------------

    def onAVStarted(self):
        if not self.isPlayingVideo():
            return
        if common.background_trailer():
            # Not tracked, and a pending tally.play.* hand-off stays for the real video.
            with self._lock:
                self.current = None
            return
        try:
            playing = self._from_handoff() or self._from_player()
        except Exception as exc:  # noqa: BLE001 - an unidentified video is simply not tracked
            common.log("Could not identify what is playing: {}".format(exc), xbmc.LOGWARNING)
            playing = None
        with self._lock:
            self.current = playing
        if not playing:
            return
        common.log("Tracking {} {} S{}E{}".format(playing.kind, playing.tmdb, playing.season, playing.episode))
        self._maybe_resume(playing)

    def onPlayBackPaused(self):
        self._save(final=False)

    def onPlayBackStopped(self):
        if common.background_trailer():
            return
        self._save(final=True)

    def onPlayBackEnded(self):
        if common.background_trailer():
            return
        with self._lock:
            if self.current and self.current.total:
                self.current.position = self.current.total
        self._save(final=True, ended=True)

    def onPlayBackError(self):
        with self._lock:
            self.current = None

    # -- resume -------------------------------------------------------------------

    def _maybe_resume(self, playing):
        mode = common.setting_int("resume_mode", 1)
        if mode == 0:
            return
        if playing.resume is None:
            playing.resume = int(common.store().get_progress(
                playing.kind, playing.tmdb, playing.season, playing.episode)[0])
        if playing.resume < MIN_POSITION:
            return
        # Give the player add-on a moment; if it already resumed, leave it alone.
        xbmc.sleep(1000)
        try:
            if self.getTime() > 30:
                return
            total = self.getTotalTime()
        except RuntimeError:
            return
        if total and playing.resume >= total * 0.95:
            return
        if mode == 1:
            self.pause()
            choice = xbmcgui.Dialog().contextmenu(
                ["Resume from {}".format(_fmt(playing.resume)), "Play from the start"])
            self.pause()
            if choice != 0:
                return
        try:
            self.seekTime(float(playing.resume))
        except RuntimeError:
            pass

    # -- saving -------------------------------------------------------------------

    def tick(self):
        """Called about once a second by the service loop."""
        if common.background_trailer():
            return
        self._watch_handoff()
        with self._lock:
            playing = self.current
        if not playing or not self.isPlayingVideo():
            return
        try:
            playing.position = self.getTime()
            playing.total = self.getTotalTime() or playing.total
        except RuntimeError:
            return
        interval = max(30, common.setting_int("progress_interval", 120))
        if time.time() - playing.last_saved >= interval:
            self._save(final=False)

    def _save(self, final, ended=False):
        with self._lock:
            playing = self.current
            if final:
                self.current = None
        if not playing:
            return
        playing.last_saved = time.time()
        completed = ended or bool(playing.total and playing.position >= playing.total * completed_ratio())
        if not completed and (playing.position < MIN_POSITION or not common.setting_bool("save_progress", True)):
            return
        self._queue.put((playing.kind, playing.tmdb, playing.season, playing.episode, playing.title,
                         playing.position, playing.total, completed, final))

    def _worker(self):
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                self._write(*job)
            except Exception as exc:  # noqa: BLE001 - never let the worker die
                common.log("Could not save playback: {}".format(exc), xbmc.LOGWARNING)

    @staticmethod
    def _write(kind, tmdb_id, season, episode, title, position, total, completed, final):
        store = common.store()
        title_kind = "movie" if kind == "movie" else "tv"
        # Ids, title and poster for a title Tally doesn't have yet (cached TMDb lookups).
        meta = None if store.get(title_kind, tmdb_id) else tracking.title_meta(title_kind, tmdb_id)
        if completed:
            store.add_play(kind, tmdb_id, season, episode, meta=meta)
            common.log("Watched {} {} S{}E{}".format(kind, tmdb_id, season, episode), xbmc.LOGINFO)
        else:
            store.save_progress(kind, tmdb_id, position, total, season, episode,
                                title=title if kind == "episode" else "", meta=meta)
            common.log("Saved position {:.0f}s of {} {} S{}E{}".format(position, kind, tmdb_id, season, episode))
        if kind == "episode" and completed:
            tracking.after_episode_change(tmdb_id)
        if final or completed:
            common.bump_widget_reload()


class Upkeep:
    """Re-reads watched shows' seasons once a day (new episodes) and fetches artwork widgets left."""

    def __init__(self, player):
        self._player = player
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._last = 0.0

    def start(self):
        self._thread.start()

    def shutdown(self, timeout=3):
        self._stop.set()
        self._thread.join(timeout)

    def _run(self):
        while not self._stop.wait(2):
            try:
                self.cycle()
            except Exception as exc:  # noqa: BLE001 - never let upkeep die
                common.log("Upkeep failed: {}".format(exc), xbmc.LOGWARNING)

    def cycle(self):
        # Leave the network to the video while one is playing.
        if self._player.isPlayingVideo() or common.background_trailer():
            return
        changed = False
        forced = common.home_property(PROP_REFRESH_SHOWS) == "1"
        if forced or time.time() - self._last > UPKEEP_EVERY:
            common.clear_home_property(PROP_REFRESH_SHOWS)
            self._last = time.time()
            changed = self._refresh_shows(force=forced)
        if tmdb.fetch_pending(self._stop):
            changed = True
        if changed and not self._stop.is_set():
            common.log("Shows or artwork updated; reloading widgets")
            common.bump_widget_reload()

    def _refresh_shows(self, force):
        if not tmdb.enabled():
            return False
        store = common.store()
        before = {r["tmdb"]: (r["next_season"], r["next_episode"], r["next_title"])
                  for r in store.titles("tv", limit=1000)}
        older_than = time.time() - (0 if force else SHOW_REFRESH_AGE)
        for tmdb_id in store.shows_to_refresh(older_than, limit=200):
            if self._stop.is_set():
                break
            tracking.refresh_show(tmdb_id, refresh=True)
        for tmdb_id, _, _ in store.next_missing_meta():
            if self._stop.is_set():
                break
            tracking.fill_next(tmdb_id)
        after = {r["tmdb"]: (r["next_season"], r["next_episode"], r["next_title"])
                 for r in store.titles("tv", limit=1000)}
        return before != after


def run():
    monitor = xbmc.Monitor()
    player = TallyPlayer()
    upkeep = Upkeep(player)
    upkeep.start()
    common.log("Service started", xbmc.LOGINFO)
    last_purge = 0
    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break
        try:
            player.tick()
        except Exception as exc:  # noqa: BLE001 - never let the service die
            common.log("tick failed: {}".format(exc), xbmc.LOGWARNING)
        if time.time() - last_purge > 6 * 3600:
            last_purge = time.time()
            try:
                cache.purge_expired()
            except Exception:  # noqa: BLE001
                pass
    upkeep.shutdown()
    player.shutdown()
    common.log("Service stopped", xbmc.LOGINFO)
