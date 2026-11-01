"""Trusted STDIO fixture: synthetic artifacts only in the explicit test directory."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


MODES = ("normal", "initfailure", "badframe", "oversized", "partial", "hang",
         "writeexit", "disconnect", "stalledstdin", "listfailure", "orphanstdout", "holdstdout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=MODES)
    options = parser.parse_args()
    if not options.directory.is_absolute():
        parser.error("--directory must be an absolute test directory")
    options.directory.mkdir(parents=True, exist_ok=True)

    def mark(name, value):
        # A reader sees either no marker or its complete JSON, never a half-write.
        target = options.directory / name
        temporary = target.with_suffix(target.suffix + ".pending")
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(target)

    def emit(value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()

    if options.mode == "holdstdout":
        # Hold only the inherited output pipe. The owning test uses a file
        # signal, never a reported PID, to release this deliberately orphaned child.
        mark("orphan-ready.json", {"pid": os.getpid()})
        deadline = time.monotonic() + 5
        while not (options.directory / "stop-orphan").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        mark("orphan-exit.json", {"pid": os.getpid()})
        return 0

    mark("worker.json", {"pid": os.getpid(), "cwd": os.getcwd()})
    for line in sys.stdin.buffer:
        message = json.loads(line)
        method = message.get("method")
        if method == "notifications/cancelled":
            mark("cancelled.json", {"pid": os.getpid(), **message.get("params", {})})
            continue
        if method == "notifications/initialized":
            mark("initialized.json", {"pid": os.getpid()})
            continue
        if "id" not in message:
            continue
        request_id = message["id"]
        envelope = {"jsonrpc": "2.0", "id": request_id}
        if method == "initialize":
            if options.mode == "initfailure":
                emit({**envelope, "error": {"code": -32602, "message": "Fixture initialization failure"}})
                continue
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "astra-transport-fixture", "version": "1"}}
        elif method == "tools/list":
            schema = {"type": "object", "properties": {"value": {"type": "number"},
                      "text": {"type": "string"}}, "required": ["value"], "additionalProperties": False}
            if options.mode == "listfailure":
                schema = {"type": "string"}
            result = {"tools": [{"name": "work", "description": "Return a synthetic numeric value",
                                 "inputSchema": schema}]}
            if options.mode == "stalledstdin":
                emit({**envelope, "result": result})
                mark("stalled.json", {"pid": os.getpid()})
                while True:
                    time.sleep(1)
        elif method == "tools/call":
            mark("sent.json", {"request_id": request_id, "pid": os.getpid()})
            arguments = message["params"]["arguments"]
            if options.mode == "orphanstdout":
                subprocess.Popen(
                    [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                     "--mode", "holdstdout", "--directory", str(options.directory)],
                    stdin=subprocess.DEVNULL, stdout=sys.stdout.buffer, stderr=subprocess.DEVNULL,
                    close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                deadline = time.monotonic() + 2
                while not (options.directory / "orphan-ready.json").exists():
                    if time.monotonic() >= deadline:
                        return 2
                    time.sleep(0.01)
                return 0
            if options.mode == "writeexit":
                mark("effect.json", {"value": arguments.get("value")})
                return 0
            if options.mode == "disconnect":
                return 0
            if options.mode == "badframe":
                sys.stdout.buffer.write(b"not-json\n")
                sys.stdout.buffer.flush()
                continue
            if options.mode == "oversized":
                sys.stdout.buffer.write(b"x" * 1_000_001 + b"\n")
                sys.stdout.buffer.flush()
                continue
            if options.mode == "partial":
                sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":')
                sys.stdout.buffer.flush()
                continue
            if options.mode == "hang":
                continue
            value = arguments.get("value")
            result = {"content": [{"type": "text", "text": str(value)}],
                      "structuredContent": {"value": value}, "isError": False}
        else:
            emit({**envelope, "error": {"code": -32601, "message": "Unsupported fixture method"}})
            continue
        emit({**envelope, "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
