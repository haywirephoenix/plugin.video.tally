"""Shared helpers: settings, logging, paths, plugin URLs and the tracking store."""

import json
import os
from urllib.parse import urlencode

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from . import perf

ADDON_ID = "plugin.video.tally"
ADDON = xbmcaddon.Addon(ADDON_ID)
ADDON_NAME = ADDON.getAddonInfo("name")
ADDON_ICON = ADDON.getAddonInfo("icon")
PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))

# Home window properties shared between the plugin and the service.
HOME = 10000
PROP_RELOAD = "tally.widget_reload"

# What is about to play, set just before playback by Tally's play route or by a skin
# (tally.play.kind, tally.play.tmdb, ...); the service reads it when the video starts.
PLAY_PREFIX = "tally.play."
# Set by skin.ultra while it plays a trailer behind Home: that playback is not tracked.
PROP_BG_TRAILER = "ultra.bg.trailer"
PLAY_FIELDS = ("kind", "tmdb", "imdb", "tvdb", "season", "episode", "title", "resume")

# A plugin run lasts well under a second, so it reads each setting once (see freeze_settings).
_frozen = None
_store = None


def addon():
    """Return a fresh Addon object so settings changed at runtime are seen."""
    return xbmcaddon.Addon(ADDON_ID)


def freeze_settings():
    """Read each setting at most once for the rest of this run (plugin runs only, not the service)."""
    global _frozen
    if _frozen is None:
        _frozen = {}


def _raw_setting(key):
    if _frozen is not None and key in _frozen:
        return _frozen[key]
    with perf.span("settings"):
        value = (ADDON if _frozen is not None else addon()).getSetting(key)
    if _frozen is not None:
        _frozen[key] = value
    return value


def setting(key, default=""):
    value = _raw_setting(key)
    return value if value != "" else default


def setting_int(key, default=0):
    try:
        return int(_raw_setting(key))
    except (TypeError, ValueError):
        return default


def setting_bool(key, default=False):
    value = _raw_setting(key)
    if value == "":
        return default
    return value.lower() == "true"


def log(message, level=xbmc.LOGDEBUG):
    if level == xbmc.LOGDEBUG and setting_bool("debug"):
        level = xbmc.LOGINFO
    xbmc.log("[{}] {}".format(ADDON_ID, message), level)


def notify(message, heading=None, time_ms=4000):
    xbmcgui.Dialog().notification(heading or ADDON_NAME, message, ADDON_ICON, time_ms)


def ensure_profile_dir():
    if not os.path.isdir(PROFILE_DIR):
        os.makedirs(PROFILE_DIR, exist_ok=True)
    return PROFILE_DIR


def store():
    """The tracking database (tally.db in the add-on's profile), one connection per process."""
    global _store
    if _store is None:
        from .store import Store

        with perf.span("store_open"):
            _store = Store(os.path.join(ensure_profile_dir(), "tally.db"))
    return _store


def plugin_url(**params):
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return "plugin://{}/?{}".format(ADDON_ID, urlencode(clean))


def home_property(name, value=None):
    window = xbmcgui.Window(HOME)
    if value is None:
        return window.getProperty(name)
    window.setProperty(name, value)
    return value


def clear_home_property(name):
    xbmcgui.Window(HOME).clearProperty(name)


def background_trailer():
    """True while a skin's background trailer is starting, playing or stopping."""
    return bool(home_property(PROP_BG_TRAILER))


def read_play_handoff():
    """The tally.play.* properties that are set, as {field: value}."""
    window = xbmcgui.Window(HOME)
    values = {f: window.getProperty(PLAY_PREFIX + f) for f in PLAY_FIELDS}
    return {f: v for f, v in values.items() if v}


def set_play_handoff(**values):
    window = xbmcgui.Window(HOME)
    for field in PLAY_FIELDS:
        value = values.get(field)
        if value is None or value == "":
            window.clearProperty(PLAY_PREFIX + field)
        else:
            window.setProperty(PLAY_PREFIX + field, str(value))


def clear_play_handoff():
    set_play_handoff()


def get_json_property(name):
    raw = home_property(name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def bump_widget_reload():
    """Change the reload token that widget paths include, so Kodi lists the widgets again.

    The token is the time in milliseconds: TMDb Helper's Tally sync reads it to resync after a change.
    """
    import time

    home_property(PROP_RELOAD, str(int(time.time() * 1000)))
