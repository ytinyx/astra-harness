"""Deterministic task recovery policy comparison, not a general retry benchmark."""

import argparse
from collections import Counter
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

from scripts.task_recovery_fixtures import (
    LIMITS, TASKS, artifact_matches, build_application, expected_artifacts,
    fixture_payload, prepare_workspace, read_ledger, score_artifacts,
)


TASK_IDS = ("materials", "csv-normalization", "config-change")
CONDITIONS = ("normal", "safe-after-first-write", "effect-before-checkpoint")
POLICIES = ("checkpoint_resume", "naive_rerun")
EXIT_CODE = 73
TIMEOUT = 20
SOURCE_FILES = ("scripts/task_recovery_lab.py", "scripts/task_recovery_fixtures.py",
                "astra_harness/application.py", "astra_harness/engine.py", "astra_harness/store.py",
                "astra_harness/tools.py", "astra_harness/contracts.py", "astra_harness/context.py",
                "astra_harness/process_state.py")


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _row_paths(row_dir, suite_dir):
    return {name: (row_dir / filename).relative_to(suite_dir).as_posix() for name, filename in (
        ("workspace", "workspace"), ("ledger", "ledger.jsonl"), ("checkpoint", "checkpoint.json"),
        ("initial_result", "initial-result.json"), ("final_result", "final-result.json"))}


def read_checkpoint(database, run_id):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return json.loads(row[0])


def _view(state):
    fields = ("id", "status", "error", "phase", "index", "inflight", "model_calls", "tool_calls", "limits", "execution_process")
    return {**{key: state.get(key) for key in fields},
            "tool_result_ids": [item["tool_result"]["call_id"] for item in state["messages"] if item.get("tool_result")]}


def _stop_at_boundary(app, condition, run_id):
    print(json.dumps({"name": condition, "reached": True, "pid": os.getpid(), "run_id": run_id,
                      "checkpoint": _view(app.store.get(run_id))}), flush=True)
    os._exit(EXIT_CODE if sys.stdin.readline().strip() == "crash" else EXIT_CODE + 1)


def _install_fault(app, condition):
    save = app.store.save

    def wrapped_save(state, event=None):
        result_ids = [item["tool_result"]["call_id"] for item in state["messages"] if item.get("tool_result")]
        if condition == "effect-before-checkpoint" and "effect-2" in result_ids and not state.get("inflight"):
            prior = app.store.get(state["id"])
            if (prior.get("inflight") or {}).get("call_id") == "effect-2":
                _stop_at_boundary(app, condition, state["id"])
        save(state, event=event)
        if (condition == "safe-after-first-write" and "effect-1" in result_ids and "effect-2" not in result_ids
                and state["phase"] == "tool" and state["index"] == 1 and not state.get("inflight")):
            _stop_at_boundary(app, condition, state["id"])

    app.store.save = wrapped_save


def _worker(task, condition, stage, row_dir, run_id=None):
    attempt = "rerun" if stage == "rerun" else "initial"
    identity = {"task": task, "stage": stage, "attempt_id": attempt, "run_id": run_id}
    with build_application(task, row_dir / "workspace", row_dir / attempt / "runs.sqlite3",
                           row_dir / "ledger.jsonl", identity) as app:
        if stage == "initial" and condition != "normal":
            _install_fault(app, condition)
        initial = app.get_run(run_id) if stage == "recovery" else app.submit(
            "Execute the fixed fixture task and verify all outputs by reading them back.", request_id="task-fixture-request")
        identity["run_id"] = initial["id"]
        app.start()
        if stage == "initial" and condition != "normal":
            threading.Event().wait(TIMEOUT)
            raise TimeoutError("fault boundary was not acknowledged")
        final = app.wait(initial["id"], timeout=15)
    print(json.dumps({"initial": initial, "final": final, "host_limits": app.limits}), flush=True)


def _spawn(task, condition, stage, row_dir, run_id=None):
    command = [sys.executable, "-X", "utf8", "-m", "scripts.task_recovery_lab", "--worker", stage,
               "--task", task, "--condition", condition, "--row-dir", str(row_dir)]
    if run_id:
        command.extend(["--run-id", run_id])
    environment = dict(os.environ)
    for key in ("ASTRA_API_KEY", "ASTRA_MODEL", "ASTRA_BASE_URL"):
        environment.pop(key, None)
    return subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], env=environment,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def _cleanup(process):
    # Only the child represented by this exact Popen handle may be terminated.
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


