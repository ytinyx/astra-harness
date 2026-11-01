"""Exercise real loopback HTTP MCP with synthetic receipts and no model calls."""
import argparse
import json
from pathlib import Path
import tempfile

from astra_harness.application import Application
from astra_harness.builtin.mcp_services import MCPServiceManager, parse_mcp_config
from astra_harness.plugins import PluginManager
from astra_harness.tools import ToolRegistry
from .mcp_http_server import LocalMCPHTTPServer


class NoModel:
    """An accidental model request is a demo failure, never a network fallback."""
    def complete(self, request):
        raise AssertionError("HTTP transport demonstration must not request a model")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def scenario(directory, name, mode, value, *, deny=False, uncertain=False):
    directory.mkdir()
    with LocalMCPHTTPServer(mode=mode) as server:
        configs = parse_mcp_config({"servers": [{"name": "local", "transport": "http",
                                                "endpoint": server.url, "timeout": 3}]})
        manager = MCPServiceManager(configs)
        registry = ToolRegistry()
        plugins = PluginManager(registry)
        try:
            plugins.activate(manager)
            require(manager.status()[0]["state"] == "ready", "local HTTP fixture did not initialize")
            registry.freeze()
            with Application(NoModel(), registry.tools(), database=directory / "runs.sqlite3",
                             workspace=directory, permissions=manager.permissions,
                             limits={"max_model_calls": 1, "max_tool_calls": 2,
                                     "tool_timeout": 5, "run_timeout": 15}, mode="http-mcp-demo") as app:
                run = app.submit_tool("mcp.local.record", {"value": value}, request_id=name)
                waiting = app.wait(run["id"], timeout=10)
                require(waiting["status"] == "waiting_approval" and waiting["tool_calls"] == 0,
                        "HTTP tool did not wait for host approval")
                require(server.receipts == [], "remote side effect occurred before approval")
                # This host approves only its newly created synthetic local fixture.
                app.approve(run["id"], approved=not deny)
                final = app.wait(run["id"], timeout=10)
                expected_status = "unknown" if uncertain else "failed" if deny else "completed"
                require(final["status"] == expected_status, "unexpected final runtime status")
                require(final["model_calls"] == 0, "a model request occurred")
                require(final["tool_calls"] == (0 if deny else 1), "unexpected tool call count")
                if deny:
                    require(final["error"] == "approval_denied", "denial was not preserved")
                elif not uncertain:
                    returned = final["tool_result"]
                    require(returned is not None and returned["error_code"] is None,
                            "successful HTTP operation did not return a tool result")
                    require(returned["value"].get("structuredContent") == {"value": value, "sequence": 1},
                            "HTTP response does not match the independent expected value")
                if uncertain:
                    try:
                        app.submit_tool("mcp.local.record", {"value": value}, request_id=name + "-again")
                    except RuntimeError:
                        blocked = True
                    else:
                        blocked = False
                    require(blocked, "unknown effect did not block new execution")
                events = app.events(run["id"])
                trace = [{key: event[key] for key in ("seq", "type", "status", "approved", "result")
                          if key in event} for event in events]
        finally:
            plugins.close()
        # Independent server-side effect receipts, not the client's success claim.
        expected_receipts = [] if deny else [{"value": value, "sequence": 1}]
        require(server.receipts == expected_receipts, "remote effect missing, repeated, or incorrect")
        result = {"name": name, "status": final["status"], "error": final["error"],
                  "model_calls": final["model_calls"], "tool_calls": final["tool_calls"],
                  "receipts": server.receipts, "approval_before_effect": True,
                  "automatic_replays": 0, "unknown_gate_checked": uncertain}
    require(server.closed, "owned fixture did not close")
    (directory / "receipts.json").write_text(json.dumps(result["receipts"], indent=2) + "\n", encoding="utf-8")
    (directory / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def execute(directory, *, retained):
    directory.mkdir(parents=True, exist_ok=False)
    cases = [scenario(directory / "json", "json", "json", 27),
             scenario(directory / "sse", "sse", "sse", 31),
             scenario(directory / "denied", "denied", "json", 47, deny=True),
             scenario(directory / "uncertain", "uncertain", "drop_after_effect", 43, uncertain=True)]
    summary = {"passed": True, "provider_requests": 0, "transport": "loopback_streamable_http",
               "retained": retained, "cases": cases,
               "boundary": "Synthetic real loopback HTTP; not external-service, authentication or LLM-quality validation."}
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def run_demo(output_dir=None):
    if output_dir is None:
        with tempfile.TemporaryDirectory(prefix="astra-http-mcp-") as temporary:
            return execute(Path(temporary) / "run", retained=False)
    return execute(Path(output_dir).resolve(), retained=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Create a new directory for synthetic results; never overwrite.")
    args = parser.parse_args(argv)
    try:
        summary = run_demo(args.output_dir)
    except FileExistsError:
        parser.exit(2, "Output exists; choose a new --output-dir. Existing data was not changed.\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
