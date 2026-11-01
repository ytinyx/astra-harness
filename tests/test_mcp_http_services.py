"""HTTP and STDIO adapters share one registry, approval and recovery contract.

These tests deliberately use inert transport doubles: protocol and sockets are
verified separately by test_mcp_http, while host policy must precede any call.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from astra_harness.builtin.mcp import MCPError
from astra_harness.builtin.mcp_services import (
    MAX_MESSAGE_BYTES, MCPServiceConfig, MCPServiceManager, load_mcp_config, parse_mcp_config,
)
from astra_harness.contracts import ToolCall
from astra_harness.engine import Engine
from astra_harness.tools import Tool, ToolExecutionUnknown, ToolExecutor, ToolRegistry


ENDPOINT = "http://127.0.0.1:8766/mcp"
SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}},
          "required": ["value"], "additionalProperties": False}


def http_config(**changes):
    value = {"name": "alpha", "transport": "http", "endpoint": ENDPOINT}
    value.update(changes)
    return value


class Client:
    def __init__(self, *, failure=None, changed_schema=False):
        self.state, self.last_error = "new", None
        self.calls, self.closed = [], 0
        self.failure, self.changed_schema = failure, changed_schema

    def start(self):
        if self.failure == "start":
            raise MCPError("private-url-or-session-detail")
        self.state = "ready"
        return self

    def discover_tools(self):
        schema = deepcopy(SCHEMA)
        if self.changed_schema:
            schema["properties"]["value"]["minimum"] = 2
        return (Tool("mcp.echo", "Synthetic echo", (), lambda **kwargs: None, input_schema=schema),)

    async def acall_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.failure == "unknown":
            self.state, self.last_error = "failed", "mcp_session_expired"
            raise ToolExecutionUnknown("mcp_execution_unknown")
        return arguments["value"]

    def close(self):
        if self.state != "closed":
            self.closed += 1
            self.state = "closed"
        if self.failure == "close":
            self.last_error = "mcp_cleanup_incomplete"


class Factory:
    def __init__(self, **options):
        self.options, self.arguments, self.clients = options, [], []

    def __call__(self, target, **options):
        self.arguments.append((target, options))
        client = Client(**self.options)
        self.clients.append(client)
        return client


class NoModel:
    def complete(self, request):
        raise AssertionError("direct tool operations cannot reach a model")


class HTTPServiceConfigTests(unittest.TestCase):
    def test_mixed_config_is_static_sorted_and_explicit(self):
        with patch("astra_harness.builtin.mcp_http.StreamableHTTPMCPClient") as constructor:
            configs = parse_mcp_config({"servers": [
                {"name": "beta", "command": ["synthetic"], "transport": "stdio"}, http_config(),
            ]})
        constructor.assert_not_called()
        self.assertEqual([c.name for c in configs], ["alpha", "beta"])
        self.assertEqual([c.transport for c in configs], ["http", "stdio"])
        self.assertEqual((configs[0].command, configs[0].endpoint), ((), ENDPOINT))
        self.assertEqual(configs[0].permission, "mcp.alpha.call")
        self.assertNotIn(ENDPOINT, repr(configs[0]))

    def test_ambiguous_transport_targets_and_untrusted_endpoints_are_rejected(self):
        invalid = [http_config(command=[]), http_config(command=["synthetic"]),
                   http_config(transport="stdio"), http_config(transport="sse"),
                   http_config(transport=None), http_config(transport=True),
                   http_config(headers={"Authorization": "synthetic"}),
                   {"name": "alpha", "endpoint": ENDPOINT},
                   {"name": "alpha", "transport": "http"},
                   {"name": "alpha", "transport": "http", "command": []}]
        invalid += [http_config(endpoint=value) for value in [
            None, "https://example.com/mcp", "http://localhost:8766/mcp",
            "http://127.0.0.1:8766/mcp?token=synthetic", "http://user@127.0.0.1:8766/mcp",
            "http://127.0.0.1:8766/mcp#fragment", "http://192.168.1.2:8766/mcp",
        ]]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                parse_mcp_config({"servers": [item]})
        with self.assertRaises(ValueError):
            parse_mcp_config({"servers": [http_config(), {"name": "alpha", "command": ["x"]}]})
        with self.assertRaises(ValueError):
            MCPServiceConfig("legacy", transport="http", endpoint=ENDPOINT, legacy=True)
        with self.assertRaises(ValueError):
            MCPServiceConfig("alpha", ("x",), endpoint=ENDPOINT)

    def test_stdio_identity_retains_the_previous_payload(self):
        config = MCPServiceConfig("alpha", ("synthetic",), 10, "1")
        previous = {
            "name": "alpha", "prefix": "mcp.alpha", "transport": "stdio",
            "protocol": "2025-06-18", "command": ["synthetic"], "cwd": config.cwd,
            "timeout": 10.0, "max_message_bytes": 1_000_000, "max_tools": 200, "revision": "1",
            "policy": {"permissions": ["mcp.alpha.call"], "side_effect": True, "requires_approval": True},
        }
        expected = hashlib.sha256(json.dumps(previous, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=False).encode("utf-8")).hexdigest()
        self.assertEqual(config.recovery_identity(), expected)

    def test_http_identity_is_order_stable_target_bound_and_cwd_independent(self):
        first, = parse_mcp_config({"servers": [http_config()]})
        same, = parse_mcp_config({"servers": [{"revision": "1", "timeout": 10.0,
                                              "endpoint": ENDPOINT, "transport": "http", "name": "alpha"}]})
        self.assertEqual(first.recovery_identity(), same.recovery_identity())
        with tempfile.TemporaryDirectory() as directory:
            with patch("astra_harness.builtin.mcp_services.Path.cwd", return_value=Path(directory)):
                another_cwd, = parse_mcp_config({"servers": [http_config()]})
            self.assertEqual(first.recovery_identity(), another_cwd.recovery_identity())
        for change in [{"endpoint": "http://127.0.0.1:8767/mcp"}, {"revision": "2"},
                       {"timeout": 11}, {"name": "beta"}]:
            with self.subTest(change=change):
                other, = parse_mcp_config({"servers": [http_config(**change)]})
                self.assertNotEqual(first.recovery_identity(), other.recovery_identity())

    def test_loader_errors_do_not_expose_endpoint_details(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(json.dumps({"servers": [http_config(endpoint=ENDPOINT+"?key=synthetic-private")]}),
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "^invalid_mcp_config$"):
                load_mcp_config(path)


class HTTPServicePolicyTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, items=None, *, http_options=None, stdio_options=None):
        configs = parse_mcp_config({"servers": items or [http_config(), {"name": "beta", "command": ["synthetic"]}]})
        http, stdio = Factory(**(http_options or {})), Factory(**(stdio_options or {}))
        manager = MCPServiceManager(configs, client_factory=stdio, http_client_factory=http)
        registry = ToolRegistry()
        manager.activate(registry)
        self.addCleanup(manager.close)
        return manager, registry, http, stdio

    async def test_factories_receive_only_their_transport_arguments(self):
        manager, registry, http, stdio = self.manager()
        self.assertEqual(http.arguments, [(ENDPOINT, {"timeout": 10.0, "max_message_bytes": MAX_MESSAGE_BYTES})])
        self.assertEqual(stdio.arguments[0][0], ["synthetic"])
        self.assertIn("cwd", stdio.arguments[0][1])
        self.assertEqual([s["transport"] for s in manager.status()], ["http", "stdio"])
        self.assertEqual([t.name for t in registry.tools()], ["mcp.alpha.echo", "mcp.beta.echo"])
        self.assertNotIn(ENDPOINT, json.dumps(manager.status()))
        self.assertTrue(all(t.side_effect and t.requires_approval for t in registry.tools()))

    async def test_http_dispatch_uses_existing_argument_permission_and_approval_checks(self):
        manager, registry, http, stdio = self.manager()
        executor = ToolExecutor(registry)
        call = ToolCall("http", "mcp.alpha.echo", {"value": 3})
        for permissions, approved, expected in [(frozenset(), True, "permission_denied"),
                                                 (frozenset({"mcp.beta.call"}), True, "permission_denied"),
                                                 (manager.permissions, False, "approval_required")]:
            result = await executor.aexecute(call, permissions, approved=approved)
            self.assertEqual(result.error_code, expected)
        invalid = ToolCall("invalid", "mcp.alpha.echo", {"value": True})
        self.assertEqual((await executor.aexecute(invalid, manager.permissions, approved=True)).error_code,
                         "invalid_arguments")
        self.assertEqual(http.clients[0].calls, [])
        self.assertEqual((await executor.aexecute(call, manager.permissions, approved=True)).value, 3)
        self.assertEqual(http.clients[0].calls, [("echo", {"value": 3})])
        self.assertEqual(stdio.clients[0].calls, [])

    async def test_one_transport_startup_failure_does_not_disable_the_other(self):
        for failed in ("http", "stdio"):
            with self.subTest(failed=failed):
                manager, registry, http, stdio = self.manager(
                    http_options={"failure": "start"} if failed == "http" else {},
                    stdio_options={"failure": "start"} if failed == "stdio" else {})
                expected = "mcp.beta.echo" if failed == "http" else "mcp.alpha.echo"
                self.assertEqual([tool.name for tool in registry.tools()], [expected])
                status = manager.status()[0 if failed == "http" else 1]
                self.assertEqual((status["state"], status["error_code"]), ("failed", "mcp_service_failed"))
                self.assertNotIn("private", json.dumps(manager.status()))
                client = http.clients[0] if failed == "http" else stdio.clients[0]
                self.assertEqual(client.closed, 1)

    async def test_unknown_http_result_does_not_replay_or_break_stdio(self):
        manager, registry, http, stdio = self.manager(http_options={"failure": "unknown"})
        executor = ToolExecutor(registry)
        call = ToolCall("http", "mcp.alpha.echo", {"value": 3})
        self.assertEqual((await executor.aexecute(call, manager.permissions, approved=True)).error_code,
                         "execution_unknown")
        self.assertEqual((await executor.aexecute(call, manager.permissions, approved=True)).error_code,
                         "tool_unavailable")
        self.assertEqual(len(http.clients[0].calls), 1)
        self.assertEqual(manager.status()[0]["error_code"], "mcp_session_expired")
        self.assertEqual(manager.status()[1]["state"], "ready")
        other = ToolCall("stdio", "mcp.beta.echo", {"value": 4})
        self.assertEqual((await executor.aexecute(other, manager.permissions, approved=True)).value, 4)

    async def test_pending_http_approval_rejects_endpoint_revision_timeout_or_schema_change(self):
        manager, registry, http, _ = self.manager()
        engine = Engine(NoModel(), registry.tools())
        state = engine.create_tool("mcp.alpha.echo", {"value": 3}, permissions=manager.permissions)
        await engine.advance(state)
        self.assertEqual(state["status"], "waiting_approval")
        for change, options in [({"endpoint": "http://127.0.0.1:8767/mcp"}, {}),
                                ({"revision": "2"}, {}), ({"timeout": 11}, {}),
                                ({}, {"changed_schema": True})]:
            with self.subTest(change=change, options=options):
                changed, changed_registry, changed_http, _ = self.manager(
                    [http_config(**change), {"name": "beta", "command": ["synthetic"]}],
                    http_options=options)
                saved = deepcopy(state)
                saved["approved"].append(saved["approval"]["key"])
                saved["status"] = "pending"
                await Engine(NoModel(), changed_registry.tools()).advance(saved)
                self.assertEqual((saved["status"], saved["error"]), ("failed", "tool_snapshot_changed"))
                self.assertEqual(changed_http.clients[0].calls, [])
                self.assertEqual(registry.tools()[1].recovery_identity, changed_registry.tools()[1].recovery_identity)

    async def test_argument_changes_cannot_reuse_http_approval(self):
        manager, registry, http, _ = self.manager()
        engine = Engine(NoModel(), registry.tools())
        state = engine.create_tool("mcp.alpha.echo", {"value": 3}, permissions=manager.permissions)
        await engine.advance(state)
        state["approved"].append(state["approval"]["key"])
        state["pending"][0]["arguments"]["value"] = 4
        state["status"] = "pending"
        await engine.advance(state)
        self.assertEqual(state["status"], "waiting_approval")
        self.assertEqual(http.clients[0].calls, [])

    async def test_close_is_idempotent_and_unavailable_tools_remain_frozen(self):
        manager, registry, http, stdio = self.manager()
        registry.freeze()
        manager.close()
        manager.close()
        self.assertEqual([http.clients[0].closed, stdio.clients[0].closed], [1, 1])
        self.assertEqual([s["state"] for s in manager.status()], ["closed", "closed"])
        call = ToolCall("http", "mcp.alpha.echo", {"value": 3})
        self.assertEqual((await ToolExecutor(registry).aexecute(call, manager.permissions, approved=True)).error_code,
                         "tool_unavailable")
        self.assertEqual(len(registry.tools()), 2)

    async def test_incomplete_http_cleanup_still_closes_stdio(self):
        manager, registry, http, stdio = self.manager(http_options={"failure": "close"})
        with self.assertRaisesRegex(MCPError, "^mcp_cleanup_failed$"):
            manager.close()
        self.assertEqual([http.clients[0].closed, stdio.clients[0].closed], [1, 1])
        self.assertEqual(manager.status()[0]["error_code"], "mcp_cleanup_incomplete")
        manager.close()

    async def test_collision_rolls_back_both_transport_tools_and_preserves_existing(self):
        configs = parse_mcp_config({"servers": [http_config(), {"name": "beta", "command": ["synthetic"]}]})
        http, stdio = Factory(), Factory()
        manager = MCPServiceManager(configs, client_factory=stdio, http_client_factory=http)
        existing = Tool("mcp.beta.echo", "Already registered", (), lambda: 1)
        registry = ToolRegistry((existing,))
        with self.assertRaises(ValueError):
            manager.activate(registry)
        self.assertEqual(registry.tools(), (existing,))
        self.assertEqual([http.clients[0].closed, stdio.clients[0].closed], [1, 1])


if __name__ == "__main__":
    unittest.main()
