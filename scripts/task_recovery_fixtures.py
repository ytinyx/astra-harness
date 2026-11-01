"""Small synthetic tasks, real tools, and a separate literal artifact oracle."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile

from astra_harness.application import Application
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.tools import Tool


PLAN_VERSION = "fixed-read-transform-write-readback-v1"
LIMITS = {"max_model_calls": 12, "max_tool_calls": 24,
          "model_timeout": 10, "tool_timeout": 10, "run_timeout": 60}
PERMISSIONS = ["task.read", "task.write", "task.append"]
TASKS = {
    "materials": {
        "inputs": {
            "inputs/research.md": "# Runtime notes\n- Checkpoints preserve progress.\n- Approval is bound to arguments.\n",
            "inputs/recovery.md": "# Recovery notes\n- Unknown effects require review.\n- Budgets survive restarts.\n",
        },
        "operations": [("write", "output/digest.json"), ("write", "output/report.md")],
    },
    "csv-normalization": {
        "inputs": {"inputs/people.csv": "name,team,hours\n Alice , core ,2\nBob,platform,3\nalice,core,1\nCara,platform,4\n"},
        "operations": [("write", "output/normalized.json"), ("write", "output/totals.json")],
    },
    "config-change": {
        "inputs": {
            "inputs/original.json": '{"service":"demo","workers":2,"logging":{"level":"info","json":false}}\n',
            "inputs/patch.json": '{"workers":4,"logging":{"level":"debug"}}\n',
        },
        "operations": [("write", "output/config.json"), ("append", "output/history.jsonl")],
    },
}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def expected_artifacts(task):
    """Literal oracle: never calls the model's parsing or transformation functions."""
    if task == "materials":
        return {
            "output/digest.json": {"items": [
                {"source": "inputs/research.md", "line": 2, "text": "Checkpoints preserve progress."},
                {"source": "inputs/research.md", "line": 3, "text": "Approval is bound to arguments."},
                {"source": "inputs/recovery.md", "line": 2, "text": "Unknown effects require review."},
                {"source": "inputs/recovery.md", "line": 3, "text": "Budgets survive restarts."},
            ]},
            "output/report.md": "# Evidence digest\n\n- `inputs/research.md:2` Checkpoints preserve progress.\n"
                                "- `inputs/research.md:3` Approval is bound to arguments.\n"
                                "- `inputs/recovery.md:2` Unknown effects require review.\n"
                                "- `inputs/recovery.md:3` Budgets survive restarts.\n",
        }
    if task == "csv-normalization":
        return {"output/normalized.json": [
            {"name": "Alice", "team": "core", "hours": 3},
            {"name": "Bob", "team": "platform", "hours": 3},
            {"name": "Cara", "team": "platform", "hours": 4},
        ], "output/totals.json": {"total_hours": 10, "by_team": {"core": 3, "platform": 7}, "people": 3}}
    if task == "config-change":
        return {"output/config.json": {"service": "demo", "workers": 4, "logging": {"level": "debug", "json": False}},
                "output/history.jsonl": [{"change_id": "tune-demo-v1", "service": "demo", "before_workers": 2,
                                           "after_workers": 4, "before_log_level": "info", "after_log_level": "debug"}]}
    raise ValueError("unknown fixture task")


def fixture_payload(task):
    return {"task": task, "spec": TASKS[task], "expected_artifacts": expected_artifacts(task),
            "plan_version": PLAN_VERSION, "limits": LIMITS, "permissions": PERMISSIONS,
            "approval": "trusted fixture authorization; approval is not an experimental variable"}


def prepare_workspace(task, workspace):
    """Seed exactly once, before the first attempt. Reruns never call this function."""
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=False)
    (root / "inputs").mkdir()
    (root / "output").mkdir()
    for name, content in TASKS[task]["inputs"].items():
        (root / name).write_bytes(content.encode("utf-8"))


def parse_artifact(name, content):
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in content.splitlines() if line.strip()]
    return json.loads(content) if name.endswith(".json") else content


