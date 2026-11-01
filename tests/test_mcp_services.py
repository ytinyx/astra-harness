import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from astra_harness.application import Application
from astra_harness.builtin.mcp import MCPError
from astra_harness.builtin.mcp_services import (
    MAX_CONFIG_BYTES, MCPServiceConfig, MCPServiceManager, load_mcp_config, parse_mcp_config,
)
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.engine import Engine
from astra_harness.tools import Tool, ToolExecutionUnknown, ToolExecutor, ToolRegistry


SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}},
          "required": ["value"], "additionalProperties": False}


class FakeClient:
    def __init__(self, command, *, timeout, max_message_bytes, cwd):
        self.command = command
        self.cwd = cwd
        self.state = "new"
        self.last_error = None
        self.calls = []
        self.closed = 0
        self.mode = command[0]

    def start(self):
        self.state = "starting"
        if self.mode == "fail-init":
            raise MCPError("private exception with synthetic credential")
        self.state = "ready"
        return self

    def discover_tools(self):
        if self.mode == "fail-discovery":
            raise MCPError("mcp_invalid_schema")
        schema = deepcopy(SCHEMA)
        if self.mode == "changed-schema":
            schema["properties"]["value"]["minimum"] = 2
        count = 101 if self.mode == "many" else 1
        return tuple(Tool(f"mcp.echo{index if count > 1 else ''}", "synthetic echo", (),
                          lambda **kwargs: None, input_schema=schema) for index in range(count))

    async def acall_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.mode == "unknown":
            self.state, self.last_error = "failed", "mcp_transport_error"
            raise ToolExecutionUnknown("mcp_execution_unknown")
        return {"server": self.mode, "value": arguments["value"]}

    def close(self):
        if self.state != "closed":
            self.closed += 1
            self.state = "closed"
        if self.mode == "fail-close":
            raise RuntimeError("synthetic private cleanup detail")


class Factory:
    def __init__(self, client_type=FakeClient):
        self.clients = []
        self.client_type = client_type

    def __call__(self, *args, **kwargs):
        client = self.client_type(*args, **kwargs)
        self.clients.append(client)
        return client


class EchoModel:
    def __init__(self, tool="mcp.alpha.echo"):
        self.tool = tool

    def complete(self, request):
        if request.messages[-1].role == "tool":
            return ModelResponse(text="done")
        return ModelResponse(tool_calls=(ToolCall("echo-1", self.tool, {"value": 3}),))


def configuration(*items):
    return parse_mcp_config({"servers": [dict(name=name, command=[mode], **extras)
                                         for name, mode, extras in items]})


