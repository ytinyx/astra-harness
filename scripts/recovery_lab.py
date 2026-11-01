"""Deterministic, local crash experiments; never a production runtime switch.

Each child uses a fixture model and an append-only, fsync'ed effect ledger. A
Store.save wrapper stops at an observed boundary; only a parent acknowledgement
causes os._exit. This tests process interruption, not power-loss durability.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import astra_harness.engine as engine_module
from astra_harness.application import Application
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.engine import Engine
from astra_harness.store import Store
from astra_harness.tools import Tool


CASE_IDS = (
    "safe-boundary", "inflight-before-handler", "effect-before-checkpoint",
    "waiting-approval", "changed-arguments", "trace-read-only",
    "batch-safe-boundary", "batch-effect-before-checkpoint", "model-inflight",
    "read-inflight", "model-budget-exhausted", "time-budget-exhausted",
)
BATCH_CASES = {"batch-safe-boundary", "batch-effect-before-checkpoint"}
UNKNOWN_CASES = {"inflight-before-handler", "effect-before-checkpoint", "batch-effect-before-checkpoint"}
CRASH_EXIT_CODE = 73
CHILD_TIMEOUT = 20
LIMITS = {"max_model_calls": 2, "max_tool_calls": 1,
          "model_timeout": 10, "tool_timeout": 10, "run_timeout": 30}
SOURCE_FILES = ("scripts/recovery_lab.py", "astra_harness/application.py", "astra_harness/engine.py",
                "astra_harness/store.py", "astra_harness/tools.py", "astra_harness/contracts.py",
                "astra_harness/context.py", "astra_harness/process_state.py")


@contextmanager
def _readonly(database):
    # mode=ro retains committed WAL records. immutable=1 would incorrectly ignore
    # a crashed writer's WAL; SQLite may still manage temporary WAL sidecar files.
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def read_checkpoint(database, run_id):
    """Read an existing run without constructing Application or running recovery."""
    with _readonly(database) as connection:
        row = connection.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return json.loads(row[0])


def read_trace(database, run_id, after=0, limit=500):
    """Read one event page; no business calls or database-record writes.

