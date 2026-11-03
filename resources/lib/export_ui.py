"""The Export route: choose a tracker's format and a folder, and write Tally's history there."""

import xbmcgui
import xbmcvfs

from . import common, exporters, tmdb


def run(router):
    dialog = xbmcgui.Dialog()
    keys = sorted(exporters.EXPORTERS)
    picked = dialog.select("Export for", [xbmcgui.ListItem(exporters.EXPORTERS[k][0], exporters.EXPORTERS[k][2])
                                          for k in keys], useDetails=True)
    if picked < 0:
        return
    name, export, _ = exporters.EXPORTERS[keys[picked]]
    folder = dialog.browse(3, "Choose a folder for the export", "files")
    if not folder:
        return
    progress = xbmcgui.DialogProgressBG()
    progress.create(common.ADDON_NAME, "Exporting for {}".format(name))
    looked_up = [0]

    def episode_ids(show, season):
        """{episode: TMDb's id} of a season (cached; one request a season the first time)."""
        looked_up[0] += 1
        if looked_up[0] % 5 == 1:
            progress.update(50, message="Looking up episodes on TMDb ({} seasons)".format(looked_up[0]))
        return {number: ep.get("id") for number, ep in tmdb.season_episodes(show, season).items()}

    try:
        files, notes = export(common.store(), episode_ids if tmdb.enabled() else None)
        written = []
        for file_name, text in files.items():
            path = folder.rstrip("/\\") + ("\\" if "\\" in folder else "/") + file_name
            handle = xbmcvfs.File(path, "w")
            try:
                if not handle.write(text.encode("utf-8")):
                    raise IOError("couldn't write {}".format(path))
            finally:
                handle.close()
            written.append("{} ({} rows)".format(file_name, max(text.count("\n") - 1, 0)))
    except Exception as exc:  # noqa: BLE001 - shown to the user, then logged
        progress.close()
        common.log("Export for {} failed: {}".format(name, exc))
        dialog.ok(common.ADDON_NAME, "The export failed: {}".format(exc))
        return
    progress.close()
    message = "Exported for {} to {}:\n{}".format(name, folder, "\n".join(written))
    if notes:
        dialog.textviewer(common.ADDON_NAME, message + "\n\n" + "\n".join(notes))
    else:
        dialog.ok(common.ADDON_NAME, message)
