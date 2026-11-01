"""Owned MCP fixtures: synthetic receipts, no files, model APIs or credentials."""
import argparse
import json
import socket
import sys
import threading


class NoModel:
    def complete(self, request):
        raise AssertionError("MCP profile demonstration must not call a model")


class DiscoveryFixture:
    """Real loopback protocol; a held initialize response observes cancellation."""
    def __init__(self, mode="json"):
        from .mcp_http_server import LocalMCPHTTPServer
        self.server = LocalMCPHTTPServer(mode=mode)
        self.initializing = threading.Event()
        self.disconnected = threading.Event()
        self.stop = threading.Event()
        self.hold_initialize = False
        owner, original = self, self.server._server.RequestHandlerClass

        class Handler(original):
            def do_POST(self):
                if not owner.hold_initialize:
                    return super().do_POST()
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 1_000_000 or not self.valid():
                    self.send(400)
                    return
                message = json.loads(self.rfile.read(size))
                with owner.server._guard:
                    owner.server.requests.append({"method": message.get("method"), "id": message.get("id")})
                owner.initializing.set()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "10000")
                self.end_headers()
                self.wfile.flush()
                self.connection.settimeout(.1)
                while not owner.stop.is_set():
                    try:
                        if not self.connection.recv(1):
                            owner.disconnected.set()
                            break
                    except socket.timeout:
                        continue
                    except OSError:
                        owner.disconnected.set()
                        break
                self.close_connection = True

        self.server._server.RequestHandlerClass = Handler

    @property
    def url(self): return self.server.url
    @property
    def requests(self): return self.server.requests
    @property
    def receipts(self): return self.server.receipts
    @property
    def closed(self): return self.server.closed

    def __enter__(self):
        self.server.__enter__()
        return self

    def close(self):
        self.stop.set()
        self.server.close()

    def __exit__(self, *args): self.close()


def stdio_main():
    """A standalone -I -S fixture: only JSON-RPC on stdin/stdout."""
    sequence = 0
    for raw in sys.stdin.buffer:
        message = json.loads(raw)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "synthetic-profile-fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "record", "description": "Record a synthetic integer",
                "inputSchema": {"type": "object", "properties": {"value": {"type": "integer"}},
                                "required": ["value"], "additionalProperties": False}}]}
        elif method == "tools/call":
            sequence += 1
            result = {"content": [{"type": "text", "text": "Synthetic receipt"}],
                      "structuredContent": {"value": message["params"]["arguments"]["value"], "sequence": sequence}}
        elif method == "ping":
            result = {}
        else:
            result = None
        response = {"jsonrpc": "2.0", "id": message["id"]}
        response.update({"result": result} if result is not None else {"error": {"code": -32601, "message": "unsupported"}})
        sys.stdout.buffer.write(json.dumps(response).encode() + b"\n")
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdio", action="store_true", required=True)
    parser.parse_args()
    stdio_main()
