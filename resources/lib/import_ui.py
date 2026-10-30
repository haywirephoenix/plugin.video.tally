"""The Import route: choose an export, match it to an importer definition, merge it into the store."""

import os
import urllib.parse
import urllib.request

import xbmcgui
import xbmcvfs

from . import common, importers, tmdb

MAX_DOWNLOAD = 64 * 1024 * 1024


def _folders():
    return [os.path.join(xbmcvfs.translatePath(common.ADDON.getAddonInfo("path")), "resources", "importers"),
            os.path.join(common.PROFILE_DIR, "importers")]


def _read(path):
    handle = xbmcvfs.File(path)
    try:
        return bytes(handle.readBytes()).decode("utf-8-sig", errors="replace")
    finally:
        handle.close()


def _download(url):
    """The text at an http(s) URL; a file too big to be an export is refused."""
    url = urllib.parse.quote(url, safe=":/?&=%#+@~!$'()*,;")  # spaces typed into the address
    request = urllib.request.Request(url,headers={"User-Agent": "Kodi {}".format(common.ADDON_ID)})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("bigger than {} MB".format(MAX_DOWNLOAD // (1024 * 1024)))
    return data.decode("utf-8-sig", errors="replace")


def _choose_source(router, dialog, masks):
    """(where, text) of the export to import, from the params, a file or a URL; None when cancelled."""
    url = router.params.get("url")
    if not url:
        picked = dialog.select("Import from", ["A file", "A URL"])
        if picked < 0:
            return None
        if picked == 0:
            path = dialog.browse(1, "Choose an export to import", "files", "|".join(masks))
            return (path, _read(path)) if path else None
        url = dialog.input("URL of the export", common.setting("import_url")).strip()
        if not url:
            return None
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("not an http(s) URL")
    common.addon().setSetting("import_url", url)  # offered again next time
    progress = xbmcgui.DialogProgressBG()
    progress.create(common.ADDON_NAME, "Downloading the export")
    try:
        return url, _download(url)
    finally:
        progress.close()


def _resolve(kind, imdb, tvdb, title, year):
    return tmdb.find_id(kind, imdb, tvdb) or tmdb.find_by_title(kind, title, year)


def _structure(tmdb_id):
    info = tmdb.show_info(tmdb_id)
    if not info:
        return None
    return [[s["season"], s["episodes"]] for s in info["seasons"]], info.get("last_aired")


def run(router):
    dialog = xbmcgui.Dialog()
    found = {key: importers.create(d) for key, d in importers.definitions(*_folders()).items()}
    if not found:
        common.notify("No importers found")
        return
    masks = sorted({ext for imp in found.values() for ext in imp.definition.get("extensions") or []})
    try:
        source = _choose_source(router, dialog, masks)
    except Exception as exc:  # noqa: BLE001 - any read or download failure is reported the same way
        common.log("Import: couldn't read the export: {}".format(exc))
        dialog.ok(common.ADDON_NAME, "Couldn't read the export: {}".format(exc))
        return
    if not source:
        return
    path, text = source

    wanted = router.params.get("importer")
    choices = [found[wanted]] if wanted in found else [i for i in found.values() if i.detects(text)]
    if len(choices) != 1:
        choices = choices or sorted(found.values(), key=lambda i: i.name)
        picked = dialog.select("Which app is this export from?", [i.name for i in choices])
        if picked < 0:
            return
        choices = [choices[picked]]
    importer = choices[0]
    if not tmdb.enabled() and not dialog.yesno(
            common.ADDON_NAME, "There is no TMDb key in Tally settings, so titles without a TMDb id "
            "and whole shows marked watched can't be imported.\n\nImport anyway?",
            nolabel="Cancel", yeslabel="Import"):
        return

    progress = xbmcgui.DialogProgressBG()
    progress.create(common.ADDON_NAME, "Importing from {}".format(importer.name))
    looked_up = [0]

    def resolve(*args):
        looked_up[0] += 1
        if looked_up[0] % 10 == 1:
            progress.update(30, message="Matching titles on TMDb ({})".format(looked_up[0]))
        return _resolve(*args)

    try:
        records = list(importer.records(text))
        progress.update(20, message="{} entries read".format(len(records)))
        counts = importers.apply(common.store(), records,
                                 resolve=resolve if tmdb.enabled() else None,
                                 structure=_structure if tmdb.enabled() else None)
    except Exception as exc:  # noqa: BLE001 - shown to the user, then logged
        progress.close()
        common.log("Import from {} failed: {}".format(importer.name, exc))
        dialog.ok(common.ADDON_NAME, "The import failed: {}".format(exc))
        return
    progress.close()
    common.store().log_import(importer.id, path, counts)
    common.home_property("tally.refresh_shows", "1")  # the service reads the shows' seasons next
    common.bump_widget_reload()
    dialog.ok(common.ADDON_NAME, "Imported from {}: {} titles, {} plays, {} resume points.{}".format(
        importer.name, counts["titles_added"], counts["plays_added"], counts["progress_added"],
        "\n{} entries had no TMDb match.".format(counts["unmatched"]) if counts["unmatched"] else ""))
