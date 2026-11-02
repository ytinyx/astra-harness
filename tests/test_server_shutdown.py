"""Local shutdown admission, HTTP acknowledgement, and real CLI cleanup."""

import asyncio
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from unittest.mock import Mock

import httpx

from astra_harness.web import ServerControl, _stream, create_app


class Probe:
    def __init__(self):
        self.runs = []
        self.submissions = []

    def list_runs(self):
        return copy.deepcopy(self.runs)

    def submit(self, prompt, **kwargs):
        self.submissions.append(prompt)
        return {"id": "run-1"}

    def get_run(self, identifier):
        return {"id": identifier, "status": "paused"}

    def events(self, identifier, after=0):
        return []


class ShutdownAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.probe, self.exit = Probe(), Mock()
        self.control = ServerControl(self.exit)
        self.api = create_app(self.probe, 8765, server_control=self.control)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api),
                                        base_url="http://127.0.0.1:8765")
        self.addAsyncCleanup(self.client.aclose)
        self.body = {"instance_id": self.control.instance_id, "confirmed": True}

    async def stop(self, body=None, **kwargs):
        return await self.client.post("/api/server/shutdown", json=self.body if body is None else body, **kwargs)

    async def test_bare_asgi_factory_does_not_own_shutdown(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.probe, 8765)),
                                     base_url="http://127.0.0.1:8765") as client:
            self.assertEqual((await client.get("/api/server")).json(), {"supported": False})
            response = await client.post("/api/server/shutdown", json=self.body)
            self.assertEqual(response.status_code, 503)
        self.exit.assert_not_called()

    async def test_confirmation_host_origin_and_instance_are_required(self):
        for body in ({}, {**self.body, "confirmed": False}, {**self.body, "confirmed": 1},
                     {**self.body, "confirmed": "true"}, {**self.body, "force": True}):
            self.assertEqual((await self.stop(body)).status_code, 400)
        for headers in ({"Origin": "https://untrusted.example"}, {"Host": "untrusted.example:8765"}):
            self.assertEqual((await self.stop(headers=headers)).status_code, 403)
        response = await self.stop({**self.body, "instance_id": "0" * 32})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "server_changed")
        self.assertEqual((await self.client.get("/api/server/shutdown")).status_code, 404)
        self.exit.assert_not_called()
        self.assertEqual(self.control.state, "running")

    async def test_acknowledgement_is_idempotent_and_blocks_later_writes(self):
        self.assertEqual((await self.client.get("/api/server")).json(), self.control.snapshot())
        response = await self.stop()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["state"], "stopping")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.exit.assert_called_once_with()
        self.assertEqual((await self.stop()).status_code, 202)
        self.exit.assert_called_once_with()
        response = await self.client.post("/api/runs", json={"prompt": "late task"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "server_stopping")
        self.assertEqual(self.probe.submissions, [])

    async def test_running_and_queued_tasks_in_any_workspace_block_exit(self):
        for status in ("pending", "running"):
            self.probe.runs = [{"status": status, "workspace_id": "other-workspace", "kind": "tool"}]
            response = await self.stop()
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"], "server_busy")
            self.assertEqual(self.control.state, "running")
        self.exit.assert_not_called()

    async def test_paused_approval_and_unknown_records_are_unchanged(self):
        self.probe.runs = [{"status": status, "approval": {"key": "original"}}
                           for status in ("paused", "waiting_approval", "unknown", "completed")]
        before = copy.deepcopy(self.probe.runs)
        self.assertEqual((await self.stop()).status_code, 202)
        self.assertEqual(self.probe.runs, before)

    async def test_inflight_mutation_blocks_shutdown_until_response_finishes(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def submit(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test gate")
            return {"id": "run-1"}

        self.probe.submit = submit
        pending = asyncio.create_task(self.client.post("/api/runs", json={"prompt": "save"}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            self.assertEqual((await self.stop()).json()["error"], "server_busy")
            self.exit.assert_not_called()
        finally:
            release.set()
            self.assertEqual((await pending).status_code, 201)
        self.assertEqual((await self.stop()).status_code, 202)

    async def test_new_submission_cannot_race_idle_inspection(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def runs():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test gate")
            return []

        self.probe.list_runs = runs
        pending = asyncio.create_task(self.stop())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            self.assertEqual((await self.client.post("/api/runs", json={"prompt": "race"})).status_code, 503)
            self.assertEqual((await self.stop()).json()["error"], "server_busy")
        finally:
            release.set()
            self.assertEqual((await pending).status_code, 202)
        self.assertEqual(self.probe.submissions, [])

    async def test_failed_inspection_restores_admission(self):
        self.probe.list_runs = Mock(side_effect=RuntimeError("inspection failed"))
        self.assertEqual((await self.stop()).status_code, 409)
        self.assertEqual(self.control.state, "running")
        self.exit.assert_not_called()
        self.assertEqual((await self.client.post("/api/runs", json={"prompt": "still usable"})).status_code, 201)

    async def test_paused_sse_subscriber_finishes_on_shutdown(self):
        stream = _stream(self.probe, "run-1", 0, self.control)
        self.assertIn(b"snapshot", await anext(stream))
        self.assertIn(b"heartbeat", await anext(stream))
        self.control.state = "stopping"
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)


class ShutdownProcessTests(unittest.TestCase):
    def test_real_cli_exits_releases_port_and_restores_pending_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            command = [sys.executable, "-X", "utf8", "-m", "astra_harness", "serve", "--demo-files",
                       "--port", str(port), "--database", str(root / "runs.sqlite3"),
                       "--workspace", str(root / "workspace")]

            def request(path, body=None):
                data = None if body is None else json.dumps(body).encode()
                with urlopen(Request(f"http://127.0.0.1:{port}" + path, data,
                                     {"Content-Type": "application/json"}), timeout=2) as response:
                    return json.load(response)

            def start():
                process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.addCleanup(cleanup, process)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(process.stderr.read().decode("utf-8", errors="replace"))
                    try:
                        return process, request("/api/server")
                    except OSError:
                        time.sleep(0.05)
                self.fail("CLI did not start")

            def cleanup(process):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                process.stderr.close()

            def stop(process, status):
                self.assertEqual(request("/api/server/shutdown", {
                    "instance_id": status["instance_id"], "confirmed": True})["state"], "stopping")
                self.assertEqual(process.wait(timeout=8), 0, process.stderr.read().decode("utf-8", errors="replace"))
                with socket.socket() as sock:
                    sock.settimeout(1)
                    self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)

            process, first = start()
            run = request("/api/runs", {"prompt": "整理会议待办并保存 todos.json"})
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                run = request(f"/api/runs/{run['id']}")
                if run["status"] == "waiting_approval":
                    break
                time.sleep(0.05)
            self.assertEqual(run["status"], "waiting_approval")
            original_approval = run["approval"]
            stop(process, first)
            process, second = start()
            self.assertNotEqual(first["instance_id"], second["instance_id"])
            restored = request(f"/api/runs/{run['id']}")
            self.assertEqual(restored["status"], "waiting_approval")
            self.assertEqual(restored["approval"], original_approval)
            paused = request(f"/api/runs/{run['id']}/pause", {})
            self.assertEqual(paused["status"], "paused")
            stop(process, second)
            process, third = start()
            restored = request(f"/api/runs/{run['id']}")
            self.assertEqual(restored["status"], "paused")
            self.assertEqual(restored["resume_status"], "waiting_approval")
            self.assertEqual(restored["approval"], original_approval)
            self.assertFalse((root / "workspace" / "todos.json").exists())
            stop(process, third)


if __name__ == "__main__":
    unittest.main()
