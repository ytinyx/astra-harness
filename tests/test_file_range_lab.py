"""Read the retained inputs and database independently of the report verdict."""

from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import file_range_lab


EXPECTED = {
    "source": {"path": "records/project-notes.txt", "anchor_line": 451},
    "fields": {
        "project": {"value": "Astra field extraction", "line": 452},
        "owner": {"value": "林舟", "line": 453},
        "due": {"value": "2027-05-20", "line": 454},
        "quota": {"value": 17, "line": 455},
    },
}


def read_state(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        records = db.execute("SELECT state FROM runs").fetchall()
    if len(records) != 1:
        raise AssertionError("one run is required")
    return json.loads(records[0][0])


class FileRangeLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.report = file_range_lab.run_suite(cls.temporary.name)
        cls.suite = Path(cls.temporary.name) / cls.report["directory"]
        cls.rows = {row["strategy"]: row for row in cls.report["rows"]}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def state(self, strategy):
        row = self.rows[strategy]
        state = read_state(self.suite / row["paths"]["database"])
        saved = json.loads((self.suite / row["paths"]["checkpoint"]).read_text(encoding="utf-8"))
        self.assertEqual(state, saved)
        return state

    def test_one_shared_real_utf8_input_exceeds_character_budget_and_remains_unchanged(self):
        info = self.report["input"]
        raw = (self.suite / info["fixture_path"]).read_bytes()
        text = raw.decode("utf-8")
        self.assertGreater(len(raw), 58_000)
        self.assertLess(len(raw), 65_000)
        self.assertGreater(len(text), 48_000)
        self.assertGreater(len(raw), len(text))
        self.assertEqual((info["characters"], info["utf8_bytes"]), (len(text), len(raw)))
        self.assertEqual(info["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(text.count("ASTRA-RANGE-TARGET-2027"), 1)
        self.assertEqual(text.splitlines()[450:455], ["ASTRA-RANGE-TARGET-2027", "project: Astra field extraction",
                                                     "owner: 林舟", "due: 2027-05-20", "quota: 17"])
        self.assertTrue(all(row["checks"]["input_unchanged"] for row in self.rows.values()))

    def test_full_read_reaches_context_limit_without_a_third_model_invocation(self):
        row, state = self.rows["full_read"], self.state("full_read")
        self.assertEqual((state["status"], state["error"]), ("failed", "context_limit"))
        self.assertEqual((state["model_calls"], state["tool_calls"]), (2, 2))
        self.assertEqual(row["actual_model_invocations"], 2)
        self.assertEqual(row["model_request_tool_results"], [[], ["files.search"]])
        self.assertGreater(state["context"]["estimated_chars"], state["context"]["max_chars"])
        results = [message["tool_result"] for message in state["messages"] if message.get("tool_result")]
        self.assertEqual([result["name"] for result in results], ["files.search", "files.read"])
        self.assertEqual(results[1]["value"], (self.suite / self.report["input"]["fixture_path"]).read_text(encoding="utf-8"))
        self.assertFalse(row["task_success"])
        self.assertFalse(row["output_correct"])
        self.assertTrue(row["experiment_passed"])

    def test_range_read_uses_search_citations_and_completes_from_five_actual_fragments(self):
        row, state = self.rows["range_read"], self.state("range_read")
        self.assertEqual(state["status"], "completed")
        self.assertEqual((state["model_calls"], state["tool_calls"]), (3, 2))
        self.assertEqual(row["actual_model_invocations"], 3)
        self.assertEqual(row["model_request_tool_results"], [[], ["files.search"], ["files.search", "files.read_range"]])
        self.assertLessEqual(state["context"]["estimated_chars"], 48_000)
        self.assertEqual(json.loads(state["output"]), EXPECTED)
        self.assertTrue(row["task_success"])
        results = [message["tool_result"] for message in state["messages"] if message.get("tool_result")]
        hit = results[0]["value"]["matches"][0]
        calls = [call for message in state["messages"] for call in message.get("tool_calls", [])]
        self.assertEqual(calls[1]["arguments"], {"path": hit["path"], "start_line": hit["line"],
                                               "start_column": 1, "line_count": 5})
        value = results[1]["value"]
        self.assertIs(value["ok"], True)
        self.assertEqual([part["line"] for part in value["fragments"]], list(range(451, 456)))
        self.assertTrue(all(not part["partial"] and part["start_column"] == 1 for part in value["fragments"]))
        self.assertLessEqual(sum(len(part["text"]) for part in value["fragments"]), 2_000)
        self.assertEqual(value["next_cursor"], {"start_line": 456, "start_column": 1})
        self.assertTrue(value["truncated"])

    def test_policies_share_model_class_tool_snapshot_permissions_and_limits(self):
        full, ranged = self.rows["full_read"], self.rows["range_read"]
        for key in ("model_class", "tool_snapshot", "permissions", "limits"):
            self.assertEqual(full[key], ranged[key])
        self.assertEqual(full["permissions"], ["files.read"])
        for row in (full, ranged):
            self.assertEqual(row["limits"], self.report["limits"])
            self.assertEqual(row["context"]["max_chars"], self.report["context_budget_chars"])

    def test_result_sizes_are_measured_from_exact_persisted_toolresult_serialization(self):
        for strategy, row in self.rows.items():
            with self.subTest(strategy=strategy):
                state = self.state(strategy)
                results = [message["tool_result"] for message in state["messages"] if message.get("tool_result")]
                total_chars = total_bytes = 0
                for result, metric in zip(results, row["tool_results"]):
                    raw = (self.suite / metric["path"]).read_bytes()
                    self.assertEqual(raw.decode("utf-8"), json.dumps(result, ensure_ascii=False, allow_nan=False))
                    self.assertEqual(metric["serialized_chars"], len(raw.decode("utf-8")))
                    self.assertEqual(metric["serialized_utf8_bytes"], len(raw))
                    self.assertEqual(metric["sha256"], hashlib.sha256(raw).hexdigest())
                    total_chars += len(raw.decode("utf-8"))
                    total_bytes += len(raw)
                self.assertEqual((row["tool_result_chars"], row["tool_result_utf8_bytes"]), (total_chars, total_bytes))

    def test_portable_reports_fingerprints_and_denominators_match_retained_evidence(self):
        report = self.report
        self.assertTrue(report["passed"])
        self.assertEqual(report["summary"], {"strategy_runs": 2, "experiment_passed": 2, "task_successes": 1})
        self.assertEqual((report["independent_inputs"], report["runs_per_strategy"], report["provider_requests"]), (1, 1, 0))
        self.assertEqual(json.loads((self.suite / "report.json").read_text(encoding="utf-8")), report)
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
            self.assertNotIn(str(Path(self.temporary.name).resolve()), value)
        source_root = Path(__file__).resolve().parents[1]
        for name, fingerprint in report["source_files_sha256"].items():
            self.assertEqual(hashlib.sha256((source_root / name).read_bytes()).hexdigest(), fingerprint)
        for row in self.rows.values():
            self.assertEqual(json.loads((self.suite / row["paths"]["result"]).read_text(encoding="utf-8")), row)
        self.assertIn("一次成对机制演示", (self.suite / "summary.md").read_text(encoding="utf-8"))


class FileRangeOracleTests(unittest.TestCase):
    def test_existing_workspace_is_rejected_without_overwriting_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            sentinel = workspace / "keep.txt"
            sentinel.write_bytes(b"existing evidence")
            with self.assertRaises(FileExistsError):
                file_range_lab.prepare_workspace(workspace)
            self.assertEqual(sentinel.read_bytes(), b"existing evidence")
            self.assertFalse((workspace / "records").exists())

    def test_wrong_values_citations_and_json_types_are_rejected(self):
        self.assertTrue(file_range_lab.score_output(json.dumps(EXPECTED))["output_correct"])
        variants = []
        for key, value in (("quota", 17.0), ("owner", "Wrong owner")):
            answer = copy.deepcopy(EXPECTED)
            answer["fields"][key]["value"] = value
            variants.append(answer)
        answer = copy.deepcopy(EXPECTED)
        answer["fields"]["quota"]["line"] = 454
        variants.append(answer)
        answer = copy.deepcopy(EXPECTED)
        answer["source"]["path"] = "invented.txt"
        variants.append(answer)
        answer = copy.deepcopy(EXPECTED)
        answer["source"]["anchor_line"] = True
        variants.append(answer)
        for answer in variants:
            self.assertFalse(file_range_lab.score_output(json.dumps(answer))["output_correct"])
        self.assertFalse(file_range_lab.score_output("not JSON")["output_correct"])

    def test_model_reads_changed_real_input_instead_of_returning_oracle_literals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            file_range_lab.prepare_workspace(workspace)
            source = workspace / file_range_lab.INPUT_PATH
            source.write_bytes(source.read_bytes().replace("owner: 林舟".encode(), "owner: 沈禾".encode()).replace(b"quota: 17", b"quota: 23"))
            row = file_range_lab.run_strategy("range_read", workspace, root / "range", root)
            state = read_state(root / row["paths"]["database"])
            self.assertEqual(state["status"], "completed")
            output = json.loads(state["output"])
            self.assertEqual(output["fields"]["owner"]["value"], "沈禾")
            self.assertEqual(output["fields"]["quota"]["value"], 23)
            self.assertFalse(row["output_correct"])
            self.assertFalse(row["task_success"])
            self.assertFalse(row["experiment_passed"])

    def test_model_failure_is_retained_as_failed_evidence_not_expected_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            file_range_lab.prepare_workspace(workspace)
            with patch.object(file_range_lab.FileTaskModel, "complete", side_effect=RuntimeError("fixture failure")):
                row = file_range_lab.run_strategy("range_read", workspace, root / "range", root)
            self.assertEqual((row["status"], row["error"]), ("failed", "model_error"))
            self.assertFalse(row["experiment_passed"])
            self.assertFalse(row["task_success"])
            self.assertTrue((root / row["paths"]["checkpoint"]).is_file())
            self.assertEqual(read_state(root / row["paths"]["database"])["status"], "failed")

    def test_range_business_error_is_not_counted_as_a_successful_tool_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            file_range_lab.prepare_workspace(workspace)
            # Preserve descriptor binding so the injection reaches the real
            # registered call instead of failing during plugin construction.
            with patch.object(file_range_lab.FilePlugin, "read_range", autospec=True, return_value={
                    "ok": False, "error_code": "file_missing", "hint": "Check the input."}) as read_range:
                row = file_range_lab.run_strategy("range_read", workspace, root / "range", root)
            read_range.assert_called_once()
            self.assertEqual(read_range.call_args.kwargs, {
                "path": "records/project-notes.txt", "start_line": 451, "start_column": 1, "line_count": 5})
            self.assertEqual((row["status"], row["error"]), ("failed", "model_error"))
            self.assertTrue(all(result["error_code"] is None for result in row["tool_results"]))
            self.assertFalse(row["checks"]["two_successful_tool_results"])
            self.assertFalse(row["experiment_passed"])


if __name__ == "__main__":
    unittest.main()
