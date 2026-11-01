"""Offline HTTP protocol demonstration; the local mock is not a language model."""

import argparse
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading

from astra_harness.application import Application
from astra_harness.builtin.models import DemoModel
from astra_harness.model_profiles import ModelProfiles
from astra_harness.tools import Tool


@contextmanager
def mock_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/v1/chat/completions" or not 0 < length < 65536:
                self.send_error(400)
                return
            payload = json.loads(self.rfile.read(length))
            # Never record request headers, credentials or arbitrary source files.
            requests.append({"model": payload["model"], "max_tokens": payload.get("max_tokens"),
                             "stream": payload.get("stream", False)})
            results = [item for item in payload["messages"] if item["role"] == "tool"]
            if payload.get("tools") and not results:
                message = {"tool_calls": [{"index": 0, "id": "sum", "type": "function",
                    "function": {"name": "add", "arguments": '{"a":7,"b":8}'}}]}
                finish = "tool_calls"
            else:
                value = json.loads(results[-1]["content"])["value"] if results else "connected"
                message, finish = {"content": f"{payload['model']}: {value}"}, "stop"
            if payload.get("stream"):
                data = {"choices": [{"index": 0, "delta": message, "finish_reason": finish}]}
                body = ("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps({"choices": [{"message": message, "finish_reason": finish}]}).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if thread.is_alive():
            raise RuntimeError("mock server did not close")


def demonstrate(output):
    output.mkdir(parents=True, exist_ok=False)
    executed = []
    tool = Tool("add", "合成示例：受审批保护的加法", ("a", "b"),
                lambda a, b: executed.append((a, b)) or a + b, requires_approval=True)
    factory = lambda store: ModelProfiles(store, environment=lambda: {"ASTRA_MODEL_DEMO_API_KEY": "local-mock-only"})
    with mock_server() as (endpoint, requests):
        with Application(DemoModel(), (tool,), database=output / "runs.sqlite3", model_catalog_factory=factory) as app:
            profile = {"name": "本地模拟档案", "provider": "chat_completions", "base_url": endpoint,
                       "model": "mock-a", "key_env": "ASTRA_MODEL_DEMO_API_KEY", "timeout": 5,
                       "max_output_tokens": 64}
            catalog = app.save_model_profile(profile, 0)
            saved = catalog["items"][0]
            identifier = saved["id"]
            catalog = app.select_model_profile(identifier, catalog["revision"])
            assert not requests
            probe = asyncio.run(app.test_model_profile(identifier, catalog["revision"]))
            first = app.submit("合成任务：计算 7 + 8", expected_model_identity=catalog["selected_identity"])
            first = app.wait(first["id"])
            assert first["status"] == "waiting_approval" and not executed
            profile.update(id=identifier, model="mock-b")
            catalog = app.save_model_profile(profile, catalog["revision"])
            app.approve(first["id"])
            first = app.wait(first["id"])
            assert first["status"] == "completed" and first["output"] == "mock-a: 15"
            second = app.submit("新会话合成任务：计算 7 + 8", expected_model_identity=catalog["selected_identity"])
            second = app.wait(second["id"])
            assert second["status"] == "waiting_approval" and len(executed) == 1
        # Reopening checks the persisted model snapshot and credential binding.
        with Application(DemoModel(), (tool,), database=output / "runs.sqlite3", model_catalog_factory=factory) as app:
            assert app.get_run(second["id"])["status"] == "waiting_approval"
            app.approve(second["id"])
            second = app.wait(second["id"])
            assert second["status"] == "completed" and second["output"] == "mock-b: 15"
            assert len(executed) == 2 and all(pair == (7, 8) for pair in executed)
            assert app.model_profiles()["selected_id"] == identifier
        assert [row["model"] for row in requests] == ["mock-a", "mock-a", "mock-a", "mock-b", "mock-b"]
        report = {"passed": True, "fixture": "local scripted HTTP; not real-model quality evaluation",
                  "external_provider_requests": 0, "mock_requests": len(requests), "tool_calls": len(executed),
                  "edited_profile_kept_old_run": first["output"], "reopened_run": second["output"],
                  "probe": probe, "requests": requests}
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="New directory for this synthetic demonstration")
    args = parser.parse_args()
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="astra-model-profiles-")) / "demo"
    report = demonstrate(output)
    print(json.dumps({"output_dir": str(output), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
