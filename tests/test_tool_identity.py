from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import platform
import tempfile
import unittest
from unittest.mock import Mock, patch

from astra_harness.benchmark import _read_report, benchmark, compare_reports, default_tool_factory, load_cases
from astra_harness.builtin.files import FilePlugin
from astra_harness.contracts import ToolCall, ToolResult
from astra_harness.engine import normalize_limits
from astra_harness.tool_identity import (build_tool_identity, canonical_schema, function_fingerprint,
                                         identity_difference, presentation_fingerprint, validate_tool_identity)
from astra_harness.tools import Tool, ToolExecutor, ToolRegistry


def _sum(a, b):
    return a + b


def _subtract(a, b):
    return a - b


def _object(a):
    return {"value": a}


def _helper(value):
    return value + 1


def _changed_helper(value):
    return value + 2


def _uses_helper(value):
    return _helper(value)


def _defaulted(value=1, *, enabled=True):
    return value if enabled else 0


def _unrestricted_result(call, tool, value):
    return ToolResult(call.call_id, call.name, value)


def _permissive_validate(value, schema, depth=0):
    return True


async def _unrestricted_async(self, call, permissions=frozenset(), *, approved=False):
    return ToolResult(call.call_id, call.name, {"changed": True})


def _schema():
    return {"type": "object", "properties": {
        "a": {"type": "number", "enum": [1, 1.0, 2], "description": "first"},
        "b": {"type": "number", "title": "second"}}, "required": ["a", "b"], "additionalProperties": False}


