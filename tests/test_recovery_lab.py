"""Observe actual child-process crashes, durable effects and read-only evidence."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from astra_harness.process_state import process_has_exited
from scripts import recovery_lab


class RecoveryLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.output = Path(cls.temporary.name) / "experiments"
        # One real twelve-case run supplies independent observations for all assertions.
        cls.report = recovery_lab.run_suite(cls.output)
        cls.directory = cls.output / cls.report["directory"]
        cls.cases = {case["id"]: case for case in cls.report["cases"]}

    def artifact(self, case, name):
        relative = Path(case["paths"][name])
        self.assertFalse(relative.is_absolute())
        path = (self.directory / relative).resolve()
        self.assertTrue(path.is_relative_to(self.directory.resolve()))
        self.assertTrue(path.is_file(), name)
        return path

    def checkpoint(self, case):
        return json.loads(self.artifact(case, "checkpoint_before_recovery").read_text(encoding="utf-8"))

    def recovery(self, case):
        return json.loads(self.artifact(case, "recovery").read_text(encoding="utf-8"))

    def ledger(self, case):
        return [json.loads(line) for line in self.artifact(case, "ledger").read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def saved_state(self, case):
        database = self.artifact(case, "database")
        # Do not instantiate Store or Application: inspection must not run recovery.
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute("SELECT state FROM runs WHERE id=?", (case["run_id"],)).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def test_report_distinguishes_safe_outcomes_from_completed_tasks(self):
        self.assertEqual(self.report["schema_version"], 1)
        self.assertEqual(self.report["suite"], "recovery_lab")
        self.assertEqual(self.report["mode"], "deterministic")
        self.assertEqual(self.report["provider_requests"], 0)
        self.assertEqual(set(self.cases), {
            "safe-boundary", "inflight-before-handler", "effect-before-checkpoint",
            "waiting-approval", "changed-arguments", "trace-read-only",
            "batch-safe-boundary", "batch-effect-before-checkpoint", "model-inflight",
            "read-inflight", "model-budget-exhausted", "time-budget-exhausted",
        })
        self.assertEqual(len(self.report["cases"]), 12)
        self.assertEqual({key: self.report["summary"][key] for key in ("total", "passed", "failed", "task_completed")},
                         {"total": 12, "passed": 12, "failed": 0, "task_completed": 5})
        saved = json.loads((self.directory / self.report["report"]).read_text(encoding="utf-8"))
        self.assertEqual(saved, self.report)
        summary_path = self.directory / self.report["summary_report"]
        self.assertTrue(summary_path.resolve().is_relative_to(self.directory.resolve()))
        summary_text = summary_path.read_text(encoding="utf-8")
        for identifier in self.cases:
            self.assertIn(identifier, summary_text)
        observed_statuses = {}
        for case in self.cases.values():
            with self.subTest(case=case["id"]):
                state = self.saved_state(case)
                self.assertTrue(case["passed"])
                self.assertEqual(case["task_completed"], state["status"] == "completed")
                self.assertEqual(case["recovered"]["status"], state["status"])
                self.assertEqual(case["recovered"]["tool_calls"], state["tool_calls"])
                self.assertEqual(case["effects_after"], sum(item["kind"] == "effect" for item in self.ledger(case)))
                observed_statuses[state["status"]] = observed_statuses.get(state["status"], 0) + 1
        self.assertEqual(observed_statuses, {"completed": 5, "unknown": 3, "failed": 4})
        self.assertEqual(self.report["summary"]["by_status"], observed_statuses)

    def test_source_fingerprints_identify_the_actual_driver_and_runtime_files(self):
        fingerprints = self.report["source_files_sha256"]
        self.assertTrue({"scripts/recovery_lab.py", "astra_harness/application.py", "astra_harness/engine.py",
                         "astra_harness/store.py"}.issubset(fingerprints))
        root = Path(__file__).resolve().parents[1]
        for name, fingerprint in fingerprints.items():
            with self.subTest(source=name):
                self.assertFalse(Path(name).is_absolute())
                source = (root / name).resolve()
                self.assertTrue(source.is_relative_to(root))
                self.assertEqual(fingerprint, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_faults_are_real_exited_child_processes_at_persisted_boundaries(self):
        for name, case in self.cases.items():
            if name == "trace-read-only":
                self.assertIsNone(case["crash"])
                continue
            with self.subTest(case=name):
                crash = case["crash"]
                before = self.checkpoint(case)
                self.assertTrue(case["boundary"]["reached"])
                self.assertEqual(crash["exit_code"], 73)
                self.assertEqual(crash["expected_exit_code"], 73)
                self.assertIsInstance(crash["launcher_pid"], int)
                self.assertEqual(crash["exit_code_source"], "owned_popen")
                self.assertNotEqual(crash["pid"], os.getpid())
                identity = before["execution_process"]
                self.assertEqual(int(identity.split("-", 1)[0]), crash["pid"])
                self.assertTrue(process_has_exited(identity))
                self.assertEqual(before["id"], case["run_id"])

    def test_saved_safe_boundary_runs_pending_effect_once_with_original_budget(self):
        case = self.cases["safe-boundary"]
        before, state = self.checkpoint(case), self.saved_state(case)
        observations = self.ledger(case)
        effects = [item for item in observations if item["kind"] == "effect"]
        models = [item for item in observations if item["kind"] == "model_call"]
        self.assertEqual(before["phase"], "tool")
        self.assertIsNone(before["inflight"])
        self.assertEqual(before["tool_calls"], 0)
        self.assertEqual(case["effects_before_recovery"], 0)
        self.assertEqual(len(effects), 1)
        self.assertEqual((effects[0]["call_id"], effects[0]["arguments"], effects[0]["stage"]),
                         ("write-1", {"value": 1}, "recovery"))
        self.assertEqual([item["call_id"] for item in models], ["model-1", "model-2"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual((state["model_calls"], state["tool_calls"]), (2, 1))
        self.assertEqual(state["limits"], before["limits"])
        self.assertGreaterEqual(state["elapsed"], before["elapsed"])
        results = [item["tool_result"] for item in state["messages"] if item.get("tool_result")]
        self.assertEqual([result["call_id"] for result in results], ["write-1"])

    def test_uncertain_windows_preserve_actual_zero_or_one_effect_without_replay(self):
        for name, expected_effects in (("inflight-before-handler", 0), ("effect-before-checkpoint", 1)):
            with self.subTest(case=name):
                case = self.cases[name]
                before, state = self.checkpoint(case), self.saved_state(case)
                recovery = self.recovery(case)
                observations = self.ledger(case)
                effects = [item for item in observations if item["kind"] == "effect"]
                self.assertEqual(before["status"], "running")
                self.assertEqual(before["phase"], "tool")
                self.assertTrue(before["inflight"]["side_effect"])
                self.assertFalse(any(item.get("tool_result") for item in before["messages"]))
                self.assertEqual(len(effects), expected_effects)
                if effects:
                    self.assertEqual(effects[0]["pid"], case["crash"]["pid"])
                    self.assertEqual((effects[0]["stage"], effects[0]["arguments"]), ("initial", {"value": 1}))
                self.assertEqual(case["effects_before_recovery"], expected_effects)
                self.assertFalse(any(item["stage"] == "recovery" for item in observations))
                self.assertEqual(recovery["initial"]["status"], "unknown")
                self.assertEqual(recovery["final"]["status"], "unknown")
                self.assertEqual(state["status"], "unknown")
                self.assertEqual(state["error"], "interrupted_effect_unknown")
                self.assertEqual(state["tool_calls"], before["tool_calls"] + 1)
                self.assertIsNone(state["inflight"])
                self.assertFalse(state.get("resolution"))
                self.assertFalse(case["task_completed"])
                self.assertTrue(case["passed"], "preserving uncertainty is the expected safety outcome")
                if name == "effect-before-checkpoint":
                    database = self.artifact(case, "database")
                    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                        events = [json.loads(row[0]) for row in connection.execute(
                            "SELECT data FROM events WHERE run_id=?", (case["run_id"],))]
                    self.assertTrue(any(event["type"] == "tool.end" for event in events))

    def test_batch_safe_boundary_preserves_first_result_and_executes_only_second_effect(self):
        case = self.cases["batch-safe-boundary"]
        before, state = self.checkpoint(case), self.saved_state(case)
        effects = [item for item in self.ledger(case) if item["kind"] == "effect"]
        original_results = [item["tool_result"] for item in before["messages"] if item.get("tool_result")]
        final_results = [item["tool_result"] for item in state["messages"] if item.get("tool_result")]
        self.assertEqual((before["phase"], before["index"], before["model_calls"], before["tool_calls"]),
                         ("tool", 1, 1, 1))
        self.assertIsNone(before["inflight"])
        self.assertEqual([call["call_id"] for call in before["pending"]], ["write-1", "write-2"])
        self.assertEqual([result["call_id"] for result in original_results], ["write-1"])
        self.assertEqual(case["effects_before_recovery"], 1)
        self.assertEqual([(item["call_id"], item["arguments"], item["stage"]) for item in effects],
                         [("write-1", {"value": 1}, "initial"), ("write-2", {"value": 2}, "recovery")])
        self.assertEqual(len({item["call_id"] for item in effects}), 2)
        self.assertEqual(final_results[:1], original_results)
        self.assertEqual([result["call_id"] for result in final_results], ["write-1", "write-2"])
        self.assertEqual((state["status"], state["model_calls"], state["tool_calls"]), ("completed", 2, 2))
        self.assertEqual(state["limits"], before["limits"])
        tools = {spec["name"]: spec for spec in before["tool_snapshot"]}
        self.assertTrue(all(tools[call["name"]]["side_effect"] for call in before["pending"]))

    def test_uncertain_second_batch_effect_does_not_erase_or_reexecute_first_result(self):
        case = self.cases["batch-effect-before-checkpoint"]
        before, recovery, state = self.checkpoint(case), self.recovery(case), self.saved_state(case)
        observations = self.ledger(case)
        effects = [item for item in observations if item["kind"] == "effect"]
        original_results = [item["tool_result"] for item in before["messages"] if item.get("tool_result")]
        final_results = [item["tool_result"] for item in state["messages"] if item.get("tool_result")]
        self.assertEqual((before["phase"], before["index"], before["tool_calls"]), ("tool", 1, 1))
        self.assertEqual(before["inflight"]["call_id"], "write-2")
        self.assertTrue(before["inflight"]["side_effect"])
        self.assertEqual([result["call_id"] for result in original_results], ["write-1"])
        self.assertEqual(final_results, original_results)
        self.assertEqual([(item["call_id"], item["arguments"], item["stage"]) for item in effects],
                         [("write-1", {"value": 1}, "initial"), ("write-2", {"value": 2}, "initial")])
        self.assertEqual(case["effects_before_recovery"], 2)
        self.assertFalse(any(item["stage"] == "recovery" for item in observations))
        self.assertEqual(recovery["initial"]["status"], "unknown")
        self.assertEqual((state["status"], state["error"], state["model_calls"], state["tool_calls"]),
                         ("unknown", "interrupted_effect_unknown", 1, 2))
        self.assertFalse(state.get("resolution"))
        self.assertFalse(case["task_completed"])

    def test_inflight_model_and_read_calls_fail_without_replay_or_business_writes(self):
        for name, phase, before_models, after_tools in (("model-inflight", "model", 0, 0),
                                                      ("read-inflight", "tool", 1, 1)):
            with self.subTest(case=name):
                case = self.cases[name]
                before, state = self.checkpoint(case), self.saved_state(case)
                observations = self.ledger(case)
                self.assertEqual(before["status"], "running")
                self.assertEqual(before["phase"], phase)
                self.assertEqual(before["model_calls"], before_models)
                self.assertEqual(before["tool_calls"], 0)
                self.assertEqual(before["inflight"]["phase"], phase)
                self.assertFalse(before["inflight"]["side_effect"])
                self.assertEqual(sum(item["kind"] == "model_call" for item in observations), 1)
                self.assertFalse(any(item["kind"] == "effect" or item["stage"] == "recovery" for item in observations))
                self.assertEqual((state["status"], state["error"], state["model_calls"], state["tool_calls"]),
                                 ("failed", "interrupted", 1, after_tools))
                self.assertIsNone(state["inflight"])
                self.assertFalse(case["task_completed"])
                self.assertFalse(any(item.get("tool_result") for item in state["messages"]))
                reads = [item for item in observations if item["kind"] == "read_call"]
                self.assertEqual(len(reads), 1 if name == "read-inflight" else 0)
                if reads:
                    data = self.artifact(case, "input").read_bytes()
                    self.assertEqual(reads[0]["call_id"], "read-1")
                    self.assertEqual((reads[0]["stage"], reads[0]["pid"]), ("initial", case["crash"]["pid"]))
                    self.assertEqual(reads[0]["evidence"], {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
                    call = before["pending"][before["index"]]
                    spec = next(tool for tool in before["tool_snapshot"] if tool["name"] == call["name"])
                    self.assertFalse(spec["side_effect"])

    def test_restart_cannot_replenish_an_exhausted_model_call_budget(self):
        case = self.cases["model-budget-exhausted"]
        before, recovery, state = self.checkpoint(case), self.recovery(case), self.saved_state(case)
        observations = self.ledger(case)
        self.assertEqual((before["phase"], before["model_calls"], before["tool_calls"]), ("model", 1, 1))
        self.assertIsNone(before["inflight"])
        self.assertEqual(before["limits"]["max_model_calls"], 1)
        self.assertGreater(recovery["host_limits"]["max_model_calls"], before["limits"]["max_model_calls"])
        self.assertEqual(recovery["initial"]["limits"], before["limits"])
        self.assertEqual(state["limits"], before["limits"])
        self.assertEqual((state["status"], state["error"], state["model_calls"], state["tool_calls"]),
                         ("failed", "model_limit", 1, 1))
        self.assertEqual([item["kind"] for item in observations], ["model_call", "effect"])
        self.assertFalse(any(item["stage"] == "recovery" for item in observations))
        self.assertEqual(case["effects_before_recovery"], 1)
        self.assertFalse(case["task_completed"])

    def test_restart_preserves_injected_elapsed_budget_and_rejects_pending_effect(self):
        case = self.cases["time-budget-exhausted"]
        before, recovery, state = self.checkpoint(case), self.recovery(case), self.saved_state(case)
        self.assertEqual(case["clock_mode"], "injected_monotonic")
        self.assertEqual((before["phase"], before["model_calls"], before["tool_calls"]), ("tool", 1, 0))
        self.assertIsNone(before["inflight"])
        self.assertGreaterEqual(before["elapsed"], before["limits"]["run_timeout"])
        self.assertGreater(recovery["host_limits"]["run_timeout"], before["limits"]["run_timeout"])
        self.assertEqual(recovery["initial"]["limits"], before["limits"])
        self.assertEqual(recovery["initial"]["elapsed"], before["elapsed"])
        self.assertEqual(state["limits"], before["limits"])
        self.assertEqual(state["elapsed"], before["elapsed"])
        self.assertEqual((state["status"], state["error"], state["model_calls"], state["tool_calls"]),
                         ("failed", "run_timeout", 1, 0))
        observations = self.ledger(case)
        self.assertEqual([item["kind"] for item in observations], ["model_call"])
        self.assertFalse(any(item["stage"] == "recovery" for item in observations))
        self.assertFalse(case["task_completed"])

    def test_approval_survives_exit_and_changed_arguments_require_a_new_decision(self):
        case = self.cases["waiting-approval"]
        before, recovery, state = self.checkpoint(case), self.recovery(case), self.saved_state(case)
        self.assertEqual(before["status"], "waiting_approval")
        self.assertEqual(recovery["initial"]["status"], "waiting_approval")
        self.assertEqual(recovery["before_approval"]["approval"], before["approval"])
        self.assertEqual(before["approved"], [])
        self.assertEqual(case["effects_before_recovery"], 0)
        self.assertEqual([item["arguments"] for item in self.ledger(case) if item["kind"] == "effect"], [{"value": 1}])
        self.assertEqual(state["status"], "completed")

        case = self.cases["changed-arguments"]
        before, recovery, state = self.checkpoint(case), self.recovery(case), self.saved_state(case)
        waiting = recovery["before_approval"]
        self.assertEqual(before["status"], "pending")
        self.assertEqual(recovery["initial"]["status"], "pending")
        self.assertEqual(waiting["status"], "waiting_approval")
        self.assertEqual(before["pending"][before["index"]]["arguments"], {"value": 1})
        initial = recovery["initial"]
        self.assertEqual(initial["pending"][initial["index"]]["arguments"], {"value": 2})
        self.assertEqual(waiting["approval"]["arguments"], {"value": 2})
        self.assertEqual(len(before["approved"]), 1)
        self.assertEqual(waiting["approved"], before["approved"])
        self.assertNotIn(waiting["approval"]["key"], before["approved"])
        self.assertIn(waiting["approval"]["key"], state["approved"])
        self.assertEqual(waiting["tool_calls"], 0)
        self.assertEqual(case["effects_before_recovery"], 0)
        self.assertEqual([item["arguments"] for item in self.ledger(case) if item["kind"] == "effect"], [{"value": 2}])
        self.assertEqual(state["status"], "completed")

    def test_trace_pages_replay_identically_without_running_application_or_writing_evidence(self):
        case = self.cases["trace-read-only"]
        database, ledger = self.artifact(case, "database"), self.artifact(case, "ledger")
        before_state = self.saved_state(case)
        before_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (database, ledger)]
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            expected_sequences = [row[0] for row in connection.execute(
                "SELECT seq FROM events WHERE run_id=? ORDER BY seq", (case["run_id"],))]
        self.assertGreater(len(expected_sequences), 2)
        copies = []
        # A trace read must work without constructing a host or starting recovery.
        with patch.object(recovery_lab, "Application", side_effect=AssertionError("trace must not start a host")):
            for _ in range(2):
                events, cursor = [], 0
                while True:
                    page = recovery_lab.read_trace(database, case["run_id"], after=cursor, limit=2)
                    if not page:
                        break
                    self.assertLessEqual(len(page), 2)
                    self.assertTrue(all(event["seq"] > cursor for event in page))
                    events.extend(page)
                    cursor = page[-1]["seq"]
                copies.append(events)
        self.assertEqual(copies[0], copies[1])
        self.assertEqual([event["seq"] for event in copies[0]], expected_sequences)
        self.assertEqual(self.saved_state(case), before_state)
        self.assertEqual([hashlib.sha256(path.read_bytes()).hexdigest() for path in (database, ledger)], before_hashes)
        self.assertEqual(sum(item["kind"] == "effect" for item in self.ledger(case)), 1)

    def test_missing_trace_database_fails_without_creating_an_empty_database(self):
        missing = Path(self.temporary.name) / "missing-trace" / "runs.sqlite3"
        with self.assertRaises((sqlite3.OperationalError, FileNotFoundError)):
            recovery_lab.read_trace(missing, "does-not-exist")
        self.assertFalse(missing.parent.exists())

    def test_recovery_failure_keeps_acknowledged_crash_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            # Keep the initial subprocess and real exit; inject only the later
            # recovery-stage failure so it cannot erase an observed crash boundary.
            with patch.object(recovery_lab, "CASE_IDS", ("safe-boundary",)), \
                    patch.object(recovery_lab, "_normal_child", side_effect=TimeoutError("injected wait failure")):
                report = recovery_lab.run_suite(output)
            case = report["cases"][0]
            self.assertFalse(report["passed"])
            self.assertEqual({key: report["summary"][key] for key in ("total", "passed", "failed", "task_completed")},
                             {"total": 1, "passed": 0, "failed": 1, "task_completed": 0})
            self.assertTrue(case["boundary"]["reached"])
            self.assertEqual(case["crash"]["exit_code"], 73)
            self.assertEqual(case["phase"], "recovery_wait")
            self.assertEqual(case["error_type"], "TimeoutError")
            directory = output / report["directory"] / "01-safe-boundary"
            with closing(sqlite3.connect((directory / "runs.sqlite3").as_uri() + "?mode=ro", uri=True)) as connection:
                before = json.loads(connection.execute("SELECT state FROM runs WHERE id=?", (case["run_id"],)).fetchone()[0])
            ledger = [json.loads(line) for line in (directory / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual((before["status"], before["phase"], before["tool_calls"]), ("running", "tool", 0))
            self.assertTrue(process_has_exited(before["execution_process"]))
            self.assertEqual([item["kind"] for item in ledger], ["model_call"])


class BoundaryHandshakeTests(unittest.TestCase):
    def test_worker_pid_is_bound_to_checkpoint_instead_of_launcher_identity(self):
        pid = os.getpid() + 10000
        signal = {"event": "boundary", "name": "safe-boundary", "run_id": "run-1", "pid": pid,
                  "checkpoint": {"id": "run-1", "execution_process": str(pid) + "-" + "a" * 32}}
        self.assertEqual(recovery_lab._validate_boundary(signal, "safe-boundary"), pid)
        for value in (True, 0, -1, str(pid), 2 ** 32, os.getpid()):
            with self.subTest(pid=value), self.assertRaises(RuntimeError):
                recovery_lab._validate_boundary({**signal, "pid": value}, "safe-boundary")
        for change in ({"name": "wrong"}, {"event": "other"}, {"run_id": "other-run"},
                       {"checkpoint": {"id": "run-1", "execution_process": "1-" + "a" * 32}}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                recovery_lab._validate_boundary({**signal, **change}, "safe-boundary")


if __name__ == "__main__":
    unittest.main()
