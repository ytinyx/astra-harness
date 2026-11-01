"""Check task recovery against files and databases, independently of report scores."""

from collections import Counter
from contextlib import closing
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import task_recovery_lab


# These expectations are deliberately written independently of the fixture model,
# transformation handlers and the experiment's artifact scorer.
MATERIAL_INPUTS = {
    "inputs/research.md": "# Runtime notes\n- Checkpoints preserve progress.\n- Approval is bound to arguments.\n",
    "inputs/recovery.md": "# Recovery notes\n- Unknown effects require review.\n- Budgets survive restarts.\n",
}
DIGEST = {"items": [
    {"source": "inputs/research.md", "line": 2, "text": "Checkpoints preserve progress."},
    {"source": "inputs/research.md", "line": 3, "text": "Approval is bound to arguments."},
    {"source": "inputs/recovery.md", "line": 2, "text": "Unknown effects require review."},
    {"source": "inputs/recovery.md", "line": 3, "text": "Budgets survive restarts."},
]}
EVIDENCE_REPORT = (
    "# Evidence digest\n\n"
    "- `inputs/research.md:2` Checkpoints preserve progress.\n"
    "- `inputs/research.md:3` Approval is bound to arguments.\n"
    "- `inputs/recovery.md:2` Unknown effects require review.\n"
    "- `inputs/recovery.md:3` Budgets survive restarts.\n"
)
PEOPLE_CSV = "name,team,hours\n Alice , core ,2\nBob,platform,3\nalice,core,1\nCara,platform,4\n"
NORMALIZED = [
    {"name": "Alice", "team": "core", "hours": 3},
    {"name": "Bob", "team": "platform", "hours": 3},
    {"name": "Cara", "team": "platform", "hours": 4},
]
TOTALS = {"total_hours": 10, "by_team": {"core": 3, "platform": 7}, "people": 3}
ORIGINAL_CONFIG = {"service": "demo", "workers": 2, "logging": {"level": "info", "json": False}}
CONFIG_PATCH = {"workers": 4, "logging": {"level": "debug"}}
UPDATED_CONFIG = {"service": "demo", "workers": 4, "logging": {"level": "debug", "json": False}}
CHANGE_RECORD = {"change_id": "tune-demo-v1", "service": "demo", "before_workers": 2,
                 "after_workers": 4, "before_log_level": "info", "after_log_level": "debug"}
TASKS = ("materials", "csv-normalization", "config-change")
CONDITIONS = ("normal", "safe-after-first-write", "effect-before-checkpoint")
POLICIES = ("checkpoint_resume", "naive_rerun")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def correct_workspace(root, task):
    """Prepare oracle cases without calling any production task transformation."""
    (root / "inputs").mkdir(parents=True)
    (root / "output").mkdir()
    if task == "materials":
        for name, content in MATERIAL_INPUTS.items():
            (root / name).write_bytes(content.encode("utf-8"))
        write_json(root / "output/digest.json", DIGEST)
        (root / "output/report.md").write_bytes(EVIDENCE_REPORT.encode("utf-8"))
    elif task == "csv-normalization":
        (root / "inputs/people.csv").write_bytes(PEOPLE_CSV.encode("utf-8"))
        write_json(root / "output/normalized.json", NORMALIZED)
        write_json(root / "output/totals.json", TOTALS)
    else:
        (root / "inputs/original.json").write_bytes(
            b'{"service":"demo","workers":2,"logging":{"level":"info","json":false}}\n')
        (root / "inputs/patch.json").write_bytes(b'{"workers":4,"logging":{"level":"debug"}}\n')
        write_json(root / "output/config.json", UPDATED_CONFIG)
        (root / "output/history.jsonl").write_bytes((json.dumps(CHANGE_RECORD) + "\n").encode("utf-8"))


class TaskRecoveryLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.output = Path(cls.temporary.name) / "experiment"
        # Only one repetition: 18 real rows, shared by all outcome assertions.
        cls.report = task_recovery_lab.run_suite(cls.output, repetitions=1)
        cls.directory = cls.output / cls.report["directory"]
        cls.rows = {(row["task"], row["condition"], row["policy"]): row for row in cls.report["rows"]}

    def relative_path(self, name):
        relative = Path(name)
        self.assertFalse(relative.is_absolute())
        path = (self.directory / relative).resolve()
        self.assertTrue(path.is_relative_to(self.directory.resolve()))
        self.assertTrue(path.exists())
        return path

    def workspace(self, row):
        path = self.relative_path(row["paths"]["workspace"])
        self.assertTrue(path.is_dir())
        return path

    def ledger(self, row):
        path = self.relative_path(row["paths"]["ledger"])
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def state(self, attempt):
        database = self.relative_path(attempt["database_path"])
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            record = connection.execute("SELECT state FROM runs WHERE id=?", (attempt["run_id"],)).fetchone()
        self.assertIsNotNone(record)
        return json.loads(record[0])

    def read_json(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_correct_files(self, row, history_count=1):
        root = self.workspace(row)
        if row["task"] == "materials":
            for name, expected in MATERIAL_INPUTS.items():
                self.assertEqual((root / name).read_text(encoding="utf-8"), expected)
            self.assertEqual(self.read_json(root / "output/digest.json"), DIGEST)
            self.assertEqual((root / "output/report.md").read_text(encoding="utf-8"), EVIDENCE_REPORT)
        elif row["task"] == "csv-normalization":
            self.assertEqual((root / "inputs/people.csv").read_text(encoding="utf-8"), PEOPLE_CSV)
            self.assertEqual(self.read_json(root / "output/normalized.json"), NORMALIZED)
            self.assertEqual(self.read_json(root / "output/totals.json"), TOTALS)
        else:
            self.assertEqual(self.read_json(root / "inputs/original.json"), ORIGINAL_CONFIG)
            self.assertEqual(self.read_json(root / "inputs/patch.json"), CONFIG_PATCH)
            actual_config = self.read_json(root / "output/config.json")
            self.assertEqual(actual_config, UPDATED_CONFIG)
            self.assertIs(actual_config["logging"]["json"], False)
            self.assertIs(type(actual_config["workers"]), int)
            history = [json.loads(line) for line in (root / "output/history.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(history, [CHANGE_RECORD] * history_count)

    def test_matrix_has_paired_identities_and_traceable_csv_rows(self):
        self.assertEqual((self.report["schema_version"], self.report["suite"], self.report["mode"]),
                         (1, "task_recovery_lab", "deterministic"))
        self.assertEqual(len(self.report["rows"]), 18)
        self.assertEqual(set(self.rows), {(task, condition, policy) for task in TASKS
                                         for condition in CONDITIONS for policy in POLICIES})
        self.assertEqual(len({row["id"] for row in self.rows.values()}), 18)
        self.assertEqual(len({row["pair_id"] for row in self.rows.values()}), 9)
        for task in TASKS:
            for condition in CONDITIONS:
                a, b = (self.rows[task, condition, policy] for policy in POLICIES)
                self.assertEqual((a["pair_id"], a["repetition"], a["input_sha256"]),
                                 (b["pair_id"], b["repetition"], b["input_sha256"]))
        saved = self.read_json(self.relative_path(self.report["report"]))
        self.assertEqual(saved, self.report)
        with self.relative_path(self.report["csv_report"]).open(encoding="utf-8", newline="") as stream:
            csv_rows = list(csv.DictReader(stream))
        self.assertEqual(len(csv_rows), 18)
        by_id = {row["id"]: row for row in self.rows.values()}
        self.assertEqual({row["id"] for row in csv_rows}, set(by_id))
        for record in csv_rows:
            row = by_id[record["id"]]
            for key in ("task", "condition", "policy", "pair_id", "repetition", "run_status"):
                self.assertEqual(record[key], str(row[key]))
            for key in ("model_calls", "tool_calls", "repeated_effect_calls", "duplicate_business_records"):
                self.assertEqual(int(record[key]), row[key])

    def test_report_counts_match_actual_ledger_and_attempt_databases(self):
        for row in self.rows.values():
            with self.subTest(row=row["id"]):
                observations = self.ledger(row)
                counts = Counter(item["kind"] for item in observations)
                self.assertTrue(row["passed"])
                self.assertEqual(row["model_calls"], counts["model"])
                self.assertEqual(row["read_calls"], counts["read"])
                self.assertEqual(row["write_calls"], counts["write"])
                self.assertEqual(row["append_calls"], counts["append"])
                self.assertEqual(row["tool_calls"], counts["read"] + counts["write"] + counts["append"])
                effect_counts = Counter(item["logical_op"] for item in observations if item["kind"] in {"write", "append"})
                self.assertEqual(row["repeated_effect_calls"], sum(max(0, count - 1) for count in effect_counts.values()))
                self.assertEqual(row["attempt_count"], len(row["attempts"]))
                attempt_ids = {attempt["id"] for attempt in row["attempts"]}
                run_ids = {attempt["run_id"] for attempt in row["attempts"]}
                self.assertEqual({entry["attempt_id"] for entry in observations}, attempt_ids)
                self.assertEqual({entry["run_id"] for entry in observations}, run_ids)
                for attempt in row["attempts"]:
                    state = self.state(attempt)
                    self.assertEqual(state["status"], attempt["status"])
                    self.assertEqual(state["limits"], attempt["limits"])
                    for key in ("model_calls", "tool_calls"):
                        self.assertEqual(state[key], attempt["state_counters"][key])
                final = self.state(row["attempts"][-1])
                self.assertEqual(row["run_status"], final["status"])
                self.assertEqual(row["runtime_completed"], final["status"] == "completed")
                self.assertEqual(row["unknown"], final["status"] == "unknown")
                self.assertEqual(row["task_success"], row["runtime_completed"] and row["artifact_correct"])

    def test_normal_policies_produce_identical_correct_outputs_and_call_counts(self):
        for task in TASKS:
            with self.subTest(task=task):
                a, b = (self.rows[task, "normal", policy] for policy in POLICIES)
                for row in (a, b):
                    self.assert_correct_files(row)
                    self.assertTrue(row["runtime_completed"])
                    self.assertTrue(row["artifact_correct"])
                    self.assertTrue(row["task_success"])
                    self.assertTrue(row["readback_verified"])
                    self.assertEqual(row["attempt_count"], 1)
                    self.assertEqual(row["repeated_effect_calls"], 0)
                    self.assertEqual(row["duplicate_business_records"], 0)
                    self.assertEqual(row["write_calls"] + row["append_calls"], 2)
                for key in ("model_calls", "tool_calls", "read_calls", "write_calls", "append_calls", "artifact_sha256"):
                    self.assertEqual(a[key], b[key])
                self.assertEqual(a["attempts"][0]["limits"], b["attempts"][0]["limits"])

    def test_safe_checkpoint_resumes_only_remaining_write_with_original_run_and_budget(self):
        for task in TASKS:
            with self.subTest(task=task):
                row = self.rows[task, "safe-after-first-write", "checkpoint_resume"]
                normal = self.rows[task, "normal", "checkpoint_resume"]
                self.assert_correct_files(row)
                self.assertTrue(row["task_success"])
                self.assertTrue(row["boundary"]["reached"])
                self.assertEqual(row["boundary"]["exit_code"], 73)
                self.assertEqual(row["attempt_count"], 1)
                effects = [item for item in self.ledger(row) if item["kind"] in {"write", "append"}]
                self.assertEqual([(item["call_id"], item["stage"]) for item in effects],
                                 [("effect-1", "initial"), ("effect-2", "recovery")])
                self.assertEqual(len({item["run_id"] for item in effects}), 1)
                self.assertEqual(row["repeated_effect_calls"], 0)
                self.assertEqual(row["attempts"][0]["limits"], normal["attempts"][0]["limits"])
                self.assertEqual((row["model_calls"], row["tool_calls"]), (normal["model_calls"], normal["tool_calls"]))

    def test_unknown_checkpoint_can_have_correct_artifacts_without_being_task_success(self):
        for task in TASKS:
            with self.subTest(task=task):
                row = self.rows[task, "effect-before-checkpoint", "checkpoint_resume"]
                self.assert_correct_files(row)
                self.assertEqual(row["run_status"], "unknown")
                self.assertTrue(row["unknown"])
                self.assertTrue(row["needs_review"])
                self.assertTrue(row["artifact_correct"])
                self.assertFalse(row["runtime_completed"])
                self.assertFalse(row["task_success"])
                self.assertEqual(row["attempt_count"], 1)
                self.assertEqual(row["boundary"]["exit_code"], 73)
                observations = self.ledger(row)
                effects = [item for item in observations if item["kind"] in {"write", "append"}]
                self.assertEqual([item["call_id"] for item in effects], ["effect-1", "effect-2"])
                self.assertTrue(all(item["stage"] == "initial" for item in observations))
                self.assertEqual(row["repeated_effect_calls"], 0)
                self.assertEqual(row["duplicate_business_records"], 0)
                original = self.read_json(self.relative_path(row["paths"]["initial_result"]))["checkpoint"]
                self.assertTrue(original["inflight"]["side_effect"])
                self.assertEqual(original["inflight"]["call_id"], "effect-2")
                state = self.state(row["attempts"][0])
                results = [item["tool_result"] for item in state["messages"] if item.get("tool_result")]
                self.assertIn("effect-1", {item["call_id"] for item in results})
                self.assertNotIn("effect-2", {item["call_id"] for item in results})

    def test_naive_rerun_retains_old_database_and_has_an_additional_attempt_budget(self):
        for task in TASKS:
            for condition in CONDITIONS[1:]:
                with self.subTest(task=task, condition=condition):
                    row = self.rows[task, condition, "naive_rerun"]
                    comparison = self.rows[task, condition, "checkpoint_resume"]
                    self.assertEqual(row["attempt_count"], 2)
                    original, rerun = row["attempts"]
                    self.assertNotEqual(original["run_id"], rerun["run_id"])
                    self.assertNotEqual(original["database_path"], rerun["database_path"])
                    first_state, final_state = self.state(original), self.state(rerun)
                    self.assertEqual(first_state["status"], "running")
                    first_result = self.read_json(self.relative_path(row["paths"]["initial_result"]))
                    self.assertEqual(first_state, first_result["checkpoint"])
                    initial_unknown = condition == "effect-before-checkpoint"
                    self.assertEqual(row["initial_inflight_unknown"], initial_unknown)
                    self.assertEqual(row["needs_review"], initial_unknown or row["unknown"])
                    self.assertEqual(bool(first_state.get("inflight")), initial_unknown)
                    if initial_unknown:
                        self.assertTrue(first_state["inflight"]["side_effect"])
                    self.assertEqual(original["limits"], rerun["limits"])
                    self.assertEqual(original["limits"], comparison["attempts"][0]["limits"])
                    self.assertEqual(final_state["limits"], original["limits"])
                    # These are equal per-attempt limits, not equal logical-job budgets.
                    self.assertGreater(sum(attempt["limits"]["max_model_calls"] for attempt in row["attempts"]),
                                       sum(attempt["limits"]["max_model_calls"] for attempt in comparison["attempts"]))
                    effects = [item for item in self.ledger(row) if item["kind"] in {"write", "append"}]
                    expected_calls = 3 if condition == "safe-after-first-write" else 4
                    self.assertEqual(len(effects), expected_calls)
                    self.assertEqual(row["repeated_effect_calls"], expected_calls - 2)
                    self.assertEqual(sum(item["stage"] == "rerun" for item in effects), 2)
                    self.assertGreater(row["model_calls"], comparison["model_calls"])

    def test_repeated_atomic_overwrites_and_duplicate_business_history_are_distinct(self):
        for task in TASKS:
            for condition in CONDITIONS:
                with self.subTest(task=task, condition=condition):
                    row = self.rows[task, condition, "naive_rerun"]
                    duplicate_history = task == "config-change" and condition == "effect-before-checkpoint"
                    self.assert_correct_files(row, history_count=2 if duplicate_history else 1)
                    self.assertEqual(row["duplicate_business_records"], 1 if duplicate_history else 0)
                    self.assertEqual(row["artifact_correct"], not duplicate_history)
                    self.assertEqual(row["task_success"], not duplicate_history)
                    if duplicate_history:
                        self.assertFalse(row["readback_verified"])
                    else:
                        self.assertTrue(row["readback_verified"])

    def test_readback_verification_is_supported_by_real_reads_and_persisted_results(self):
        for row in self.rows.values():
            with self.subTest(row=row["id"]):
                final = self.state(row["attempts"][-1])
                reads = [item for item in self.ledger(row)
                         if item["kind"] == "read" and item["call_id"].startswith("verify-")]
                if row["unknown"]:
                    self.assertEqual(reads, [])
                    self.assertFalse(row["readback_verified"])
                    continue
                self.assertEqual({item["call_id"] for item in reads}, {"verify-1", "verify-2"})
                self.assertEqual(len(reads), 2)
                results = {item["tool_result"]["call_id"]: item["tool_result"]
                           for item in final["messages"] if item.get("tool_result")}
                for observation in reads:
                    raw = (self.workspace(row) / observation["path"]).read_bytes()
                    self.assertEqual(observation["content_sha256"], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(observation["bytes"], len(raw))
                    result = results[observation["call_id"]]
                    self.assertEqual(result["name"], "lab.read")
                    self.assertFalse(result.get("error_code"))
                    self.assertEqual(result["value"], {"path": observation["path"], "content": raw.decode("utf-8")})
                expected_reply = "task_verified" if row["artifact_correct"] else "task_verification_failed"
                self.assertEqual(final["output"], expected_reply)

    def test_source_input_and_task_fingerprints_match_retained_evidence(self):
        source_root = Path(__file__).resolve().parents[1]
        fingerprints = self.report["source_files_sha256"]
        self.assertIn("scripts/task_recovery_lab.py", fingerprints)
        for name, fingerprint in fingerprints.items():
            source = (source_root / name).resolve()
            self.assertTrue(source.is_relative_to(source_root))
            self.assertEqual(fingerprint, hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(set(self.report["task_fingerprints"]), set(TASKS))
        for task in TASKS:
            fixture = self.read_json(self.relative_path(self.report["task_fixture_paths"][task]))
            canonical = json.dumps(fixture, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            self.assertEqual(self.report["task_fingerprints"][task], hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        for row in self.rows.values():
            with self.subTest(row=row["id"]):
                root = self.workspace(row)
                self.assertTrue(row["input_unchanged"])
                for name, fingerprint in {**row["input_sha256"], **row["artifact_sha256"]}.items():
                    path = (root / name).resolve()
                    self.assertTrue(path.is_relative_to(root))
                    self.assertEqual(fingerprint, hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual({self.relative_path(name) for name in row["artifact_paths"].values()},
                                 {(root / name).resolve() for name in row["artifact_sha256"]})

    def test_aggregate_sample_sizes_and_metrics_use_exactly_the_observed_rows(self):
        self.assertEqual(len(self.report["aggregates"]), 18)
        aggregate_keys = set()
        for aggregate in self.report["aggregates"]:
            key = aggregate["task"], aggregate["condition"], aggregate["policy"]
            self.assertNotIn(key, aggregate_keys)
            aggregate_keys.add(key)
            row = self.rows[key]
            self.assertEqual(aggregate["n"], 1)
            self.assertEqual(aggregate["passed"], int(row["passed"]))
            self.assertEqual(aggregate["task_successes"], int(row["task_success"]))
            self.assertEqual(aggregate["artifact_correct_count"], int(row["artifact_correct"]))
            self.assertEqual(aggregate["unknown_count"], int(row["unknown"]))
            for name in ("duration_ms", "model_calls", "tool_calls", "repeated_effect_calls", "duplicate_business_records"):
                self.assertEqual(aggregate["metrics"][name], {"min": row[name], "median": row[name], "max": row[name]})
        self.assertEqual(aggregate_keys, set(self.rows))


class TaskRecoveryFailureEvidenceTests(unittest.TestCase):
    def test_boundary_rejects_invalid_pid_or_a_different_checkpoint(self):
        pid = os.getpid() + 10000
        signal = {"name": "effect-before-checkpoint", "reached": True, "run_id": "run-1", "pid": pid,
                  "checkpoint": {"id": "run-1", "execution_process": str(pid) + "-" + "b" * 32}}
        self.assertEqual(task_recovery_lab._validate_boundary(signal, "effect-before-checkpoint"), pid)
        for value in (True, 0, -1, str(pid), 2 ** 32, os.getpid()):
            with self.subTest(pid=value), self.assertRaises(RuntimeError):
                task_recovery_lab._validate_boundary({**signal, "pid": value}, "effect-before-checkpoint")
        for change in ({"name": "other"}, {"reached": 1}, {"run_id": "other-run"},
                       {"checkpoint": {"id": "run-1", "execution_process": "1-" + "b" * 32}}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                task_recovery_lab._validate_boundary({**signal, **change}, "effect-before-checkpoint")

    def test_driver_failure_after_real_crash_preserves_original_evidence(self):
        # Only recovery is replaced: initial writes, checkpoint and exit are real.
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(task_recovery_lab, "TASK_IDS", ("config-change",)), \
                patch.object(task_recovery_lab, "CONDITIONS", ("effect-before-checkpoint",)), \
                patch.object(task_recovery_lab, "POLICIES", ("checkpoint_resume",)), \
                patch.object(task_recovery_lab, "_execute_recovery", side_effect=TimeoutError("recovery unavailable")) as recovery:
            report = task_recovery_lab.run_suite(temporary, repetitions=1)
            recovery.assert_called_once()
            self.assertEqual(len(report["rows"]), 1)
            row = report["rows"][0]
            root = Path(temporary) / report["directory"]
            self.assertFalse(report["passed"])
            self.assertFalse(row["passed"])
            self.assertFalse(row["task_success"])
            self.assertEqual((row["error_type"], row["phase"]), ("TimeoutError", "recovery_wait"))
            self.assertEqual((report["summary"]["failed"], report["summary"]["task_successes"]), (1, 0))
            self.assertTrue(row["boundary"]["reached"])
            self.assertEqual(row["boundary"]["exit_code"], 73)
            self.assertIsInstance(row["boundary"]["pid"], int)
            checkpoint_path = root / row["paths"]["checkpoint"]
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            initial = json.loads((root / row["paths"]["initial_result"]).read_text(encoding="utf-8"))
            self.assertEqual(checkpoint, initial["checkpoint"])
            self.assertEqual(checkpoint["status"], "running")
            self.assertEqual(checkpoint["inflight"]["call_id"], "effect-2")
            self.assertTrue(checkpoint["inflight"]["side_effect"])
            database = checkpoint_path.parent / "initial/runs.sqlite3"
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA query_only=ON")
                record = connection.execute("SELECT state FROM runs WHERE id=?", (checkpoint["id"],)).fetchone()
            self.assertEqual(json.loads(record[0]), checkpoint)
            observations = [json.loads(line) for line in (root / row["paths"]["ledger"]).read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(item["stage"] == "initial" for item in observations))
            self.assertEqual([(item["kind"], item["call_id"]) for item in observations if item["kind"] in {"write", "append"}],
                             [("write", "effect-1"), ("append", "effect-2")])
            workspace = root / row["paths"]["workspace"]
            self.assertEqual(json.loads((workspace / "output/config.json").read_text(encoding="utf-8")), UPDATED_CONFIG)
            history = [json.loads(line) for line in (workspace / "output/history.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(history, [CHANGE_RECORD])
            self.assertFalse((root / row["paths"]["final_result"]).exists())
            self.assertEqual(json.loads((root / report["report"]).read_text(encoding="utf-8")), report)
            self.assertEqual(report["aggregates"][0]["n"], 1)
            self.assertEqual(report["aggregates"][0]["passed"], 0)
            self.assertIsNone(report["aggregates"][0]["metrics"]["model_calls"])


class TaskArtifactOracleTests(unittest.TestCase):
    def test_correct_independent_literal_artifacts_pass_without_model_output(self):
        for task in TASKS:
            with self.subTest(task=task), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                correct_workspace(root, task)
                result = task_recovery_lab.score_artifacts(task, root)
                self.assertTrue(result["artifact_correct"])
                self.assertTrue(result["input_unchanged"])
                self.assertEqual(result["duplicate_business_records"], 0)

    def test_wrong_digest_and_normalized_values_fail_despite_other_correct_outputs(self):
        for task in ("materials", "csv-normalization"):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                correct_workspace(root, task)
                if task == "materials":
                    incorrect = json.loads(json.dumps(DIGEST))
                    incorrect["items"][0]["source"] = "fabricated.md"
                    write_json(root / "output/digest.json", incorrect)
                else:
                    incorrect = json.loads(json.dumps(NORMALIZED))
                    incorrect[0]["hours"] = 999
                    write_json(root / "output/normalized.json", incorrect)
                self.assertFalse(task_recovery_lab.score_artifacts(task, root)["artifact_correct"])

    def test_duplicate_history_is_rejected_even_when_current_config_is_correct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            correct_workspace(root, "config-change")
            with (root / "output/history.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(CHANGE_RECORD) + "\n")
            result = task_recovery_lab.score_artifacts("config-change", root)
            self.assertEqual(json.loads((root / "output/config.json").read_text(encoding="utf-8")), UPDATED_CONFIG)
            self.assertEqual(result["duplicate_business_records"], 1)
            self.assertFalse(result["artifact_correct"])

    def test_config_oracle_rejects_equal_in_python_but_different_json_types(self):
        for field, value in (("json", 0), ("workers", 4.0)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                correct_workspace(root, "config-change")
                incorrect = json.loads(json.dumps(UPDATED_CONFIG))
                if field == "json":
                    incorrect["logging"]["json"] = value
                else:
                    incorrect["workers"] = value
                self.assertEqual(incorrect, UPDATED_CONFIG, "the counterexample must expose Python's equality coercion")
                write_json(root / "output/config.json", incorrect)
                self.assertFalse(task_recovery_lab.score_artifacts("config-change", root)["artifact_correct"])


if __name__ == "__main__":
    unittest.main()
