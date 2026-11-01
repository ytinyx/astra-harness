"""Saved MCP profiles, explicit activation and approvals over owned loopback HTTP."""
import argparse
from contextlib import ExitStack
import http.client
import json
from pathlib import Path
import tempfile
import threading

from astra_harness.application import Application
from astra_harness.builtin.files import FilePlugin, inspect_workspace_directory
from astra_harness.mcp_profiles import MCPProfiles
from astra_harness.web import create_server
from astra_harness.workspace_binding import WorkspaceBinding
from ._mcp_profiles_fixture import DiscoveryFixture, NoModel


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class DemoHost:
    """One fixed temporary workspace, one worker, no implicit service activation."""
    def __init__(self, directory):
        self.directory = directory
        self.resources = ExitStack()

    def __enter__(self):
        workspace = self.directory / "workspace"
        workspace.mkdir(exist_ok=True)
        files = FilePlugin(workspace)
        specification = {"id": "demo-workspace", **inspect_workspace_directory(workspace)}
        permissions = ("files.read", "files.write")

        def factory(tools, granted):
            def bind(spec):
                require(spec == specification, "unexpected demo workspace")
                return WorkspaceBinding(**spec, tools=tuple(tools), permissions=tuple(granted), file_plugin=files)
            return bind

        try:
            self.app = Application(NoModel(), files.tools, database=self.directory / "runs.sqlite3",
                permissions=permissions, workspace=workspace, workspace_spec=specification,
                workspace_factory=factory(files.tools, permissions), mode="mcp-profile-demo",
                limits={"max_model_calls": 1, "max_tool_calls": 2, "tool_timeout": 5, "run_timeout": 15})
            self.resources.callback(self.app.close)
            self.controller = MCPProfiles(self.app, workspace=files, base_tools=files.tools,
                base_permissions=permissions, workspace_factory_builder=factory, environment=lambda: {})
            self.resources.callback(self.controller.close)
            self.server = create_server(self.app, port=0, mcp_profiles=self.controller)
            self.port = self.server.server_port
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.resources.callback(self.close_http)
            return self
        except BaseException:
            self.resources.close()
            raise

    def close_http(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        require(not self.thread.is_alive(), "owned management server did not stop")

    def __exit__(self, *args):
        self.resources.close()

    def request(self, action="", body=None, *, path=None, expected=200):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": "127.0.0.1:%s" % self.port}
        if body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            connection.request("POST" if body is not None else "GET",
                path or "/api/mcp-profiles" + ("/" + action if action else ""), body, headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            require(response.status == expected, "unexpected management response: " + str(result.get("error", response.status)))
            return result
        finally:
            connection.close()

    def operation(self, action, item, *, expected=200):
        body = {"id": item["id"], "expected_identity": item["identity"],
                "expected_revision": self.request()["revision"]}
        if action in {"test", "enable"}:
            body["confirmed"] = True
        return self.request(action, body, expected=expected)

    def waiting(self, value):
        run = self.app.wait(self.app.submit_tool("mcp.synthetic.record", {"value": value})["id"], timeout=10)
        require(run["status"] == "waiting_approval" and run["tool_calls"] == 0, "tool bypassed approval")
        require(run["model_calls"] == 0, "unexpected model request")
        return run

    def decide(self, run, approved=True):
        self.request(body={"approved": approved}, path="/api/runs/" + run["id"] + "/approve")
        return self.app.wait(run["id"], timeout=10)


def profile(server):
    return {"name": "synthetic", "transport": "http", "endpoint": server.url, "timeout": 3}


def run_summary(run):
    return {key: run[key] for key in ("status", "error", "model_calls", "tool_calls")}


def lifecycle(directory):
    directory.mkdir()
    with DiscoveryFixture() as server:
        with DemoHost(directory) as host:
            catalog = host.request("save", {"profile": profile(server), "expected_revision": 0})
            item = catalog["items"][0]
            require(server.requests == [], "saving settings contacted service")
            tested = host.operation("test", item)
            require(tested["ok"] and tested["registered"] is False and tested["tool_count"] == 1,
                    "discovery did not remain separate from registration")
            require(server.receipts == [], "discovery called a tool")
            require("mcp.synthetic.record" not in host.app.info()["tools"], "test registered a tool")
            host.operation("enable", item)
            denied = host.decide(host.waiting(23), approved=False)
            require(denied["error"] == "approval_denied" and server.receipts == [], "denial caused an effect")
            completed = host.decide(host.waiting(27))
            require(completed["status"] == "completed" and server.receipts == [{"value": 27, "sequence": 1}],
                    "approved effect failed independent receipt oracle")
            stale = host.waiting(55)
            blocked = host.operation("disable", item, expected=409)
            require(blocked["error"] == "runtime_update_busy", "pending approval did not protect tool set")
        requests_after_close = len(server.requests)
        with DemoHost(directory) as host:
            restored = host.request()["items"][0]
            require(restored["enabled"] and restored["state"] == "offline" and restored["activation_required"],
                    "saved intent was confused with an active connection")
            require(len(server.requests) == requests_after_close, "restart implicitly contacted MCP")
            old = host.app.get_run(stale["id"])
            require(old["status"] == "failed", "old connection approval remained executable")
            host.operation("enable", restored)
            host.request(body={"approved": True}, path="/api/runs/" + stale["id"] + "/approve", expected=409)
            fresh = host.decide(host.waiting(31))
            require(fresh["status"] == "completed" and server.receipts == [
                {"value": 27, "sequence": 1}, {"value": 31, "sequence": 2}], "reconnect receipt oracle failed")
            host.operation("disable", restored)
            require("mcp.synthetic.record" not in host.app.info()["tools"], "disable retained tool registration")
            host.operation("delete", restored)
            require(host.request()["items"] == [], "delete did not remove saved profile")
            runs = [run_summary(denied), run_summary(completed), run_summary(old), run_summary(fresh)]
        result = {"name": "lifecycle", "runs": runs, "receipts": list(server.receipts),
            "saved_without_requests": True, "test_tool_calls": 0, "restart_requests": 0,
            "old_approval_rejected": True, "deleted": True,
            "mcp_requests": len(server.requests), "session_cleanup_requests": sum(r["method"] == "DELETE" for r in server.requests)}
    require(server.closed, "MCP fixture did not close")
    return result


def uncertain(directory):
    directory.mkdir()
    with DiscoveryFixture(mode="drop_after_effect") as server:
        with DemoHost(directory) as host:
            item = host.request("save", {"profile": profile(server), "expected_revision": 0})["items"][0]
            host.operation("enable", item)
            unknown = host.decide(host.waiting(63))
            require(unknown["status"] == "unknown", "lost effect response was not unknown")
            require(host.operation("disable", item, expected=409)["error"] == "runtime_update_busy", "unknown did not block tool update")
        requests_after_close = len(server.requests)
        with DemoHost(directory) as host:
            require(host.app.get_run(unknown["id"])["status"] == "unknown", "restart lost unknown status")
            require(host.operation("enable", item, expected=409)["error"] == "runtime_update_busy", "unknown allowed reconnect")
            require(len(server.requests) == requests_after_close, "restart replayed or reconnected unknown")
        require(server.receipts == [{"value": 63, "sequence": 1}], "unknown effect repeated or missing")
        result = {"name": "uncertain", "runs": [run_summary(unknown)], "receipts": list(server.receipts),
            "restart_requests": 0, "automatic_replays": 0, "update_blocked": True, "mcp_requests": len(server.requests)}
    require(server.closed, "uncertain fixture did not close")
    return result


def execute(directory, retained):
    directory.mkdir(parents=True, exist_ok=False)
    cases = [lifecycle(directory / "lifecycle"), uncertain(directory / "uncertain")]
    runs = [run for case in cases for run in case["runs"]]
    summary = {"passed": True, "retained": retained, "model_calls": sum(run["model_calls"] for run in runs),
        "tool_calls": sum(run["tool_calls"] for run in runs), "run_count": len(runs), "external_provider_requests": 0,
        "cases": cases, "boundary": "Synthetic local MCP/HTTP lifecycle, not external-service trust or model-quality validation."}
    require(summary["model_calls"] == 0, "model guard was bypassed")
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def run_demo(output_dir=None):
    if output_dir is None:
        with tempfile.TemporaryDirectory(prefix="astra-mcp-profiles-") as temporary:
            return execute(Path(temporary) / "run", False)
    return execute(Path(output_dir).resolve(), True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Create a new directory for synthetic evidence; never overwrite.")
    args = parser.parse_args(argv)
    try:
        result = run_demo(args.output_dir)
    except FileExistsError:
        parser.exit(2, "Output exists; choose a new --output-dir. Existing data was not changed.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
