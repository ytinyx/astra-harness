"""Trusted STDIO gate. The host assigns its Job before releasing this payload.

Read one byte at a time so the gate cannot consume the first MCP frame from the
shared stdin. Only this bounded configuration is handled here; all later bytes
belong to the service process. No output is written to the protocol stream.
"""
import json
import os
import subprocess
import sys

MAX_PAYLOAD = 16384

def main():
    try:
        raw = bytearray()
        while len(raw) <= MAX_PAYLOAD:
            item = os.read(sys.stdin.fileno(), 1)
            if not item:
                return 253
            raw.extend(item)
            if item == b"\n":
                break
        if len(raw) > MAX_PAYLOAD or not raw.endswith(b"\n"):
            return 253
        payload = json.loads(raw)
        argv, cwd = payload["argv"], payload["cwd"]
        if (set(payload) != {"argv", "cwd"} or not isinstance(argv, list) or not argv
                or any(not isinstance(value, str) or "\0" in value for value in argv)
                or not os.path.isabs(argv[0]) or not isinstance(cwd, str) or not os.path.isabs(cwd)):
            return 253
        child = subprocess.Popen(argv, executable=argv[0], cwd=cwd, shell=False,
            stdin=sys.stdin, stdout=sys.stdout, stderr=subprocess.DEVNULL, close_fds=True,
            env=dict(os.environ))
        return child.wait()
    except BaseException:
        return 253  # No exception, arguments, environment or server output.

if __name__ == "__main__":
    sys.exit(main())
