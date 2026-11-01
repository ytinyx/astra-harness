"""实际 HTTP 工作台流程：文件读取、审批预览、会话管理与历史。"""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from astra_harness.application import Application
from astra_harness.builtin.files import file_tools
from astra_harness.builtin.models import DemoModel
from astra_harness.cli import seed_workspace
from astra_harness.web import create_server


class WorkbenchHTTPTests(unittest.TestCase):
    def test_approval_preview_and_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = seed_workspace(root / "workspace")
            with Application(DemoModel(), file_tools(workspace), database=root / "runs.sqlite3",
                             permissions=["files.read", "files.write"], workspace=workspace) as app:
                server = create_server(app, port=0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                base = f"http://127.0.0.1:{server.server_port}"

                def request(path, body=None):
                    data = None if body is None else json.dumps(body).encode()
                    req = Request(base + path, data, {"Content-Type": "application/json"})
                    with urlopen(req, timeout=5) as response:
                        return json.load(response)

                try:
                    listing = request("/api/files")
                    self.assertEqual(len(listing["entries"]), 3)
                    path = listing["entries"][0]["path"]
                    self.assertIn("content", request("/api/files/read?path=" + quote(path)))
                    with self.assertRaises(HTTPError) as error:
                        request("/api/files/read?path=../secret.txt")
                    self.assertIn(error.exception.code, (400, 403))
                    error.exception.close()
                    run = request("/api/runs", {"prompt": "整理会议待办"})
                    waiting = app.wait(run["id"])
                    paused = request(f"/api/runs/{run['id']}/pause", {})
                    self.assertEqual(paused["status"], "paused")
                    self.assertEqual(paused["resume_status"], "waiting_approval")
                    self.assertEqual(paused["approval"], waiting["approval"])
                    with self.assertRaises(HTTPError) as error:
                        request(f"/api/runs/{run['id']}/approve", {"approved": True})
                    self.assertEqual(error.exception.code, 409)
                    error.exception.close()
                    self.assertFalse((workspace / "todos.json").exists())
                    resumed = request(f"/api/runs/{run['id']}/resume", {})
                    self.assertEqual(resumed["status"], "waiting_approval")
                    self.assertEqual(resumed["model_calls"], waiting["model_calls"])
                    self.assertEqual(resumed["tool_calls"], waiting["tool_calls"])
                    self.assertEqual(resumed["elapsed"], paused["elapsed"])
                    preview = request(f"/api/runs/{run['id']}/preview")
                    self.assertFalse(preview["exists"])
                    self.assertEqual(len(json.loads(preview["after"])), 6)
                    self.assertFalse((workspace / "todos.json").exists())
                    with self.assertRaises(HTTPError) as error:
                        request(f"/api/sessions/{run['session_id']}/delete", {})
                    self.assertEqual(error.exception.code, 409)
                    error.exception.close()
                    request(f"/api/runs/{run['id']}/approve", {"approved": True})
                    self.assertEqual(app.wait(waiting["id"])["status"], "completed")
                    artifact = request("/api/files/read?path=todos.json")
                    self.assertEqual(len(json.loads(artifact["content"])), 6)
                    session = request(f"/api/sessions/{run['session_id']}", {"title": "交付演示"})
                    self.assertEqual(session["title"], "交付演示")
                    self.assertEqual(len(session["runs"]), 1)
                    request(f"/api/sessions/{run['session_id']}", {"archived": True})
                    self.assertEqual(request("/api/sessions"), [])
                    self.assertTrue(request("/api/sessions?archived=1")[0]["archived"])
                    request(f"/api/sessions/{run['session_id']}", {"archived": False})
                    self.assertEqual(len(request("/api/sessions")), 1)
                    self.assertTrue(request(f"/api/sessions/{run['session_id']}/delete", {})["deleted"])
                    self.assertEqual(request("/api/sessions?archived=1"), [])
                    with self.assertRaises(HTTPError) as error:
                        request("/api/sessions?archived=maybe")
                    self.assertEqual(error.exception.code, 400)
                    error.exception.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(2)
