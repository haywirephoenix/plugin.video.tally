"""Fill an item's values into a player path, and read TMDb Helper-style player files.

Placeholders work as in TMDb Helper's player files: {title} as it is, {title_url} URL-encoded
(ENCODINGS), any name TMDb Helper uses (ALIASES), unknown names empty. A path without
placeholders gets the values of query keys it already has replaced, and missing ones added.
No Kodi imports here except in player_folders(), so this runs outside Kodi too.
"""

import glob
import json
import os
import re
from urllib.parse import parse_qsl, quote, quote_plus, urlencode

# Query keys a path may already use for each value; the first is added when none is.
PATH_KEYS = (
    ("tmdb", ("tmdb_id", "tmdb", "tmdbid")),
    ("imdb", ("imdb_id", "imdb", "imdbnumber")),
    ("tvdb", ("tvdb_id", "tvdb", "tvdbid")),
    ("season", ("season",)),
    ("episode", ("episode",)),
    ("title", ("title",)),
    ("year", ("year",)),
)
# TMDb Helper's other names for some values.
ALIASES = {"tvshowtitle": "showname", "clearname": "showname", "firstaired": "premiered",
           "released": "premiered", "showyear": "year"}
TEXT_FIELDS = {"title", "showname", "eptitle"}  # templatize() makes these {title_url}
ENCODINGS = {
    "": lambda v: v,
    "_url": quote,
    "_url+": quote_plus,
    "_+": lambda v: v.replace(" ", "+"),
    "_-": lambda v: v.replace(" ", "-"),
    "_escaped": lambda v: quote(quote(v)),
    "_escaped+": lambda v: quote(quote_plus(v)),
}
_SUFFIXES = "|".join(re.escape(s) for s in sorted(ENCODINGS, key=len, reverse=True) if s)
PLACEHOLDER = re.compile(r"\{([a-z_]+?)(" + _SUFFIXES + r")?\}")

# Player files, the user's imported ones first; the first copy of a file name wins. Read only:
# neither add-on is needed.
PLAYER_FOLDERS = (
    "special://profile/addon_data/plugin.video.themoviedb.helper/players/",
    "special://profile/addon_data/plugin.video.tmdb.bingie.helper/players/",
    "special://home/addons/plugin.video.themoviedb.helper/resources/players/",
    "special://home/addons/plugin.video.tmdb.bingie.helper/resources/players/",
)


def values(entry):
    """Every value a path can use, as text. title is the show's for an episode (this add-on's own
    meaning); eptitle is the episode's; id is TMDb Helper's (the TMDb id); imdb is the show's
    for an episode, as in TMDb Helper."""
    def text(v):
        return "" if v is None else str(v)

    episode = entry.get("kind") == "episode"
    show = entry.get("show_title") or entry.get("title") or ""
    title = text(entry.get("title"))
    year = text(entry.get("year"))
    if episode:
        try:
            name = "{} S{:02d}E{:02d}".format(show, int(entry["season"]), int(entry["episode"]))
        except (KeyError, TypeError, ValueError):
            name = show
    else:
        name = "{} ({})".format(title, year) if year else title
    return {
        "tmdb": text(entry.get("tmdb")),
        "id": text(entry.get("tmdb")),
        "imdb": text(entry.get("show_imdb") if episode else entry.get("imdb")),
        "tvdb": text(entry.get("tvdb")),
        "season": text(entry.get("season")),
        "episode": text(entry.get("episode")),
        "title": show if episode else title,
        "showname": show,
        "eptitle": title if episode else "",
        "year": year,
        "premiered": text(entry.get("premiered")),
        "poster": text(entry.get("poster")),
        "fanart": text(entry.get("fanart")),
        "thumbnail": text(entry.get("thumb")),
        "name": name,
    }


def fill_placeholders(template, vals):
    def put(match):
        name = ALIASES.get(match.group(1), match.group(1))
        return ENCODINGS[match.group(2) or ""](vals.get(name, ""))
    return PLACEHOLDER.sub(put, template)


def fill(path, vals):
    """path with vals put into its {placeholders}, or as its query keys' values (adding
    missing ones, dropping empty ones)."""
    if PLACEHOLDER.search(path):
        return fill_placeholders(path, vals)
    base, _, query = path.partition("?")
    params = parse_qsl(query, keep_blank_values=True)
    present = {k.lower() for k, _ in params}
    replace = {key: field for field, keys in PATH_KEYS for key in keys}
    params = [(k, vals[replace[k.lower()]] if k.lower() in replace else v) for k, v in params]
    for field, keys in PATH_KEYS:
        if vals.get(field) and not present.intersection(keys):
            params.append((keys[0], vals[field]))
    params = [(k, v) for k, v in params if v != ""]
    return base + "?" + urlencode(params) if params else base


def templatize(path):
    """path with the values of known query keys turned into placeholders (tmdb_id=27205 ->
    tmdb_id={tmdb}, title=Inception -> title={title_url}), so the setting shows what is filled."""
    base, sep, query = path.partition("?")
    if not sep:
        return path
    names = {key: field for field, keys in PATH_KEYS for key in keys}
    parts = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        field = names.get(key.lower())
        if field:
            parts.append("{}={{{}{}}}".format(key, field, "_url" if field in TEXT_FIELDS else ""))
        else:
            parts.append(urlencode([(key, value)]))
    return base + "?" + "&".join(parts)


def player_template(player, kind):
    """A player file's play_movie / play_episode path, or None. In player files an episode's
    {title} is the episode's own; here that is {eptitle}."""
    template = player.get("play_" + kind) if isinstance(player, dict) else None
    if not isinstance(template, str) or not template.startswith("plugin://"):
        return None  # list forms ([path, rules]) need TMDb Helper's own steps
    if kind == "episode":
        template = re.sub(r"\{title(?=[_+}-])", "{eptitle", template)
    return template


def read_players(folders):
    """{file name: player} from the folders' *.json, the first folder's copy winning."""
    out = {}
    for folder in folders:
        for name in sorted(glob.glob(os.path.join(folder, "*.json"))):
            key = os.path.basename(name).lower()
            if key in out:
                continue
            try:
                with open(name, encoding="utf-8") as f:
                    out[key] = json.load(f)
            except (OSError, ValueError):
                continue
    return out


def players(kind, folders, installed, plugin=None):
    """[(name, plugin, path, resolvable)] for kind from the folders' player files whose add-on
    is installed (installed(addon_id) -> bool), or only plugin's; highest priority first."""
    found = []
    for player in read_players(folders).values():
        template = player_template(player, kind)
        addon = player.get("plugin") if isinstance(player, dict) else None
        if not template or not isinstance(addon, str) or (plugin and addon != plugin):
            continue
        if not installed(addon):
            continue
        resolvable = str(player.get("is_resolvable", "true")).lower() != "false"
        try:
            priority = int(player.get("priority", 1000))
        except (TypeError, ValueError):
            priority = 1000
        found.append((priority, str(player.get("name") or addon), addon, template, resolvable))
    return [f[1:] for f in sorted(found, key=lambda f: (f[0], f[1].lower()))]
