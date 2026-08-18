"""Real HTTP/HTTPS servers standing in for Prometheus and the kube-apiserver.

Deliberately not mocks of the script's own functions: the script is exercised
through actual sockets, urllib, TLS verification against a CA file, and real
JSON decoding, so the harness proves the runtime path rather than the shape of
a stub. Fault injection is done by making the servers behave badly, which is
what the real ones would do.
"""
import copy
import http.server
import json
import ssl
import threading
import uuid


class PromHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        status, body = self.server.scenario(path, self.path)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeProm:
    """Serves /api/v1/query and /api/v1/query_range from an injectable callable."""

    def __init__(self, scenario):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PromHandler)
        self.server.scenario = scenario
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class K8sHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _name(self):
        return self.path.rstrip("/").rsplit("/", 1)[-1]

    def do_GET(self):  # noqa: N802
        state = self.server.state
        state.gets += 1
        name = self._name()
        # Snapshot BEFORE the hook runs: the point of on_get is to model a
        # competing writer that commits between our read and our write, so the
        # reader must be handed the pre-mutation object (and therefore a now-
        # stale resourceVersion). Firing the hook first would hand us the
        # rival's own version and no conflict could ever occur - which is
        # exactly the harness defect this comment exists to prevent.
        snapshot = copy.deepcopy(state.configmaps.get(name))
        if state.on_get:
            state.on_get(state)
        if snapshot is None:
            return self._reply(404, {"kind": "Status", "reason": "NotFound"})
        return self._reply(200, snapshot)

    def do_POST(self):  # noqa: N802
        state = self.server.state
        state.posts += 1
        body = self._read_body()
        name = body["metadata"]["name"]
        if state.post_status is not None:
            return self._reply(state.post_status, {"kind": "Status", "reason": "Injected"})
        if name in state.configmaps:
            return self._reply(409, {"kind": "Status", "reason": "AlreadyExists"})
        body["metadata"]["resourceVersion"] = uuid.uuid4().hex[:8]
        state.configmaps[name] = body
        return self._reply(201, body)

    def do_PUT(self):  # noqa: N802
        state = self.server.state
        state.puts += 1
        if state.put_status is not None:
            return self._reply(state.put_status, {"kind": "Status", "reason": "Injected"})
        body = self._read_body()
        name = self._name()
        current = state.configmaps.get(name)
        if current is None:
            return self._reply(404, {"kind": "Status", "reason": "NotFound"})
        sent = body["metadata"].get("resourceVersion")
        if sent != current["metadata"]["resourceVersion"]:
            return self._reply(409, {"kind": "Status", "reason": "Conflict"})
        body["metadata"]["resourceVersion"] = uuid.uuid4().hex[:8]
        state.configmaps[name] = body
        return self._reply(200, body)


class K8sState:
    def __init__(self):
        self.configmaps = {}
        self.gets = 0
        self.posts = 0
        self.puts = 0
        self.put_status = None
        self.post_status = None
        # Hook fired on every GET - used to simulate a competing writer that
        # bumps resourceVersion between our read and our write.
        self.on_get = None


class FakeK8s:
    """HTTPS apiserver stand-in with real TLS, verified against a real CA file."""

    def __init__(self, certfile, keyfile):
        self.state = K8sState()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), K8sHandler)
        self.server.state = self.state
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"https://localhost:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