def _execute_normal(task, condition, stage, row_dir, run_id=None):
    process = _spawn(task, condition, stage, row_dir, run_id)
    try:
        output, error = process.communicate(timeout=TIMEOUT)
        if process.returncode:
            (row_dir / f"{stage}-error.log").write_text(error, encoding="utf-8")
            raise RuntimeError("fixture child failed")
        return json.loads(output)
    finally:
        _cleanup(process)


def _validate_boundary(boundary, condition):
    # Only the Popen-owned stdout pipe supplies this record. A Windows venv
    # launcher forwards the actual worker's stdio; retain both PIDs as evidence,
    # and never use the reported worker PID to terminate any process.
    if not isinstance(boundary, dict):
        raise RuntimeError("invalid boundary handshake")
    pid, checkpoint, run_id = boundary.get("pid"), boundary.get("checkpoint"), boundary.get("run_id")
    if (boundary.get("reached") is not True or boundary.get("name") != condition
            or type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or pid == os.getpid()
            or not isinstance(run_id, str) or not run_id
            or not isinstance(checkpoint, dict) or checkpoint.get("id") != run_id
            or not re.fullmatch(str(pid) + r"-[0-9a-f]{32}", str(checkpoint.get("execution_process", "")))):
        raise RuntimeError("invalid boundary handshake")
    return pid


def _execute_fault(task, condition, row_dir):
    process = _spawn(task, condition, "initial", row_dir)
    received = queue.Queue()
    reader = threading.Thread(target=lambda: received.put(process.stdout.readline()), daemon=True)
    reader.start()
    boundary = None
    try:
        try:
            line = received.get(timeout=TIMEOUT)
        except queue.Empty:
            raise TimeoutError("boundary wait expired") from None
        reader.join(timeout=1)
        boundary = json.loads(line)
        _validate_boundary(boundary, condition)
        boundary["launcher_pid"] = process.pid
        boundary["exit_code_source"] = "owned_popen"
        boundary["exit_code"] = None
        _write_json(row_dir / "boundary.json", boundary)
        _, error = process.communicate("crash\n", timeout=TIMEOUT)
        if process.returncode != EXIT_CODE:
            (row_dir / "initial-error.log").write_text(error, encoding="utf-8")
            raise RuntimeError("fault child did not exit as acknowledged")
        return boundary
    finally:
        if process.poll() is None:
            process.kill()
        reader.join(timeout=2)
        _cleanup(process)
        if boundary is not None:
            boundary["exit_code"] = process.returncode
            _write_json(row_dir / "boundary.json", boundary)


def _execute_recovery(task, condition, policy, row_dir, run_id):
    # Naive means one NEW attempt, same fixed plan and workspace, no extra dedup.
    # Never reopen, mutate or resolve the initial database under this policy.
    return _execute_normal(task, condition, "recovery" if policy == "checkpoint_resume" else "rerun",
                           row_dir, run_id if policy == "checkpoint_resume" else None)


def _calls(entries):
    counts = Counter(item["kind"] for item in entries)
    return {"model": counts["model"], "read": counts["read"], "write": counts["write"], "append": counts["append"],
            "tool": counts["read"] + counts["write"] + counts["append"]}


def _readback_verified(task, final):
    results = {item["tool_result"]["call_id"]: item["tool_result"] for item in final["messages"] if item.get("tool_result")}
    if final.get("output") != "task_verified":
        return False
    for index, (name, expected) in enumerate(expected_artifacts(task).items(), 1):
        result = results.get(f"verify-{index}")
        try:
            if (not result or result.get("error_code") or result["name"] != "lab.read"
                    or result["value"]["path"] != name or not artifact_matches(name, result["value"]["content"], expected)):
                return False
        except (ValueError, KeyError, TypeError):
            return False
    return True


