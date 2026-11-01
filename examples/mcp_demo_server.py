"""Self-authored synthetic STDIO fixture for the multi-service teaching example.

The host supplies the ledger path. Model arguments cannot choose files. This is
trusted local code implementing a small 2025-06-18 subset, not a general server.
"""
import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4


PROTOCOL = "2025-06-18"


def serve(service, ledger, *, bad_initialize=False):
    instance = uuid4().hex

    def record(event, **fields):
        with ledger.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"event": event, "service": service,
                                     "instance": instance, **fields}, ensure_ascii=False) + "\n")

    def send(identifier, result):
        sys.stdout.buffer.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result},
                                           ensure_ascii=False).encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()

    record("started")
    try:
        for raw in sys.stdin.buffer:
            request = json.loads(raw)
            method, identifier = request.get("method"), request.get("id")
            if identifier is None:  # initialized/cancelled notifications have no response.
                continue
            if method == "initialize":
                send(identifier, {"protocolVersion": "unsupported" if bad_initialize else PROTOCOL,
                                  "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "astra-synthetic-service", "version": "1"}})
            elif method == "tools/list":
                send(identifier, {"tools": [{"name": "record", "description": "Record one synthetic quantity.",
                    "inputSchema": {"type": "object", "properties": {"value": {
                        "type": "integer", "minimum": 0, "maximum": 100}},
                        "required": ["value"], "additionalProperties": False}}]})
            elif method == "tools/call":
                params = request.get("params", {})
                arguments = params.get("arguments", {})
                if (params.get("name") != "record" or type(arguments) is not dict
                        or set(arguments) != {"value"} or type(arguments["value"]) is not int
                        or not 0 <= arguments["value"] <= 100):
                    send(identifier, {"isError": True, "content": [{"type": "text", "text": "invalid quantity"}]})
                    continue
                previous = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
                receipt = 1 + sum(item["event"] == "recorded" for item in previous)
                data = {"service": service, "value": arguments["value"], "receipt": receipt}
                # This independently persisted business receipt precedes the
                # response. It can reveal an accidental replay after reopening.
                record("recorded", **{"value": data["value"], "receipt": receipt})
                send(identifier, {"content": [{"type": "text", "text": json.dumps(data)}],
                                  "structuredContent": data, "isError": False})
            else:
                sys.stdout.buffer.write(json.dumps({"jsonrpc": "2.0", "id": identifier,
                    "error": {"code": -32601, "message": "Unsupported fixture method"}}).encode() + b"\n")
                sys.stdout.buffer.flush()
    finally:
        record("stdin_closed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("alpha", "beta", "broken"), required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--bad-initialize", action="store_true")
    args = parser.parse_args(argv)
    serve(args.service, args.ledger, bad_initialize=args.bad_initialize)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
