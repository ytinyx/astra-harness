"""Actual loopback protocol checks with synthetic registered knowledge tools.

The fixtures test HTTP admission, persistent runs and approval semantics. They
do not stand in for source-file/citation validation by KnowledgePlugin tests.
"""

from copy import deepcopy
from dataclasses import replace
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from astra_harness.application import Application
from astra_harness.tools import Tool
from astra_harness.web import create_server


SOURCE_SHA = "a" * 64


class NoModel:
    def __init__(self):
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise AssertionError("a manual knowledge operation must not invoke a model")


class SyntheticCollection:
    def __init__(self, identifier):
        self.identifier = identifier
        self.prefix = "knowledge." + identifier
        self.reads = []
        self.effects = []
        self.source_sha = SOURCE_SHA
        self.metadata_error = False
        string = {"type": "string", "minLength": 1, "maxLength": 240}
        digest = {"type": "string", "maxLength": 64}
        schemas = {
            "catalog": ({}, []),
            "inspect": ({"path": string}, ["path"]),
            "import_file": ({"path": string, "expected_source_sha256": digest,
                             "expected_index_sha256": digest},
                            ["path", "expected_source_sha256", "expected_index_sha256"]),
            "remove": ({"path": string, "expected_index_sha256": digest},
                       ["path", "expected_index_sha256"]),
            "search": ({"query": string, "limit": {"type": "integer", "minimum": 1, "maximum": 5}},
                       ["query"]),
        }
        self.tools = []
        for action, (properties, required) in schemas.items():
            write = action in {"import_file", "remove"}
            self.tools.append(Tool(self.prefix + "." + action, "Synthetic protocol fixture", (),
                getattr(self, action), input_schema={"type": "object", "properties": properties,
                    "required": required, "additionalProperties": False},
                permissions=frozenset({self.prefix + (".write" if write else ".read")}),
                side_effect=write, requires_approval=write, implementation_version="http-fixture-v1",
                recovery_identity=hashlib.sha256((self.prefix + "-fixture-v1").encode()).hexdigest()))

    def catalog(self):
        self.reads.append("catalog")
        if self.metadata_error:
            return {"ok": False, "error_code": "index_closed"}
        return {"ok": True, "namespace": self.prefix, "available_files": [{"path": "guide.md", "bytes": 16}],
                "documents": [], "truncated": False, "scanned_entries": 1}

    def inspect(self, path):
        self.reads.append(("inspect", path))
        return {"ok": True, "path": path, "status": "unindexed", "source_sha256": self.source_sha,
                "source_bytes": 16, "indexed_sha256": "", "indexed_bytes": 0}

    def import_file(self, path, expected_source_sha256, expected_index_sha256):
        if expected_source_sha256 != self.source_sha:
            return {"ok": False, "error_code": "source_changed"}
        self.effects.append(("import_file", path))
        return {"ok": True, "path": path, "collection": self.identifier}

    def remove(self, path, expected_index_sha256):
        self.effects.append(("remove", path))
        return {"ok": True, "path": path, "removed": True}

    def search(self, query, limit=3):
        self.reads.append(("search", query))
        return {"ok": True, "hits": [], "collection": self.identifier, "query": query}


class SyntheticManager:
    def __init__(self):
        self.items = {name: SyntheticCollection(name) for name in ("alpha", "beta")}
        self.tools = tuple(tool for item in self.items.values() for tool in item.tools)
        self.permissions = sorted({permission for tool in self.tools for permission in tool.permissions})

    def collections(self):
        return [{"id": key, "label": "Fixture " + key, "source_scope": key,
                 "tools": {action: item.prefix + "." + action
                           for action in ("catalog", "inspect", "import_file", "search", "remove")}}
                for key, item in self.items.items()]


class KnowledgeWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = SyntheticManager()
        self.model = NoModel()
        self.database = Path(self.temp.name) / "runs.sqlite3"
        self.app = self.make_application()
        self.port = self.start_server(self.app, self.manager)

    def make_application(self, *, permissions=None, database=None):
        app = Application(self.model, self.manager.tools, database=database or self.database,
                          permissions=self.manager.permissions if permissions is None else permissions)
        self.addCleanup(app.close)
        return app

    def start_server(self, app, manager):
        server = create_server(app, port=0, knowledge=manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "loopback server must close")

        self.addCleanup(close)
        return server.server_port

    def request(self, path, method="GET", body=None, headers=None, port=None):
        port = port or self.port
        request_headers = {"Host": f"127.0.0.1:{port}"}
        if body is not None:
            body = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read().decode("utf-8")
            # HTTP field names are case-insensitive. Preserve HTTPMessage's
            # lookup semantics instead of converting it to a plain dict.
            return response.status, response.headers, json.loads(payload)
        finally:
            connection.close()

    def operation(self, action="import_file", *, collection="alpha", request_id="request-1", arguments=None):
        if arguments is None:
            arguments = {"path": "guide.md", "expected_source_sha256": SOURCE_SHA, "expected_index_sha256": ""}
        return self.request(f"/api/knowledge/{collection}/operations", "POST",
                            {"action": action, "arguments": arguments, "request_id": request_id})

    def test_collection_binding_is_same_snapshot_identity_and_changes_with_host_binding(self):
        descriptor = self.request("/api/knowledge")[2]["collections"][0]
        self.assertRegex(descriptor["recovery_identity"], r"^[0-9a-f]{64}$")
        _, _, run = self.operation("search", request_id="bound-search", arguments={"query": "test"})
        result = self.app.wait(run["id"])
        old_snapshot = next(item for item in result["tool_snapshot"] if item["name"] == descriptor["tools"]["search"])
        self.assertEqual(descriptor["recovery_identity"], old_snapshot["recovery_identity"])
        replacement = hashlib.sha256(b"different-source-and-index-binding").hexdigest()
        self.manager.tools = tuple(replace(tool, recovery_identity=replacement) for tool in self.manager.tools)
        changed_app = self.make_application(database=Path(self.temp.name) / "different.sqlite3")
        changed_port = self.start_server(changed_app, self.manager)
        changed = self.request("/api/knowledge", port=changed_port)[2]["collections"][0]
        self.assertEqual(changed["id"], descriptor["id"])
        self.assertEqual(changed["recovery_identity"], replacement)
        self.assertNotEqual(changed["recovery_identity"], old_snapshot["recovery_identity"])
        self.assertNotIn(str(Path(self.temp.name)), json.dumps(changed))

    def test_listing_and_selected_inspect_are_readonly_and_bounded_by_route(self):
        status, headers, data = self.request("/api/knowledge")
        self.assertEqual(status, 200)
        self.assertTrue(data["enabled"])
        self.assertEqual([item["id"] for item in data["collections"]], ["alpha", "beta"])
        self.assertNotIn(str(Path(self.temp.name)), json.dumps(data))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("/api/knowledge/alpha")[2]["namespace"], "knowledge.alpha")
        inspected = self.request("/api/knowledge/beta/inspect?path=guide.md")
        self.assertEqual(inspected[0], 200)
        self.assertEqual(inspected[2]["source_sha256"], SOURCE_SHA)
        self.assertEqual(self.manager.items["alpha"].reads, ["catalog"])
        self.assertEqual(self.manager.items["beta"].reads, [("inspect", "guide.md")])
        self.assertEqual(self.app.list_runs(), [])
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.request("/api/knowledge/alpha/inspect?path=a&path=b")[0], 400)

    def test_disabled_unknown_and_invalid_collection_never_execute(self):
        disabled = self.start_server(self.app, None)
        self.assertEqual(self.request("/api/knowledge", port=disabled)[2], {"enabled": False, "collections": []})
        self.assertEqual(self.request("/api/knowledge/alpha", port=disabled)[0], 404)
        self.assertEqual(self.request("/api/knowledge/missing")[0], 404)
        self.assertEqual(self.operation(collection="missing")[0], 404)
        self.assertEqual(self.request("/api/knowledge/%2e%2e")[0], 404)
        self.assertEqual(self.app.list_runs(), [])

    def test_catalog_and_inspect_enforce_executor_permissions(self):
        other = self.make_application(permissions=[], database=Path(self.temp.name) / "denied.sqlite3")
        port = self.start_server(other, self.manager)
        for path in ("/api/knowledge/alpha", "/api/knowledge/alpha/inspect?path=guide.md"):
            status, _, body = self.request(path, port=port)
            self.assertEqual((status, body["error"]), (403, "permission_denied"))
        self.assertEqual(self.manager.items["alpha"].reads, [])
        self.assertEqual(self.model.calls, 0)

    def test_metadata_failure_is_not_disguised_as_empty_success(self):
        self.manager.items["alpha"].metadata_error = True
        status, _, body = self.request("/api/knowledge/alpha")
        self.assertEqual((status, body["error"]), (409, "index_closed"))
        self.assertNotIn("documents", body)

    def test_writes_require_revision_binding_and_reject_forged_authority(self):
        base = {"action": "import_file", "arguments": {"path": "guide.md",
                "expected_source_sha256": SOURCE_SHA, "expected_index_sha256": ""}, "request_id": "binding"}
        invalid = []
        for field in ("expected_source_sha256", "expected_index_sha256"):
            body = deepcopy(base)
            del body["arguments"][field]
            invalid.append(body)
        for key, value in (("approved", True), ("permissions", ["knowledge.alpha.write"]),
                           ("call_id", "forged"), ("name", "knowledge.beta.import_file")):
            body = deepcopy(base)
            body[key] = value
            invalid.append(body)
        body = deepcopy(base)
        body["arguments"]["expected_source_sha256"] = "bad"
        invalid.append(body)
        body = {"action": "remove", "arguments": {"path": "guide.md", "expected_index_sha256": ""},
                "request_id": "remove-empty"}
        invalid.append(body)
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.request("/api/knowledge/alpha/operations", "POST", body)[0], 400)
        self.assertEqual(self.app.list_runs(), [])
        self.assertEqual(self.manager.items["alpha"].effects, [])

    def test_approved_import_idempotency_and_parameter_conflicts(self):
        status, _, submitted = self.operation()
        self.assertEqual(status, 201)
        pending = self.app.wait(submitted["id"], timeout=4)
        self.assertEqual((pending["status"], pending["model_calls"], pending["tool_calls"]),
                         ("waiting_approval", 0, 0))
        self.assertEqual(self.manager.items["alpha"].effects, [])
        self.assertEqual(self.operation()[2]["id"], submitted["id"])
        self.assertEqual(self.request(f"/api/runs/{submitted['id']}/approve", "POST", {"approved": True})[0], 200)
        self.app.wait(submitted["id"], timeout=4)
        final = self.request(f"/api/runs/{submitted['id']}")[2]
        self.assertEqual((final["kind"], final["status"], final["model_calls"], final["tool_calls"]),
                         ("tool", "completed", 0, 1))
        self.assertEqual(final["operation"]["name"], "knowledge.alpha.import_file")
        self.assertIsNone(final["tool_result"]["error_code"])
        self.assertTrue(final["tool_result"]["value"]["ok"])
        self.assertEqual(self.operation()[2]["id"], submitted["id"])
        self.assertEqual(self.manager.items["alpha"].effects, [("import_file", "guide.md")])
        changed = {"path": "other.md", "expected_source_sha256": SOURCE_SHA, "expected_index_sha256": ""}
        self.assertEqual(self.operation(arguments=changed)[0], 400)
        self.assertEqual(self.operation(collection="beta")[0], 400)
        self.assertEqual(self.manager.items["beta"].effects, [])
        self.assertEqual(self.model.calls, 0)

    def test_rejected_and_cancelled_approval_do_not_write_or_retry(self):
        first = self.operation(request_id="reject")[2]
        self.app.wait(first["id"], timeout=4)
        self.request(f"/api/runs/{first['id']}/approve", "POST", {"approved": False})
        rejected = self.operation(request_id="reject")[2]
        self.assertEqual((rejected["id"], rejected["status"], rejected["error"]),
                         (first["id"], "failed", "approval_denied"))
        second = self.operation(request_id="cancel")[2]
        self.app.wait(second["id"], timeout=4)
        self.assertEqual(self.request(f"/api/runs/{second['id']}/cancel", "POST", {})[0], 200)
        self.assertEqual(self.request(f"/api/runs/{second['id']}")[2]["status"], "cancelled")
        self.assertEqual(self.manager.items["alpha"].effects, [])
        self.assertEqual(self.model.calls, 0)

    def test_search_uses_selected_collection_and_schema_rejects_tool_injection(self):
        response = self.operation("search", collection="beta", request_id="search-beta", arguments={"query": "topic"})
        self.assertEqual(response[0], 201)
        final = self.app.wait(response[2]["id"], timeout=4)
        self.assertEqual(final["tool_result"]["name"], "knowledge.beta.search")
        self.assertEqual(final["tool_result"]["value"]["collection"], "beta")
        self.assertEqual(final["model_calls"], 0)
        self.assertEqual(self.manager.items["alpha"].reads, [])
        forged = self.operation("search", request_id="extra-arg",
                                arguments={"query": "topic", "name": "knowledge.beta.search"})
        failed = self.app.wait(forged[2]["id"], timeout=4)
        self.assertEqual((failed["status"], failed["tool_result"]["error_code"]), ("failed", "invalid_arguments"))
        self.assertEqual(self.manager.items["alpha"].reads, [])

    def test_business_failure_survives_transport_without_false_success(self):
        submitted = self.operation(request_id="source-change")[2]
        self.app.wait(submitted["id"], timeout=4)
        self.manager.items["alpha"].source_sha = "c" * 64
        self.request(f"/api/runs/{submitted['id']}/approve", "POST", {"approved": True})
        self.app.wait(submitted["id"], timeout=4)
        status, _, final = self.request(f"/api/runs/{submitted['id']}")
        self.assertEqual(status, 200)
        # Generic runtime completion does not mean the business operation
        # succeeded; this explicit result is what the knowledge UI must use.
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["tool_result"]["value"], {"ok": False, "error_code": "source_changed"})
        self.assertEqual(self.manager.items["alpha"].effects, [])
        self.assertEqual(self.model.calls, 0)

    def test_host_origin_and_duplicate_json_are_rejected_before_operation(self):
        body = {"action": "search", "arguments": {"query": "topic"}, "request_id": "guarded"}
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
            status, response_headers, result = self.request("/api/knowledge/alpha/operations", "POST", body, headers)
            self.assertEqual(status, 403)
            self.assertIn(result["error"], {"invalid_host", "invalid_origin"})
            self.assertEqual(response_headers["X-Content-Type-Options"], "nosniff")
        duplicate = b'{"action":"search","action":"remove","arguments":{},"request_id":"dup"}'
        self.assertEqual(self.request("/api/knowledge/alpha/operations", "POST", duplicate)[0], 400)
        self.assertEqual(self.app.list_runs(), [])
        self.assertEqual(self.manager.items["alpha"].reads, [])


if __name__ == "__main__":
    unittest.main()