This is a SQLite reader, not CLI inspect. It does not open Application, recover
checkpoints, register tools, or create a missing database. No claim is made that
SQLite cannot create temporary WAL sidecars in a writable directory.
"""
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("invalid trace cursor or page size")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("invalid run id")
    with _readonly(database) as connection:
        if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise KeyError(run_id)
        rows = connection.execute(
            "SELECT seq,data FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (run_id, after, limit),
        ).fetchall()
    return [{**json.loads(data), "seq": seq} for seq, data in rows]


def read_ledger(path):
    """Read the independent fixture call/effect ledger; a missing ledger is empty."""
    candidate = Path(path)
    if not candidate.exists():
        return []
    return [json.loads(line) for line in candidate.read_text(encoding="utf-8").splitlines() if line]


def _append_ledger(path, kind, run_id, stage, arguments=None, *, call_id=None, evidence=None):
    entries = read_ledger(path)
    step = 1 + sum(item["kind"] == kind for item in entries)
    entry = {"kind": kind, "run_id": run_id, "step": step, "stage": stage,
             "call_id": call_id or (f"model-{step}" if kind == "model_call" else "write-1"), "pid": os.getpid()}
    if arguments is not None:
        entry["arguments"] = arguments
    if evidence is not None:
        entry["evidence"] = evidence
    with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class _LedgerModel:
    model = "recovery-fixture-v1"

    def __init__(self, ledger, identity, stage, scenario):
        self.ledger, self.identity, self.stage, self.scenario = ledger, identity, stage, scenario

    def complete(self, request):
        _append_ledger(self.ledger, "model_call", self.identity["run_id"], self.stage)
        if self.scenario == "model-inflight" and self.stage == "initial":
            _boundary(self.scenario, self.identity["app"], self.identity["run_id"])
        if request.messages[-1].role == "tool":
            return ModelResponse(text="fixture complete")
        if self.scenario == "read-inflight":
            return ModelResponse(tool_calls=(ToolCall("read-1", "lab.read", {}),))
        if self.scenario in BATCH_CASES:
            return ModelResponse(tool_calls=(ToolCall("write-1", "lab.write", {"value": 1}),
                                             ToolCall("write-2", "lab.write", {"value": 2})))
        return ModelResponse(tool_calls=(ToolCall("write-1", "lab.write", {"value": 1}),))


def _scenario_limits(scenario, stage="initial"):
    limits = dict(LIMITS)
    if scenario in BATCH_CASES:
        limits["max_tool_calls"] = 2
    if scenario == "model-budget-exhausted":
        limits["max_model_calls"] = 99 if stage == "recovery" else 1
    if scenario == "time-budget-exhausted" and stage == "recovery":
        limits["run_timeout"] = 300
    return limits


def _application(case_dir, scenario, identity, stage):
    ledger = case_dir / "ledger.jsonl"

    def write(value):
        # The append is the real fixture side effect and is independent of SQLite.
        # A second dispatch creates another durable record, making duplicates visible.
        # Correlate with the durable in-flight call; value is independently recorded
        # by the actual handler, so a repeated or wrong write remains observable.
        marker = identity["app"].store.get(identity["run_id"])["inflight"]
        _append_ledger(ledger, "effect", identity["run_id"], stage, {"value": value}, call_id=marker["call_id"])
        return value

    def read():
        data = (case_dir / "input.txt").read_bytes()
        # This is instrumentation, not a business write. The input file is checked
        # unchanged and only kind=effect records count as business side effects.
        _append_ledger(ledger, "read_call", identity["run_id"], stage, call_id="read-1",
                       evidence={"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        if stage == "initial":
            _boundary(scenario, identity["app"], identity["run_id"])
        return len(data)

    if scenario == "read-inflight":
        tool = Tool("lab.read", "read a fixed fixture file", (), read,
                    permissions=frozenset({"lab.read"}))
    else:
        tool = Tool("lab.write", "append one durable fixture record", ("value",), write,
                    side_effect=True, permissions=frozenset({"lab.write"}),
                    requires_approval=scenario in {"waiting-approval", "changed-arguments"})
    app = Application(_LedgerModel(ledger, identity, stage, scenario), (tool,),
                      database=case_dir / "runs.sqlite3", permissions=sorted(tool.permissions),
                      workspace=case_dir, mode="demo", limits=_scenario_limits(scenario, stage), autostart=False)
    identity["app"] = app
    return app


def _inject_elapsed_budget(app):
    # Only this fixture child replaces the engine module's time reference. Never
    # patch time.monotonic itself: asyncio, Application and parent deadlines must
    # keep using the real clock. No saved elapsed value is assigned by the fixture.
    original_time = engine_module.time

    class MonotonicProxy:
        offset = 0.0

        def monotonic(self):
            return original_time.monotonic() + self.offset

    clock = MonotonicProxy()
    engine_module.time = clock
    original_model = app.engine._model

    async def model_then_advance_clock(state, cancelled):
        result = await original_model(state, cancelled)
        if result["status"] == "running" and result["phase"] == "tool":
            clock.offset += state["limits"]["run_timeout"] + 1
        return result

    app.engine._model = model_then_advance_clock
    return original_time


def _checkpoint_view(state):
    keys = ("id", "status", "error", "phase", "index", "inflight", "model_calls", "tool_calls",
            "limits", "elapsed", "execution_process", "approval", "approved")
    return {**{key: state.get(key) for key in keys},
            "tool_results": sum(item.get("tool_result") is not None for item in state["messages"])}


def _boundary(name, app, run_id):
    print(json.dumps({"event": "boundary", "name": name, "run_id": run_id, "pid": os.getpid(),
                      "checkpoint": _checkpoint_view(app.store.get(run_id))}), flush=True)
    # Parent-controlled acknowledgement is an actual synchronization point. EOF or
    # any other command cannot be mistaken for a successful fault injection.
    command = sys.stdin.readline().strip()
    os._exit(CRASH_EXIT_CODE if command == "crash" else CRASH_EXIT_CODE + 1)


def _install_boundary(app, scenario):
    save = app.store.save
    fired = False

    def wrapped_save(state, event=None):
        nonlocal fired
        marker = state.get("inflight")
        ledger = read_ledger(Path(app.workspace) / "ledger.jsonl")
        effects = sum(item["kind"] == "effect" for item in ledger)
        is_tool_marker = bool(marker and marker.get("phase") == "tool" and marker.get("side_effect"))
        target_call = "write-2" if scenario == "batch-effect-before-checkpoint" else "write-1"
        has_result = any(item.get("tool_result", {}).get("call_id") == target_call
                         for item in state["messages"] if item.get("tool_result"))
        # A committed tool.end event is not a committed checkpoint. Intercept before
        # save clears the on-disk inflight marker, after the real handler returned.
        expected_effects = 2 if scenario == "batch-effect-before-checkpoint" else 1
        if (not fired and scenario in {"effect-before-checkpoint", "batch-effect-before-checkpoint"}
                and not marker and effects == expected_effects and has_result):
            previous = app.store.get(state["id"])
            if (previous.get("inflight") or {}).get("side_effect"):
                fired = True
                _boundary(scenario, app, state["id"])
        save(state, event=event)
        match = (
            scenario == "safe-boundary" and state["phase"] == "tool" and not marker
            and state["model_calls"] == 1 and state["tool_calls"] == 0 and state["status"] == "running"
        ) or (
            scenario == "inflight-before-handler" and is_tool_marker and effects == 0
        ) or (
            scenario == "waiting-approval" and state["status"] == "waiting_approval"
        ) or (
            scenario == "changed-arguments" and state["status"] == "pending"
            and state["phase"] == "tool" and bool(state["approved"]) and not marker
        ) or (
            scenario == "batch-safe-boundary" and state["phase"] == "tool" and state["index"] == 1
            and state["tool_calls"] == 1 and not marker and effects == 1
        ) or (
            scenario == "model-budget-exhausted" and state["phase"] == "model"
            and state["model_calls"] == state["limits"]["max_model_calls"] == 1
            and state["tool_calls"] == 1 and not marker and effects == 1
        ) or (
            scenario == "time-budget-exhausted" and state["phase"] == "tool" and not marker
            and state["model_calls"] == 1 and state["elapsed"] >= state["limits"]["run_timeout"]
        )
        if not fired and match:
            fired = True
            _boundary(scenario, app, state["id"])

    app.store.save = wrapped_save


def _initial_worker(case_dir, scenario):
    identity = {}
    with _application(case_dir, scenario, identity, "initial") as app:
        _install_boundary(app, scenario)
        if scenario == "time-budget-exhausted":
            _inject_elapsed_budget(app)
        run = app.submit("append one fixture value", request_id="recovery-lab-request")
        identity["run_id"] = run["id"]
        app.start()
        if scenario == "changed-arguments":
            waiting = app.wait(run["id"], timeout=10)
            if waiting["status"] != "waiting_approval":
                raise RuntimeError("fixture did not reach initial approval")
            app.approve(run["id"])
        # Boundary hooks may be running on the worker thread. Keep this process
        # alive until acknowledged, without normal Application.close cancelling it.
        threading.Event().wait(CHILD_TIMEOUT)
        raise TimeoutError("fixture did not receive the fault acknowledgement")


def _recovery_worker(case_dir, scenario, run_id):
    identity = {"run_id": run_id}
    with _application(case_dir, scenario, identity, "recovery") as app:
        initial = app.get_run(run_id)
        evidence = {"initial": initial, "host_limits": app.limits}
        if scenario in UNKNOWN_CASES:
            app.start()
            for name, operation in (("new_run_blocked", lambda: app.submit("must remain blocked")),
                                    ("resume_blocked", lambda: app.resume(run_id))):
                try:
                    operation()
                except RuntimeError:
                    evidence[name] = True
                else:
                    evidence[name] = False
        else:
            app.start()
            before = app.wait(run_id, timeout=10)
            if scenario in {"waiting-approval", "changed-arguments"}:
                evidence["before_approval"] = before
                evidence["effects_before_approval"] = sum(
                    item["kind"] == "effect" for item in read_ledger(case_dir / "ledger.jsonl"))
                app.approve(run_id)
                app.wait(run_id, timeout=10)
        evidence["final"] = app.get_run(run_id)
    print(json.dumps(evidence), flush=True)


def _complete_worker(case_dir):
    identity = {}
    with _application(case_dir, "trace-read-only", identity, "complete") as app:
        initial = app.submit("append one fixture value", request_id="recovery-lab-request")
        identity["run_id"] = initial["id"]
        app.start()
        final = app.wait(initial["id"], timeout=10)
    print(json.dumps({"initial": initial, "final": final}), flush=True)


def _child_command(stage, scenario, case_dir, run_id=None):
    command = [sys.executable, "-X", "utf8", "-m", "scripts.recovery_lab", "--worker", stage,
               "--scenario", scenario, "--case-dir", str(case_dir)]
    if run_id is not None:
        command.extend(["--run-id", run_id])
    return command


def _spawn(command):
    environment = dict(os.environ)
    for key in ("ASTRA_API_KEY", "ASTRA_MODEL", "ASTRA_BASE_URL"):
        environment.pop(key, None)
    return subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], env=environment,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def _cleanup_owned(process):
    # Only this exact Popen handle is ever terminated; never search for processes
    # by port, name, or an externally supplied PID. Timeout cleanup is not a pass.
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


def _validate_boundary(signal, scenario):
    """Validate a message read only from our owned child's stdout pipe.

    Windows venv launchers forward stdio/exit status from another Python PID.
    The worker identity must match its checkpoint; it is never a kill target.
    """
    if not isinstance(signal, dict):
        raise RuntimeError("child boundary handshake was invalid")
    pid, checkpoint, run_id = signal.get("pid"), signal.get("checkpoint"), signal.get("run_id")
    if (signal.get("event") != "boundary" or signal.get("name") != scenario
            or type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or pid == os.getpid()
            or not isinstance(run_id, str) or not run_id
            or not isinstance(checkpoint, dict) or checkpoint.get("id") != run_id
            or not re.fullmatch(str(pid) + r"-[0-9a-f]{32}", str(checkpoint.get("execution_process", "")))):
        raise RuntimeError("child boundary handshake was invalid")
    return pid


def _crash_child(scenario, case_dir):
    process = _spawn(_child_command("initial", scenario, case_dir))
    evidence = None
    messages = queue.Queue()
    reader = threading.Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        try:
            line = messages.get(timeout=CHILD_TIMEOUT)
        except queue.Empty:
            raise TimeoutError("child did not reach the requested boundary") from None
        reader.join(timeout=1)
        signal = json.loads(line)
        worker_pid = _validate_boundary(signal, scenario)
        evidence = {"run_id": signal["run_id"], "phase": "crash_acknowledgement",
                    "boundary": {"name": scenario, "reached": True, "checkpoint": signal["checkpoint"]},
                    "crash": {"pid": worker_pid, "launcher_pid": process.pid, "exit_code_source": "owned_popen",
                              "exit_code": None, "expected_exit_code": CRASH_EXIT_CODE}}
        _write_json(case_dir / "boundary.json", evidence)
        _, error = process.communicate("crash\n", timeout=CHILD_TIMEOUT)
        if process.returncode != CRASH_EXIT_CODE:
            (case_dir / "child-error.log").write_text(error, encoding="utf-8")
            raise RuntimeError("child did not exit with the acknowledged crash code")
        evidence["phase"] = "checkpoint_read"
        return signal, {"pid": worker_pid, "launcher_pid": process.pid, "exit_code_source": "owned_popen",
                        "exit_code": process.returncode, "expected_exit_code": CRASH_EXIT_CODE}
    finally:
        if process.poll() is None:
            process.kill()
        reader.join(timeout=2)
        _cleanup_owned(process)
        if evidence is not None:
            evidence["crash"]["exit_code"] = process.returncode
            _write_json(case_dir / "boundary.json", evidence)


def _normal_child(stage, scenario, case_dir, run_id=None):
    process = _spawn(_child_command(stage, scenario, case_dir, run_id))
    try:
        output, error = process.communicate(timeout=CHILD_TIMEOUT)
        if process.returncode != 0:
            (case_dir / "child-error.log").write_text(error, encoding="utf-8")
            raise RuntimeError("fixture child failed")
        return json.loads(output)
    finally:
        _cleanup_owned(process)


def _write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _mutate_fixture_arguments(database, run_id):
    # This deliberately edits a private experiment checkpoint. It is not a product
    # API for editing approved calls, nor a defence against hostile database access.
    with_store = Store(database)
    try:
        state = with_store.get(run_id)
        call = state["pending"][state["index"]]
        old_key = Engine.approval_key(call)
        if old_key not in state["approved"] or state.get("inflight"):
            raise RuntimeError("parameter fault requires an approved safe boundary")
        call["arguments"] = {"value": 2}
        for message in state["messages"]:
            for saved_call in message.get("tool_calls", []):
                if saved_call["call_id"] == call["call_id"]:
                    saved_call["arguments"] = {"value": 2}
        with_store.save(state)
        return {"old_key": old_key, "new_key": Engine.approval_key(call),
                "old_arguments": {"value": 1}, "new_arguments": {"value": 2}}
    finally:
        with_store.close()


def _hash_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _all_trace(database, run_id):
    result, after = [], 0
    while True:
        page = read_trace(database, run_id, after=after)
        if not page:
            return result
        result.extend(page)
        after = page[-1]["seq"]


def _run_case(suite_dir, index, scenario):
    started = time.perf_counter()
    case_dir = suite_dir / f"{index:02d}-{scenario}"
    case_dir.mkdir()
    database, ledger = case_dir / "runs.sqlite3", case_dir / "ledger.jsonl"
    paths = {key: str((case_dir / name).relative_to(suite_dir).as_posix()) for key, name in (
        ("database", "runs.sqlite3"), ("ledger", "ledger.jsonl"),
        ("checkpoint_before_recovery", "checkpoint-before-recovery.json"), ("recovery", "recovery.json"))}
    input_digest = None
    if scenario == "read-inflight":
        sample = b"Local recovery fixture: read without changing this file.\n"
        (case_dir / "input.txt").write_bytes(sample)
        input_digest = hashlib.sha256(sample).hexdigest()
        paths["input"] = (case_dir / "input.txt").relative_to(suite_dir).as_posix()
    checks, mutation = {}, None
    if scenario == "trace-read-only":
        recovery = _normal_child("complete", scenario, case_dir)
        run_id = recovery["final"]["id"]
        before = read_checkpoint(database, run_id)
        signal, crash = {"name": "completed-closed-database", "checkpoint": _checkpoint_view(before)}, None
        entries_before = read_ledger(ledger)
        trace_before = _all_trace(database, run_id)
        state_hash, event_hash = _hash_json(before), _hash_json(trace_before)
        ledger_hash = hashlib.sha256(ledger.read_bytes()).hexdigest()
        page_reads, collected, after, repeat_equal = 0, [], 0, True
        while True:
            page = read_trace(database, run_id, after=after, limit=2)
            repeated = read_trace(database, run_id, after=after, limit=2)
            page_reads += 2
            repeat_equal &= page == repeated
            if not page:
                break
            collected.extend(page)
            after = page[-1]["seq"]
        checks.update(
            completed_before_reads=before["status"] == "completed",
            repeat_pages_equal=repeat_equal, pagination_complete=collected == trace_before,
            monotonic_unique_cursor=[item["seq"] for item in collected] == sorted({item["seq"] for item in collected}),
            checkpoint_unchanged=_hash_json(read_checkpoint(database, run_id)) == state_hash,
            events_unchanged=_hash_json(_all_trace(database, run_id)) == event_hash,
            ledger_unchanged=hashlib.sha256(ledger.read_bytes()).hexdigest() == ledger_hash,
        )
        recovery["trace_observation"] = {"page_size": 2, "page_reads": page_reads,
                                         "event_count": len(trace_before), "checkpoint_sha256": state_hash,
                                         "events_sha256": event_hash, "ledger_sha256": ledger_hash}
    else:
        signal, crash = _crash_child(scenario, case_dir)
        run_id = signal["run_id"]
        before = read_checkpoint(database, run_id)
        entries_before = read_ledger(ledger)
        checks["acknowledged_process_exit"] = crash["exit_code"] == CRASH_EXIT_CODE
        checks["observed_checkpoint_matches_handshake"] = signal["checkpoint"] == _checkpoint_view(before)
        checks["checkpoint_from_crashed_process"] = before["execution_process"].startswith(str(crash["pid"]) + "-")
        _write_json(case_dir / "checkpoint-before-recovery.json", before)
        if scenario == "changed-arguments":
            mutation = _mutate_fixture_arguments(database, run_id)
        progress = json.loads((case_dir / "boundary.json").read_text(encoding="utf-8"))
        progress["phase"] = "recovery_wait"
        _write_json(case_dir / "boundary.json", progress)
        recovery = _normal_child("recover", scenario, case_dir, run_id)
    _write_json(case_dir / "checkpoint-before-recovery.json", before)
    _write_json(case_dir / "recovery.json", recovery)
    final = read_checkpoint(database, run_id)
    entries = read_ledger(ledger)
    effects = [item for item in entries if item["kind"] == "effect"]
    effects_before = [item for item in entries_before if item["kind"] == "effect"]
    models = [item for item in entries if item["kind"] == "model_call"]
    reads = [item for item in entries if item["kind"] == "read_call"]
    effect_ids = [(item["run_id"], item["call_id"]) for item in effects]
    duplicate_effects = len(effect_ids) - len(set(effect_ids))
    recovery_calls = {name: sum(item["kind"] == kind and item["stage"] == "recovery" for item in entries)
                      for name, kind in (("model", "model_call"), ("read", "read_call"), ("effect", "effect"))}
    saved_results = [item["tool_result"] for item in before["messages"] if item.get("tool_result")]
    checks.update(
        stable_run_identity=final["id"] == run_id == recovery["final"]["id"],
        original_limits_preserved=final["limits"] == before["limits"] == _scenario_limits(scenario),
        no_duplicate_effect=duplicate_effects == 0,
        ledger_identity_matches=all(item["run_id"] == run_id for item in entries),
        observed_model_calls_match_checkpoint=len(models) == final["model_calls"],
    )
    if scenario in UNKNOWN_CASES:
        expected_effects = {"inflight-before-handler": 0, "effect-before-checkpoint": 1,
                            "batch-effect-before-checkpoint": 2}[scenario]
        expected_saved = ["write-1"] if scenario == "batch-effect-before-checkpoint" else []
        checks.update(
            committed_inflight=bool(before.get("inflight", {}).get("side_effect")),
            result_not_checkpointed=[item["call_id"] for item in saved_results] == expected_saved,
            conservative_unknown=final["status"] == "unknown" and final["error"] == "interrupted_effect_unknown",
            effect_count_exact=len(effects_before) == len(effects) == expected_effects,
            no_recovery_business_calls=not any(item["stage"] == "recovery" for item in entries),
            blocked_new_submission=recovery["new_run_blocked"], blocked_resume=recovery["resume_blocked"],
            unknown_not_resolved=not final.get("resolution"),
            conservative_tool_count=final["tool_calls"] == before["tool_calls"] + 1,
            no_recovered_result=final["messages"] == before["messages"],
        )
        if scenario in {"effect-before-checkpoint", "batch-effect-before-checkpoint"}:
            target = "write-2" if scenario == "batch-effect-before-checkpoint" else "write-1"
            checks["tool_end_can_precede_checkpoint"] = any(
                item["type"] == "tool.end" and item["result"]["call_id"] == target
                for item in _all_trace(database, run_id))
        if scenario == "batch-effect-before-checkpoint":
            checks.update(second_tool_inflight=before["inflight"]["call_id"] == "write-2" and before["index"] == 1,
                          first_result_retained=before["tool_calls"] == 1 and len(saved_results) == 1,
                          distinct_effect_values=[(item["call_id"], item["arguments"]["value"]) for item in effects]
                          == [("write-1", 1), ("write-2", 2)])
    elif scenario in {"model-inflight", "read-inflight"}:
        expected_phase = "model" if scenario == "model-inflight" else "tool"
        counter = "model_calls" if expected_phase == "model" else "tool_calls"
        checks.update(
            non_effect_inflight=before["inflight"]["phase"] == expected_phase and not before["inflight"]["side_effect"],
            interrupted_failure=final["status"] == "failed" and final["error"] == "interrupted",
            conservative_call_count=final[counter] == before[counter] + 1,
            no_business_write=not effects_before and not effects,
            no_recovery_business_calls=not any(item["stage"] == "recovery" for item in entries),
            result_not_checkpointed=not saved_results and final["messages"] == before["messages"],
        )
        if scenario == "model-inflight":
            checks["model_really_entered"] = len(models) == 1 and models[0]["pid"] == crash["pid"] and before["model_calls"] == 0
        else:
            checks.update(read_really_entered=len(reads) == 1 and reads[0]["pid"] == crash["pid"],
                          input_unchanged=hashlib.sha256((case_dir / "input.txt").read_bytes()).hexdigest() == input_digest,
                          read_evidence_matches=len(reads) == 1 and reads[0]["evidence"] == {
                              "sha256": input_digest, "bytes": (case_dir / "input.txt").stat().st_size})
    elif scenario in {"model-budget-exhausted", "time-budget-exhausted"}:
        budget_name = "max_model_calls" if scenario == "model-budget-exhausted" else "run_timeout"
        expected_error = "model_limit" if scenario == "model-budget-exhausted" else "run_timeout"
        expected_effects = 1 if scenario == "model-budget-exhausted" else 0
        checks.update(
            saved_safe_boundary=before.get("inflight") is None,
            exhausted_original_budget=(before["model_calls"] == before["limits"][budget_name]
                                       if scenario == "model-budget-exhausted"
                                       else before["elapsed"] >= before["limits"][budget_name]),
            wider_host_does_not_replenish=recovery["host_limits"][budget_name] > before["limits"][budget_name],
            original_elapsed_preserved=recovery["initial"]["elapsed"] == before["elapsed"],
            budget_failure=final["status"] == "failed" and final["error"] == expected_error,
            no_recovery_business_calls=not any(item["stage"] == "recovery" for item in entries),
            counters_preserved=final["model_calls"] == before["model_calls"] == 1
            and final["tool_calls"] == before["tool_calls"] == expected_effects,
            effects_preserved=len(effects_before) == len(effects) == expected_effects,
        )
    else:
        expected_values = [1, 2] if scenario == "batch-safe-boundary" else [2 if scenario == "changed-arguments" else 1]
        checks.update(completed_once=final["status"] == "completed" and final["tool_calls"] == len(expected_values),
                      effect_value_exact=[item["arguments"] for item in effects] == [{"value": value} for value in expected_values])
        if scenario not in {"trace-read-only", "batch-safe-boundary"}:
            checks["no_effect_before_recovery"] = not effects_before
        if scenario == "batch-safe-boundary":
            checks.update(first_result_committed=[item["call_id"] for item in saved_results] == ["write-1"],
                          second_tool_pending=before["phase"] == "tool" and before["index"] == 1
                          and before["tool_calls"] == 1 and before.get("inflight") is None,
                          first_effect_before_exit=len(effects_before) == 1 and effects_before[0]["call_id"] == "write-1",
                          only_second_effect_on_recovery=[item["call_id"] for item in effects if item["stage"] == "recovery"] == ["write-2"])
        if scenario == "safe-boundary":
            checks["safe_tool_boundary"] = before["phase"] == "tool" and before.get("inflight") is None and before["model_calls"] == 1
        if scenario in {"waiting-approval", "changed-arguments"}:
            waiting = recovery["before_approval"]
            checks.update(waiting_before_new_decision=waiting["status"] == "waiting_approval",
                          no_unapproved_effect=recovery["effects_before_approval"] == 0)
            if scenario == "waiting-approval":
                checks["same_approval_restored"] = waiting["approval"] == before["approval"]
            else:
                checks.update(old_approval_retained=mutation["old_key"] in waiting["approved"],
                              changed_arguments_require_new_key=waiting["approval"]["key"] == mutation["new_key"] != mutation["old_key"],
                              new_key_not_preapproved=mutation["new_key"] not in waiting["approved"])
    return {"id": scenario, "run_id": run_id, "passed": all(checks.values()),
            "task_completed": final["status"] == "completed", "checks": checks,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "clock_mode": "injected_monotonic" if scenario == "time-budget-exhausted" else "real_monotonic",
            "boundary": {"name": signal["name"], "reached": True, "checkpoint": signal["checkpoint"]},
            "crash": crash, "recovered": {key: final.get(key) for key in ("status", "error", "model_calls", "tool_calls")},
            "effects_before_recovery": len(effects_before), "effects_after": len(effects),
            "model_calls_observed": len(models), "read_calls_observed": len(reads),
            "recovery_calls": recovery_calls, "duplicate_effects": duplicate_effects,
            "mutation": mutation, "paths": paths}


def _write_summary(path, report):
    lines = ["# Recovery experiment results", "",
             f"Safety checks passed: {report['summary']['passed']}/{report['summary']['total']}. "
             f"Completed tasks: {report['summary']['task_completed']}.", "",
             "Run statuses: " + ", ".join(f"{key}={value}" for key, value in report["summary"]["by_status"].items()) + ".", "",
             "| Case | Safety pass | Run status / error | Effects before / after | Recovery model / read calls | Driver ms |",
             "| --- | --- | --- | --- | --- | --- |"]
    for case in report["cases"]:
        state = case.get("recovered", {})
        calls = case.get("recovery_calls", {})
        status = state.get("status", "driver_error") + " / " + str(state.get("error") or case.get("error_type") or "—")
        lines.append(f"| {case['id']} | {case['passed']} | {status} | "
                     f"{case.get('effects_before_recovery', '—')} / {case.get('effects_after', '—')} | "
                     f"{calls.get('model', '—')} / {calls.get('read', '—')} | {case['duration_ms']} |")
    lines.extend(["", "`passed` means the expected safety/recovery behavior was observed; an unknown run is not a completed task.",
                  "Driver duration uses the real wall clock and includes process startup, recovery and evidence inspection; it is not model latency.",
                  "`time-budget-exhausted` uses an injected monotonic-clock offset inside its fixture child. Its saved elapsed value is synthetic, not wall-clock performance.",
                  "The model and file-read interruptions are local deterministic fixtures, not provider or network-failure measurements.",
                  "No ablation or performance-improvement conclusion is established by these results.", ""])
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def run_suite(output_dir=".local/recovery-lab"):
    """Run the isolated deterministic process matrix and save its evidence."""
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    suite_dir = Path(tempfile.mkdtemp(prefix="suite-", dir=root))
    report = {"schema_version": 1, "suite": "recovery_lab", "mode": "deterministic", "provider_requests": 0,
              "directory": suite_dir.name, "report": "report.json", "summary_report": "summary.md",
              "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                              "system": platform.system(), "machine": platform.machine()}, "cases": []}
    source_root = Path(__file__).resolve().parents[1]
    report["source_files_sha256"] = {
        name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in SOURCE_FILES
    }
    for index, scenario in enumerate(CASE_IDS, 1):
        started = time.perf_counter()
        try:
            result = _run_case(suite_dir, index, scenario)
        except Exception as error:
            # A timeout, missing handshake or harness error is a failed experiment,
            # never evidence that recovery was safe. Continue independent cases.
            boundary_path = suite_dir / f"{index:02d}-{scenario}" / "boundary.json"
            progress = json.loads(boundary_path.read_text(encoding="utf-8")) if boundary_path.exists() else {
                "phase": "completion_wait" if scenario == "trace-read-only" else "boundary_wait",
                "boundary": {"name": scenario, "reached": False},
            }
            result = {**progress, "id": scenario, "passed": False, "task_completed": False,
                      "checks": {"driver_completed": False}, "error_type": type(error).__name__,
                      "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
        report["cases"].append(result)
    passed = sum(case["passed"] for case in report["cases"])
    by_status = {}
    for case in report["cases"]:
        status = case.get("recovered", {}).get("status", "driver_error")
        by_status[status] = by_status.get(status, 0) + 1
    report["summary"] = {"total": len(CASE_IDS), "passed": passed, "failed": len(CASE_IDS) - passed,
                         "task_completed": sum(case["task_completed"] for case in report["cases"]), "by_status": by_status}
    report["passed"] = passed == len(CASE_IDS)
    _write_summary(suite_dir / "summary.md", report)
    _write_json(suite_dir / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run local deterministic recovery experiments; no provider requests.")
    parser.add_argument("--output-dir", default=".local/recovery-lab")
    parser.add_argument("--worker", choices=("initial", "recover", "complete"), help=argparse.SUPPRESS)
    parser.add_argument("--scenario", choices=CASE_IDS, help=argparse.SUPPRESS)
    parser.add_argument("--case-dir", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if not args.case_dir or not args.scenario or (args.worker == "recover" and not args.run_id):
            parser.error("worker requires its fixture paths and identity")
        case_dir = Path(args.case_dir).resolve()
        if args.worker == "initial":
            _initial_worker(case_dir, args.scenario)
        elif args.worker == "recover":
            _recovery_worker(case_dir, args.scenario, args.run_id)
        else:
            _complete_worker(case_dir)
        return 0
    report = run_suite(args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
