import asyncio
import inspect
import unittest
from typing import Annotated, Any, Literal, Optional

from astra_harness import ModelResponse, Tool, ToolCall, tool_from_function
from astra_harness.engine import Engine
from astra_harness.tool_identity import build_tool_identity
from astra_harness.tools import ToolExecutor, ToolRegistry


POLICY = {"permissions": frozenset(), "side_effect": False, "requires_approval": False,
          "implementation_version": "1"}


def summarize(values: list[int], prefix: str = "total", *, scale: float = 1):
    """Summarize integer values with an optional label."""
    return {"label": prefix, "total": sum(values) * scale, "scale_type": type(scale).__name__}


def add(a: int, b: int = 2):
    return a + b


def subtract(a: int, b: int = 2):
    return a - b


async def async_add(a: int, b: int = 2):
    await asyncio.sleep(0)
    return a + b


class ToolingTests(unittest.TestCase):
    def execute(self, function, arguments, **overrides):
        tool = tool_from_function(function, **{**POLICY, **overrides})
        return ToolExecutor(ToolRegistry((tool,))).execute(ToolCall("call-1", tool.name, arguments))

    def test_public_factory_retains_function_and_defaults(self):
        tool = tool_from_function(summarize, **POLICY)
        self.assertIs(tool.handler, summarize)
        self.assertEqual(tool.name, "summarize")
        self.assertEqual(tool.description, inspect.getdoc(summarize))
        self.assertEqual(tool.parameters, ("values", "prefix", "scale"))
        self.assertEqual(tool.spec().schema()["required"], ["values"])
        result = ToolExecutor(ToolRegistry((tool,))).execute(ToolCall("call-1", tool.name, {"values": [2, 3]}))
        self.assertIsNone(result.error_code)
        self.assertEqual(result.value, {"label": "total", "total": 5, "scale_type": "int"})
        self.assertEqual(result.value, summarize([2, 3]))

    def test_explicit_arguments_are_not_coerced(self):
        result = self.execute(summarize, {"values": [2, 3], "prefix": "sum", "scale": 0.5})
        self.assertEqual(result.value, {"label": "sum", "total": 2.5, "scale_type": "float"})
        for arguments in ({"values": [True]}, {"values": ["2"]}, {"values": [2.0]},
                          {"values": [2], "scale": True}, {"values": [2], "scale": "2"},
                          {"values": [2], "unknown": 1}, {}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.execute(summarize, arguments).error_code, "invalid_arguments")

    def test_required_keyword_only_null_and_nested_list(self):
        def describe(values: list[list[int]], *, enabled: bool, empty: None):
            return {"values": values, "enabled": enabled, "empty": empty}

        self.assertEqual(self.execute(describe, {"values": [[1], []], "enabled": False, "empty": None}).value,
                         {"values": [[1], []], "enabled": False, "empty": None})
        self.assertEqual(self.execute(describe, {"values": [[1]], "enabled": 0, "empty": None}).error_code,
                         "invalid_arguments")
        self.assertEqual(self.execute(describe, {"values": [], "enabled": True}).error_code, "invalid_arguments")

    def test_literal_membership_keeps_scalar_types(self):
        def choose(mode: Literal["brief", "full"] = "brief", level: Literal[1, 2] = 1,
                   enabled: Literal[True] = True, empty: Literal[None] = None):
            return [mode, level, enabled, empty]

        self.assertEqual(self.execute(choose, {}).value, ["brief", 1, True, None])
        for arguments in ({"mode": "other"}, {"level": True}, {"level": 1.0}, {"enabled": 1}):
            self.assertEqual(self.execute(choose, arguments).error_code, "invalid_arguments")

    def test_permission_approval_and_arguments_prevent_execution(self):
        calls = []

        def write(value: int):
            calls.append(value)
            return {"written": value}

        tool = tool_from_function(write, permissions=frozenset({"report.write"}), side_effect=True,
                                  requires_approval=True, implementation_version="1")
        executor = ToolExecutor(ToolRegistry((tool,)))
        call = ToolCall("c", tool.name, {"value": 7})
        self.assertTrue(tool.side_effect)
        self.assertEqual(executor.execute(call, approved=True).error_code, "permission_denied")
        self.assertEqual(executor.execute(call, frozenset({"report.write"})).error_code, "approval_required")
        self.assertEqual(executor.execute(ToolCall("c", tool.name, {"value": True}),
                                          frozenset({"report.write"}), approved=True).error_code, "invalid_arguments")
        self.assertEqual(calls, [])
        self.assertEqual(executor.execute(call, frozenset({"report.write"}), approved=True).value, {"written": 7})
        self.assertEqual(calls, [7])

    def test_async_handler_runs_through_engine(self):
        tool = tool_from_function(async_add, **POLICY)
        self.assertIs(tool.handler, async_add)
        self.assertTrue(inspect.iscoroutinefunction(tool.handler))

        class Model:
            def complete(self, request):
                if request.messages[-1].role == "tool":
                    return ModelResponse(text=str(request.messages[-1].tool_result.value))
                return ModelResponse(tool_calls=(ToolCall("c", "async_add", {"a": 3}),))

        engine = Engine(Model(), (tool,))
        state = engine.create("calculate")
        while state["status"] not in {"completed", "failed", "unknown", "cancelled"}:
            asyncio.run(engine.advance(state))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["output"], "5")
        self.assertEqual((state["model_calls"], state["tool_calls"]), (2, 1))

    def test_sync_executor_preserves_async_boundary(self):
        self.assertEqual(self.execute(async_add, {"a": 3}).error_code, "async_tool_requires_engine")

    def test_future_annotations_sync_and_async(self):
        namespace = {}
        exec("from __future__ import annotations\n"
             "from typing import Literal\n"
             "def combine(values: list[int], mode: Literal['sum'] = 'sum') -> dict[str, int]:\n"
             "    return {'value': sum(values)}\n"
             "async def double(value: int = 3) -> int:\n"
             "    return value * 2\n", namespace)
        sync_tool = tool_from_function(namespace["combine"], **POLICY)
        async_tool = tool_from_function(namespace["double"], **POLICY)
        self.assertIs(sync_tool.handler, namespace["combine"])
        self.assertEqual(ToolExecutor(ToolRegistry((sync_tool,))).execute(
            ToolCall("c", sync_tool.name, {"values": [2, 3]})).value, {"value": 5})
        self.assertEqual(asyncio.run(ToolExecutor(ToolRegistry((async_tool,))).aexecute(
            ToolCall("c", async_tool.name, {}))).value, 6)

    def test_unresolved_input_is_rejected_but_return_annotation_is_not_a_gate(self):
        namespace = {}
        exec("from __future__ import annotations\n"
             "def missing(value: MissingInput): return value\n"
             "def return_only(value: int) -> MissingOutput: return {'value': value}\n", namespace)
        with self.assertRaisesRegex(ValueError, "resolve"):
            tool_from_function(namespace["missing"], **POLICY)
        self.assertEqual(self.execute(namespace["return_only"], {"value": 4}).value, {"value": 4})

    def test_return_annotation_does_not_replace_json_result_gate(self):
        def invalid(value: int) -> str:
            return {value}

        self.assertEqual(self.execute(invalid, {"value": 3}).error_code, "invalid_tool_result")

    def test_evaluated_annotation_is_not_reinterpreted_after_global_rebinding(self):
        namespace = {}
        exec("def typed(value): return value\n", namespace)
        function = namespace["typed"]
        function.__annotations__ = {"value": int}
        namespace["int"] = str
        tool = tool_from_function(function, **POLICY)
        self.assertIs(function.__annotations__["value"], int)
        self.assertEqual(tool.spec().schema()["properties"]["value"], {"type": "integer"})
        executor = ToolExecutor(ToolRegistry((tool,)))
        self.assertEqual(executor.execute(ToolCall("c", tool.name, {"value": 2})).value, 2)
        self.assertEqual(executor.execute(ToolCall("c", tool.name, {"value": "2"})).error_code, "invalid_arguments")

    def test_mutable_defaults_are_rejected_without_execution(self):
        for default in ([], {}, (), set()):
            def function(value: list[int] = default):
                raise AssertionError("registration must not execute the function")

            with self.subTest(default=type(default).__name__), self.assertRaisesRegex(ValueError, "immutable"):
                tool_from_function(function, **POLICY)

    def test_invalid_default_is_rejected(self):
        for annotation, default in ((int, True), (int, "1"), (str, None), (float, float("inf")),
                                    (float, float("nan")), (Literal["yes"], "no")):
            def function(value=default):
                return value

            function.__annotations__ = {"value": annotation}
            with self.subTest(annotation=annotation, default=default), self.assertRaises(ValueError):
                tool_from_function(function, **POLICY)

    def test_unsupported_types_are_rejected_early(self):
        for annotation in (Any, list, dict[str, int], tuple[int, int], Optional[int], int | str,
                           Literal["one", 1], Literal[1.0], set[int], Annotated[int, "positive"]):
            def function(value):
                raise AssertionError("registration must not execute the function")

            function.__annotations__ = {"value": annotation}
            with self.subTest(annotation=annotation), self.assertRaises(ValueError):
                tool_from_function(function, **POLICY)

    def test_signature_errors_are_rejected(self):
        def positional(value: int, /):
            return value

        def variadic(*values: int):
            return values

        def mapping(**values: int):
            return values

        def missing(value):
            return value

        for function in (positional, variadic, mapping, missing):
            with self.subTest(function=function.__name__), self.assertRaises(ValueError):
                tool_from_function(function, **POLICY)

    def test_generators_and_callable_instances_are_rejected(self):
        def generator(value: int):
            yield value

        async def async_generator(value: int):
            yield value

        class Callable:
            def __call__(self, value: int):
                return value

        for function in (generator, async_generator, Callable(), len, lambda value: value):
            with self.subTest(function=type(function).__name__), self.assertRaises(ValueError):
                tool_from_function(function, **POLICY)

    def test_custom_signature_is_not_trusted_over_actual_handler(self):
        def function():
            return 1

        function.__signature__ = inspect.Signature()
        with self.assertRaisesRegex(ValueError, "custom"):
            tool_from_function(function, **POLICY)

    def test_all_policy_fields_are_required_and_validated(self):
        for field in POLICY:
            policy = {key: value for key, value in POLICY.items() if key != field}
            with self.subTest(missing=field), self.assertRaises(TypeError):
                tool_from_function(add, **policy)
        for field, value in (("permissions", {"write"}), ("permissions", "write"),
                             ("side_effect", 1), ("requires_approval", "yes"),
                             ("implementation_version", None), ("implementation_version", ""),
                             ("implementation_version", "x" * 129)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                tool_from_function(add, **{**POLICY, field: value})

    def test_name_description_and_schema_copy(self):
        tool = tool_from_function(add, name="math.add", description="Explicit description", **POLICY)
        self.assertEqual((tool.name, tool.description), ("math.add", "Explicit description"))
        schema = tool.spec().schema()
        schema["properties"]["b"]["default"] = 99
        self.assertEqual(tool.spec().schema()["properties"]["b"]["default"], 2)
        self.assertEqual(ToolExecutor(ToolRegistry((tool,))).execute(ToolCall("c", "math.add", {"a": 3})).value, 5)

    def test_deep_annotation_is_rejected_at_registration(self):
        annotation = int
        for _ in range(17):
            annotation = list[annotation]

        def function(value):
            return value

        function.__annotations__ = {"value": annotation}
        with self.assertRaises(ValueError):
            tool_from_function(function, **POLICY)

    def test_empty_signature_is_supported(self):
        def ready():
            return True

        self.assertEqual(self.execute(ready, {}).value, True)
        self.assertEqual(self.execute(ready, {"unexpected": 1}).error_code, "invalid_arguments")

    def test_generated_and_explicit_tools_have_same_identity(self):
        generated = tool_from_function(add, **POLICY)
        explicit = Tool("add", generated.description, ("a", "b"), add,
                        input_schema=generated.spec().schema(), **POLICY)
        first = build_tool_identity((generated,))
        self.assertEqual(first, build_tool_identity((explicit,)))
        self.assertEqual(first, build_tool_identity((tool_from_function(add, **POLICY),)))
        self.assertNotEqual(first["sha256"], build_tool_identity((tool_from_function(
            subtract, name="add", **POLICY),))["sha256"])
        self.assertNotEqual(first["sha256"], build_tool_identity((tool_from_function(
            add, **{**POLICY, "implementation_version": "2"}),))["sha256"])

    def test_existing_legacy_tool_remains_numeric(self):
        tool = Tool("legacy", "Legacy numeric shorthand", ("a", "b"), add)
        self.assertIs(tool.handler, add)
        self.assertIsNone(tool.input_schema)
        self.assertEqual(ToolExecutor(ToolRegistry((tool,))).execute(
            ToolCall("c", "legacy", {"a": 2, "b": 3})).value, 5)

    def test_default_and_parameter_type_changes_affect_identity(self):
        def typed(value: int = 1):
            return value

        initial = build_tool_identity((tool_from_function(typed, **POLICY),))
        typed.__defaults__ = (2,)
        changed_default = tool_from_function(typed, **POLICY)
        self.assertEqual(changed_default.spec().schema()["properties"]["value"]["default"], 2)
        self.assertNotEqual(initial["sha256"], build_tool_identity((changed_default,))["sha256"])
        typed.__defaults__ = (1,)
        typed.__annotations__ = {"value": float}
        changed_type = tool_from_function(typed, **POLICY)
        self.assertEqual(changed_type.spec().schema()["properties"]["value"]["type"], "number")
        self.assertNotEqual(initial["sha256"], build_tool_identity((changed_type,))["sha256"])

    def test_closure_execution_does_not_relax_benchmark_identity(self):
        offset = 4

        def adjusted(value: int):
            return value + offset

        tool = tool_from_function(adjusted, **POLICY)
        self.assertEqual(ToolExecutor(ToolRegistry((tool,))).execute(ToolCall("c", tool.name, {"value": 3})).value, 7)
        with self.assertRaisesRegex(ValueError, "closures"):
            build_tool_identity((tool,))


if __name__ == "__main__":
    unittest.main()
