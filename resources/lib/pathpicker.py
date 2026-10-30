"""Browse video add-ons to pick the plugin path movies or episodes play through.

Kodi's own path setting only lists media sources and drives, not plugins, so this walks
add-on directories over JSON-RPC instead. Player files (TMDb Helper's format, see
playpaths.PLAYER_FOLDERS) are offered ready-made; a browsed path has its values turned into
placeholders before it is saved.
"""

import json

import xbmc
import xbmcgui
import xbmcvfs

from . import common, playpaths

_SETTING = {"movie": "movie_plugin_path", "episode": "episode_plugin_path"}
_RUNPLUGIN = {"movie": "movie_plugin_runplugin", "episode": "episode_plugin_runplugin"}
_HEADING = {"movie": "Movie path", "episode": "Episode path"}


def _rpc(method, params):
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(request))).get("result") or {}
    except ValueError:
        return {}


def _addons():
    result = _rpc("Addons.GetAddons", {"type": "xbmc.addon.video", "content": "video",
                                       "enabled": True, "properties": ["name", "thumbnail"]})
    addons = [a for a in result.get("addons") or [] if a.get("addonid") != common.ADDON_ID]
    return sorted(addons, key=lambda a: (a.get("name") or "").lower())


def _list(path):
    result = _rpc("Files.GetDirectory", {"directory": path, "media": "video",
                                         "properties": ["thumbnail"]})
    return result.get("files") or []


def _players(kind, plugin=None):
    folders = [xbmcvfs.translatePath(f) for f in playpaths.PLAYER_FOLDERS]
    return playpaths.players(
        kind, folders, lambda a: xbmc.getCondVisibility("System.HasAddon({})".format(a)), plugin)


def _item(label, thumb="", label2=""):
    li = xbmcgui.ListItem(label, label2, offscreen=True)
    if thumb:
        li.setArt({"thumb": thumb, "icon": thumb})
    return li


def _pick_player(heading, kind):
    found = _players(kind)
    if not found:
        xbmcgui.Dialog().ok(heading, "No player files for {}s found.".format(kind))
        return None
    items = [_item(name, label2=path) for name, _, path, _ in found]
    choice = xbmcgui.Dialog().select(heading, items, useDetails=True)
    return None if choice < 0 else found[choice][2:]


def browse(heading, kind):
    """Return (path, resolvable) for the choice, or None when cancelled."""
    dialog = xbmcgui.Dialog()
    while True:
        addons = _addons()
        items = [_item("[B]Player files...[/B]")]
        items += [_item(a.get("name") or a["addonid"], a.get("thumbnail")) for a in addons]
        choice = dialog.select(heading, items, useDetails=True)
        if choice < 0:
            return None
        if choice == 0:
            picked = _pick_player(heading, kind)
            if picked:
                return picked
            continue
        addon = addons[choice - 1]["addonid"]
        ready = _players(kind, addon)
        trail = ["plugin://{}/".format(addon)]
        while trail:
            path = trail[-1]
            files = _list(path)
            folders = [f for f in files if f.get("filetype") == "directory"]
            playable = [f for f in files if f.get("filetype") != "directory"]
            shown = ready if len(trail) == 1 else []
            labels = ["[B]Player: {}[/B]".format(name) for name, _, _, _ in shown]
            labels += ["[B]Use this path[/B]", ".."]
            labels += ["{}  >".format(f.get("label") or "") for f in folders]
            labels += [f.get("label") or "" for f in playable]
            choice = dialog.select("{}: {}".format(heading, path), labels)
            if choice < 0:
                return None
            if choice < len(shown):
                return shown[choice][2:]
            choice -= len(shown)
            if choice == 0:
                return playpaths.templatize(path), True
            if choice == 1:
                trail.pop()
                continue
            picked = (folders + playable)[choice - 2]
            if picked.get("filetype") == "directory":
                trail.append(picked["file"])
            else:
                return playpaths.templatize(picked["file"]), True


def pick(kind):
    kind = kind if kind in _SETTING else "movie"
    addon = common.addon()
    picked = browse(_HEADING[kind], kind)
    if picked is not None:
        path, resolvable = picked
        addon.setSetting(_SETTING[kind], path)
        addon.setSetting(_RUNPLUGIN[kind], "false" if resolvable else "true")
        common.notify("{} set".format(_HEADING[kind]))
    # The settings window closed to run this; reopen it so the new value shows.
    addon.openSettings()
