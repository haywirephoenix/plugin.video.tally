"""Tests for the Sync settings' dialogs, with Kodi's dialogs stubbed and a real node to pair with.

python -m unittest discover tests
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for name in ("xbmc", "xbmcaddon", "xbmcgui", "xbmcplugin", "xbmcvfs"):
    sys.modules.setdefault(name, mock.MagicMock())

from resources.lib import common, sync, sync_ui  # noqa: E402
from resources.lib.store import Store  # noqa: E402


class SyncUITestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Store(os.path.join(self.dir.name, "tv.db"))
        self.pc = sync.Node(Store(os.path.join(self.dir.name, "pc.db")), "PC", port=0, discovery_port=0,
                            sync_every=3600)
        self.pc.start()
        self.props, self.notes = {}, []
        self.dialog = mock.MagicMock()
        self.kodi = mock.MagicMock()
        self.kodi.Monitor.return_value.abortRequested.return_value = False
        self.kodi.Monitor.return_value.waitForAbort.return_value = False
        self.kodi.getInfoLabel.return_value = "192.168.1.5"
        self.gui = mock.MagicMock()
        self.gui.Dialog.return_value = self.dialog

        def prop(name, value=None):
            if value is None:
                return self.props.get(name, "")
            self.props[name] = value
            return value

        for patch in (mock.patch.object(sync_ui, "xbmc", self.kodi), mock.patch.object(sync_ui, "xbmcgui", self.gui),
                      mock.patch.object(common, "store", lambda: self.db),
                      mock.patch.object(common, "setting_bool", lambda key, default=False: True),
                      mock.patch.object(common, "setting_int", lambda key, default=0: default),
                      mock.patch.object(common, "sync_name", lambda: "TV"),
                      mock.patch.object(common, "home_property", prop),
                      mock.patch.object(common, "clear_home_property", lambda name: self.props.pop(name, None)),
                      mock.patch.object(common, "notify", self.notes.append)):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.pc.stop()
        self.pc.store.close()
        self.db.close()
        self.dir.cleanup()

    def found(self):
        return [{"device": self.pc.store.device_id, "name": "PC", "address": "127.0.0.1", "port": self.pc.port}]


class EnterCodeTest(SyncUITestCase):
    def test_pick_found_device(self):
        self.dialog.select.side_effect = [1, 0]  # Enter another device's code; the PC
        self.dialog.numeric.return_value = self.pc.open_pairing()
        with mock.patch.object(sync, "discover", lambda **kw: self.found()):
            sync_ui.pair(None)
        self.assertEqual([p["name"] for p in self.db.peers()], ["PC"])
        self.assertEqual([p["name"] for p in self.pc.store.peers()], ["TV"])
        self.assertEqual((self.notes, self.props[common.PROP_SYNC_REQUEST]), (["Paired with PC"], "now"))

    def test_typed_address(self):
        self.dialog.select.side_effect = [1, 0]  # nothing found: Enter an address
        self.dialog.input.return_value = "127.0.0.1:{}".format(self.pc.port)
        self.dialog.numeric.return_value = self.pc.open_pairing()
        with mock.patch.object(sync, "discover", lambda **kw: []):
            sync_ui.pair(None)
        self.assertEqual(self.db.peer(self.pc.store.device_id)["port"], self.pc.port)

    def test_wrong_code(self):
        self.pc.open_pairing()
        self.dialog.select.side_effect = [1, 0]
        self.dialog.numeric.return_value = "000000x"
        with mock.patch.object(sync, "discover", lambda **kw: self.found()):
            sync_ui.pair(None)
        self.assertEqual(self.db.peers(), [])
        self.assertIn("Wrong code", self.dialog.ok.call_args[0][1])


class ShowCodeTest(SyncUITestCase):
    def test_paired_while_shown(self):
        def prop(name, value=None):  # the service: a code when asked; the TV pairs as it shows
            if value is None:
                return self.props.get(name, "")
            self.props[name] = value
            if name == common.PROP_SYNC_REQUEST and value == "pair":
                self.props[common.PROP_SYNC_CODE] = "123456"
            return value

        progress = self.gui.DialogProgress.return_value
        progress.iscanceled.return_value = False
        progress.update.side_effect = lambda *a: self.props.__setitem__(common.PROP_SYNC_PAIRED, "Phone")
        self.dialog.select.return_value = 0
        with mock.patch.object(common, "home_property", prop):
            sync_ui.pair(None)
        shown = progress.create.call_args[0][1]
        self.assertIn("123 456", shown)
        self.assertIn("192.168.1.5", shown)
        self.assertEqual(self.notes, ["Paired with Phone"])

    def test_cancelled(self):
        self.props[common.PROP_SYNC_CODE] = "123456"
        with mock.patch.object(sync_ui, "_ask_service", lambda *a: "123456"):
            progress = self.gui.DialogProgress.return_value
            progress.iscanceled.return_value = True
            self.dialog.select.return_value = 0
            sync_ui.pair(None)
        self.assertEqual(self.props[common.PROP_SYNC_REQUEST], "unpair_code")
        self.dialog.ok.assert_not_called()


class DevicesTest(SyncUITestCase):
    def test_remove(self):
        sync.pair(self.db, "TV", 0, "127.0.0.1", self.pc.port, self.pc.open_pairing())
        self.dialog.select.side_effect = [0, 1]  # the PC; Remove
        self.dialog.yesno.return_value = True
        sync_ui.devices(None)
        self.assertEqual((self.db.peers(), self.pc.store.peers()), ([], []))
        self.assertEqual(self.notes, ["Removed PC"])

    def test_ago(self):
        now = time.time()
        self.assertEqual(sync_ui._ago(0), "never synced")
        self.assertEqual(sync_ui._ago(now - 30), "synced just now")
        self.assertEqual(sync_ui._ago(now - 600), "synced 10 min ago")
        self.assertEqual(sync_ui._ago(now - 7300), "synced 2 hours ago")
        self.assertEqual(sync_ui._ago(now - 86400), "synced 1 day ago")


if __name__ == "__main__":
    unittest.main()
