"""Syncing Tally between devices on the home network, with no server in the middle.

Every device is equal: each keeps the whole database and serves its changes log (store.changes)
over HTTP to the devices it is paired with, and pulls theirs into its own (store.apply_changes).
What it pulls goes into its own log too, so a change reaches devices that were never paired
with the one it was made on, and any device can be lost without losing the others' data.

A Node is this device's side: its HTTP server (PORT), a UDP responder so other devices can find
it (DISCOVERY_PORT, discover), and a thread that pulls from every peer every SYNC_EVERY seconds,
pulls from one at once when it says it has news (poke), and pokes the peers when this device's
log grows. Pairing: one device opens a short code (open_pairing); the other sends it (pair), and
both keep a long random token that every later request carries. Removing a device (unpair)
deletes its token, so its requests are refused.

Plain HTTP on the home network: the token is not encrypted, as Kodi's own web server's isn't.
No Kodi imports, so it runs (and is tested) outside Kodi.
"""

import hmac
import http.server
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse

PORT = 52401
DISCOVERY_PORT = 52402
PROTOCOL = 1
PAIR_SECONDS = 120  # a pairing code is good for this long
PAIR_ATTEMPTS = 5  # wrong codes before it is closed
CODE_DIGITS = 6
PAGE = 500  # changes per request
TIMEOUT = 5
SYNC_EVERY = 300
POKE_DELAY = 3  # a change here is told to the peers after this long (a burst of them once)
WATCH_EVERY = 2  # how often the log is checked for changes made here (or pulled)
DISCOVER_TIMEOUT = 1.5


class SyncError(Exception):
    pass


def _log_nothing(message):
    pass


# -- the client side: used by a Node, and by the plugin to pair or find devices ----------------

def _request(address, port, path, body=None, peer=None, store=None, own_port=0, params=None):
    """A request to another device: GET, or POST with a JSON body. With peer (its store row) it
    carries this device's id and their token. Returns the JSON reply; SyncError when it can't
    be reached or refuses (its .status the HTTP status, 0 unreachable)."""
    url = "http://{}:{}/tally/{}".format(address, port, path)
    if params:
        url += "?" + urlencode(params)
    headers = {"Content-Type": "application/json", "X-Tally-Protocol": str(PROTOCOL)}
    if peer is not None:
        headers.update({"X-Tally-Device": store.device_id, "X-Tally-Port": str(own_port or 0),
                        "Authorization": "Bearer " + peer["token"]})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error") or exc.reason
        except (ValueError, AttributeError):
            message = exc.reason
        error = SyncError(message)
        error.status = exc.code
        raise error
    except (OSError, ValueError) as exc:
        error = SyncError(str(getattr(exc, "reason", exc)))
        error.status = 0
        raise error


def hello(address, port=PORT):
    """Whether a Tally answers there: {device, name, protocol, pairing}."""
    return _request(address, port, "hello")


