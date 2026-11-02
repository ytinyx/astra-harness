"""Model profile boundaries over real loopback HTTP, with synthetic credentials."""

from argparse import Namespace
from contextlib import ExitStack
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from astra_harness.application import Application
from astra_harness.builtin.models import DemoModel
from astra_harness.cli import build_application
from astra_harness.config import load_model_environment
from astra_harness.model_binding import ModelProfileError
from astra_harness.model_profiles import ModelProfiles
from astra_harness.web import create_server


SYNTHETIC_KEY = "synthetic-profile-key-not-a-credential"
KEY_ENV = "ASTRA_MODEL_TEST_API_KEY"


class ProfileMock:
    """The fixture owns its sockets; held responses observe client cancellation."""

    def __init__(self, *, key=SYNTHETIC_KEY):
        self.key = key
        self.mode = "normal"
        self.requests = []
        self.started = threading.Event()
        self.disconnected = threading.Event()
        self.stop = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append({"path": self.path, "body": body,
                    "authorized": self.headers.get("Authorization") == "Bearer " + fixture.key})
                mode = fixture.mode
                fixture.started.set()
                if mode == "hold":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "10000")
                    self.end_headers()
                    self.wfile.flush()
                    self.connection.settimeout(.1)
                    while not fixture.stop.is_set():
                        try:
                            if not self.connection.recv(1):
                                fixture.disconnected.set()
                                break
                        except socket.timeout:
                            continue
                        except OSError:
                            fixture.disconnected.set()
                            break
                    self.close_connection = True
                    return
                usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
                status, content_type = 200, "application/json"
                if mode == "error":
                    status = 401
                    payload = json.dumps({"error": fixture.key}).encode()
                elif body.get("stream"):
                    content_type = "text/event-stream"
                    values = [
                        {"choices": [{"index": 0, "delta": {"content": "profile reply"}, "finish_reason": None}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
                    ]
                    payload = b"".join(b"data: " + json.dumps(value).encode() + b"\n\n" for value in values)
                    payload += b"data: [DONE]\n\n"
                else:
                    payload = json.dumps({"choices": [{"message": {"content": "profile reply"},
                        "finish_reason": "stop"}], "usage": usage}).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%s/v1" % self.server.server_port

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("fixture server did not stop")


class ModelProfilesWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mock = ProfileMock()
        self.addCleanup(self.mock.close)
        self.environment_reads = 0
        self.environment = {KEY_ENV: SYNTHETIC_KEY}
        self.app = Application(DemoModel(), database=self.root / "runs.sqlite3",
            model_catalog_factory=lambda store: ModelProfiles(store, environment=self.read_environment))
        self.addCleanup(self.app.close)
        self.server = create_server(self.app, port=0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def read_environment(self):
        self.environment_reads += 1
        return self.environment

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def request(self, path="/api/model-profiles", method="GET", body=None, headers=None, *, raw=False):
        fields = {"Host": "127.0.0.1:%s" % self.port}
        if body is not None:
            body = body if isinstance(body, bytes) else json.dumps(body).encode()
            fields["Content-Type"] = "application/json"
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=fields)
            response = connection.getresponse()
            data = response.read()
            return response.status, data if raw else json.loads(data)
        finally:
            connection.close()

    def catalog(self):
        status, result = self.request()
        self.assertEqual(status, 200, result)
        return result

    def profile(self, **changes):
        return {"name": "Synthetic localhost", "base_url": self.mock.url,
                "model": "fixture", "key_env": KEY_ENV, **changes}

    def save(self, **changes):
        status, catalog = self.request("/api/model-profiles/save", "POST",
            {"profile": self.profile(**changes), "expected_revision": self.catalog()["revision"]})
        self.assertEqual(status, 200, catalog)
        return catalog, catalog["items"][-1]

    def select(self, item):
        status, value = self.request("/api/model-profiles/select", "POST",
            {"id": item["id"] if item else None, "expected_revision": self.catalog()["revision"]})
        self.assertEqual(status, 200, value)
        return value

    def test_catalog_crud_is_pure_and_selection_identity_tracks_revision(self):
        self.assertEqual(self.catalog()["selected_identity"], "startup")
        catalog, item = self.save()
        self.assertRegex(item["id"], r"^[0-9a-f]{32}$")
        self.assertRegex(item["identity"], r"^[0-9a-f]{64}$")
        self.assertEqual(item["revision"], 1)
        self.assertIn("adapter_revision", item)
        self.assertIsNone(catalog["selected_id"])
        self.assertEqual(self.select(item)["selected_identity"], item["identity"])
        _, updated = self.save(id=item["id"], name="Renamed synthetic profile")
        self.assertEqual(updated["revision"], 2)
        self.assertNotEqual(updated["identity"], item["identity"])
        self.assertEqual(self.catalog()["selected_identity"], updated["identity"])
        self.assertEqual(self.select(None)["selected_identity"], "startup")
        status, deleted = self.request("/api/model-profiles/delete", "POST",
            {"id": item["id"], "expected_revision": self.catalog()["revision"]})
        self.assertEqual((status, deleted["items"]), (200, []))
        self.assertEqual(self.environment_reads, 0)
        self.assertEqual(self.mock.requests, [])

    def test_secret_and_read_only_fields_are_rejected_without_echo(self):
        for field in ("api_key", "password", "headers", "identity", "revision", "adapter_revision"):
            with self.subTest(field=field):
                status, value = self.request("/api/model-profiles/save", "POST",
                    {"profile": self.profile(**{field: SYNTHETIC_KEY}), "expected_revision": 0})
                self.assertEqual((status, value), (400, {"error": "invalid_model_profile"}))
                self.assertNotIn(SYNTHETIC_KEY, json.dumps(value))
        self.assertEqual(self.catalog()["revision"], 0)
        self.assertEqual(self.environment_reads, 0)
        self.assertEqual(self.mock.requests, [])

    def test_strict_request_types_invalid_numbers_and_stale_cas_do_not_mutate(self):
        for revision in (True, 1.0, "0", -1, float("nan")):
            with self.subTest(revision=revision):
                status, value = self.request("/api/model-profiles/save", "POST",
                    {"profile": self.profile(), "expected_revision": revision})
                self.assertEqual((status, value), (400, {"error": "invalid_request"}))
        for changes, code in (({"timeout": True}, "invalid_model_profile"),
                              ({"timeout": float("nan")}, "invalid_request"),
                              ({"max_output_tokens": True}, "invalid_model_profile")):
            status, value = self.request("/api/model-profiles/save", "POST",
                {"profile": self.profile(**changes), "expected_revision": 0})
            self.assertEqual((status, value), (400, {"error": code}))
        before, item = self.save()
        for action, fields in (("save", {"profile": self.profile(id=item["id"])}),
                               ("select", {"id": item["id"]}), ("delete", {"id": item["id"]}),
                               ("test", {"id": item["id"], "confirmed": True})):
            status, value = self.request("/api/model-profiles/" + action, "POST", {**fields, "expected_revision": 0})
            self.assertEqual((status, value), (409, {"error": "model_profile_conflict"}))
        self.assertEqual(self.catalog(), before)
        self.assertEqual(self.environment_reads, 0)
        self.assertEqual(self.mock.requests, [])

    def test_local_host_origin_and_unknown_query_guards_remain_active(self):
        body = {"profile": self.profile(), "expected_revision": 0}
        for headers in ({"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"}):
            status, _ = self.request("/api/model-profiles/save", "POST", body, headers)
            self.assertEqual(status, 403)
        self.assertEqual(self.request("/api/model-profiles?api_key=" + SYNTHETIC_KEY),
                         (404, {"error": "not_found"}))
        self.assertEqual(self.catalog()["revision"], 0)
        self.assertEqual(self.environment_reads, 0)
        self.assertEqual(self.mock.requests, [])

    def test_test_requires_explicit_boolean_confirmation_then_one_bounded_request(self):
        catalog, item = self.save()
        body = {"id": item["id"], "expected_revision": catalog["revision"]}
        for fields in ({}, {"confirmed": False}, {"confirmed": 1}, {"confirmed": "true"}):
            status, value = self.request("/api/model-profiles/test", "POST", {**body, **fields})
            self.assertEqual((status, value), (400, {"error": "invalid_request"}))
        self.assertEqual(self.environment_reads, 0)
        self.assertEqual(self.mock.requests, [])
        status, value = self.request("/api/model-profiles/test", "POST", {**body, "confirmed": True})
        self.assertEqual(status, 200, value)
        self.assertTrue(value["ok"])
        self.assertTrue(value["request_issued"])
        self.assertEqual(value["identity"], item["identity"])
        self.assertEqual(self.environment_reads, 1)
        self.assertEqual(len(self.mock.requests), 1)
        sent = self.mock.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertTrue(sent["authorized"])
        self.assertLessEqual(sent["body"]["max_tokens"], 32)
        self.assertNotIn("tools", sent["body"])
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(value))

    def test_missing_key_is_local_and_provider_error_body_is_not_exposed(self):
        catalog, item = self.save()
        body = {"id": item["id"], "expected_revision": catalog["revision"], "confirmed": True}
        self.environment.clear()
        self.assertEqual(self.request("/api/model-profiles/test", "POST", body),
            (409, {"error": "model_credentials_missing"}))
        self.assertEqual(self.mock.requests, [])
        self.environment[KEY_ENV] = SYNTHETIC_KEY
        self.mock.mode = "error"
        status, result = self.request("/api/model-profiles/test", "POST", body)
        self.assertEqual(status, 200)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "model_authentication_failed")
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))
        self.assertEqual(len(self.mock.requests), 1)

    def test_http_disconnect_cancels_single_active_test_and_releases_gate(self):
        catalog, item = self.save()
        body = {"id": item["id"], "expected_revision": catalog["revision"], "confirmed": True}
        self.mock.mode = "hold"
        data = json.dumps(body).encode()
        client = socket.create_connection(("127.0.0.1", self.port), timeout=4)
        self.addCleanup(client.close)
        request = ("POST /api/model-profiles/test HTTP/1.1\r\nHost: 127.0.0.1:%s\r\n"
                   "Content-Type: application/json\r\nContent-Length: %s\r\n\r\n" % (self.port, len(data))).encode()
        client.sendall(request + data)
        self.assertTrue(self.mock.started.wait(3), "confirmed test did not reach owned server")
        self.assertEqual(self.request("/api/model-profiles/test", "POST", body),
            (409, {"error": "model_profile_in_use"}))
        self.assertEqual(len(self.mock.requests), 1)
        client.shutdown(socket.SHUT_RDWR)
        client.close()
        self.assertTrue(self.mock.disconnected.wait(3), "upstream remained open after browser disconnect")
        self.mock.mode = "normal"
        status, result = self.request("/api/model-profiles/test", "POST", body)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.mock.requests), 2)

    def test_selected_run_public_surfaces_strip_private_seal_and_do_not_store_key(self):
        _, item = self.save()
        self.select(item)
        body = {"prompt": "synthetic task", "request_id": "public-surfaces",
                "expected_model_identity": item["identity"]}
        for identity in (True, "", "F" * 64):
            self.assertEqual(self.request("/api/runs", "POST", {**body, "expected_model_identity": identity}),
                (400, {"error": "invalid_request"}))
        self.assertEqual(self.request("/api/runs", "POST", {**body, "expected_model_identity": "startup"}),
            (409, {"error": "model_profile_not_selected"}))
        self.assertEqual(self.mock.requests, [])
        status, submitted = self.request("/api/runs", "POST", body)
        self.assertEqual(status, 201, submitted)
        finished = self.app.wait(submitted["id"])
        self.assertEqual(finished["status"], "completed", finished)
        stored = self.app.store.get(submitted["id"])
        seal = stored["_model_credential_seal"]
        self.assertRegex(seal, r"^[0-9a-f]{64}$")
        results = [submitted, finished, self.app.list_runs(), self.app.get_session(finished["session_id"])]
        for path in ("/api/info", "/api/sessions", "/api/sessions/" + finished["session_id"],
                     "/api/runs/" + finished["id"]):
            status, value = self.request(path)
            self.assertEqual(status, 200, value)
            results.append(value)
        status, events = self.request("/api/runs/" + finished["id"] + "/events", raw=True)
        self.assertEqual(status, 200)
        combined = json.dumps(results).encode() + events
        for forbidden in (SYNTHETIC_KEY, seal, "_model_credential_seal"):
            self.assertNotIn(forbidden.encode(), combined)
        # The process-ownership .lock file is exclusively locked on Windows;
        # only SQLite payload files can contain serialized application data.
        for name in ("runs.sqlite3", "runs.sqlite3-wal", "runs.sqlite3-journal"):
            file = self.root / name
            if file.exists():
                self.assertNotIn(SYNTHETIC_KEY.encode(), file.read_bytes())
        self.assertEqual(len(self.mock.requests), 1)

    def test_profile_credentials_are_bound_to_the_correct_provider(self):
        other_key = "second-synthetic-provider-key"
        other = ProfileMock(key=other_key)
        self.addCleanup(other.close)
        self.environment["ASTRA_MODEL_OTHER_API_KEY"] = other_key
        _, first = self.save()
        _, second = self.save(name="Other provider", base_url=other.url, key_env="ASTRA_MODEL_OTHER_API_KEY")
        for item in (first, second):
            self.select(item)
            status, submitted = self.request("/api/runs", "POST", {"prompt": "synthetic task",
                "request_id": item["id"], "expected_model_identity": item["identity"]})
            self.assertEqual(status, 201, submitted)
            self.assertEqual(self.app.wait(submitted["id"])["status"], "completed")
        self.assertEqual(len(self.mock.requests), 1)
        self.assertEqual(len(other.requests), 1)
        self.assertTrue(self.mock.requests[0]["authorized"])
        self.assertTrue(other.requests[0]["authorized"])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_browser_key_save_use_delete_and_replace_without_restart(self):
        catalog, item = self.save(key_env=None, api_key=SYNTHETIC_KEY)
        self.assertTrue(item["has_api_key"])
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(catalog))
        self.assertEqual(self.mock.requests, [], "saving must not contact provider")
        self.select(item)
        status, submitted = self.request("/api/runs", "POST", {"prompt": "synthetic stored key task"})
        self.assertEqual(status, 201, submitted)
        finished = self.app.wait(submitted["id"])
        self.assertEqual(finished["status"], "completed", finished)
        self.assertTrue(self.mock.requests[0]["authorized"])
        for path in ("/api/model-profiles", "/api/info", "/api/runs/" + submitted["id"],
                     "/api/sessions/" + submitted["session_id"]):
            self.assertNotIn(SYNTHETIC_KEY, json.dumps(self.request(path)))
        body = {"id": item["id"], "expected_revision": self.catalog()["revision"]}
        status, deleted = self.request("/api/model-profiles/delete-key", "POST", body)
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted["selected_id"], item["id"])
        self.assertFalse(deleted["items"][0]["has_api_key"])
        self.assertEqual(self.request("/api/runs", "POST", {"prompt": "must fail locally"}),
                         (409, {"error": "model_credentials_missing"}))
        self.assertEqual(len(self.mock.requests), 1)
        self.save(id=item["id"], key_env=None, api_key=SYNTHETIC_KEY)
        status, submitted = self.request("/api/runs", "POST", {"prompt": "fresh synthetic task"})
        self.assertEqual(status, 201, submitted)
        self.assertEqual(self.app.wait(submitted["id"])["status"], "completed")
        self.assertEqual(len(self.mock.requests), 2)
        self.assertEqual(self.environment_reads, 0)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_key_deletion_is_guarded_by_origin_revision_and_inflight_probe(self):
        catalog, item = self.save(key_env=None, api_key=SYNTHETIC_KEY)
        body = {"id": item["id"], "expected_revision": catalog["revision"]}
        for headers in ({"Origin": "https://attacker.invalid"}, {"Host": "attacker.invalid"}):
            self.assertEqual(self.request("/api/model-profiles/delete-key", "POST", body, headers)[0], 403)
        self.assertEqual(self.request("/api/model-profiles/delete-key", "POST", {**body, "expected_revision": 0}),
                         (409, {"error": "model_profile_conflict"}))
        self.mock.mode = "hold"
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.request("/api/model-profiles/test", "POST",
                                  {**body, "confirmed": True})))
        thread.start()
        try:
            self.assertTrue(self.mock.started.wait(3))
            for action in ("delete-key", "delete"):
                self.assertEqual(self.request("/api/model-profiles/" + action, "POST", body),
                                 (409, {"error": "model_profile_in_use"}))
            self.assertTrue(self.catalog()["items"][0]["has_api_key"])
        finally:
            self.mock.stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_malformed_browser_secret_is_rejected_without_echo(self):
        for secret in (None, True, "synthetic key with spaces", "synthetic\r\nheader", "x" * 4097):
            status, value = self.request("/api/model-profiles/save", "POST", {
                "profile": self.profile(key_env=None, api_key=secret), "expected_revision": 0})
            self.assertEqual((status, value), (400, {"error": "model_credentials_invalid"}))
        self.assertEqual(self.catalog()["revision"], 0)
        self.assertEqual(self.mock.requests, [])


