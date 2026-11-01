"""Actual loopback API with real collection adapters and an in-process provider.

No external embedding service is contacted and no semantic quality is claimed.
"""
import http.client
import json
import threading
import unittest

from astra_harness.application import Application
from astra_harness.web import create_server
from tests.test_embedding_host import EMBEDDING, HostFixture, RecordingProvider
from tests.test_knowledge_web import NoModel


class EmbeddingWebTests(HostFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.value["collections"][0]["embedding"] = EMBEDDING
        self.write_config(self.value)
        self.providers = []
        def factory(config):
            provider = RecordingProvider(config)
            self.providers.append(provider)
            return provider
        self.collections, self.registry = self.manager(factory=factory)
        self.app = self.application()
        self.port = self.server(self.app)

    def application(self, permissions=None, name="runs.sqlite3"):
        app = Application(NoModel(), self.registry.tools(), database=self.root / name,
                          permissions=self.collections.permissions if permissions is None else permissions)
        self.addCleanup(app.close)
        return app

    def server(self, app):
        server = create_server(app, port=0, knowledge=self.collections)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)
        return server.server_port

    def request(self, path, method="GET", body=None, headers=None, port=None):
        port = port or self.port
        fields = {"Host": f"127.0.0.1:{port}"}
        if body is not None:
            body = json.dumps(body).encode() if not isinstance(body, bytes) else body
            fields["Content-Type"] = "application/json"
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            connection.request(method, path, body=body, headers=fields)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def config(self, collection="alpha"):
        return self.request(f"/api/knowledge/{collection}/embedding-config")

    def save(self, embedding, *, collection="alpha", revision=None, **request_options):
        revision = revision or self.config(collection)[1]["config_revision"]
        return self.request(f"/api/knowledge/{collection}/embedding-config", "PUT",
                            {"embedding": embedding, "expected_revision": revision}, **request_options)

    def operation(self, action, arguments=None, request_id="operation-1", collection="alpha", **options):
        return self.request(f"/api/knowledge/{collection}/operations", "POST",
                            {"action": action, "arguments": arguments or {}, "request_id": request_id}, **options)

    def test_configuration_and_status_are_readonly_and_identity_is_action_specific(self):
        status, config = self.config()
        self.assertEqual(status, 200)
        self.assertTrue(config["editable"])
        self.assertFalse(config["restart_required"])
        self.assertEqual(self.request("/api/knowledge/alpha/embedding-status")[0], 200)
        disabled = self.request("/api/knowledge/beta/embedding-status")
        self.assertEqual((disabled[0], disabled[1]["state"]), (200, "not_configured"))
        item = self.request("/api/knowledge")[1]["collections"][0]
        for action, name in item["tools"].items():
            self.assertEqual(item["tool_recovery_identities"][action], self.registry.get(name).recovery_identity)
        self.assertNotEqual(item["tool_recovery_identities"]["embedding_test"], item["recovery_identity"])
        self.assertEqual(self.providers[0].calls, [])
        self.assertEqual(self.app.list_runs(), [])
        self.assertNotIn(str(self.root), json.dumps(config))

    def test_save_requires_cas_and_defers_new_network_actions_until_restart(self):
        before = self.config()[1]
        result = self.save(dict(EMBEDDING, model="synthetic-v2"), revision=before["config_revision"])
        self.assertEqual(result[0], 200)
        self.assertTrue(result[1]["restart_required"])
        self.assertEqual(result[1]["active"]["model"], "synthetic-v1")
        self.assertEqual(self.save(EMBEDDING, revision=before["config_revision"])[0], 409)
        self.assertEqual(self.operation("embedding_test")[0], 409)
        self.assertEqual(self.providers[0].calls, [])
        lexical = self.operation("search", {"query": "guide"}, request_id="lexical")
        self.assertEqual(lexical[0], 201)
        self.assertEqual(self.app.wait(lexical[1]["id"])["status"], "completed")

    def test_disabled_collection_can_save_without_adding_tools_or_network(self):
        result = self.save(EMBEDDING, collection="beta")
        self.assertEqual(result[0], 200)
        self.assertFalse(result[1]["active"]["enabled"])
        self.assertTrue(result[1]["restart_required"])
        self.assertEqual(self.operation("embedding_test", collection="beta")[0], 409)
        self.assertEqual(len(self.providers), 1)
        self.assertEqual(self.providers[0].calls, [])

    def test_test_operation_waits_for_approval_and_rejection_sends_nothing(self):
        status, run = self.operation("embedding_test", request_id="rejected")
        self.assertEqual(status, 201)
        waiting = self.app.wait(run["id"])
        self.assertEqual(waiting["status"], "waiting_approval")
        self.assertEqual(self.providers[0].calls, [])
        rejected = self.request(f'/api/runs/{run["id"]}/approve', "POST", {"approved": False})
        self.assertEqual(rejected[0], 200)
        self.app.wait(run["id"])
        self.assertEqual(self.providers[0].calls, [])
        _, approved = self.operation("embedding_test", request_id="approved")
        self.app.wait(approved["id"])
        self.assertEqual(self.request(f'/api/runs/{approved["id"]}/approve', "POST", {"approved": True})[0], 200)
        done = self.app.wait(approved["id"])
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["tool_result"]["value"]["ok"])
        self.assertEqual(len(self.providers[0].calls), 1)
        self.assertEqual(len(self.providers[0].calls[0]), 1)

    def test_semantic_operations_require_reviewed_corpus_and_no_injected_endpoint(self):
        invalid = [("embedding_test", {"endpoint": "http://127.0.0.1:1/embeddings"}),
                   ("embedding_rebuild", {}), ("semantic_search", {"query": "guide"}),
                   ("semantic_search", {"query": "guide", "expected_corpus_sha256": "a" * 64,
                                        "tool": "knowledge.beta.search"})]
        for action, arguments in invalid:
            with self.subTest(action=action, arguments=arguments):
                self.assertEqual(self.operation(action, arguments)[0], 400)
        self.assertEqual(self.app.list_runs(), [])
        self.assertEqual(self.providers[0].calls, [])

    def test_config_and_status_obey_selected_collection_permissions(self):
        app = self.application(permissions=["files.read", "knowledge.beta.read"], name="denied.sqlite3")
        port = self.server(app)
        for endpoint in ("embedding-config", "embedding-status"):
            self.assertEqual(self.request("/api/knowledge/alpha/" + endpoint, port=port)[0], 403)
        self.assertEqual(self.request("/api/knowledge/beta/embedding-config", port=port)[0], 200)
        self.assertEqual(self.save(EMBEDDING, collection="beta", port=port)[0], 403)
        self.assertEqual(self.providers[0].calls, [])

    def test_put_retains_host_origin_bounded_json_and_secret_redaction_guards(self):
        before = self.path.read_bytes()
        self.assertEqual(self.save(EMBEDDING, headers={"Origin": "https://malicious.example"})[0], 403)
        self.assertEqual(self.save(EMBEDDING, headers={"Host": "malicious.example"})[0], 403)
        path = "/api/knowledge/alpha/embedding-config"
        duplicate = b'{"embedding":{},"embedding":{},"expected_revision":"' + b'a' * 64 + b'"}'
        self.assertEqual(self.request(path, "PUT", duplicate)[0], 400)
        status, body = self.save(dict(EMBEDDING, api_key="synthetic-private-marker"))
        self.assertEqual(status, 400)
        self.assertNotIn("synthetic-private-marker", json.dumps(body))
        extra = {"embedding": EMBEDDING, "expected_revision": "a" * 64, "path": "another.json"}
        self.assertEqual(self.request(path, "PUT", extra)[0], 400)
        self.assertEqual(self.request(path, "PUT", b'{"padding":"' + b'a' * 65536 + b'"}')[0], 413)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.providers[0].calls, [])


if __name__ == "__main__":
    unittest.main()