def discover(port=DISCOVERY_PORT, timeout=DISCOVER_TIMEOUT, targets=("<broadcast>",), own=None):
    """The Tallys with sync on that answer on the network: [{device, name, address, port}]
    (address: where the answer came from). own: this device's id, left out."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.2)
        message = json.dumps({"tally": "discover", "protocol": PROTOCOL}).encode("utf-8")
        for target in targets:
            try:
                sock.sendto(message, (target, port))
            except OSError:
                pass
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (address, _) = sock.recvfrom(2048)
                reply = json.loads(data.decode("utf-8"))
            except socket.timeout:
                continue
            except (OSError, ValueError):
                continue
            if reply.get("tally") == "here" and reply.get("device") and reply["device"] != own:
                found[reply["device"]] = {"device": reply["device"], "name": reply.get("name") or address,
                                          "address": address, "port": int(reply.get("port") or PORT)}
    finally:
        sock.close()
    return sorted(found.values(), key=lambda d: d["name"].lower())


def pair(store, name, own_port, address, port, code):
    """Pair with the device at address:port that shows code. Returns its store row; SyncError
    when it refuses (a wrong or expired code)."""
    reply = _request(address, port, "pair", {"device": store.device_id, "name": name, "port": own_port,
                                             "code": str(code).strip(), "protocol": PROTOCOL})
    if reply.get("device") == store.device_id:
        raise SyncError("That is this device")
    store.save_peer(reply["device"], reply.get("name") or address, reply["token"], address, port)
    return store.peer(reply["device"])


def unpair(store, device, own_port=0):
    """Forget a paired device here, and tell it to forget this one (when it can be reached)."""
    peer = store.peer(device)
    if not peer:
        return False
    try:
        _request(peer["address"], peer["port"], "unpair", {}, peer=peer, store=store, own_port=own_port)
    except SyncError:
        pass
    return store.remove_peer(device)


# -- this device's side ---------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Tally"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except ValueError:
            return {}

    def _peer(self):
        """The paired device making this request, or None (refused) when its token doesn't match."""
        node = self.server.node
        device = self.headers.get("X-Tally-Device") or ""
        token = (self.headers.get("Authorization") or "")[len("Bearer "):]
        peer = node.store.peer(device) if device else None
        if not peer or not token or not hmac.compare_digest(peer["token"].encode(), token.encode()):
            return None
        # Where it is now (a router may give it a new address)
        address, port = self.client_address[0], int(self.headers.get("X-Tally-Port") or 0) or peer["port"]
        if (address, port) != (peer["address"], peer["port"]):
            node.store.update_peer(device, address=address, port=port)
        return peer

    def do_GET(self):
        node = self.server.node
        url = urlparse(self.path)
        if url.path == "/tally/hello":
            return self._reply(200, node.hello())
        if url.path == "/tally/changes":
            peer = self._peer()
            if not peer:
                return self._reply(401, {"error": "Not paired"})
            query = parse_qs(url.query)
            try:
                after = int(query.get("after", ["0"])[0])
                limit = max(1, min(PAGE, int(query.get("limit", [str(PAGE)])[0])))
            except ValueError:
                return self._reply(400, {"error": "Bad request"})
            return self._reply(200, {"device": node.store.device_id, "last": node.store.last_change(),
                                     "changes": node.store.changes(after, limit)})
        self._reply(404, {"error": "Not found"})

    def do_POST(self):
        node = self.server.node
        path = urlparse(self.path).path
        body = self._body()
        if path == "/tally/pair":
            status, reply = node.answer_pairing(body, self.client_address[0])
            return self._reply(status, reply)
        peer = self._peer()
        if not peer:
            return self._reply(401, {"error": "Not paired"})
        if path == "/tally/poke":
            node.want(peer["device"])
            return self._reply(200, {})
        if path == "/tally/unpair":
            node.store.remove_peer(peer["device"])
            node.log("{} removed this device".format(peer["name"]))
            return self._reply(200, {})
        self._reply(404, {"error": "Not found"})


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # HTTPServer's also looks up this machine's full DNS name, which can take seconds and isn't used.
        http.server.socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Node:
    """This device's side of sync (see the module docstring). on_changes(new) is called after
    pulls brought new changes; on_paired(name) when a device pairs with an open code."""

    def __init__(self, store, name, port=PORT, discovery_port=DISCOVERY_PORT, log=None,
                 on_changes=None, on_paired=None, sync_every=SYNC_EVERY, poke_delay=POKE_DELAY,
                 discovery_targets=("<broadcast>",)):
        self.store = store
        self.name = name
        self.port = port
        self.discovery_port = discovery_port
        self.log = log or _log_nothing
        self.on_changes = on_changes
        self.on_paired = on_paired
        self.sync_every = sync_every
        self.poke_delay = poke_delay
        self.discovery_targets = discovery_targets
        self._http = None
        self._udp = None
        self._threads = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._wanted = set()
        self._lock = threading.Lock()
        self._pull_lock = threading.Lock()
        self._pairing = None  # (code, expires, attempts)

    # -- running --------------------------------------------------------------------------

    def start(self):
        """Serve (the HTTP server on port: 0 picks one, then self.port is it) and start syncing.
        A port in use is logged; the node still pulls from its peers."""
        try:
            self._http = _Server(("", self.port), _Handler)
            self._http.node = self
            self.port = self._http.server_address[1]
            self._spawn(lambda: self._http.serve_forever(poll_interval=0.2))
        except OSError as exc:
            self.log("Sync can't listen on port {}: {}".format(self.port, exc))
            self._http = None
        try:
            self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._udp.bind(("", self.discovery_port))
            self._udp.settimeout(0.5)
            self.discovery_port = self._udp.getsockname()[1]
            self._spawn(self._answer_discovery)
        except OSError as exc:
            self.log("Sync can't answer discovery on port {}: {}".format(self.discovery_port, exc))
            self._udp = None
        self._spawn(self._run)

    def stop(self, timeout=3):
        self._stop.set()
        self._wake.set()
        if self._http:
            self._http.shutdown()
            self._http.server_close()
        if self._udp:
            self._udp.close()
        for thread in self._threads:
            thread.join(timeout)

    def _spawn(self, target):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self._threads.append(thread)

    def hello(self):
        return {"tally": True, "device": self.store.device_id, "name": self.name, "protocol": PROTOCOL,
                "port": self.port, "pairing": self._pairing_open()}

    def _answer_discovery(self):
        while not self._stop.is_set():
            try:
                data, sender = self._udp.recvfrom(2048)
                request = json.loads(data.decode("utf-8"))
            except socket.timeout:
                continue
            except (OSError, ValueError):
                if self._stop.is_set():
                    return
                continue
            if request.get("tally") != "discover":
                continue
            reply = {"tally": "here", "device": self.store.device_id, "name": self.name, "port": self.port}
            try:
                self._udp.sendto(json.dumps(reply).encode("utf-8"), sender)
            except OSError:
                pass

    def _run(self):
        """Pull from every peer now and every sync_every seconds, from one at once when it pokes;
        poke the peers once the log grew (a change made here, or pulled: passed on)."""
        seen = self.store.last_change()
        grew_at = None
        next_all = 0
        while not self._stop.is_set():
            now = time.time()
            if now >= next_all:
                next_all = now + self.sync_every
                self.pull_all()
            with self._lock:
                wanted, self._wanted = self._wanted, set()
            for device in wanted:
                self._pull_logged(device)
            last = self.store.last_change()
            if last != seen:
                seen, grew_at = last, grew_at or now
            if grew_at and now - grew_at >= self.poke_delay:
                grew_at = None
                self.poke_all()
            self._wake.wait(WATCH_EVERY)
            self._wake.clear()

    def want(self, device):
        """Pull from a peer soon (it poked: it has news)."""
        with self._lock:
            self._wanted.add(device)
        self._wake.set()

    # -- pairing ----------------------------------------------------------------------------

    def open_pairing(self, seconds=PAIR_SECONDS):
        """Let a device pair with a new code for ``seconds``. Returns the code."""
        code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
        with self._lock:
            self._pairing = [code, time.time() + seconds, 0]
        return code

    def close_pairing(self):
        with self._lock:
            self._pairing = None

    def _pairing_open(self):
        with self._lock:
            return bool(self._pairing and self._pairing[1] > time.time())

    def answer_pairing(self, body, address):
        """A device sent a code (POST /tally/pair): (HTTP status, reply)."""
        with self._lock:
            pairing = self._pairing
            if not pairing or pairing[1] <= time.time():
                self._pairing = None
                return 403, {"error": "Not pairing: open a code on this device first"}
            pairing[2] += 1
            if pairing[2] > PAIR_ATTEMPTS:
                self._pairing = None
                return 403, {"error": "Too many wrong codes: open a new one"}
            right = hmac.compare_digest(pairing[0].encode(), str(body.get("code") or "").encode())
            if right:
                self._pairing = None
        if not right:
            return 403, {"error": "Wrong code"}
        device = str(body.get("device") or "")
        if not device or device == self.store.device_id:
            return 400, {"error": "Bad request"}
        token = secrets.token_hex(32)
        name = str(body.get("name") or address)
        self.store.save_peer(device, name, token, address, int(body.get("port") or PORT))
        self.log("Paired with {}".format(name))
        if self.on_paired:
            self.on_paired(name)
        self._wake.set()
        return 200, {"device": self.store.device_id, "name": self.name, "token": token, "port": self.port}

    # -- pulling --------------------------------------------------------------------------------

    def pull_all(self):
        for peer in self.store.peers():
            if self._stop.is_set():
                return
            self._pull_logged(peer["device"])

    def _pull_logged(self, device):
        try:
            return self.pull(device)
        except SyncError as exc:
            self.log("Sync with {} failed: {}".format(device, exc))
            return 0

    def pull(self, device):
        """Pull a peer's new changes; returns how many were new here. Unreachable at its last
        address, it is looked for on the network once (its address may have changed)."""
        with self._pull_lock:
            peer = self.store.peer(device)
            if not peer:
                return 0
            try:
                new = self._pull(peer)
            except SyncError as exc:
                if exc.status == 401:
                    self.store.update_peer(device, error="Not paired any more")
                    raise
                found = next((d for d in discover(self.discovery_port, targets=self.discovery_targets,
                                                  own=self.store.device_id) if d["device"] == device), None)
                if not found or (found["address"], found["port"]) == (peer["address"], peer["port"]):
                    self.store.update_peer(device, error=str(exc))
                    raise
                self.store.update_peer(device, address=found["address"], port=found["port"])
                new = self._pull(self.store.peer(device))
            self.store.update_peer(device, synced_at=time.time(), error="")
        if new:
            self.log("Got {} changes from {}".format(new, peer["name"]))
            if self.on_changes:
                self.on_changes(new)
        return new

    def _pull(self, peer):
        new, cursor = 0, peer["cursor"]
        while not self._stop.is_set():
            reply = _request(peer["address"], peer["port"], "changes", peer=peer, store=self.store,
                             own_port=self.port, params={"after": cursor, "limit": PAGE})
            if reply.get("device") != peer["device"]:
                error = SyncError("Another device answers at {}".format(peer["address"]))
                error.status = 0
                raise error
            changes = reply.get("changes") or []
            if changes:
                new += self.store.apply_changes(changes)
                cursor = changes[-1]["seq"]
                self.store.update_peer(peer["device"], cursor=cursor)
            if len(changes) < PAGE:
                break
        return new

    def poke_all(self):
        """Tell every peer this device has news (it pulls)."""
        for peer in self.store.peers():
            try:
                _request(peer["address"], peer["port"], "poke", {}, peer=peer, store=self.store, own_port=self.port)
            except SyncError:
                pass
