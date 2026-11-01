"""Offline local function + independent MCP children + persisted approval demo."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile

from astra_harness import tool_from_function
from astra_harness.application import Application
from astra_harness.builtin.mcp_services import load_mcp_config, MCPServiceManager
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.plugins import PluginManager
from astra_harness.tools import ToolRegistry


HOST_PERMISSIONS = ("demo.total", "mcp.alpha.call", "mcp.beta.call")


def total(values: list[int]):
    """Add actual synthetic quantities returned by the two services."""
    return {"total": sum(values), "count": len(values)}


def _remote_value(result):
    if result.error_code:
        raise ValueError("remote tool did not succeed")
    data = result.value["structuredContent"]
    if type(data.get("value")) is not int:
        raise ValueError("remote quantity is not an integer")
    return data["value"]


class DemoModel:
    """A fixed plan driven by real ToolResults; no model service is contacted."""
    def complete(self, request):
        results = {message.tool_result.name: message.tool_result for message in request.messages
                   if message.tool_result is not None}
        if "mcp.alpha.record" not in results:
            return ModelResponse(tool_calls=(ToolCall("alpha-1", "mcp.alpha.record", {"value": 7}),))
        alpha = _remote_value(results["mcp.alpha.record"])
        if "mcp.beta.record" not in results:
            return ModelResponse(tool_calls=(ToolCall("beta-1", "mcp.beta.record", {"value": 11}),))
        beta = _remote_value(results["mcp.beta.record"])
        if "local.total" not in results:
            return ModelResponse(tool_calls=(ToolCall("total-1", "local.total", {"values": [alpha, beta]}),))
        result = results["local.total"]
        if result.error_code:
            raise ValueError("local total did not succeed")
        return ModelResponse(text=f"Verified tool total: {result.value['total']}")


def prepare(directory):
    """Create only a new example directory; never reuse caller-owned data."""
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "children").mkdir()
    server = Path(__file__).with_name("mcp_demo_server.py").resolve()
    servers = []
    for name in ("alpha", "beta", "broken"):
        command = [sys.executable, "-X", "utf8", str(server), "--service", name,
                   "--ledger", str((directory / "children" / (name + ".jsonl")).resolve())]
        if name == "broken":
            command.append("--bad-initialize")
        servers.append({"name": name, "command": command, "timeout": 5, "revision": "demo-v1"})
    (directory / "mcp.json").write_text(json.dumps({"servers": servers}, indent=2) + "\n", encoding="utf-8")


@contextmanager
def open_host(directory, *, permissions=HOST_PERMISSIONS):
    local = tool_from_function(total, name="local.total", permissions=frozenset({"demo.total"}),
                               side_effect=False, requires_approval=False, implementation_version="demo-total-v1")
    registry = ToolRegistry((local,))
    plugins = PluginManager(registry)
    manager = MCPServiceManager(load_mcp_config(directory / "mcp.json"))
    try:
        plugins.activate(manager)
        registry.freeze()
        with Application(DemoModel(), registry.tools(), database=directory / "runs.sqlite3",
                         workspace=directory, permissions=permissions, mode="mcp-services-demo",
                         service_status=manager.status,
                         limits={"max_model_calls": 4, "max_tool_calls": 3, "run_timeout": 30}) as app:
            yield app, manager
    finally:
        # Stop the Application worker before disposing of its tool transports.
        plugins.close()


def child_records(directory):
    return {name: [json.loads(line) for line in (directory / "children" / (name + ".jsonl"))
                   .read_text(encoding="utf-8").splitlines()]
            for name in ("alpha", "beta", "broken")}


def recorded_values(directory):
    return {name: [item["value"] for item in items if item["event"] == "recorded"]
            for name, items in child_records(directory).items()}


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _healthy_with_failed_service(manager):
    statuses = manager.status()
    _require({item["name"]: item["state"] for item in statuses} ==
             {"alpha": "ready", "beta": "ready", "broken": "failed"}, "unexpected service discovery states")
    _require([name for item in statuses for name in item["tools"]] ==
             ["mcp.alpha.record", "mcp.beta.record"], "unexpected discovered names")
    _require(all(set(item) == {"name", "state", "error_code", "transport", "tools"} for item in statuses),
             "unexpected public status fields")
    return statuses


def _execute(directory, *, approve_second, retained):
    with open_host(directory) as (app, manager):
        first_services = _healthy_with_failed_service(manager)
        run = app.submit("Record the synthetic alpha quantity 7 and beta quantity 11, then calculate their total.")
        first = app.wait(run["id"], timeout=15)
        _require(first["status"] == "waiting_approval" and first["approval"]["name"] == "mcp.alpha.record",
                 "first service did not wait for approval")
        _require(recorded_values(directory) == {"alpha": [], "beta": [], "broken": []},
                 "a service executed before approval")
        app.approve(run["id"])
        second = app.wait(run["id"], timeout=15)
        _require(second["status"] == "waiting_approval" and second["approval"]["name"] == "mcp.beta.record",
                 "second service did not require its own approval")
        _require(recorded_values(directory) == {"alpha": [7], "beta": [], "broken": []},
                 "unexpected receipts before reopening")
        key = second["approval"]["key"]
        _require((second["model_calls"], second["tool_calls"]) == (2, 1), "unexpected pre-reopen budgets")
    closed_services = manager.status()
    _require(all(item["state"] == "closed" for item in closed_services), "manager did not close")

    with open_host(directory) as (app, manager):
        reopened_services = _healthy_with_failed_service(manager)
        restored = app.get_run(run["id"])
        _require(restored["status"] == "waiting_approval" and restored["approval"]["key"] == key,
                 "pending approval did not survive reopening")
        _require((restored["model_calls"], restored["tool_calls"]) == (2, 1), "reopening changed used budgets")
        _require(recorded_values(directory) == {"alpha": [7], "beta": [], "broken": []},
                 "reopening replayed a tool")
        app.approve(run["id"], approved=approve_second)
        final = app.wait(run["id"], timeout=15)
        events = app.events(run["id"])
    records = child_records(directory)
    starts = {name: len({item["instance"] for item in items if item["event"] == "started"})
              for name, items in records.items()}
    _require(starts == {"alpha": 2, "beta": 2, "broken": 2}, "expected fresh child instances after reopen")
    expected_values = {"alpha": [7], "beta": [11] if approve_second else [], "broken": []}
    _require(recorded_values(directory) == expected_values, "independent child receipts mismatch")
    tool_results = [event["result"] for event in events if event["type"] == "tool.end"]
    if approve_second:
        _require(final["status"] == "completed" and final["output"] == "Verified tool total: 18",
                 "final independently expected total mismatch")
        _require((final["model_calls"], final["tool_calls"]) == (4, 3), "unexpected final budgets")
        _require([result["name"] for result in tool_results] ==
                 ["mcp.alpha.record", "mcp.beta.record", "local.total"], "unexpected executed tool order")
        _require(tool_results[-1]["value"] == {"total": 18, "count": 2}, "local result mismatch")
    else:
        _require(final["status"] == "failed" and final["error"] == "approval_denied", "denial was not respected")
        _require((final["model_calls"], final["tool_calls"]) == (2, 1), "denied service was executed")
    trace = [{key: event[key] for key in ("seq", "type", "status", "approved", "result") if key in event}
             for event in events]
    (directory / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "passed": True, "model_kind": "scripted_offline", "provider_requests": 0,
        "protocol_version": "2025-06-18", "second_decision": "approved" if approve_second else "denied",
        "status": final["status"], "error": final["error"], "output": final["output"],
        "model_calls": final["model_calls"], "tool_calls": final["tool_calls"],
        "services_before": first_services, "services_after_close": closed_services,
        "services_after_reopen": reopened_services, "child_instances": starts,
        "recorded_values": expected_values, "tool_results": tool_results,
        "first_call_replayed": False, "second_approval_preserved": True,
        "used_budget_before_and_after_reopen": {"model_calls": 2, "tool_calls": 1},
        "retained": retained, "config": "mcp.json", "trace": "trace.json", "database": "runs.sqlite3",
        "receipt_files": ["children/alpha.jsonl", "children/beta.jsonl", "children/broken.jsonl"],
        "boundary": "Normal host close/reopen with fresh STDIO children; not host crash injection or an exactly-once guarantee.",
    }
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def run_demo(output_dir=None, *, approve_second=True):
    if type(approve_second) is not bool:
        raise ValueError("approve_second must be boolean")
    if output_dir is None:
        with tempfile.TemporaryDirectory(prefix="astra-mcp-demo-") as temporary:
            directory = Path(temporary) / "run"
            prepare(directory)
            return _execute(directory, approve_second=approve_second, retained=False)
    directory = Path(output_dir).resolve()
    prepare(directory)
    return _execute(directory, approve_second=approve_second, retained=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Exclusively create and retain a new synthetic example directory.")
    parser.add_argument("--deny", action="store_true", help="Reject the second service after reopening.")
    args = parser.parse_args(argv)
    try:
        summary = run_demo(args.output_dir, approve_second=not args.deny)
    except FileExistsError:
        parser.exit(2, "Output directory already exists; choose a new --output-dir. Nothing was overwritten.\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
