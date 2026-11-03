"""Tests for the service's start after an update: python -m unittest discover tests"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for name in ("xbmc", "xbmcaddon", "xbmcgui", "xbmcplugin", "xbmcvfs"):
    sys.modules.setdefault(name, mock.MagicMock())

from resources.lib import common, player_service  # noqa: E402


class UpdatedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.props, self.reloads = {}, []
        self.version = "0.1.10"
        for patch in (mock.patch.object(common, "ensure_profile_dir", lambda: self.dir.name),
                      mock.patch.object(common.ADDON, "getAddonInfo", lambda key: self.version),
                      mock.patch.object(common, "home_property", lambda name, value=None: self.props.__setitem__(name, value)),
                      mock.patch.object(common, "bump_widget_reload", lambda: self.reloads.append(1)),
                      mock.patch.object(common, "log", lambda *a, **k: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_once_per_version(self):
        player_service.updated()  # first run (no version saved): an update
        self.assertEqual(self.props, {player_service.PROP_REFRESH_SHOWS: "1"})
        self.assertEqual(len(self.reloads), 1)
        self.props.clear()
        player_service.updated()  # same version again: nothing
        self.assertEqual((self.props, len(self.reloads)), ({}, 1))
        self.version = "0.1.11"
        player_service.updated()  # the next update
        self.assertEqual(len(self.reloads), 2)


if __name__ == "__main__":
    unittest.main()