def _run_row(suite_dir, task, condition, policy, repetition, index):
    started = time.perf_counter()
    row_id = f"{index:03d}-{task}-{condition}-{policy}-r{repetition}"
    row_dir = suite_dir / row_id
    row_dir.mkdir()
    workspace, ledger = row_dir / "workspace", row_dir / "ledger.jsonl"
    prepare_workspace(task, workspace)
    rel = lambda path: Path(path).relative_to(suite_dir).as_posix()
    paths = _row_paths(row_dir, suite_dir)
    initial_db = row_dir / "initial" / "runs.sqlite3"
    _write_json(row_dir / "progress.json", {"phase": "normal_wait" if condition == "normal" else "boundary_wait"})
    if condition == "normal":
        result = _execute_normal(task, condition, "initial", row_dir)
        original = read_checkpoint(initial_db, result["final"]["id"])
        boundary = {"name": "normal", "reached": False, "pid": None, "exit_code": 0, "checkpoint": _view(original)}
        _write_json(row_dir / "initial-result.json", result)
        _write_json(row_dir / "checkpoint.json", original)
    else:
        boundary = _execute_fault(task, condition, row_dir)
        original = read_checkpoint(initial_db, boundary["run_id"])
        _write_json(row_dir / "checkpoint.json", original)
        _write_json(row_dir / "initial-result.json", {"checkpoint": original, "boundary": boundary})
        _write_json(row_dir / "progress.json", {"phase": "recovery_wait"})
        result = _execute_recovery(task, condition, policy, row_dir, original["id"])
    _write_json(row_dir / "final-result.json", result)
    _write_json(row_dir / "progress.json", {"phase": "artifact_scoring"})
    final = result["final"]
    entries = read_ledger(ledger)
    counts = _calls(entries)
    initial_calls = _calls([item for item in entries if item["stage"] == "initial"])
    recovery_calls = _calls([item for item in entries if item["stage"] != "initial"])
    effects = [item for item in entries if item["kind"] in {"write", "append"}]
    repeated = len(effects) - len({item["logical_op"] for item in effects})
    score = score_artifacts(task, workspace)
    readback = _readback_verified(task, final)
    initial_uncertain = bool((original.get("inflight") or {}).get("side_effect")) if condition != "normal" else False
    unknown = final["status"] == "unknown"
    runtime_completed = final["status"] == "completed"
    attempts = []
    for attempt in ("initial", "rerun"):
        observed = [item for item in entries if item["attempt_id"] == attempt]
        if not observed:
            continue
        run_ids = {item["run_id"] for item in observed}
        if len(run_ids) != 1:
            raise RuntimeError("one attempt unexpectedly had multiple run identities")
        run_id = next(iter(run_ids))
        database = row_dir / attempt / "runs.sqlite3"
        state = read_checkpoint(database, run_id)
        attempts.append({"id": attempt, "run_id": run_id, "database_path": rel(database), "limits": state["limits"],
                         "status": state["status"], "state_counters": {key: state[key] for key in ("model_calls", "tool_calls")},
                         "calls": _calls(observed)})
    is_rerun = condition != "normal" and policy == "naive_rerun"
    should_unknown = condition == "effect-before-checkpoint" and policy == "checkpoint_resume"
    expected_correct = not (task == "config-change" and condition == "effect-before-checkpoint" and policy == "naive_rerun")
    expected_repeated = (1 if condition == "safe-after-first-write" else 2) if is_rerun else 0
    checks = {
        "input_unchanged": score["input_unchanged"], "attempt_count": len(attempts) == (2 if is_rerun else 1),
        "same_attempt_limits": all(attempt["limits"] == LIMITS for attempt in attempts),
        "expected_final_status": final["status"] == ("unknown" if should_unknown else "completed"),
        "artifact_outcome_observed": score["artifact_correct"] == expected_correct,
        "readback_outcome_observed": readback == (expected_correct and not should_unknown),
        "repeat_calls_observed": repeated == expected_repeated,
        "business_records_observed": score["duplicate_business_records"] == (0 if expected_correct else 1),
        "actual_effect_count": len(effects) == 2 + expected_repeated,
        "ledger_task_identity": all(item["task"] == task for item in entries),
    }
    if condition != "normal":
        checks.update(real_acknowledged_exit=boundary["exit_code"] == EXIT_CODE,
                      checkpoint_matches_handshake=_view(original) == boundary["checkpoint"])
        if policy == "checkpoint_resume":
            checks["same_run_resumed"] = final["id"] == original["id"]
            if should_unknown:
                checks["no_uncertain_replay"] = not any(item["stage"] != "initial" for item in entries)
                checks["unknown_not_resolved"] = not final.get("resolution")
        else:
            checks["new_run_and_database"] = final["id"] != original["id"] and attempts[-1]["database_path"] != attempts[0]["database_path"]
            checks["original_checkpoint_preserved"] = read_checkpoint(initial_db, original["id"]) == original
    return {"id": row_id, "task": task, "condition": condition, "policy": policy, "repetition": repetition,
            "pair_id": f"{task}/{condition}/r{repetition}", "passed": all(checks.values()), "checks": checks,
            "run_status": final["status"], "runtime_completed": runtime_completed,
            "artifact_correct": score["artifact_correct"], "task_success": runtime_completed and score["artifact_correct"],
            "readback_verified": readback, "unknown": unknown, "initial_inflight_unknown": initial_uncertain,
            "needs_review": unknown or initial_uncertain, "input_unchanged": score["input_unchanged"],
            "model_calls": counts["model"], "tool_calls": counts["tool"], "read_calls": counts["read"],
            "write_calls": counts["write"], "append_calls": counts["append"], "repeated_effect_calls": repeated,
            "duplicate_business_records": score["duplicate_business_records"], "initial_calls": initial_calls,
            "recovery_calls": recovery_calls, "attempts": attempts, "attempt_count": len(attempts), "boundary": boundary,
            "input_sha256": score["input_sha256"], "artifact_sha256": score["artifact_sha256"],
            "artifact_paths": {name: rel(workspace / name) for name in expected_artifacts(task)}, "paths": paths,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


def aggregate_rows(rows):
    groups = {}
    for row in rows:
        groups.setdefault((row["task"], row["condition"], row["policy"]), []).append(row)
    result = []
    for (task, condition, policy), values in groups.items():
        metrics = {}
        for name in ("duration_ms", "model_calls", "tool_calls", "repeated_effect_calls", "duplicate_business_records"):
            observed = [row[name] for row in values if name in row]
            metrics[name] = {"min": min(observed), "median": statistics.median(observed), "max": max(observed)} if observed else None
        result.append({"task": task, "condition": condition, "policy": policy, "n": len(values),
                       "passed": sum(row["passed"] for row in values), "task_successes": sum(row.get("task_success", False) for row in values),
                       "artifact_correct_count": sum(row.get("artifact_correct", False) for row in values),
                       "unknown_count": sum(row.get("unknown", False) for row in values), "metrics": metrics})
    return result


def _reports(directory, report):
    columns = ("id", "task", "condition", "policy", "repetition", "pair_id", "passed", "run_status", "runtime_completed",
               "artifact_correct", "task_success", "readback_verified", "unknown", "initial_inflight_unknown", "needs_review",
               "input_unchanged", "attempt_count", "model_calls", "tool_calls", "read_calls", "write_calls", "append_calls",
               "repeated_effect_calls", "duplicate_business_records", "duration_ms")
    with (directory / "rows.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["rows"])
    lines = ["# Deterministic task recovery policy comparison", "",
             f"Rows: {len(report['rows'])}; paired trials: {report['summary']['paired_trials']}; independent tasks: {len(TASK_IDS)}.",
             "", "Each cell reports repeated measurements of the same fixture, not additional independent tasks.", "",
             "| Task | Condition | Policy | n | Task success | Artifact correct | Unknown | Tool calls min / median / max |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for group in report["aggregates"]:
        calls = group["metrics"]["tool_calls"]
        display = " / ".join(str(calls[key]) for key in ("min", "median", "max")) if calls else "unavailable"
        lines.append(f"| {group['task']} | {group['condition']} | {group['policy']} | {group['n']} | "
                     f"{group['task_successes']} | {group['artifact_correct_count']} | {group['unknown_count']} | {display} |")
    lines.extend(["", "Task success requires a completed final run and every artifact passing a separate literal oracle. Correct artifacts do not complete an unknown run.",
                  "The model also reads outputs back; verification failure is retained, not replaced by an unconditional success message.",
                  "naive_rerun is one new submission of the same fixed plan with no extra deduplication, not all possible retry strategies.",
                  "Each attempt receives the same limits; a new attempt receives a new budget. Total observed calls include both attempts.",
                  "Atomic overwrite repeats count as repeated effect calls, not duplicate business records. Only config history has an append-specific business duplication check.",
                  "The naive policy preserves the old crashed database unchanged. needs_review remains true for its original uncertain effect even if the new run succeeds.",
                  "Durations use the real parent clock and include child startup and inspection. No injected clock, significance claim, causal performance improvement or complete ablation is established.",
                  "No provider requests were made. Tools are preauthorized by this trusted fixture; these trials do not test approval behavior.", ""])
    (directory / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    _write_json(directory / "report.json", report)


def run_suite(output_dir=".local/task-recovery", repetitions=3):
    if type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise ValueError("repetitions must be an integer from 1 to 10")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="suite-", dir=root))
    (directory / "fixtures").mkdir()
    report = {"schema_version": 1, "suite": "task_recovery_lab", "mode": "deterministic", "provider_requests": 0,
              "directory": directory.name, "report": "report.json", "csv_report": "rows.csv", "summary_report": "summary.md",
              "repetitions": repetitions, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                              "system": platform.system(), "machine": platform.machine()},
              "task_fingerprints": {}, "task_fixture_paths": {}, "rows": []}
    source_root = Path(__file__).resolve().parents[1]
    report["source_files_sha256"] = {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    for task in TASK_IDS:
        payload = fixture_payload(task)
        report["task_fingerprints"][task] = canonical_hash(payload)
        report["task_fixture_paths"][task] = f"fixtures/{task}.json"
        _write_json(directory / "fixtures" / f"{task}.json", payload)
    for task in TASK_IDS:
        for condition in CONDITIONS:
            for repetition in range(1, repetitions + 1):
                for policy in POLICIES:
                    index = len(report["rows"]) + 1
                    started = time.perf_counter()
                    try:
                        row = _run_row(directory, task, condition, policy, repetition, index)
                    except Exception as error:
                        row_id = f"{index:03d}-{task}-{condition}-{policy}-r{repetition}"
                        row_dir = directory / row_id
                        boundary_path = row_dir / "boundary.json"
                        boundary = json.loads(boundary_path.read_text(encoding="utf-8")) if boundary_path.exists() else {"name": condition, "reached": False}
                        progress_path = row_dir / "progress.json"
                        phase = json.loads(progress_path.read_text(encoding="utf-8"))["phase"] if progress_path.exists() else "setup"
                        row = {"id": row_id, "task": task, "condition": condition, "policy": policy, "repetition": repetition,
                               "pair_id": f"{task}/{condition}/r{repetition}", "passed": False, "task_success": False,
                               "error_type": type(error).__name__, "checks": {"driver_completed": False}, "boundary": boundary,
                               "phase": phase, "paths": _row_paths(row_dir, directory),
                               "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
                    report["rows"].append(row)
    report["aggregates"] = aggregate_rows(report["rows"])
    report["summary"] = {"rows": len(report["rows"]), "paired_trials": len({row["pair_id"] for row in report["rows"]}),
                         "independent_tasks": len(TASK_IDS), "passed": sum(row["passed"] for row in report["rows"]),
                         "failed": sum(not row["passed"] for row in report["rows"]),
                         "task_successes": sum(row.get("task_success", False) for row in report["rows"]),
                         "unknown_count": sum(row.get("unknown", False) for row in report["rows"])}
    report["passed"] = report["summary"]["failed"] == 0
    _reports(directory, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare two deterministic local task recovery policies.")
    parser.add_argument("--output-dir", default=".local/task-recovery")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--worker", choices=("initial", "recovery", "rerun"), help=argparse.SUPPRESS)
    parser.add_argument("--task", choices=TASK_IDS, help=argparse.SUPPRESS)
    parser.add_argument("--condition", choices=CONDITIONS, help=argparse.SUPPRESS)
    parser.add_argument("--row-dir", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if not args.task or not args.condition or not args.row_dir or (args.worker == "recovery" and not args.run_id):
            parser.error("worker fixture arguments are incomplete")
        _worker(args.task, args.condition, args.worker, Path(args.row_dir).resolve(), args.run_id)
        return 0
    report = run_suite(args.output_dir, args.repetitions)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