class ToolIdentityTests(unittest.TestCase):
    def tool(self, **changes):
        return replace(Tool("sum", "sum values", ("a", "b"), _sum, input_schema=_schema(),
                            permissions=frozenset({"compute", "read"}), implementation_version="1"), **changes)

    def test_definitions_require_no_workspace_and_match_bound_tools(self):
        with patch.object(FilePlugin, "__init__", side_effect=AssertionError("workspace constructor")), \
                patch.object(Path, "resolve", side_effect=AssertionError("workspace path")), \
                patch.object(Path, "mkdir", side_effect=AssertionError("directory write")):
            definitions = default_tool_factory()
            identity = build_tool_identity(definitions)
        self.assertEqual(len(identity["tools"]), 6)
        with tempfile.TemporaryDirectory() as temporary:
            actual = default_tool_factory(Path(temporary))
            self.assertEqual(build_tool_identity(actual), identity)
            self.assertEqual(presentation_fingerprint(actual), presentation_fingerprint(definitions))
            self.assertEqual(actual[-1].name, "files.write")

    def test_unordered_schema_parts_tool_order_and_permissions_are_canonical(self):
        first, second = self.tool(), self.tool(name="other")
        changed = deepcopy(first.input_schema)
        changed["required"].reverse()
        changed["properties"]["a"]["enum"] = [2, 1.0, 1, 2]
        self.assertEqual(build_tool_identity((first, second)), build_tool_identity((second, replace(first, input_schema=changed))))
        self.assertEqual(presentation_fingerprint((first, second)), presentation_fingerprint((second, replace(first, input_schema=changed))))
        values = canonical_schema(changed)["properties"]["a"]["enum"]
        self.assertEqual({(type(value), value) for value in values}, {(int, 1), (float, 1.0), (int, 2)})

    def test_wording_only_changes_presentation_but_not_hard_identity(self):
        original = self.tool()
        schema = deepcopy(original.input_schema)
        schema["description"] = "rewritten object help"
        schema["properties"]["a"]["description"] = "rewritten field help"
        schema["properties"]["b"]["title"] = "rewritten title"
        changed = replace(original, description="rewritten tool help", input_schema=schema)
        self.assertEqual(build_tool_identity((original,)), build_tool_identity((changed,)))
        self.assertNotEqual(presentation_fingerprint((original,)), presentation_fingerprint((changed,)))

    def test_business_keys_and_nested_arrays_are_not_treated_as_schema_annotations(self):
        schema = {"type": "object", "properties": {
            "description": {"type": "string"}, "title": {"type": "array", "items": {"type": "string"}},
            "payload": {"type": "object", "enum": [{"description": "business", "title": [1, 2]}],
                        "default": {"description": "business", "title": [1, 2]}}}}
        original = self.tool(input_schema=schema)
        for update in ("property", "enum", "default", "array"):
            changed = deepcopy(schema)
            if update == "property":
                changed["properties"]["description"]["type"] = "number"
            elif update == "array":
                changed["properties"]["payload"]["enum"][0]["title"].reverse()
            elif update == "enum":
                changed["properties"]["payload"]["enum"][0]["description"] = "changed"
            else:
                changed["properties"]["payload"]["default"]["description"] = "changed"
            with self.subTest(update=update):
                self.assertNotEqual(build_tool_identity((original,)), build_tool_identity((replace(original, input_schema=changed),)))
        # Existing check_schema ignores this inapplicable keyword. Preserve its
        # JSON declaration conservatively rather than attempting child schemas.
        self.assertEqual(canonical_schema({"type": "string", "properties": 1}), {"type": "string", "properties": 1})

    def test_metadata_is_copied_and_constants_are_type_sensitive(self):
        constants = {"options": {"values": [False, 1]}}
        tool = self.tool(implementation_constants=constants)
        before = build_tool_identity((tool,))
        constants["options"]["values"][0] = True
        self.assertEqual(build_tool_identity((tool,)), before)
        changed = replace(tool, implementation_constants={"options": {"values": [0, 1.0]}})
        self.assertNotEqual(build_tool_identity((changed,)), before)
        self.assertIn("implementation.constants", identity_difference(build_tool_identity((changed,)), before))

    def test_ast_ignores_documentation_but_detects_body_and_runtime_defaults(self):
        with patch("astra_harness.tool_identity.inspect.getsource", return_value='def _sum(a, b):\n    "one"\n    return a + b\n'):
            first = function_fingerprint(_sum)
        with patch("astra_harness.tool_identity.inspect.getsource", return_value='def _sum(a,b):\n    "two"\n    # changed comment\n    return a+b\n'):
            self.assertEqual(function_fingerprint(_sum), first)
        self.assertNotEqual(function_fingerprint(_sum), function_fingerprint(_subtract))
        positional, keyword = _defaulted.__defaults__, _defaulted.__kwdefaults__
        try:
            before = function_fingerprint(_defaulted)
            _defaulted.__defaults__ = (2,)
            self.assertNotEqual(function_fingerprint(_defaulted), before)
            _defaulted.__defaults__ = positional
            _defaulted.__kwdefaults__ = {"enabled": False}
            self.assertNotEqual(function_fingerprint(_defaulted), before)
            _defaulted.__defaults__ = ([1],)
            mutable_before = function_fingerprint(_defaulted)
            _defaulted.__defaults__[0].append(2)
            self.assertNotEqual(function_fingerprint(_defaulted), mutable_before)
            _defaulted.__defaults__ = ((1, 2),)
            self.assertNotEqual(function_fingerprint(_defaulted), mutable_before)
            _defaulted.__defaults__ = (frozenset({1, 2}),)
            set_before = function_fingerprint(_defaulted)
            _defaulted.__defaults__ = (frozenset({2, 1}),)
            self.assertEqual(function_fingerprint(_defaulted), set_before)
            _defaulted.__defaults__ = ({1, 2},)
            self.assertNotEqual(function_fingerprint(_defaulted), set_before)
        finally:
            _defaulted.__defaults__, _defaulted.__kwdefaults__ = positional, keyword

    def test_declared_helper_and_execution_gate_changes_are_detected(self):
        tool = Tool("helper", "use helper", ("value",), _uses_helper, implementation_version="1",
                    implementation_dependencies=(_helper,))
        before = build_tool_identity((tool,))
        original = _helper.__code__
        try:
            _helper.__code__ = _changed_helper.__code__
            self.assertEqual(_uses_helper(1), 3)
            self.assertNotEqual(build_tool_identity((tool,)), before)
        finally:
            _helper.__code__ = original
        for target, replacement in (("ToolExecutor.result", staticmethod(_unrestricted_result)),
                                    ("ToolExecutor.aexecute", _unrestricted_async), ("validate", _permissive_validate)):
            with self.subTest(target=target), patch("astra_harness.tools." + target, replacement):
                changed = build_tool_identity((tool,))
                self.assertNotEqual(changed, before)
                self.assertIn("execution_contract changed", identity_difference(changed, before))

    def test_unknown_dynamic_implementations_fail_closed(self):
        value = 1
        def closure(a, b):
            return a + b + value
        for handler in (closure, lambda a, b: a + b, len):
            with self.subTest(handler=handler), self.assertRaises(ValueError):
                build_tool_identity((self.tool(handler=handler),))
        with self.assertRaisesRegex(ValueError, "explicit implementation version"):
            build_tool_identity((self.tool(implementation_version=None),))
        with patch("astra_harness.tool_identity.inspect.getsource", side_effect=OSError("unavailable")), self.assertRaises(ValueError):
            build_tool_identity((self.tool(),))
        default = _defaulted.__defaults__
        try:
            _defaulted.__defaults__ = (object(),)
            with self.assertRaises(ValueError):
                function_fingerprint(_defaulted)
        finally:
            _defaulted.__defaults__ = default

    def test_equivalent_input_schemas_do_not_hide_different_result_contracts(self):
        numeric = Tool("value", "object", ("a",), _object, implementation_version="1")
        structured = replace(numeric, input_schema=numeric.spec().schema())
        call = ToolCall("one", "value", {"a": 1})
        self.assertEqual(ToolExecutor(ToolRegistry((numeric,))).execute(call).error_code, "invalid_tool_result")
        self.assertEqual(ToolExecutor(ToolRegistry((structured,))).execute(call).value, {"value": 1})
        self.assertNotEqual(build_tool_identity((numeric,)), build_tool_identity((structured,)))

    def test_report_manifest_tampering_and_malformed_types_are_value_errors(self):
        before = build_tool_identity((self.tool(),))
        broken = []
        for name, value in (("sha256", "0" * 64), ("algorithm", "unknown"), ("execution_contract", {})):
            broken.append({**before, name: value})
        for field, value in (("result_contract", []), ("input_schema", {"type": "object", "properties": {"x": {"type": []}}}),
                             ("side_effect", 1), ("permissions", ["read", "read"])):
            candidate = deepcopy(before)
            candidate["tools"][0][field] = value
            broken.append(candidate)
        candidate = deepcopy(before)
        candidate["tools"].append(deepcopy(candidate["tools"][0]))
        broken.append(candidate)
        for candidate in broken:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_tool_identity(candidate)


class BenchmarkToolIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "not-created"
        self.dataset = self.root / "cases.jsonl"
        self.dataset.write_text('{"id":"chat","prompt":"hello"}\n', encoding="utf-8")
        _, digest = load_cases(self.dataset)
        self.definitions = default_tool_factory()
        self.report = {"schema_version": 3, "mode": "demo", "dataset_sha256": digest,
                       "tool_identity": build_tool_identity(self.definitions),
                       "tool_presentation_sha256": presentation_fingerprint(self.definitions),
                       "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                                       "system": platform.system()},
                       "runtime_config": {"limits": normalize_limits(), "context_chars": 48000},
                       "model": {"adapter": "DemoModel", "name": None}, "passed": True,
                       "cases": [{"id": "chat", "passed": True, "duration_ms": 1}],
                       "summary": {"total": 1, "passed": 1, "failed": 0, "pass_rate": 1, "mean_duration_ms": 1}}
        self.baseline = self.root / "baseline.json"
        self.write(self.report)

    def write(self, report):
        self.baseline.write_text(json.dumps(report), encoding="utf-8")

    def assert_preflight_rejection(self, factory, pattern):
        model = Mock(side_effect=AssertionError("model constructor must not run"))
        with patch("astra_harness.benchmark.Application", side_effect=AssertionError("application must not open")) as app, \
                patch("astra_harness.benchmark.seed_workspace", side_effect=AssertionError("workspace must not be seeded")) as seed:
            with self.assertRaisesRegex(ValueError, pattern):
                benchmark(self.dataset, self.output, baseline=self.baseline, model_factory=model, tools_factory=factory)
        model.assert_not_called()
        app.assert_not_called()
        seed.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_changed_tools_reject_before_model_workspace_and_handler(self):
        first, *rest = self.definitions
        variants = {
            "added": (*self.definitions, replace(first, name="extra")), "removed": tuple(rest),
            "schema": (replace(first, parameters=("a", "b", "c")), *rest),
            "permissions": (replace(first, permissions=frozenset({"new"})), *rest),
            "side_effect": (replace(first, side_effect=True), *rest),
            "requires_approval": (replace(first, requires_approval=True), *rest),
            "result_contract": (replace(first, input_schema=first.spec().schema()), *rest),
            "implementation.version": (replace(first, implementation_version="2"), *rest),
            "implementation.handler": (replace(first, handler=_subtract), *rest),
            "implementation.constants": (replace(first, implementation_constants={"offset": 1}), *rest),
        }
        for field, definitions in variants.items():
            with self.subTest(field=field):
                factory = Mock(return_value=definitions)
                self.assert_preflight_rejection(factory, field.replace(".", r"\."))
                factory.assert_called_once_with(None)
                other = {**self.report, "tool_identity": build_tool_identity(definitions)}
                with self.assertRaisesRegex(ValueError, "tool identity"):
                    compare_reports(other, self.report)

    def test_legacy_reports_are_read_unchanged_but_never_compared_or_filled_in(self):
        legacy = {key: value for key, value in self.report.items() if key not in {"tool_identity", "tool_presentation_sha256"}}
        legacy["schema_version"] = 2
        self.write(legacy)
        self.assertEqual(_read_report(self.baseline), legacy)
        factory = Mock(side_effect=AssertionError("legacy does not need definitions"))
        self.assert_preflight_rejection(factory, "legacy schema 2")
        factory.assert_not_called()
        for current, previous in ((legacy, legacy), (legacy, self.report), (self.report, legacy)):
            with self.assertRaisesRegex(ValueError, "legacy schema 2"):
                compare_reports(current, previous)
        self.assertNotIn("tool_identity", _read_report(self.baseline))

    def test_presentation_change_is_reported_without_claiming_identical_model_input(self):
        def factory(workspace):
            return tuple(replace(tool, description="new wording: " + tool.description) for tool in default_tool_factory(workspace))
        current = benchmark(self.dataset, self.output, baseline=self.baseline, tools_factory=factory)
        self.assertTrue(current["passed"])
        self.assertTrue(current["comparison"]["metadata_changed"])
        self.assertTrue(any("wording" in note for note in current["comparison"]["notes"]))
        self.assertEqual(current["tool_identity"], self.report["tool_identity"])

    def test_case_binding_drift_stops_before_application(self):
        def factory(workspace):
            tools = default_tool_factory(workspace)
            return tools if workspace is None else (replace(tools[0], implementation_version="changed"), *tools[1:])
        with patch("astra_harness.benchmark.Application", side_effect=AssertionError("must not run changed tools")) as app:
            with self.assertRaisesRegex(ValueError, "tool factory changed identity"):
                benchmark(self.dataset, self.output, tools_factory=factory)
        app.assert_not_called()
        suite, = self.output.iterdir()
        self.assertFalse((suite / "case-01" / "runs.sqlite3").exists())

    def test_malformed_identity_presentation_or_environment_rejects_before_factory(self):
        for key, value in (("tool_identity", {}), ("tool_presentation_sha256", None), ("environment", {})):
            with self.subTest(key=key):
                self.write({**self.report, key: value})
                factory = Mock(side_effect=AssertionError("malformed report must not construct definitions"))
                self.assert_preflight_rejection(factory, "report|identity")
                factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