def artifact_matches(name, content, expected):
    # Python equality conflates False/0 and 3/3.0. Preserve JSON value types in the
    # oracle while allowing harmless object-key ordering and whitespace changes.
    actual = parse_artifact(name, content)
    options = {"sort_keys": True, "ensure_ascii": False, "allow_nan": False, "separators": (",", ":")}
    return json.dumps(actual, **options) == json.dumps(expected, **options)


def score_artifacts(task, workspace):
    root, checks, hashes, duplicate_records = Path(workspace), {}, {}, 0
    for name, expected in expected_artifacts(task).items():
        try:
            raw = (root / name).read_bytes()
            hashes[name] = hashlib.sha256(raw).hexdigest()
            actual = parse_artifact(name, raw.decode("utf-8"))
            checks[name] = artifact_matches(name, raw.decode("utf-8"), expected)
            if name.endswith("history.jsonl") and isinstance(actual, list):
                duplicate_records = max(0, sum(isinstance(item, dict) and item.get("change_id") == "tune-demo-v1"
                                               for item in actual) - 1)
        except (OSError, ValueError, UnicodeError):
            hashes.setdefault(name, None)
            checks[name] = False
    input_hashes, unchanged = {}, True
    for name, original in TASKS[task]["inputs"].items():
        try:
            raw = (root / name).read_bytes()
        except OSError:
            input_hashes[name], unchanged = None, False
        else:
            input_hashes[name] = hashlib.sha256(raw).hexdigest()
            unchanged &= raw == original.encode("utf-8")
    return {"artifact_correct": all(checks.values()), "input_unchanged": unchanged,
            "duplicate_business_records": duplicate_records, "checks": checks,
            "artifact_sha256": hashes, "input_sha256": input_hashes}


