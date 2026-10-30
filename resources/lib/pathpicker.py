"""Pick the plugin path movies or episodes play through.

The list is the installed player files (TMDb Helper's format, see playpaths.player_folders),
each with its add-on's icon: a player file says how its add-on plays a title, where a path
browsed by hand is often just a folder that can't. Browsing an add-on's folders is still
offered last; a browsed path has its values turned into placeholders before it is saved.
Kodi's own path setting only lists media sources and drives, not plugins, so the browsing
walks add-on directories over JSON-RPC.
"""

import json

import xbmc
import xbmcgui
import xbmcvfs

from . import common, playpaths

_SETTING = {"movie": "movie_plugin_path", "episode": "episode_plugin_path"}
_RUNPLUGIN = {"movie": "movie_plugin_runplugin", "episode": "episode_plugin_runplugin"}
_HEADING = {"movie": "Movie path", "episode": "Episode path"}
_OWN_PLAYERS = "special://profile/addon_data/{}/players/"


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


def _enabled(addon):
    return xbmc.getCondVisibility("System.HasAddon({0}) + System.AddonIsEnabled({0})".format(addon))


def _players(kind):
    folders = playpaths.player_folders(
        [xbmcvfs.translatePath(f) for f in playpaths.PLAYER_FOLDERS],
        xbmcvfs.translatePath(_OWN_PLAYERS.format(common.ADDON_ID)),
        xbmcvfs.translatePath("special://home/addons/"))
    return playpaths.players(kind, folders, _enabled)


def _icon(addon):
    result = _rpc("Addons.GetAddonDetails", {"addonid": addon, "properties": ["thumbnail"]})
    return (result.get("addon") or {}).get("thumbnail") or ""


def _item(label, thumb="", label2=""):
    li = xbmcgui.ListItem(label, label2, offscreen=True)
    if thumb:
        li.setArt({"thumb": thumb, "icon": thumb})
    return li


def _browse_addon(heading):
    """An add-on's folder or item, picked by walking its directories: (path, resolvable), or
    None to go back."""
    dialog = xbmcgui.Dialog()
    while True:
        addons = _addons()
        items = [_item(a.get("name") or a["addonid"], a.get("thumbnail")) for a in addons]
        choice = dialog.select(heading, items, useDetails=True)
        if choice < 0:
            return None
        trail = ["plugin://{}/".format(addons[choice]["addonid"])]
        while trail:
            path = trail[-1]
            files = _list(path)
            folders = [f for f in files if f.get("filetype") == "directory"]
            playable = [f for f in files if f.get("filetype") != "directory"]
            labels = ["[B]Use this path[/B]", ".."]
            labels += ["{}  >".format(f.get("label") or "") for f in folders]
            labels += [f.get("label") or "" for f in playable]
            choice = dialog.select("{}: {}".format(heading, path), labels)
            if choice < 0:
                return None
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


def browse(heading, kind):
    """Return (path, resolvable) for the choice, or None when cancelled."""
    dialog = xbmcgui.Dialog()
    found = _players(kind)
    if not found:
        dialog.ok(heading, "No player files for {}s from installed add-ons. Put TMDb Helper player files in "
                           "{}, or pick a path from an add-on's folders.".format(
                               kind, _OWN_PLAYERS.format(common.ADDON_ID)))
        return _browse_addon(heading)
    icons = {}
    items = []
    for name, addon, path, _ in found:
        if addon not in icons:
            icons[addon] = _icon(addon)
        items.append(_item(name, icons[addon], path))
    items.append(_item("Browse an add-on's folders...", "DefaultFolder.png",
                       "A path picked by hand may only open a folder rather than play"))
    while True:
        choice = dialog.select(heading, items, useDetails=True)
        if choice < 0:
            return None
        if choice < len(found):
            return found[choice][2:]
        picked = _browse_addon(heading)
        if picked:
            return picked


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
