"""Real Application/SQLite with owned synthetic connection oracles."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from astra_harness.application import Application, RuntimeUpdateError
from astra_harness.builtin.models import DemoModel
from astra_harness.mcp_profiles import MCPProfiles, MCPProfileError, CATALOG_KEY
from astra_harness.tools import Tool


class FixtureConnection:
    def __init__(self, profile, workspace, *, environment=None):
        self.profile = deepcopy(profile)
        self.tools = ()
        self.closed = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = self.fail_start = self.fail_close = False
        self.calls = []
        self.state = "new"
        self.tool_name = "mcp." + profile["name"] + ".echo"

    def start(self):
        self.started.set()
        if self.block:
            self.release.wait(5)
        if self.fail_start or self.closed:
            raise RuntimeError("do-not-echo-provider-body")
        self.tools = (Tool(self.tool_name, "Synthetic fixture", (), self.call,
                           input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                           permissions=frozenset({"mcp." + self.profile["name"] + ".call"}),
                           requires_approval=True, side_effect=True, recovery_identity=uuid4().hex),)
        self.state = "ready"
        return self

    def call(self):
        if self.closed:
            raise RuntimeError("fixture closed")
        self.calls.append("call")
        return {"synthetic": True}

    def status(self):
        return {"state": self.state, "error_code": "mcp_cleanup_failed" if self.fail_close else None,
                "name": self.profile["name"], "transport": self.profile["transport"],
                "tools": [tool.name for tool in self.tools]}

    def close(self):
        self.closed = True
        self.state = "closed"
        self.release.set()
        if self.fail_close:
            raise RuntimeError("private-cleanup-diagnostic")


class MCPProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="astra-mcp-catalog-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.database = self.base / "state/runs.sqlite3"
        self.tools = (Tool("host_only", "Not granted", (), lambda: None, permissions=frozenset({"host.secret"})),)
        self.connections = []
        self.configure = lambda connection: None

        def connection(profile, workspace, **options):
            result = FixtureConnection(profile, workspace, **options)
            self.configure(result)
            self.connections.append(result)
            return result

        self.connection_factory = connection
        self.app = Application(DemoModel(), self.tools, database=self.database)
        self.catalog = self.make_catalog(self.app)
        self.addCleanup(self.shutdown)

    def make_catalog(self, app, **overrides):
        def environment():
            raise AssertionError("catalog must not read credential values")
        return MCPProfiles(app, workspace=self.workspace, base_tools=self.tools, base_permissions=(),
                           connection_factory=self.connection_factory, environment=environment, **overrides)

    def shutdown(self):
        for connection in self.connections:
            connection.fail_close = False
            connection.release.set()
        self.catalog.close()
        self.app.close()

    def create(self, name="alpha", **extra):
        result = self.catalog.save({"name": name, "transport": "http", "endpoint": "http://127.0.0.1:12345/mcp", **extra},
                                   self.catalog.list()["revision"])
        return result["items"][-1]

    def operation(self, action, item, **kwargs):
        return asyncio.run(getattr(self.catalog, action)(item["id"], self.catalog.list()["revision"], item["identity"], **kwargs))

    def test_crud_default_disabled_and_no_connection_created(self):
        item = self.create()
        self.assertFalse(item["enabled"])
        self.assertEqual(item["state"], "disabled")
        self.assertFalse(item["activation_required"])
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_conflict"):
            self.catalog.delete(item["id"], 1, "0" * 64)
        result = self.catalog.delete(item["id"], 1, item["identity"])
        self.assertEqual(result["items"], [])
        self.assertEqual(self.connections, [])

    def test_cas_unknown_fields_names_and_catalog_limit(self):
        item = self.create()
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_conflict"):
            self.catalog.save({"name": "alpha", "transport": "http", "endpoint": "http://127.0.0.1:12345/mcp"}, 0)
        with self.assertRaisesRegex(MCPProfileError, "invalid_mcp_profile"):
            self.catalog.save({"name": "beta", "api_key": "do-not-return"}, 1)
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_conflict"):
            self.create()
        for number in range(15):
            self.create("fixture" + str(number))
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_limit"):
            self.create("overflow")
        self.assertEqual(len(self.catalog.list()["items"]), 16)
        self.assertFalse(self.connections)

    def test_content_identity_changes_but_enabled_intent_does_not_change_it(self):
        item = self.create()
        updated = self.catalog.save({"id": item["id"], "name": "alpha", "transport": "http",
                                     "endpoint": "http://127.0.0.1:12345/other"}, 1)["items"][0]
        self.assertEqual(updated["config_revision"], 2)
        self.assertNotEqual(updated["identity"], item["identity"])
        enabled = self.operation("enable", updated, confirmed=True)["items"][0]
        self.assertEqual(updated["identity"], enabled["identity"])
        self.assertEqual(enabled["state"], "ready")
        disabled = self.operation("disable", enabled)["items"][0]
        self.assertEqual(updated["identity"], disabled["identity"])

    def test_explicit_test_discovers_then_closes_without_registering_or_calling(self):
        item = self.create()
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_confirmation_required"):
            self.operation("test", item)
        self.assertFalse(self.connections)
        before = self.app.tools
        result = self.operation("test", item, confirmed=True)
        self.assertEqual(result["tools"], ["mcp.alpha.echo"])
        self.assertFalse(result["registered"])
        self.assertEqual(self.app.tools, before)
        self.assertTrue(self.connections[0].closed)
        self.assertEqual(self.connections[0].calls, [])
        self.assertEqual(self.catalog.list()["revision"], 1)
        self.assertIsNone(self.app._tool_update)

    def test_enable_uses_approval_and_never_grants_unrelated_base_permission(self):
        item = self.create()
        self.operation("enable", item, confirmed=True)
        self.assertNotIn("host.secret", self.app.permissions)
        run = self.app.submit_tool("mcp.alpha.echo", {})
        run = self.app.wait(run["id"])
        self.assertEqual(run["status"], "waiting_approval")
        self.assertEqual(self.connections[0].calls, [])
        with self.assertRaisesRegex(RuntimeUpdateError, "runtime_update_busy"):
            self.operation("disable", item)
        self.app.approve(run["id"])
        self.assertEqual(self.app.wait(run["id"])["status"], "completed")
        self.assertEqual(self.connections[0].calls, ["call"])
        self.operation("disable", item)
        self.assertEqual(self.app.tools, self.tools)
        self.assertTrue(self.connections[0].closed)

    def test_enabled_or_offline_enabled_requires_disable_before_edit_delete(self):
        item = self.create()
        self.operation("enable", item, confirmed=True)
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_in_use"):
            self.catalog.delete(item["id"], 2, item["identity"])
        self.catalog.close()
        self.app.close()
        count = len(self.connections)
        self.app = Application(DemoModel(), self.tools, database=self.database)
        self.catalog = self.make_catalog(self.app)
        restored = self.catalog.list()["items"][0]
        self.assertTrue(restored["enabled"] and restored["activation_required"])
        self.assertEqual(restored["state"], "offline")
        self.assertEqual(len(self.connections), count)
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_in_use"):
            self.catalog.save({"id": item["id"], "name": "alpha", "transport": "http",
                               "endpoint": "http://127.0.0.1:12345/changed"}, 2)
        self.operation("disable", item)
        self.catalog.delete(item["id"], 3, item["identity"])
        self.assertEqual(len(self.connections), count)

    def test_enable_persistence_failure_rolls_back_before_releasing_gate(self):
        item = self.create()
        old = self.app.tools
        with patch.object(self.app.store, "compare_and_swap_host_setting", return_value=False):
            with self.assertRaisesRegex(MCPProfileError, "mcp_profile_conflict"):
                self.operation("enable", item, confirmed=True)
        self.assertEqual(self.app.tools, old)
        self.assertFalse(self.catalog.list()["items"][0]["enabled"])
        self.assertTrue(self.connections[-1].closed)
        self.assertIsNone(self.app._tool_update)

    def test_disable_persistence_failure_preserves_existing_connection(self):
        item = self.create()
        self.operation("enable", item, confirmed=True)
        old = self.app.tools
        with patch.object(self.app.store, "compare_and_swap_host_setting", return_value=False):
            with self.assertRaisesRegex(MCPProfileError, "mcp_profile_conflict"):
                self.operation("disable", item)
        self.assertEqual(self.app.tools, old)
        self.assertTrue(self.catalog.list()["items"][0]["enabled"])
        self.assertFalse(self.connections[-1].closed)
        self.assertIsNone(self.app._tool_update)

    def test_registration_conflict_closes_candidate_and_preserves_host(self):
        item = self.create()
        self.configure = lambda connection: setattr(connection, "tool_name", "host_only")
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_connection_failed"):
            self.operation("enable", item, confirmed=True)
        self.assertEqual(self.app.tools, self.tools)
        self.assertFalse(self.catalog.list()["items"][0]["enabled"])
        self.assertTrue(self.connections[-1].closed)

    def test_cancel_closes_owned_discovery_then_releases_gate_and_blocks_crud(self):
        async def scenario():
            item = self.create()
            self.configure = lambda connection: setattr(connection, "block", True)
            task = asyncio.create_task(self.catalog.test(item["id"], 1, item["identity"], confirmed=True))
            for _ in range(100):
                if self.connections and self.connections[-1].started.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.connections[-1].started.is_set())
            self.assertEqual(self.catalog.list()["operation"]["action"], "test")
            with self.assertRaisesRegex(MCPProfileError, "mcp_profile_busy"):
                self.catalog.delete(item["id"], 1, item["identity"])
            with self.assertRaisesRegex(RuntimeUpdateError, "runtime_update_busy"):
                self.app.submit("must not start during discovery")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.connections[-1].closed)
            self.assertIsNone(self.app._tool_update)
            self.assertIsNone(self.catalog.list()["operation"])
            self.assertEqual(self.connections[-1].calls, [])
        asyncio.run(scenario())

    def test_timeout_is_total_discovery_deadline_and_does_not_enable(self):
        item = self.create(timeout=1)
        self.configure = lambda connection: setattr(connection, "block", True)
        tick = time.monotonic()
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_timeout"):
            self.operation("enable", item, confirmed=True)
        self.assertLess(time.monotonic() - tick, 3)
        self.assertTrue(self.connections[-1].closed)
        self.assertIsNone(self.app._tool_update)
        self.assertFalse(self.catalog.list()["items"][0]["enabled"])

    def test_cleanup_failure_holds_host_gate_until_shutdown(self):
        item = self.create()
        self.configure = lambda connection: setattr(connection, "fail_close", True)
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_cleanup_failed"):
            self.operation("test", item, confirmed=True)
        self.assertTrue(self.catalog.list()["cleanup_required"])
        with self.assertRaisesRegex(RuntimeUpdateError, "runtime_update_busy"):
            self.app.submit("must not proceed")
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_unavailable"):
            self.create("blocked")
        self.connections[-1].fail_close = False
        self.catalog.close()
        self.assertIsNone(self.app._tool_update)

    def test_stale_identity_and_reserved_namespace_reject_before_connection(self):
        item = self.create()
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_conflict"):
            asyncio.run(self.catalog.enable(item["id"], 1, "0" * 64, confirmed=True))
        other = self.make_catalog(self.app, reserved_names=("legacy", "reserved"))
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_conflict"):
            other.save({"name": "reserved", "transport": "http", "endpoint": "http://127.0.0.1:12345/mcp"}, 1)
        other.close()
        self.assertEqual(self.connections, [])

    def test_active_cap_includes_startup_services_and_isolation_on_failure(self):
        self.catalog.close()
        self.catalog = self.make_catalog(self.app, reserved_names=tuple("static" + str(i) for i in range(7)))
        alpha = self.create()
        beta = self.create("beta")
        self.operation("enable", alpha, confirmed=True)
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_limit"):
            self.operation("enable", beta, confirmed=True)
        self.assertEqual(len(self.connections), 1)
        self.assertFalse(self.connections[0].closed)

    def test_corrupt_persisted_catalog_fails_without_reset(self):
        self.create()
        damaged = '{"format":1,"revision":1,"revision":2,"items":[]}'
        with self.app.store.db:
            self.app.store.db.execute("UPDATE host_settings SET value=? WHERE key=?", (damaged, CATALOG_KEY))
        with self.assertRaisesRegex(MCPProfileError, "mcp_profile_unavailable"):
            self.catalog.list()
        self.assertEqual(self.app.store.host_setting(CATALOG_KEY), damaged)
        self.assertEqual(self.connections, [])

    def test_rollback_failure_keeps_gate_and_candidate_owned_until_shutdown(self):
        item = self.create()
        publish = self.app.publish_tool_update
        count = 0

        def failing_rollback(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeUpdateError("runtime_update_invalid")
            return publish(*args, **kwargs)

        with patch.object(self.app, "publish_tool_update", failing_rollback), \
                patch.object(self.app.store, "compare_and_swap_host_setting", return_value=False):
            with self.assertRaisesRegex(MCPProfileError, "mcp_profile_cleanup_failed"):
                self.operation("enable", item, confirmed=True)
        self.assertTrue(self.catalog.list()["cleanup_required"])
        self.assertFalse(self.connections[-1].closed)
        with self.assertRaisesRegex(RuntimeUpdateError, "runtime_update_busy"):
            self.app.submit("cannot execute uncertain composition")
        self.catalog.close()
        self.assertTrue(self.connections[-1].closed)
        self.assertIsNone(self.app._tool_update)

    def test_close_serializes_publish_cas_and_rejects_late_database_reads(self):
        item = self.create()
        entered, proceed, closed = threading.Event(), threading.Event(), threading.Event()
        original = self.app.publish_tool_update
        outcomes = []

        def delayed_publish(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            if not proceed.wait(3):
                raise AssertionError("test synchronization timeout")
            return result

        def enable():
            try:
                outcomes.append(self.operation("enable", item, confirmed=True))
            except MCPProfileError as error:
                outcomes.append(error.code)

        def close():
            self.catalog.close()
            self.app.close()
            closed.set()

        with patch.object(self.app, "publish_tool_update", delayed_publish):
            operation = threading.Thread(target=enable)
            operation.start()
            self.assertTrue(entered.wait(3))
            shutdown = threading.Thread(target=close)
            shutdown.start()
            self.assertFalse(closed.wait(.05))
            proceed.set()
            operation.join(3)
            shutdown.join(3)
        self.assertFalse(operation.is_alive() or shutdown.is_alive())
        self.assertTrue(closed.is_set())
        self.assertTrue(self.connections[-1].closed)
        self.assertEqual(self.catalog._active, {})
        self.assertEqual(len(outcomes), 1)
        with patch.object(self.app.store, "host_setting", side_effect=AssertionError("closed Store must not be read")):
            with self.assertRaisesRegex(MCPProfileError, "mcp_profile_unavailable"):
                self.catalog.list()

    def test_discovery_failure_preserves_other_active_service(self):
        alpha = self.create()
        beta = self.create("beta")
        self.operation("enable", alpha, confirmed=True)
        self.configure = lambda connection: setattr(connection, "fail_start", True)
        with self.assertRaisesRegex(MCPProfileError, "^mcp_profile_connection_failed$"):
            self.operation("enable", beta, confirmed=True)
        self.assertFalse(self.connections[0].closed)
        self.assertTrue(self.connections[1].closed)
        self.assertEqual([tool.name for tool in self.app.tools], ["host_only", "mcp.alpha.echo"])
        self.assertIsNone(self.app._tool_update)

    def test_close_waits_until_acquired_runtime_reservation_is_recorded(self):
        item = self.create()
        entered, proceed, closed = threading.Event(), threading.Event(), threading.Event()
        original = self.app.reserve_tool_update
        outcomes, failures = [], []

        def delayed_reserve():
            token = original()
            entered.set()
            if not proceed.wait(3):
                raise AssertionError("test synchronization timeout")
            return token

        def test_connection():
            try:
                outcomes.append(self.operation("test", item, confirmed=True))
            except MCPProfileError as error:
                outcomes.append(error.code)
            except BaseException as error:
                failures.append(type(error).__name__)

        def shutdown():
            try:
                self.catalog.close()
                self.app.close()
                closed.set()
            except BaseException as error:
                failures.append(type(error).__name__)

        with patch.object(self.app, "reserve_tool_update", delayed_reserve):
            operation = threading.Thread(target=test_connection)
            operation.start()
            self.assertTrue(entered.wait(3))
            closing = threading.Thread(target=shutdown)
            closing.start()
            self.assertFalse(closed.wait(.05))
            proceed.set()
            operation.join(3)
            closing.join(3)
        self.assertFalse(operation.is_alive() or closing.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(closed.is_set())
        self.assertEqual(len(outcomes), 1)
        self.assertIsNone(self.app._tool_update)
        self.assertTrue(all(connection.closed for connection in self.connections))


if __name__ == "__main__":
    unittest.main()