def _transform(task, actual_inputs):
    """Model-side planning uses only text returned by actual input tool calls."""
    if task == "materials":
        items = []
        for name in TASKS[task]["inputs"]:
            for number, line in enumerate(actual_inputs[name].splitlines(), 1):
                if line.startswith("- "):
                    items.append({"source": name, "line": number, "text": line[2:]})
        markdown = "# Evidence digest\n\n" + "".join(f"- `{item['source']}:{item['line']}` {item['text']}\n" for item in items)
        return {"output/digest.json": json_text({"items": items}), "output/report.md": markdown}
    if task == "csv-normalization":
        groups = {}
        for row in csv.DictReader(io.StringIO(actual_inputs["inputs/people.csv"])):
            key = row["name"].strip().title(), row["team"].strip().lower()
            groups[key] = groups.get(key, 0) + int(row["hours"])
        normalized = [{"name": name, "team": team, "hours": hours} for (name, team), hours in sorted(groups.items())]
        totals = {}
        for row in normalized:
            totals[row["team"]] = totals.get(row["team"], 0) + row["hours"]
        return {"output/normalized.json": json_text(normalized),
                "output/totals.json": json_text({"total_hours": sum(totals.values()), "by_team": totals, "people": len(normalized)})}
    original = json.loads(actual_inputs["inputs/original.json"])
    patch = json.loads(actual_inputs["inputs/patch.json"])
    updated = {**original, **patch, "logging": {**original["logging"], **patch.get("logging", {})}}
    entry = {"change_id": "tune-demo-v1", "service": original["service"],
             "before_workers": original["workers"], "after_workers": updated["workers"],
             "before_log_level": original["logging"]["level"], "after_log_level": updated["logging"]["level"]}
    return {"output/config.json": json_text(updated),
            "output/history.jsonl": json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"}


def read_ledger(path):
    candidate = Path(path)
    return [json.loads(line) for line in candidate.read_text(encoding="utf-8").splitlines() if line] if candidate.exists() else []


def _record(ledger, identity, kind, *, path=None, content=None):
    previous = read_ledger(ledger)
    step = 1 + sum(item["attempt_id"] == identity["attempt_id"] and item["kind"] == kind for item in previous)
    if kind == "model":
        call_id = f"model-{step}"
    else:
        call_id = identity["app"].store.get(identity["run_id"])["inflight"]["call_id"]
    entry = {key: identity[key] for key in ("task", "stage", "attempt_id", "run_id")}
    entry.update(kind=kind, call_id=call_id, logical_op=f"{identity['task']}:{kind}:{path or call_id}", step=step, pid=os.getpid())
    if path is not None:
        raw = content.encode("utf-8")
        entry.update(path=path, content_sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    with Path(ledger).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class TaskModel:
    model = PLAN_VERSION

    def __init__(self, task, identity, ledger):
        self.task, self.identity, self.ledger = task, identity, ledger

    def complete(self, request):
        _record(self.ledger, self.identity, "model")
        results = {item.tool_result.call_id: item.tool_result for item in request.messages if item.tool_result is not None}
        input_names = list(TASKS[self.task]["inputs"])
        if any(f"input-{i}" not in results for i in range(1, len(input_names) + 1)):
            return ModelResponse(tool_calls=tuple(ToolCall(f"input-{i}", "lab.read", {"path": name})
                                                 for i, name in enumerate(input_names, 1)))
        inputs = {name: results[f"input-{i}"].value["content"] for i, name in enumerate(input_names, 1)}
        outputs = _transform(self.task, inputs)
        operations = TASKS[self.task]["operations"]
        if any(f"effect-{i}" not in results for i in range(1, len(operations) + 1)):
            return ModelResponse(tool_calls=tuple(ToolCall(f"effect-{i}", f"lab.{kind}", {"path": name, "content": outputs[name]})
                                                 for i, (kind, name) in enumerate(operations, 1)))
        if any(f"verify-{i}" not in results for i in range(1, len(operations) + 1)):
            return ModelResponse(tool_calls=tuple(ToolCall(f"verify-{i}", "lab.read", {"path": name})
                                                 for i, (_, name) in enumerate(operations, 1)))
        verified = all(results[f"verify-{i}"].value["content"] == outputs[name]
                       for i, (_, name) in enumerate(operations, 1))
        return ModelResponse(text="task_verified" if verified else "task_verification_failed")


def build_application(task, workspace, database, ledger, identity):
    root = Path(workspace).resolve()
    inputs = list(TASKS[task]["inputs"])
    outputs = [name for _, name in TASKS[task]["operations"]]

    def target(name, allowed):
        if name not in allowed:
            raise ValueError("fixture path not allowed")
        current = root
        for part in Path(name).parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("fixture link not allowed")
        return current

    def read(path):
        content = target(path, inputs + outputs).read_text(encoding="utf-8")
        _record(ledger, identity, "read", path=path, content=content)
        return {"path": path, "content": content}

    def write(path, content):
        destination = target(path, [name for kind, name in TASKS[task]["operations"] if kind == "write"])
        descriptor, temporary = tempfile.mkstemp(prefix=".task-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _record(ledger, identity, "write", path=path, content=content)
        return {"path": path, "bytes": len(content.encode("utf-8"))}

    def append(path, content):
        destination = target(path, [name for kind, name in TASKS[task]["operations"] if kind == "append"])
        with destination.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _record(ledger, identity, "append", path=path, content=content)
        return {"path": path, "appended_bytes": len(content.encode("utf-8"))}

    tools = []
    for kind, handler, names in (("read", read, inputs + outputs),
                                 ("write", write, [name for op, name in TASKS[task]["operations"] if op == "write"]),
                                 ("append", append, [name for op, name in TASKS[task]["operations"] if op == "append"])):
        if not names:
            continue
        properties = {"path": {"type": "string", "enum": names}}
        if kind != "read":
            properties["content"] = {"type": "string", "maxLength": 100000}
        tools.append(Tool(f"lab.{kind}", f"fixture {kind} within its task workspace", (), handler,
                          input_schema={"type": "object", "properties": properties,
                                        "required": list(properties), "additionalProperties": False},
                          permissions=frozenset({f"task.{kind}"}), side_effect=kind != "read", requires_approval=False))
    app = Application(TaskModel(task, identity, ledger), tools, database=database, workspace=root,
                      permissions=PERMISSIONS, limits=LIMITS, mode="demo", autostart=False)
    identity["app"] = app
    return app
