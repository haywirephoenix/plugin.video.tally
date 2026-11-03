"""The Sync settings' routes: pair a device, the paired devices, sync now.

The service runs this device's side of sync (player_service.SyncRunner); these dialogs ask it
for a pairing code and to sync through Home window properties, and pair with or remove another
device directly (sync.pair, sync.unpair).
"""

import time

import xbmc
import xbmcgui

from . import common, sync

SERVICE_WAIT = 10  # seconds for the service to answer (it looks at the settings every 5)


def _port():
    return common.setting_int("sync_port", sync.PORT)


def _ago(when):
    if not when:
        return "never synced"
    seconds = max(0, time.time() - when)
    if seconds < 90:
        return "synced just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "min")):
        if seconds >= size:
            count = int(seconds // size)
            return "synced {} {}{} ago".format(count, unit, "s" if count > 1 and unit != "min" else "")
    return "synced just now"


def _sync_on(dialog):
    """True when Sync is on; offers to turn it on (the Sync setting) when it is off."""
    if common.setting_bool("sync_enabled"):
        return True
    if not dialog.yesno(common.ADDON_NAME, "Sync is off. Turn it on?"):
        return False
    common.addon().setSetting("sync_enabled", "true")
    return True


def _ask_service(request, answer, timeout=SERVICE_WAIT):
    """Ask the service (common.PROP_SYNC_REQUEST) and wait for the property ``answer``; its
    value, or "" when the service doesn't answer in time."""
    common.clear_home_property(answer)
    common.home_property(common.PROP_SYNC_REQUEST, request)
    monitor = xbmc.Monitor()
    end = time.time() + timeout
    while time.time() < end and not monitor.abortRequested():
        value = common.home_property(answer)
        if value:
            return value
        monitor.waitForAbort(0.25)
    return ""


def pair(router):
    dialog = xbmcgui.Dialog()
    if not _sync_on(dialog):
        return
    choice = dialog.select("Pair a device", ["Show a code on this device", "Enter another device's code"])
    if choice == 0:
        _show_code(dialog)
    elif choice == 1:
        _enter_code(dialog)


def _show_code(dialog):
    code = _ask_service("pair", common.PROP_SYNC_CODE)
    if not code:
        dialog.ok(common.ADDON_NAME, "Tally's service didn't answer. Try again in a moment.")
        return
    common.clear_home_property(common.PROP_SYNC_PAIRED)
    address = xbmc.getInfoLabel("Network.IPAddress")
    text = ("On the other device, choose Pair a device, then Enter another device's code, and pick "
            "[B]{}[/B]{}.[CR][CR]Code: [B]{} {}[/B]").format(
        common.sync_name(), " ({})".format(address) if address else "", code[:3], code[3:])
    progress = xbmcgui.DialogProgress()
    progress.create("Pair a device", text)
    monitor = xbmc.Monitor()
    end = time.time() + sync.PAIR_SECONDS
    paired, cancelled = "", False
    try:
        while not monitor.abortRequested():
            cancelled = progress.iscanceled()
            if cancelled:
                break
            paired = common.home_property(common.PROP_SYNC_PAIRED)
            left = end - time.time()
            if paired or left <= 0 or common.home_property(common.PROP_SYNC_CODE) != code:
                break
            progress.update(int(100 * left / sync.PAIR_SECONDS), text)
            monitor.waitForAbort(0.5)
    finally:
        progress.close()
    if paired:
        common.clear_home_property(common.PROP_SYNC_PAIRED)
        common.notify("Paired with {}".format(paired))
        return
    common.home_property(common.PROP_SYNC_REQUEST, "unpair_code")  # cancelled, or ran out
    if not cancelled:
        dialog.ok("Pair a device", "The code ran out. Choose Pair a device to show a new one.")


def _enter_code(dialog):
    store = common.store()
    busy = xbmcgui.DialogProgressBG()
    busy.create(common.ADDON_NAME, "Looking for devices with Sync on")
    try:
        found = sync.discover(own=store.device_id)
    finally:
        busy.close()
    labels = ["{} ({})".format(d["name"], d["address"]) for d in found] + ["Enter an address"]
    picked = dialog.select("Pair with", labels)
    if picked < 0:
        return
    if picked < len(found):
        device = found[picked]
        address, port, name = device["address"], device["port"], device["name"]
    else:
        typed = dialog.input("The other device's address (shown with its code)").strip()
        if not typed:
            return
        address, _, port = typed.partition(":")
        port = int(port) if port.isdigit() else sync.PORT
        try:
            name = sync.hello(address, port)["name"]
        except (sync.SyncError, KeyError) as exc:
            dialog.ok("Pair a device", "No Tally with Sync on answers at {}: {}".format(typed, exc))
            return
    code = dialog.numeric(0, "The code {} shows".format(name)).strip()
    if not code:
        return
    try:
        peer = sync.pair(store, common.sync_name(), _port(), address, port, code)
    except sync.SyncError as exc:
        dialog.ok("Pair a device", "{} didn't pair: {}".format(name, exc))
        return
    common.home_property(common.PROP_SYNC_REQUEST, "now")  # get its history now
    common.notify("Paired with {}".format(peer["name"]))


def devices(router):
    dialog = xbmcgui.Dialog()
    store = common.store()
    peers = store.peers()
    if not peers:
        dialog.ok("Paired devices", "No devices are paired with this one. Choose Pair a device to pair one.")
        return
    labels = ["{}: {}{}".format(p["name"], _ago(p["synced_at"]), " ({})".format(p["error"]) if p["error"] else "")
              for p in peers]
    picked = dialog.select("Paired devices", labels)
    if picked < 0:
        return
    peer = peers[picked]
    action = dialog.select(peer["name"], ["Sync now", "Remove"])
    if action == 0:
        now(router)
    elif action == 1 and dialog.yesno(
            "Remove {}".format(peer["name"]),
            "It won't sync with this device again until it is paired again. What it already has stays on it."):
        sync.unpair(store, peer["device"], _port())
        common.notify("Removed {}".format(peer["name"]))


def now(router):
    if not common.setting_bool("sync_enabled"):
        common.notify("Sync is off")
        return
    common.home_property(common.PROP_SYNC_REQUEST, "now")
    common.notify("Syncing with the paired devices")