class ModelProfilesCLIIntegrationTests(unittest.TestCase):
    def test_run_uses_startup_while_serve_restores_web_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, data = root / "workspace", root / "data"
            workspace.mkdir()
            data.mkdir()
            fixture = ProfileMock()
            self.addCleanup(fixture.close)
            args = Namespace(command="serve", workspace=workspace, database=data / "runs.sqlite3",
                config=None, mcp_config=None, env_file=None, live=False, no_files=True,
                demo_files=False, knowledge_index=None, knowledge_config=None, allow_command=[])
            catalog_factory = lambda store: ModelProfiles(store, environment=lambda: {KEY_ENV: SYNTHETIC_KEY})
            with patch("astra_harness.model_profiles.ModelProfiles", side_effect=catalog_factory), \
                 patch("astra_harness.cli.load_model_environment", side_effect=lambda path: load_model_environment(path, environ={})):
                with ExitStack() as resources:
                    app = build_application(args, resources)
                    catalog = app.save_model_profile({"name": "Web selection", "base_url": fixture.url,
                        "model": "fixture", "key_env": KEY_ENV}, 0)
                    item = catalog["items"][0]
                    app.select_model_profile(item["id"], catalog["revision"])
                args.command = "run"
                with ExitStack() as resources:
                    app = build_application(args, resources)
                    run = app.wait(app.submit("hello", expected_model_identity="startup")["id"])
                    self.assertEqual(run["status"], "completed")
                    self.assertEqual(run.get("model_identity", "startup"), "startup")
                    self.assertEqual(fixture.requests, [])
                    self.assertEqual(app.model_profiles()["selected_id"], item["id"])
                args.command = "serve"
                with ExitStack() as resources:
                    app = build_application(args, resources)
                    run = app.wait(app.submit("hello", expected_model_identity=item["identity"])["id"])
                    self.assertEqual(run["status"], "completed", run)
                    self.assertEqual(run["model_identity"], item["identity"])
                    self.assertEqual(len(fixture.requests), 1)

    def test_explicit_synthetic_env_file_remains_startup_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, data = root / "workspace", root / "data"
            workspace.mkdir()
            data.mkdir()
            fixture = ProfileMock()
            self.addCleanup(fixture.close)
            env_file = data / "synthetic-startup.env"
            env_file.write_text("ASTRA_API_KEY=" + SYNTHETIC_KEY + "\nASTRA_MODEL=fixture\nASTRA_BASE_URL="
                                + fixture.url + "\n", encoding="utf-8")
            args = Namespace(command="serve", workspace=workspace, database=data / "runs.sqlite3",
                config=None, mcp_config=None, env_file=env_file, live=True, no_files=True,
                demo_files=False, knowledge_index=None, knowledge_config=None, allow_command=[])
            # The actual loader parses only this generated file; environment
            # lookup for dynamic profiles is separate and intentionally empty.
            catalog_factory = lambda store: ModelProfiles(store, environment=lambda: {})
            with patch("astra_harness.model_profiles.ModelProfiles", side_effect=catalog_factory), \
                 patch("astra_harness.cli.load_model_environment", side_effect=lambda path: load_model_environment(path, environ={})):
                with ExitStack() as resources:
                    app = build_application(args, resources)
                    catalog = app.save_model_profile({"name": "No copied startup key", "base_url": fixture.url,
                        "model": "fixture", "key_env": "ASTRA_API_KEY"}, 0)
                    item = catalog["items"][0]
                    app.select_model_profile(item["id"], catalog["revision"])
                    with self.assertRaises(ModelProfileError) as error:
                        app.submit("hello", expected_model_identity=item["identity"])
                    self.assertEqual(error.exception.code, "model_credentials_missing")
                    self.assertEqual(fixture.requests, [])
                args.command = "run"
                with ExitStack() as resources:
                    app = build_application(args, resources)
                    run = app.wait(app.submit("hello", expected_model_identity="startup")["id"])
                    self.assertEqual(run["status"], "completed", run)
                    self.assertEqual(len(fixture.requests), 1)
                    self.assertTrue(fixture.requests[0]["authorized"])


if __name__ == "__main__":
    unittest.main()
