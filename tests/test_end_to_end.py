"""真实 HTTP → 应用 → SQLite → 文件工具验收，无外网和供应商调用。"""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from astra_harness.application import Application
from astra_harness.builtin.files import file_tools
from astra_harness.builtin.models import DemoModel
from astra_harness.cli import seed_workspace
from astra_harness.evaluation import EXPECTED
from astra_harness.web import create_server


class EndToEndTests(unittest.TestCase):
    def test_http_approval_artifact_and_event_reconnect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = seed_workspace(root / "workspace")
            with Application(DemoModel(), file_tools(workspace), database=root / "runs.sqlite3",
                             permissions=["files.read", "files.write"], workspace=workspace) as app:
                server = create_server(app, port=0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                base = f"http://127.0.0.1:{server.server_port}"

                def post(path, body):
                    request = Request(base + path, json.dumps(body).encode(),
                                      {"Content-Type": "application/json", "Origin": base})
                    with urlopen(request, timeout=5) as result:
                        return json.load(result)

                try:
                    body = {"prompt": "整理会议待办", "request_id": "http-test"}
                    run = post("/api/runs", body)
                    waiting = app.wait(run["id"])
                    self.assertEqual(waiting["status"], "waiting_approval")
                    self.assertFalse((workspace / "todos.json").exists())
                    self.assertEqual(post("/api/runs", body)["id"], run["id"])
                    post(f"/api/runs/{run['id']}/approve", {"approved": True})
                    finished = app.wait(run["id"])
                    self.assertEqual(finished["status"], "completed")
                    self.assertEqual(json.loads((workspace / "todos.json").read_text(encoding="utf-8")), EXPECTED)
                    with urlopen(base + f"/api/runs/{run['id']}/events", timeout=5) as result:
                        stream = result.read().decode()
                    self.assertIn("event: run", stream)
                    self.assertIn("completed", stream)
                    last_seq = app.events(run["id"])[-1]["seq"]
                    with urlopen(base + f"/api/runs/{run['id']}/events?after={last_seq}", timeout=5) as result:
                        replay = result.read().decode()
                    self.assertNotIn("event: run\n", replay)
                    self.assertEqual(len(app.list_runs()), 1)
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(2)
