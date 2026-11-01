"""本地真实 JSON-RPC 测试服务器；不请求模型或公网。"""

import json
import sys
import time


for line in sys.stdin.buffer:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "astra-test", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "multiply", "description": "Multiply two numbers",
                             "inputSchema": {"type": "object", "properties": {
                                 "a": {"type": "number"}, "b": {"type": "number"}},
                                 "required": ["a", "b"], "additionalProperties": False}}]}
    elif method == "tools/call":
        if "--hang-call" in sys.argv:
            time.sleep(10)
        args = message["params"]["arguments"]
        result = {"content": [{"type": "text", "text": str(args["a"] * args["b"])}], "isError": False}
    else:
        result = {}
    sys.stdout.buffer.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode() + b"\n")
    sys.stdout.buffer.flush()
