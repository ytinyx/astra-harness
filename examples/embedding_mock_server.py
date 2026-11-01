"""Trusted loopback protocol fixture with manually assigned vectors.

This is not an embedding model and demonstrates no semantic understanding.
Only use newly created synthetic text. Neither authorization values nor real
provider credentials are recorded. Nothing is downloaded or proxied.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading


class MockEmbeddingServer:
    def __init__(self, *, mode="normal", vectors_by_text=None, dimension=3,
                 expected_key="mock-embedding-key"):
        self.mode = mode
        self.vectors_by_text = dict(vectors_by_text or {})
        self.dimension = dimension
        self._expected_key = expected_key
        self.requests = []
        self.paths = []
        self.received = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections = set()
        self.closed = False
        self.server = None
        self.thread = None

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(3)
                with owner._lock:
                    owner._connections.add(self.connection)

            def finish(self):
                try:
                    super().finish()
                finally:
                    with owner._lock:
                        owner._connections.discard(self.connection)

            def send(self, status, payload, *, headers=None):
                raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()

            def do_POST(self):
                try:
                    self.handle_post()
                except (OSError, ValueError, TypeError, UnicodeError):
                    self.close_connection = True

            def handle_post(self):
                with owner._lock:
                    owner.paths.append(self.path)
                # The fixture exposes only one local endpoint; a hostile web
                # Origin cannot use it as a general local proxy.
                expected_host = "127.0.0.1:" + str(owner.server.server_port)
                if (self.path != "/v1/embeddings" or self.headers.get("Host") != expected_host
                        or self.headers.get("Origin") not in (None, "http://" + expected_host)):
                    self.send(403, {"error": "fixture_origin_or_path"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    self.send(413, {"error": "fixture_input_limit"})
                    return
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if type(body) is not dict or type(body.get("input")) is not list:
                    self.send(400, {"error": "fixture_input"})
                    return
                inputs = body["input"]
                if (not 1 <= len(inputs) <= 8 or any(not isinstance(x, str) or len(x) > 1000 for x in inputs)):
                    self.send(400, {"error": "fixture_input"})
                    return
                auth_ok = self.headers.get("Authorization") == "Bearer " + owner._expected_key
                with owner._lock:
                    owner.requests.append({"input": list(inputs), "model": body.get("model"),
                                           "dimensions": body.get("dimensions"),
                                           "encoding_format": body.get("encoding_format"),
                                           "auth_ok": auth_ok, "cookie_present": "Cookie" in self.headers})
                    number = len(owner.requests)
                owner.received.set()
                mode = owner.mode
                if not auth_ok or mode == "auth_error":
                    self.send(401, {"error": "synthetic private error body"})
                    return
                if mode == "rate_limit":
                    self.send(429, {"error": "synthetic private error body"})
                    return
                if mode == "error" or (mode == "error_after_first" and number > 1):
                    self.send(500, {"error": "synthetic private error body"})
                    return
                if mode == "redirect":
                    self.send(307, {}, headers={"Location": owner.url + "/redirect-trap"})
                    return
                if mode == "delayed" and owner._stop.wait(.4):
                    return
                if mode == "slow":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for _ in range(200):
                        if owner._stop.is_set():
                            return
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        if owner._stop.wait(.025):
                            return
                    self.close_connection = True
                    return
                if mode in {"oversized", "oversized_stream"}:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    if mode == "oversized":
                        self.send_header("Content-Length", str(4 * 1024 * 1024 + 1))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for _ in range(65):
                        if owner._stop.is_set():
                            return
                        self.wfile.write(b" " * 65536)
                        self.wfile.flush()
                    self.close_connection = True
                    return
                if mode == "invalid_json":
                    self.send(200, b'{"private":"fixture-secret"')
                    return
                dimension = owner.dimension + (1 if mode == "wrong_dimension" else 0)
                default = ([0.0] * (dimension - 1)) + [1.0]
                rows = []
                for i, text in enumerate(inputs):
                    vector = list(owner.vectors_by_text.get(text, default))
                    if mode == "zero":
                        vector = [0.0] * dimension
                    elif mode == "nonfinite":
                        vector = [float("nan")] + [0.0] * (dimension - 1)
                    rows.append({"object": "embedding", "index": i, "embedding": vector})
                if mode == "reverse":
                    rows.reverse()
                elif mode == "missing":
                    rows = rows[:-1]
                elif mode == "duplicate" and len(rows) > 1:
                    rows[-1]["index"] = rows[0]["index"]
                payload = {"object": "list", "model": "wrong-model" if mode == "model_mismatch" else body.get("model"),
                           "data": rows, "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)}}
                # These synthetic usage counts are not tokenizer measurements.
                headers = {"Set-Cookie": "fixture-cookie=never-forward"}
                if mode == "gzip":
                    headers["Content-Encoding"] = "gzip"
                self.send(200, payload, headers=headers)

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def handle_error(self, request, client_address):
                # Expected peer cancellation is not a console traceback.
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1/embeddings"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        return self

    def close(self):
        if self.closed:
            return
        self._stop.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        with self._lock:
            sockets = tuple(self._connections)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(2)
            if self.thread.is_alive():
                raise RuntimeError("fixture did not close")
        self.closed = True

    def __exit__(self, *args):
        self.close()
