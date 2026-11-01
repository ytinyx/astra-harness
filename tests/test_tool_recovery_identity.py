import asyncio
from copy import deepcopy
from dataclasses import replace
import unittest

from astra_harness import Harness, ModelResponse, Tool, ToolCall
from astra_harness.engine import Engine
from astra_harness.tool_identity import build_tool_identity, identity_difference, validate_tool_identity
from astra_harness.tools import ToolExecutor, ToolRegistry, ToolUnavailable


def add(value: int):
    return value + 1


class ToolRecoveryIdentityTests(unittest.TestCase):
    def test_optional_binding_preserves_default_snapshot_and_is_not_model_input(self):
        tool = Tool("add", "Add", ("value",), add, implementation_version="1")
        original = Engine(None, (tool,)).snapshot()
        self.assertEqual(original, [{"name": "add", "description": "Add", "schema": tool.spec().schema(),
                                     "permissions": [], "side_effect": False, "requires_approval": False}])
        bound = replace(tool, recovery_identity="service-configuration-v1")
        self.assertEqual(bound.spec(), tool.spec())
        expected = deepcopy(original)
        expected[0]["recovery_identity"] = "service-configuration-v1"
        self.assertEqual(Engine(None, (bound,)).snapshot(), expected)

    def test_binding_validation_is_explicit(self):
        for value in ("", " ", 42, True, [], "x" * 129):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Tool("add", "Add", ("value",), add, recovery_identity=value)

    def test_benchmark_contract_includes_binding_and_reads_unbound_descriptor(self):
        tool = Tool("add", "Add", ("value",), add, implementation_version="1")
        before = build_tool_identity((tool,))
        self.assertNotIn("recovery_identity", before["tools"][0])
        validate_tool_identity(before)
        after = build_tool_identity((replace(tool, recovery_identity="one"),))
        other = build_tool_identity((replace(tool, recovery_identity="two"),))
        self.assertNotEqual(before["sha256"], after["sha256"])
        self.assertNotEqual(after["sha256"], other["sha256"])
        self.assertIn("recovery_identity", identity_difference(after, before))
        self.assertNotIn("one", identity_difference(after, before))
        for value in (None, "", True, "x" * 129):
            changed = deepcopy(after)
            changed["tools"][0]["recovery_identity"] = value
            with self.assertRaisesRegex(ValueError, "recovery identity"):
                validate_tool_identity(changed)

    def test_confirmed_unavailable_is_fixed_error_after_admission(self):
        calls = []

        def unavailable():
            calls.append("checked")
            raise ToolUnavailable("private transport detail")

        tool = Tool("remote", "Remote", (), unavailable, input_schema={"type": "object", "properties": {}},
                    permissions=frozenset({"remote.call"}), requires_approval=True, side_effect=True)
        executor = ToolExecutor(ToolRegistry((tool,)))
        call = ToolCall("c", "remote", {})
        self.assertEqual(executor.execute(call).error_code, "permission_denied")
        self.assertEqual(executor.execute(call, frozenset({"remote.call"})).error_code, "approval_required")
        self.assertEqual(calls, [])
        result = executor.execute(call, frozenset({"remote.call"}), approved=True)
        self.assertEqual(result.error_code, "tool_unavailable")
        self.assertIsNone(result.value)
        self.assertEqual(calls, ["checked"])

    def test_async_unavailable_is_not_unknown(self):
        async def unavailable():
            raise ToolUnavailable("private detail")

        tool = Tool("remote", "Remote", (), unavailable, input_schema={"type": "object", "properties": {}},
                    side_effect=True)
        result = asyncio.run(ToolExecutor(ToolRegistry((tool,))).aexecute(ToolCall("c", "remote", {})))
        self.assertEqual(result.error_code, "tool_unavailable")

    def test_engine_can_report_predispatch_unavailability_without_unknown(self):
        def unavailable():
            raise ToolUnavailable("connection already closed; nothing sent")

        class Model:
            def complete(self, request):
                result = request.messages[-1].tool_result
                if result is None:
                    return ModelResponse(tool_calls=(ToolCall("c", "remote", {}),))
                return ModelResponse(text=result.error_code)

        tool = Tool("remote", "Remote", (), unavailable, input_schema={"type": "object", "properties": {}},
                    side_effect=True)
        result = Harness(Model(), (tool,)).run("Check availability")
        self.assertEqual(result.reason, "completed")
        self.assertEqual(result.output, "tool_unavailable")
        self.assertEqual((result.model_calls, result.tool_calls), (2, 1))


if __name__ == "__main__":
    unittest.main()
