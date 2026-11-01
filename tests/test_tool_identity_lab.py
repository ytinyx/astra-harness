"""Independently inspect real baselines, current tools and preflight effects."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from astra_harness.contracts import Message, ModelRequest, ToolResult
from scripts import tool_identity_lab as lab


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_only_run(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT state FROM runs").fetchall()
        events = db.execute("SELECT data FROM events ORDER BY seq").fetchall()
    if len(rows) != 1:
        raise AssertionError("expected exactly one actual task")
    return json.loads(rows[0][0]), [json.loads(row[0]) for row in events]


class ToolIdentityLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.report = lab.run_suite(cls.temporary.name)
        cls.suite = Path(cls.temporary.name) / cls.report["directory"]
        cls.rows = {row["id"]: row for row in cls.report["rows"]}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def evidence(self, entry):
        report_path = self.suite / entry["path"]
        raw = report_path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
        report = json.loads(raw)
        state, events = read_only_run(report_path.parent / "case-01" / "runs.sqlite3")
        self.assertEqual((state["status"], state["model_calls"], state["tool_calls"]), ("completed", 2, 1))
        results = [message["tool_result"] for message in state["messages"] if message.get("tool_result")]
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]["name"], results[0]["value"], results[0]["error_code"]), ("lab.sum", 5, None))
        self.assertEqual(json.loads(state["output"]), {"sum": 5})
        self.assertTrue(report["passed"])
        self.assertTrue(report["cases"][0]["checks"]["tool_value:lab.sum"])
        self.assertEqual(sum(event["type"] == "model.start" for event in events), 2)
        self.assertEqual(sum(event["type"] == "tool.start" for event in events), 1)
        return report, state

    def test_baseline_is_an_actual_tool_run_with_independent_expected_value(self):
        baseline = self.report["baseline"]
        report, state = self.evidence(baseline)
        self.assertEqual(baseline["origin"], "actual_benchmark_run")
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(baseline["counts"]["model_factory"], 1)
        self.assertEqual(baseline["counts"]["model_complete"], 2)
        self.assertEqual(baseline["counts"]["tool_handler"], 1)
        self.assertFalse(report["real_model_requested"])
        self.assertFalse(report["real_model_verified"])
        dataset = json.loads((self.suite / "task.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(dataset["tool_values"], {"lab.sum": 5})
        calls = [call for message in state["messages"] for call in message.get("tool_calls", [])]
        self.assertEqual(calls[0]["arguments"], {"a": -2, "b": 7})

    def test_allowed_comparisons_each_execute_a_new_actual_task(self):
        identifiers = set()
        _, baseline_state = self.evidence(self.report["baseline"])
        identifiers.add(baseline_state["id"])
        for name in ("same", "normalization", "description"):
            with self.subTest(name=name):
                row = self.rows[name]
                report, state = self.evidence(row["report"])
                identifiers.add(state["id"])
                self.assertEqual((row["expected"], row["outcome"]), ("accepted", "accepted"))
                self.assertEqual(row["origin"], "actual_tools_factory")
                self.assertEqual(row["counts"]["model_factory"], 1)
                self.assertEqual(row["counts"]["model_complete"], 2)
                self.assertEqual(row["counts"]["tool_handler"], 1)
                self.assertEqual(row["counts"]["tool_factory_bound"], 1)
                self.assertEqual(report["comparison"]["regressions"], [])
                self.assertEqual(report["comparison"]["improvements"], [])
                self.assertEqual(row["current_tool_identity"], self.report["baseline"]["tool_identity"])
        self.assertEqual(len(identifiers), 4)

    def test_normalized_order_changes_are_real_definitions_not_edited_reports(self):
        row = self.rows["normalization"]
        before = self.report["baseline"]["definition"]
        after = row["current_definition"]
        self.assertEqual([tool["name"] for tool in before], ["lab.sum", "lab.marker"])
        self.assertEqual([tool["name"] for tool in after], ["lab.marker", "lab.sum"])
        original = next(tool for tool in before if tool["name"] == "lab.sum")
        changed = next(tool for tool in after if tool["name"] == "lab.sum")
        self.assertEqual(changed["schema"]["required"], list(reversed(original["schema"]["required"])))
        self.assertEqual(changed["schema"]["properties"]["a"]["enum"], [7, 2, -2])
        self.assertEqual(row["current_tool_identity"], self.report["baseline"]["tool_identity"])

    def test_description_changes_remain_comparable_with_presentation_notice(self):
        row = self.rows["description"]
        self.assertEqual(row["current_tool_identity"], self.report["baseline"]["tool_identity"])
        self.assertNotEqual(row["current_tool_presentation_sha256"], self.report["baseline"]["tool_presentation_sha256"])
        self.assertTrue(row["comparison"]["metadata_changed"])
        self.assertTrue(row["comparison"]["notes"])
        self.assertNotEqual(row["current_definition"][0]["description"], self.report["baseline"]["definition"][0]["description"])

    def test_each_actual_tool_difference_is_rejected_with_distinct_identity(self):
        baseline = self.report["baseline"]["tool_identity"]
        for name in lab.REJECTED_VARIANTS[:-1]:
            with self.subTest(name=name):
                row = self.rows[name]
                self.assertEqual(row["origin"], "actual_tools_factory")
                self.assertEqual(row["outcome"], "rejected")
                self.assertNotEqual(row["current_tool_identity"]["sha256"], baseline["sha256"])
                self.assertNotEqual(row["current_tool_identity"]["tools"], baseline["tools"])
                self.assertIn("tool identity", row["exception"]["message"].casefold())
        definitions = {name: {tool["name"]: tool for tool in self.rows[name]["current_definition"]}
                       for name in lab.REJECTED_VARIANTS[:-1]}
        self.assertIn("lab.extra", definitions["tool_added"])
        self.assertNotIn("lab.marker", definitions["tool_removed"])
        self.assertEqual(definitions["schema"]["lab.sum"]["schema"]["properties"]["a"]["minimum"], 0)
        self.assertEqual(definitions["permission"]["lab.sum"]["permissions"], ["lab.restricted"])
        self.assertTrue(definitions["approval"]["lab.sum"]["requires_approval"])
        self.assertEqual(definitions["implementation_body"]["lab.sum"]["handler"], "Probe.revised_sum")
        self.assertEqual(definitions["implementation_version"]["lab.sum"]["implementation_version"], "lab-v2")
        self.assertEqual(definitions["implementation_constants"]["lab.sum"]["implementation_constants"], {"offset": 1})

    def test_refusals_make_no_model_or_handler_calls_and_no_benchmark_directories(self):
        for name in lab.REJECTED_VARIANTS:
            with self.subTest(name=name):
                row = self.rows[name]
                self.assertEqual(row["outcome"], "rejected")
                for counter in ("model_factory", "model_complete", "tool_handler", "tool_factory_bound"):
                    self.assertEqual(row["counts"][counter], 0)
                self.assertFalse(row["output_existed_before"])
                self.assertFalse(row["output_exists_after"])
                self.assertFalse((self.suite / row["output_path"]).exists())
                self.assertEqual(row["entries_before"], row["entries_after"])
                self.assertTrue(row["baseline_unchanged"])
        self.assertEqual({path.name for path in (self.suite / "benchmarks").iterdir()},
                         {"baseline", "same", "normalization", "description"})

    def test_legacy_data_is_explicitly_derived_and_never_claimed_as_a_historical_run(self):
        lineage = self.report["derived_fixture"]
        self.assertEqual(lineage["origin"], "derived_fixture")
        self.assertIs(lineage["historical_run"], False)
        self.assertEqual(lineage["parent_sha256"], self.report["baseline"]["sha256"])
        parent_path = self.suite / lineage["parent_path"]
        self.assertEqual(hashlib.sha256(parent_path.read_bytes()).hexdigest(), lineage["parent_sha256"])
        parent = read_json(parent_path)
        expected = deepcopy(parent)
        expected["schema_version"] = 2
        del expected["tool_identity"]
        del expected["tool_presentation_sha256"]
        derived_path = self.suite / lineage["path"]
        self.assertEqual(read_json(derived_path), expected)
        self.assertEqual(hashlib.sha256(derived_path.read_bytes()).hexdigest(), lineage["sha256"])
        row = self.rows["legacy_schema2"]
        self.assertEqual(row["origin"], "derived_fixture")
        self.assertEqual(row["baseline_path"], lineage["path"])
        self.assertEqual(row["outcome"], "rejected")
        self.assertNotIn("current_tool_identity", row)

    def test_portable_report_and_fingerprints_match_actual_sources(self):
        report = self.report
        self.assertTrue(report["passed"])
        self.assertEqual(report["summary"], {"attempts": 12, "expectations_met": 12,
                                            "accepted_comparisons": 3, "rejected_comparisons": 9})
        self.assertEqual((report["independent_tasks"], report["attempts_per_variant"], report["provider_requests"]), (1, 1, 0))
        self.assertEqual(read_json(self.suite / "report.json"), report)
        self.assertEqual(hashlib.sha256((self.suite / "task.jsonl").read_bytes()).hexdigest(), report["dataset"]["bytes_sha256"])
        self.assertEqual(report["dataset"]["canonical_sha256"], report["baseline"]["dataset_sha256"])
        project = Path(__file__).resolve().parents[1]
        for name, fingerprint in report["source_files_sha256"].items():
            self.assertEqual(hashlib.sha256((project / name).read_bytes()).hexdigest(), fingerprint)
        # Inspect decoded strings, so JSON backslash escaping cannot hide paths.
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
        for value in strings(report):
            self.assertNotIn(str(self.suite), value)
            self.assertNotIn(str(project), value)
        self.assertIn("未冒充历史运行", (self.suite / "summary.md").read_text(encoding="utf-8"))


class FixtureGroundTruthTests(unittest.TestCase):
    def test_implementation_variants_change_actual_methods_or_declared_constants(self):
        for variant, expected in (("same", 5), ("implementation_body", 6),
                                  ("implementation_constants", 6), ("implementation_version", 5)):
            with self.subTest(variant=variant):
                probe = lab.Probe(variant)
                tools = probe.tools_factory()
                tool = next(tool for tool in tools if tool.name == "lab.sum")
                self.assertEqual(tool.implementation_constants, probe.constants)
                self.assertEqual(tool.handler(a=-2, b=7), expected)
                self.assertEqual(probe.counts["tool_handler"], 1)
                self.assertEqual(probe.counts["model_complete"], 0)

    def test_fixed_model_returns_actual_tool_results_instead_of_the_expected_answer(self):
        probe = lab.Probe("same")
        model = probe.model_factory()
        request = ModelRequest((Message("user", "Compute -2 + 7"),
                                Message("tool", tool_result=ToolResult("sum-1", "lab.sum", 123))), ())
        self.assertEqual(json.loads(model.complete(request).text), {"sum": 123})
        self.assertEqual(probe.counts["model_complete"], 1)
        self.assertEqual(probe.counts["tool_handler"], 0)


if __name__ == "__main__":
    unittest.main()