class ConfigTests(unittest.TestCase):
    def test_legacy_and_named_normalization(self):
        legacy, = parse_mcp_config({"command": ["synthetic-python", "server.py"]})
        self.assertTrue(legacy.legacy)
        self.assertEqual((legacy.prefix, legacy.permission), ("mcp", "mcp.call"))
        configs = configuration(("beta", "b", {}), ("alpha", "a", {"timeout": 10.0}))
        self.assertEqual([c.name for c in configs], ["alpha", "beta"])
        self.assertEqual(configs[0].permission, "mcp.alpha.call")
        self.assertNotIn("synthetic-python", repr(legacy))

    def test_config_rejects_unsupported_or_ambiguous_shapes(self):
        invalid = [None, [], {}, {"command": "python"}, {"command": []},
                   {"servers": []}, {"command": ["python"], "servers": []},
                   {"servers": [{"name": "alpha", "command": ["x"], "env": {}}]},
                   {"servers": [{"name": "alpha", "command": ["x"], "cwd": "ignored"}]},
                   {"servers": [{"name": "alpha", "url": "https://invalid.example"}]},
                   {"servers": [{"name": "alpha", "command": "x"}]},
                   {"servers": [{"name": "a", "command": ["x"]}] * 2},
                   {"servers": [{"name": f"a{i}", "command": ["x"]} for i in range(9)]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_mcp_config(value)

    def test_strict_names_limits_revision_and_commands(self):
        changes = [("name", value) for value in ["A", "a.b", "", "1a", "中", "a" * 33]]
        changes += [("timeout", value) for value in [True, 0, -1, 301, float("nan"), float("inf"), "10"]]
        changes += [("revision", value) for value in [None, 1, "", "a b", "a" * 65]]
        changes += [("command", value) for value in [[""], [1], ["a\0b"], ["x"] * 65, ["\ud800"]]]
        for key, value in changes:
            config = {"name": "alpha", "command": ["synthetic"]}
            config[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                parse_mcp_config({"servers": [config]})

    def test_loader_rejects_duplicate_nan_oversize_and_private_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for content in [b'{"command":["a"],"command":["b"]}',
                            b'{"servers":[{"name":"a","command":["x"],"timeout":NaN}]}',
                            b'{"private-secret":', b'\xff', b" " * (MAX_CONFIG_BYTES + 1)]:
                path.write_bytes(content)
                with self.subTest(content=content[:70]), self.assertRaises(ValueError) as caught:
                    load_mcp_config(path)
                self.assertNotIn("private-secret", str(caught.exception))
            path.write_text('{"servers":[{"name":"alpha","command":["local"]}]}', encoding="utf-8")
            self.assertEqual(load_mcp_config(path)[0].name, "alpha")
            for target in [Path(directory), Path(directory) / "absent"]:
                with self.assertRaisesRegex(ValueError, "^mcp_config_not_readable$"):
                    load_mcp_config(target)

    def test_identity_is_order_stable_and_configuration_sensitive(self):
        a, = configuration(("alpha", "local", {}))
        b, = parse_mcp_config({"servers": [{"revision": "1", "timeout": 10.0,
                                            "command": ["local"], "name": "alpha"}]})
        self.assertEqual(a.recovery_identity(), b.recovery_identity())
        for changed in [MCPServiceConfig("beta", ("local",)), MCPServiceConfig("alpha", ("other",)),
                        MCPServiceConfig("alpha", ("local",), revision="2"),
                        MCPServiceConfig("alpha", ("local",), timeout=11)]:
            self.assertNotEqual(a.recovery_identity(), changed.recovery_identity())


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, *items, factory=None):
        factory = factory or Factory()
        manager = MCPServiceManager(configuration(*items), client_factory=factory)
        self.addCleanup(manager.close)
        registry = ToolRegistry()
        manager.activate(registry)
        return manager, registry, factory

    async def test_two_same_named_tools_route_with_separate_permission_and_approval(self):
        manager, registry, factory = self.manager(("beta", "b", {}), ("alpha", "a", {}))
        self.assertEqual([t.name for t in registry.tools()], ["mcp.alpha.echo", "mcp.beta.echo"])
        self.assertEqual(manager.permissions, frozenset({"mcp.alpha.call", "mcp.beta.call"}))
        executor = ToolExecutor(registry)
        alpha = ToolCall("a", "mcp.alpha.echo", {"value": 3})
        self.assertEqual((await executor.aexecute(alpha)).error_code, "permission_denied")
        self.assertEqual((await executor.aexecute(alpha, manager.permissions)).error_code, "approval_required")
        invalid = ToolCall("invalid", "mcp.alpha.echo", {"value": True})
        self.assertEqual((await executor.aexecute(invalid, manager.permissions, approved=True)).error_code,
                         "invalid_arguments")
        self.assertTrue(all(not client.calls for client in factory.clients))
        result = await executor.aexecute(alpha, frozenset({"mcp.alpha.call"}), approved=True)
        self.assertEqual(result.value, {"server": "a", "value": 3})
        beta = ToolCall("b", "mcp.beta.echo", {"value": 4})
        self.assertEqual((await executor.aexecute(beta, frozenset({"mcp.alpha.call"}), approved=True)).error_code,
                         "permission_denied")
        self.assertEqual((await executor.aexecute(beta, manager.permissions, approved=True)).value["server"], "b")
        self.assertTrue(all(t.side_effect and t.requires_approval for t in registry.tools()))

    async def test_init_and_discovery_failure_are_independent_and_sanitized(self):
        for mode in ["fail-init", "fail-discovery"]:
            manager, registry, factory = self.manager(("alpha", mode, {}), ("beta", "healthy", {}))
            self.assertEqual([t.name for t in registry.tools()], ["mcp.beta.echo"])
            self.assertEqual([s["state"] for s in manager.status()], ["failed", "ready"])
            self.assertEqual(factory.clients[0].closed, 1)
            self.assertEqual(set(manager.status()[0]), {"name", "state", "error_code", "transport", "tools"})
            self.assertNotIn("credential", json.dumps(manager.status()))
            self.assertIn("mcp.alpha.call", manager.permissions)
            result = await ToolExecutor(registry).aexecute(
                ToolCall("b", "mcp.beta.echo", {"value": 3}), manager.permissions, approved=True)
            self.assertIsNone(result.error_code)

    async def test_aggregate_tool_limit_rejects_whole_service(self):
        manager, registry, factory = self.manager(("alpha", "many", {}), ("beta", "many", {}))
        self.assertEqual(len(registry.tools()), 101)
        self.assertEqual(manager.status()[1]["error_code"], "mcp_tool_limit")
        self.assertEqual(manager.status()[1]["tools"], [])
        self.assertEqual(factory.clients[1].closed, 1)

    async def test_failed_or_closed_client_is_unavailable_without_second_dispatch(self):
        manager, registry, factory = self.manager(("alpha", "unknown", {}), ("beta", "healthy", {}))
        executor = ToolExecutor(registry)
        call = ToolCall("a", "mcp.alpha.echo", {"value": 3})
        self.assertEqual((await executor.aexecute(call, manager.permissions, approved=True)).error_code, "execution_unknown")
        self.assertEqual((await executor.aexecute(call, manager.permissions, approved=True)).error_code, "tool_unavailable")
        self.assertEqual(len(factory.clients[0].calls), 1)
        self.assertEqual(manager.status()[0]["state"], "failed")
        self.assertEqual(manager.status()[1]["state"], "ready")
        manager.close()
        self.assertEqual((await executor.aexecute(call, manager.permissions, approved=True)).error_code, "tool_unavailable")

    async def test_legacy_snapshot_shape_names_policy_and_fail_fast(self):
        factory = Factory()
        manager = MCPServiceManager(parse_mcp_config({"command": ["healthy"]}), client_factory=factory)
        self.addCleanup(manager.close)
        registry = ToolRegistry()
        self.assertTrue(callable(manager.activate(registry)))
        tool, = registry.tools()
        self.assertEqual((tool.name, tool.permissions, tool.recovery_identity), ("mcp.echo", frozenset({"mcp.call"}), None))
        old = Tool("mcp.echo", tool.description, (), lambda **kwargs: None, input_schema=SCHEMA,
                   permissions=frozenset({"mcp.call"}), side_effect=True, requires_approval=True)
        self.assertEqual(Engine(EchoModel(), (tool,)).snapshot(), Engine(EchoModel(), (old,)).snapshot())
        for mode in ["fail-init", "fail-discovery"]:
            failed = MCPServiceManager(parse_mcp_config({"command": [mode]}), client_factory=Factory())
            with self.assertRaises(MCPError):
                failed.activate(ToolRegistry())

    async def test_close_all_and_activation_are_idempotent_without_reconnect(self):
        manager, registry, factory = self.manager(("alpha", "a", {}), ("beta", "b", {}))
        manager.close()
        manager.close()
        self.assertEqual([c.closed for c in factory.clients], [1, 1])
        self.assertEqual([s["state"] for s in manager.status()], ["closed", "closed"])
        with self.assertRaises(RuntimeError):
            manager.activate(registry)

    async def test_cleanup_failure_still_attempts_other_clients(self):
        factory = Factory()
        manager = MCPServiceManager(configuration(("alpha", "healthy", {}), ("beta", "fail-close", {})),
                                    client_factory=factory)
        manager.activate(ToolRegistry())
        with self.assertRaisesRegex(MCPError, "^mcp_cleanup_failed$"):
            manager.close()
        self.assertEqual([c.closed for c in factory.clients], [1, 1])
        manager.close()

    async def test_cleanup_incomplete_is_reported_after_all_services_close(self):
        class IncompleteClient(FakeClient):
            def close(self):
                super().close()
                if self.mode == "incomplete":
                    self.last_error = "mcp_cleanup_incomplete"

        factory = Factory(IncompleteClient)
        manager = MCPServiceManager(configuration(("alpha", "healthy", {}), ("beta", "incomplete", {})),
                                    client_factory=factory)
        manager.activate(ToolRegistry())
        with self.assertRaisesRegex(MCPError, "^mcp_cleanup_failed$"):
            manager.close()
        self.assertEqual([client.closed for client in factory.clients], [1, 1])
        self.assertEqual([item["state"] for item in manager.status()], ["closed", "closed"])
        self.assertIsNone(manager.status()[0]["error_code"])
        self.assertEqual(manager.status()[1]["error_code"], "mcp_cleanup_incomplete")
        manager.close()
        self.assertEqual(manager.status()[1]["error_code"], "mcp_cleanup_incomplete")

    async def test_startup_failure_preserves_incomplete_cleanup(self):
        class IncompleteClient(FakeClient):
            def close(self):
                super().close()
                if self.mode == "fail-init":
                    self.last_error = "mcp_cleanup_incomplete"

        factory = Factory(IncompleteClient)
        manager = MCPServiceManager(configuration(("alpha", "fail-init", {}), ("beta", "healthy", {})),
                                    client_factory=factory)
        registry = ToolRegistry()
        manager.activate(registry)
        self.assertEqual([tool.name for tool in registry.tools()], ["mcp.beta.echo"])
        self.assertEqual(manager.status()[0]["state"], "failed")
        self.assertEqual(manager.status()[0]["error_code"], "mcp_cleanup_incomplete")
        with self.assertRaisesRegex(MCPError, "^mcp_cleanup_failed$"):
            manager.close()
        self.assertEqual(factory.clients[1].closed, 1)
        self.assertEqual(manager.status()[0]["error_code"], "mcp_cleanup_incomplete")

    async def test_registration_failure_rolls_back_only_own_tools_and_closes_all(self):
        factory = Factory()
        manager = MCPServiceManager(configuration(("alpha", "a", {}), ("beta", "b", {})), client_factory=factory)
        existing = Tool("mcp.beta.echo", "existing", (), lambda: 1)
        registry = ToolRegistry((existing,))
        with self.assertRaises(ValueError):
            manager.activate(registry)
        self.assertEqual(registry.tools(), (existing,))
        self.assertEqual([c.closed for c in factory.clients], [1, 1])

    async def test_mutated_status_does_not_change_frozen_tools(self):
        manager, registry, factory = self.manager(("alpha", "healthy", {}))
        status = manager.status()
        status[0]["tools"].clear()
        status[0]["state"] = "closed"
        self.assertEqual(manager.status()[0]["tools"], ["mcp.alpha.echo"])
        self.assertEqual(manager.status()[0]["state"], "ready")
        self.assertNotIn("recovery_identity", json.dumps(registry.specs()[0].schema()))

    async def test_local_and_mcp_share_registry_executor_without_hot_removal(self):
        local = Tool("local.echo", "local numeric echo", ("value",), lambda value: value,
                     permissions=frozenset({"local.read"}))
        registry = ToolRegistry((local,))
        factory = Factory()
        manager = MCPServiceManager(configuration(("alpha", "healthy", {})), client_factory=factory)
        self.addCleanup(manager.close)
        manager.activate(registry)
        registry.freeze()
        executor = ToolExecutor(registry)
        call = ToolCall("local", "local.echo", {"value": 5})
        self.assertEqual((await executor.aexecute(call, manager.permissions)).error_code, "permission_denied")
        permissions = manager.permissions | {"local.read"}
        self.assertEqual((await executor.aexecute(call, permissions)).value, 5)
        remote = ToolCall("remote", "mcp.alpha.echo", {"value": 3})
        self.assertEqual((await executor.aexecute(remote, permissions)).error_code, "approval_required")
        manager.close()
        self.assertEqual([tool.name for tool in registry.tools()], ["local.echo", "mcp.alpha.echo"])
        self.assertEqual((await executor.aexecute(remote, permissions, approved=True)).error_code, "tool_unavailable")
        self.assertEqual((await executor.aexecute(call, permissions)).value, 5)
        self.assertEqual(factory.clients[0].calls, [])

    async def test_approval_is_bound_to_arguments(self):
        manager, registry, factory = self.manager(("alpha", "healthy", {}))
        engine = Engine(EchoModel(), registry.tools())
        state = engine.create("echo", permissions=manager.permissions)
        await engine.advance(state)
        await engine.advance(state)
        self.assertEqual(state["status"], "waiting_approval")
        state["approved"].append(state["approval"]["key"])
        state["pending"][0]["arguments"]["value"] = 4
        state["status"] = "pending"
        await engine.advance(state)
        self.assertEqual(state["status"], "waiting_approval")
        self.assertEqual(factory.clients[0].calls, [])

    async def test_pending_resume_rejects_changed_command_revision_and_timeout(self):
        manager, registry, factory = self.manager(("alpha", "healthy", {}))
        engine = Engine(EchoModel(), registry.tools())
        state = engine.create("echo", permissions=manager.permissions)
        await engine.advance(state)
        await engine.advance(state)
        for mode, options in [("other", {}), ("healthy", {"revision": "2"}),
                              ("healthy", {"timeout": 11})]:
            changed, changed_registry, changed_factory = self.manager(("alpha", mode, options))
            saved = deepcopy(state)
            saved["approved"].append(saved["approval"]["key"])
            saved["status"] = "pending"
            await Engine(EchoModel(), changed_registry.tools()).advance(saved)
            self.assertEqual((saved["status"], saved["error"]), ("failed", "tool_snapshot_changed"))
            self.assertEqual(changed_factory.clients[0].calls, [])

    async def test_pending_resume_rejects_schema_change_with_same_configuration(self):
        manager, registry, _ = self.manager(("alpha", "healthy", {}))
        engine = Engine(EchoModel(), registry.tools())
        state = engine.create("echo", permissions=manager.permissions)
        await engine.advance(state)
        await engine.advance(state)

        class ChangedSchemaClient(FakeClient):
            def discover_tools(self):
                self.mode = "changed-schema"
                return super().discover_tools()

        changed, changed_registry, changed_factory = self.manager(
            ("alpha", "healthy", {}), factory=Factory(ChangedSchemaClient))
        self.assertEqual(registry.tools()[0].recovery_identity, changed_registry.tools()[0].recovery_identity)
        state["approved"].append(state["approval"]["key"])
        state["status"] = "pending"
        await Engine(EchoModel(), changed_registry.tools()).advance(state)
        self.assertEqual((state["status"], state["error"]), ("failed", "tool_snapshot_changed"))
        self.assertEqual(changed_factory.clients[0].calls, [])

    async def test_cwd_is_frozen_for_launch_and_changed_cwd_rejects_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            first_cwd = Path(directory) / "first"
            second_cwd = Path(directory) / "second"
            first_cwd.mkdir()
            second_cwd.mkdir()
            with patch("astra_harness.builtin.mcp_services.Path.cwd", return_value=first_cwd):
                config = configuration(("alpha", "relative-server", {}))
            factory = Factory()
            manager = MCPServiceManager(config, client_factory=factory)
            self.addCleanup(manager.close)
            registry = ToolRegistry()
            with patch("astra_harness.builtin.mcp_services.Path.cwd", return_value=second_cwd):
                manager.activate(registry)
                changed, changed_registry, changed_factory = self.manager(("alpha", "relative-server", {}))
            self.assertEqual(factory.clients[0].cwd, str(first_cwd.resolve()))
            self.assertEqual(changed_factory.clients[0].cwd, str(second_cwd.resolve()))
            self.assertNotIn(str(first_cwd), json.dumps(manager.status()))
            engine = Engine(EchoModel(), registry.tools())
            state = engine.create("echo", permissions=manager.permissions)
            await engine.advance(state)
            await engine.advance(state)
            state["approved"].append(state["approval"]["key"])
            state["status"] = "pending"
            await Engine(EchoModel(), changed_registry.tools()).advance(state)
            self.assertEqual((state["status"], state["error"]), ("failed", "tool_snapshot_changed"))
            self.assertEqual(changed_factory.clients[0].calls, [])


class PersistenceTests(unittest.TestCase):
    def test_unknown_is_not_replayed_after_new_manager_and_application(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runs.sqlite3"
            config = configuration(("alpha", "unknown", {}))
            first_factory = Factory()
            first = MCPServiceManager(config, client_factory=first_factory)
            registry = ToolRegistry()
            first.activate(registry)
            with Application(EchoModel(), registry.tools(), database=database, permissions=first.permissions) as app:
                run = app.wait(app.submit("echo")['id'])
                self.assertEqual(run["status"], "waiting_approval")
                app.approve(run["id"])
                self.assertEqual(app.wait(run["id"])["status"], "unknown")
            first.close()
            second_factory = Factory()
            second = MCPServiceManager(config, client_factory=second_factory)
            registry = ToolRegistry()
            second.activate(registry)
            try:
                with Application(EchoModel(), registry.tools(), database=database, permissions=second.permissions) as app:
                    self.assertEqual(app.get_run(run["id"])["status"], "unknown")
                    with self.assertRaises(RuntimeError):
                        app.submit("must remain blocked")
                    self.assertEqual(second_factory.clients[0].calls, [])
            finally:
                second.close()

    def test_close_during_start_never_registers_late_tools(self):
        started, released = threading.Event(), threading.Event()

        class SlowClient(FakeClient):
            def start(self):
                self.state = "starting"
                started.set()
                released.wait(2)
                if self.state == "closed":
                    raise MCPError("mcp_closed")
                self.state = "ready"
                return self

            def close(self):
                super().close()
                released.set()

        factory = Factory(SlowClient)
        manager = MCPServiceManager(configuration(("alpha", "slow", {})), client_factory=factory)
        registry = ToolRegistry()
        errors = []

        def activate():
            try:
                manager.activate(registry)
            except RuntimeError as error:
                errors.append(str(error))

        worker = threading.Thread(target=activate)
        worker.start()
        self.assertTrue(started.wait(1))
        manager.close()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(registry.tools(), ())
        self.assertEqual(errors, ["mcp_services_closed"])


if __name__ == "__main__":
    unittest.main()
