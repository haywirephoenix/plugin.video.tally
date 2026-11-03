"""Tests for syncing over the network: two or three nodes on this machine, real sockets (no Kodi).

python -m unittest discover tests
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from resources.lib import sync  # noqa: E402
from resources.lib.store import Store  # noqa: E402

LOCAL = ("127.0.0.1",)


class NetTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.nodes = []
        self.pc, self.tv = self.node("PC"), self.node("TV")

    def tearDown(self):
        for node in self.nodes:
            node.stop()
            node.store.close()
        self.dir.cleanup()

    def node(self, name, **kwargs):
        store = Store(os.path.join(self.dir.name, name + ".db"))
        kwargs.setdefault("sync_every", 3600)
        node = sync.Node(store, name, port=0, discovery_port=0, discovery_targets=LOCAL, **kwargs)
        node.start()
        self.nodes.append(node)
        return node

    def pair(self, a, b):
        """b pairs with a, using the code a shows."""
        code = a.open_pairing()
        return sync.pair(b.store, b.name, b.port, "127.0.0.1", a.port, code)

    @staticmethod
    def wait(condition, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if condition():
                return True
            time.sleep(0.05)
        return False


class PairingTest(NetTestCase):
    def test_hello_and_discover(self):
        self.assertEqual(sync.hello("127.0.0.1", self.pc.port)["name"], "PC")
        found = sync.discover(self.pc.discovery_port, timeout=0.5, targets=LOCAL)
        self.assertEqual([(d["name"], d["port"]) for d in found], [("PC", self.pc.port)])
        self.assertEqual(sync.discover(self.pc.discovery_port, timeout=0.5, targets=LOCAL,
                                       own=self.pc.store.device_id), [])

    def test_pair_and_sync_both_ways(self):
        paired = []
        self.pc.on_paired = paired.append
        peer = self.pair(self.pc, self.tv)
        self.assertEqual((peer["name"], peer["device"]), ("PC", self.pc.store.device_id))
        mine = self.pc.store.peer(self.tv.store.device_id)
        self.assertEqual((mine["name"], mine["token"], paired), ("TV", peer["token"], ["TV"]))
        self.assertFalse(self.pc.hello()["pairing"])  # the code is used up

        self.pc.store.add_play("movie", "603", when=1000)
        self.tv.store.add_watchlist("tv", "1396", {"title": "Breaking Bad"})
        self.assertEqual(self.tv.pull(self.pc.store.device_id), 1)
        self.assertEqual(self.pc.pull(self.tv.store.device_id), 1)
        self.assertEqual(self.tv.store.playcount("movie", "603"), 1)
        self.assertEqual(self.pc.store.get("tv", "1396")["title"], "Breaking Bad")
        self.tv.pull(self.pc.store.device_id)  # its own watchlist add, passed back: not logged twice
        self.assertEqual([c["op"] for c in self.tv.store.changes()], ["title", "play"])
        self.assertTrue(self.tv.store.peer(self.pc.store.device_id)["synced_at"])

    def test_wrong_codes(self):
        with self.assertRaises(sync.SyncError):  # no code open
            sync.pair(self.tv.store, "TV", self.tv.port, "127.0.0.1", self.pc.port, "123456")
        code = self.pc.open_pairing()
        for _ in range(sync.PAIR_ATTEMPTS):
            with self.assertRaisesRegex(sync.SyncError, "Wrong code"):
                sync.pair(self.tv.store, "TV", self.tv.port, "127.0.0.1", self.pc.port, "x")
        with self.assertRaisesRegex(sync.SyncError, "Too many"):  # closed, even with the right one now
            sync.pair(self.tv.store, "TV", self.tv.port, "127.0.0.1", self.pc.port, code)
        self.assertEqual((self.pc.store.peers(), self.tv.store.peers()), ([], []))

    def test_code_expires(self):
        code = self.pc.open_pairing(seconds=0)
        with self.assertRaisesRegex(sync.SyncError, "Not pairing"):
            sync.pair(self.tv.store, "TV", self.tv.port, "127.0.0.1", self.pc.port, code)

    def test_refused_without_token(self):
        self.pair(self.pc, self.tv)
        stranger = self.node("Stranger")
        fake = dict(self.tv.store.peer(self.pc.store.device_id), token="0" * 64)
        with self.assertRaises(sync.SyncError) as caught:
            sync._request("127.0.0.1", self.pc.port, "changes", peer=fake, store=stranger.store)
        self.assertEqual(caught.exception.status, 401)

    def test_unpair(self):
        self.pair(self.pc, self.tv)
        self.assertTrue(sync.unpair(self.tv.store, self.pc.store.device_id))
        self.assertEqual((self.pc.store.peers(), self.tv.store.peers()), ([], []))

    def test_removed_peer_refused(self):
        self.pair(self.pc, self.tv)
        self.pc.store.remove_peer(self.tv.store.device_id)  # removed while the TV was off
        with self.assertRaises(sync.SyncError):
            self.tv.pull(self.pc.store.device_id)
        self.assertEqual(self.tv.store.peer(self.pc.store.device_id)["error"], "Not paired any more")


class SyncingTest(NetTestCase):
    def test_pages(self):
        self.pair(self.pc, self.tv)
        self.pc.store.add_plays("1396", [(1, e) for e in range(1, 8)])
        with mock.patch.object(sync, "PAGE", 3):
            self.assertEqual(self.tv.pull(self.pc.store.device_id), 7)
        self.assertEqual(self.tv.store.episode_playcounts("1396"), {(1, e): 1 for e in range(1, 8)})

    def test_change_reaches_peers_soon(self):
        with mock.patch.object(sync, "WATCH_EVERY", 0.1):
            phone = self.node("Phone", poke_delay=0)
            got = []
            self.tv.on_changes = got.append
            self.pair(self.pc, self.tv)
            self.pair(self.tv, phone)  # the phone and PC never pair
            for node in self.nodes:
                node._wake.set()
            time.sleep(0.3)
            phone.store.add_play("movie", "603", when=1000)
            self.assertTrue(self.wait(lambda: self.pc.store.playcount("movie", "603") == 1))
            self.assertTrue(got)

    def test_moved_address_found(self):
        self.pair(self.pc, self.tv)
        self.pc.store.add_play("movie", "603", when=1000)
        device = self.pc.store.device_id
        self.tv.store.update_peer(device, port=1)  # where the PC was
        self.tv.discovery_port = self.pc.discovery_port  # on a network, every node's is the same
        self.assertEqual(self.tv.pull(device), 1)
        self.assertEqual(self.tv.store.peer(device)["port"], self.pc.port)

    def test_peer_address_kept_current(self):
        self.pair(self.pc, self.tv)
        self.pc.store.update_peer(self.tv.store.device_id, address="10.0.0.9", port=1)
        self.tv.pull(self.pc.store.device_id)  # the PC sees where the TV asks from
        peer = self.pc.store.peer(self.tv.store.device_id)
        self.assertEqual((peer["address"], peer["port"]), ("127.0.0.1", self.tv.port))


if __name__ == "__main__":
    unittest.main()
