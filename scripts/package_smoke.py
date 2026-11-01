"""Run with the installed wheel's Python -I, outside every source directory."""

import argparse
from contextlib import closing
from email.parser import BytesParser
import hashlib
import importlib
from importlib.metadata import distribution, distributions
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
from urllib.request import ProxyHandler, build_opener
import zipfile


def canonical_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def exact_pins(requirements):
    result = {}
    for requirement in requirements:
        match = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([A-Za-z0-9][A-Za-z0-9.!+_-]*)\s*", requirement)
        if not match or canonical_name(match[1]) in result:
            raise RuntimeError("unexpected or duplicate declared runtime dependency")
        result[canonical_name(match[1])] = match[2]
    return result


def verify_distributions(items, expected, prefix):
    actual = {}
    for item in items:
        name = canonical_name(item.metadata["Name"])
        if name in actual or not Path(item.locate_file("")).resolve().is_relative_to(Path(prefix).resolve()):
            raise RuntimeError("duplicate distribution or dependency outside the virtual environment")
        actual[name] = item.version
    if actual != expected:
        raise RuntimeError("installed distributions differ from the locked local installation")
    return actual


def verify_mcp_service():
    """Exercise the installed public API with our own standard-library child."""
    import asyncio
    import time

    from astra_harness.builtin.mcp import StdioMCPClient
    from astra_harness.builtin.mcp_services import MCPServiceManager, load_mcp_config
    from astra_harness.contracts import ToolCall
    from astra_harness.process_state import process_has_exited
    from astra_harness.tools import ToolExecutor, ToolRegistry, ToolUnavailable

    root = Path("mcp-workspace").resolve()
    root.mkdir()
    server = root / "server.py"
    # This fixture is deliberately self-contained: neither examples nor tests
    # belong to the wheel, and the child must not import the source checkout.
    server.write_text('''import json, os, sys, uuid
from pathlib import Path
root = Path(sys.argv[1])
(root / "worker.json").write_text(json.dumps({"identity": f"{os.getpid()}-{uuid.uuid4().hex}"}))
calls = 0
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "installed-smoke", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "double", "description": "Double an integer",
                  "inputSchema": {"type": "object", "properties": {"value": {"type": "integer"}},
                                  "required": ["value"], "additionalProperties": False}}]}
    elif method == "tools/call":
        calls += 1
        value = message["params"]["arguments"]["value"] * 2
        (root / "calls.json").write_text(json.dumps({"calls": calls, "result": value}))
        result = {"content": [{"type": "text", "text": str(value)}],
                  "structuredContent": {"value": value}, "isError": False}
    else:
        raise RuntimeError("unexpected smoke method")
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
''', encoding="utf-8")
    config = root / "services.json"
    config.write_text(json.dumps({"servers": [{
        "name": "installed", "command": [sys.executable, "-I", "-X", "utf8", str(server), str(root)],
        "timeout": 3, "revision": "installed-smoke-v1"}]}), encoding="utf-8")
    clients = []

    def client_factory(*args, **kwargs):
        client = StdioMCPClient(*args, **kwargs)
        clients.append(client)
        return client

    manager = MCPServiceManager(load_mcp_config(config), client_factory=client_factory)
    registry = ToolRegistry()
    try:
        manager.activate(registry)
        ready = manager.status()
        if ready != [{"name": "installed", "state": "ready", "error_code": None,
                      "transport": "stdio", "tools": ["mcp.installed.double"]}]:
            raise RuntimeError("installed MCP service failed discovery or exposed unexpected status")
        if manager.permissions != frozenset({"mcp.installed.call"}) or len(clients) != 1:
            raise RuntimeError("installed MCP service has incorrect permission ownership")
        process = clients[0].process
        worker = json.loads((root / "worker.json").read_text(encoding="utf-8"))
        executor = ToolExecutor(registry)
        call = ToolCall("installed-mcp", "mcp.installed.double", {"value": 21})
        if (asyncio.run(executor.aexecute(call)).error_code != "permission_denied"
                or asyncio.run(executor.aexecute(call, manager.permissions)).error_code != "approval_required"
                or (root / "calls.json").exists()):
            raise RuntimeError("installed MCP call bypassed permission or approval")
        result = asyncio.run(executor.aexecute(call, manager.permissions, approved=True))
        if (result.error_code or result.value.get("structuredContent") != {"value": 42}
                or json.loads((root / "calls.json").read_text(encoding="utf-8")) != {"calls": 1, "result": 42}):
            raise RuntimeError("installed MCP tool result did not match the independent expected value")
    finally:
        manager.close()
    manager.close()
    deadline = time.monotonic() + 2
    while not process_has_exited(worker["identity"]) and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is None or not process_has_exited(worker["identity"]):
        raise RuntimeError("installed MCP cleanup left its owned launcher or worker running")
    unavailable = asyncio.run(executor.aexecute(call, manager.permissions, approved=True))
    if (not issubclass(ToolUnavailable, RuntimeError) or unavailable.error_code != "tool_unavailable"
            or manager.status()[0]["state"] != "closed"
            or json.loads((root / "calls.json").read_text(encoding="utf-8"))["calls"] != 1):
        raise RuntimeError("installed closed service did not reject a new call before dispatch")
    return {"tool": "mcp.installed.double", "permission": "mcp.installed.call",
            "permission_required": True, "approval_required": True, "result": 42,
            "remote_calls": 1, "launcher_exited": True, "worker_exited": True,
            "closed_service_unavailable": True}


def verify_http_mcp():
    """Check installed HTTP transport against a self-contained loopback fixture."""
    import asyncio
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import socket
    from astra_harness.application import Application
    from astra_harness.builtin.mcp_http import validate_http_endpoint
    from astra_harness.builtin.mcp_services import MCPServiceManager, load_mcp_config
    from astra_harness.contracts import ToolCall
    from astra_harness.tools import ToolExecutor, ToolRegistry

    root = Path("http-mcp-workspace").resolve()
    root.mkdir()
    calls, protocol_errors = [], []
    session = "package-http-session"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, code, body=b"", content_type="application/json", initialize=False):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if initialize:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            self.reply(204)

        def do_POST(self):
            self.connection.settimeout(5)
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/mcp" or not 0 < length < 16384:
                protocol_errors.append("request_boundary")
                self.reply(400)
                return
            message = json.loads(self.rfile.read(length))
            method = message["method"]
            if method != "initialize" and (self.headers.get("Mcp-Session-Id") != session
                    or self.headers.get("MCP-Protocol-Version") != "2025-06-18"):
                protocol_errors.append("session_or_version")
                self.reply(400)
                return
            if method.startswith("notifications/"):
                self.reply(202)
                return
            mode = "json"
            if method == "initialize":
                value = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                         "serverInfo": {"name": "installed-package-fixture", "version": "1"}}
            elif method == "tools/list":
                value = {"tools": [{"name": "double", "description": "Double a synthetic integer.",
                    "inputSchema": {"type": "object", "properties": {"value": {"type": "integer"},
                        "mode": {"type": "string", "enum": ["json", "sse", "drop"]}},
                        "required": ["value", "mode"], "additionalProperties": False}}]}
            elif method == "tools/call":
                args = message["params"]["arguments"]
                mode, doubled = args["mode"], args["value"] * 2
                calls.append({"mode": mode, "value": args["value"], "result": doubled})
                (root / "calls.json").write_text(json.dumps(calls), encoding="utf-8")
                if mode == "drop":
                    # A real local effect followed by a closed connection must
                    # remain unknown; repeating the request would be unsafe.
                    with (root / "effect.json").open("x", encoding="utf-8") as stream:
                        json.dump({"value": doubled}, stream)
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                value = {"content": [{"type": "text", "text": str(doubled)}],
                         "structuredContent": {"value": doubled}}
            else:
                protocol_errors.append("unexpected_method")
                self.reply(400)
                return
            encoded = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": value}).encode("utf-8")
            if mode == "sse":
                encoded = b"event: message\ndata: " + encoded + b"\n\n"
            self.reply(200, encoded, "text/event-stream" if mode == "sse" else "application/json",
                       initialize=method == "initialize")

    class NoModel:
        calls = 0

        def complete(self, request):
            self.calls += 1
            raise AssertionError("installed HTTP tool task reached a model")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
    models = []
    try:
        if validate_http_endpoint(endpoint) != endpoint:
            raise RuntimeError("installed HTTP endpoint parser changed the fixture endpoint")
        config = root / "mcp.json"
        config.write_text(json.dumps({"servers": [{"name": "installedhttp", "transport": "http",
                                                  "endpoint": endpoint, "timeout": 3}]}), encoding="utf-8")
        with closing(MCPServiceManager(load_mcp_config(config))) as manager:
            registry = ToolRegistry()
            manager.activate(registry)
            status = manager.status()
            if (len(status) != 1 or status[0]["state"] != "ready" or status[0]["transport"] != "http"
                    or status[0]["tools"] != ["mcp.installedhttp.double"] or endpoint in json.dumps(status)):
                raise RuntimeError("installed HTTP service discovery or sanitized status is incorrect")
            permission = frozenset({"mcp.installedhttp.call"})
            executor = ToolExecutor(registry)
            call = ToolCall("http-unapproved", "mcp.installedhttp.double", {"value": 21, "mode": "json"})
            if (asyncio.run(executor.aexecute(call)).error_code != "permission_denied"
                    or asyncio.run(executor.aexecute(call, permission)).error_code != "approval_required" or calls):
                raise RuntimeError("installed HTTP service dispatched before permission/approval")
            model = NoModel()
            models.append(model)
            with Application(model, registry.tools(), database=root / "runs.sqlite3", permissions=permission) as app:
                for mode, value in (("json", 21), ("sse", 12), ("drop", 7)):
                    waiting = app.wait(app.submit_tool("mcp.installedhttp.double", {"value": value, "mode": mode})["id"])
                    if waiting["status"] != "waiting_approval" or waiting["tool_calls"] != 0:
                        raise RuntimeError("installed HTTP mutation bypassed host approval")
                    app.approve(waiting["id"])
                    done = app.wait(waiting["id"])
                    if (done["model_calls"], done["tool_calls"]) != (0, 1):
                        raise RuntimeError("installed HTTP tool task has incorrect execution counts")
                    if mode == "drop":
                        expected_unknown = {"call_id": waiting["pending"][0]["call_id"],
                            "name": "mcp.installedhttp.double", "value": None, "error_code": "execution_unknown"}
                        if (done["status"] != "unknown" or done["error"] != "execution_unknown"
                                or done["tool_result"] != expected_unknown
                                or done["index"] != waiting["index"] or done["pending"] != waiting["pending"]):
                            raise RuntimeError("installed HTTP disconnect lost its uncertain effect")
                        unknown_id = done["id"]
                    elif (done["status"] != "completed" or done["tool_result"]["error_code"]
                          or done["tool_result"]["value"]["structuredContent"] != {"value": value * 2}):
                        raise RuntimeError("installed HTTP JSON/SSE response differs from the real result")
            model = NoModel()
            models.append(model)
            with Application(model, registry.tools(), database=root / "runs.sqlite3", permissions=permission) as app:
                reopened_unknown = app.get_run(unknown_id)
                if (reopened_unknown["status"] != "unknown" or reopened_unknown["tool_result"] != expected_unknown
                        or reopened_unknown["index"] != 0 or reopened_unknown["tool_calls"] != 1):
                    raise RuntimeError("installed HTTP unknown state changed on application reopen")
                try:
                    app.resume(unknown_id)
                except RuntimeError:
                    pass
                else:
                    raise RuntimeError("installed HTTP unknown write was automatically resumed")
        expected = [{"mode": mode, "value": value, "result": value * 2}
                    for mode, value in (("json", 21), ("sse", 12), ("drop", 7))]
        if (calls != expected or json.loads((root / "calls.json").read_text(encoding="utf-8")) != expected
                or json.loads((root / "effect.json").read_text(encoding="utf-8")) != {"value": 14}
                or protocol_errors or any(model.calls for model in models)):
            raise RuntimeError("installed HTTP request/effect receipts or no-model boundary differ")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        if worker.is_alive():
            raise RuntimeError("installed HTTP fixture server did not stop")
    return {"permission_required": True, "approval_required": True, "json_result": 42, "sse_result": 24,
            "session_and_protocol_headers": True, "actual_tool_requests": 3, "uncertain_effect": 14,
            "unknown_persisted_without_retry": True, "model_calls": 0, "loopback_fixture_closed": True}


def verify_knowledge_index():
    """Verify persistent retrieval against independent synthetic source bytes."""
    from astra_harness.builtin.knowledge import KnowledgePlugin
    from astra_harness.contracts import ToolCall
    from astra_harness.tools import ToolExecutor, ToolRegistry

    workspace = Path("knowledge-workspace").resolve()
    workspace.mkdir()
    document = workspace / "guide.txt"
    index = Path("knowledge-index.sqlite3").resolve()
    original = "兰花计划 Orchid access code 731 belongs to Lin.\n"
    updated = "兰花计划 Orchid access code 942 belongs to Lin.\n"
    document.write_bytes(original.encode("utf-8"))
    permissions = frozenset({"files.read", "knowledge.read", "knowledge.write"})
    sequence = 0

    def execute(executor, name, arguments, *, approved=False):
        nonlocal sequence
        sequence += 1
        result = executor.execute(ToolCall(f"knowledge-smoke-{sequence}", name, arguments),
                                  permissions, approved=approved)
        if result.error_code or not isinstance(result.value, dict) or result.value.get("ok") is not True:
            raise RuntimeError("installed knowledge tool failed: " + name)
        return result.value

    def verify_hit(result, source, marker):
        if len(result["hits"]) != 1:
            raise RuntimeError("installed knowledge search did not return the expected source fragment")
        hit = result["hits"][0]
        start, end = hit["start_offset"], hit["end_offset"]
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source)
                or hit["path"] != "guide.txt" or not hit["chunk_id"]
                or hit["document_sha256"] != hashlib.sha256(source.encode("utf-8")).hexdigest()
                or hit["text"] != source[start:end] or marker not in hit["text"]):
            raise RuntimeError("installed knowledge citation does not match the actual document")
        for offset, label in ((start, "start"), (end, "end")):
            prefix = source[:offset]
            if (hit[label + "_line"] != prefix.count("\n") + 1
                    or hit[label + "_column"] != len(prefix.rsplit("\n", 1)[-1]) + 1):
                raise RuntimeError("installed knowledge line/column citation is incorrect")
        return {key: hit[key] for key in ("path", "document_sha256", "start_offset", "end_offset")}

    with closing(KnowledgePlugin(workspace, index)) as plugin:
        registry = ToolRegistry()
        plugin.activate(registry)
        if (frozenset(plugin.permissions) != permissions or {tool.name for tool in registry.tools()}
                != {"knowledge.import_file", "knowledge.search", "knowledge.remove",
                    "knowledge.catalog", "knowledge.inspect"}):
            raise RuntimeError("installed knowledge plugin has an incorrect public contract")
        executor = ToolExecutor(registry)
        call = ToolCall("knowledge-unapproved", "knowledge.import_file", {"path": "guide.txt"})
        if (executor.execute(call).error_code != "permission_denied"
                or executor.execute(call, permissions).error_code != "approval_required"
                or execute(executor, "knowledge.search", {"query": "Orchid"})["hits"]):
            raise RuntimeError("installed knowledge import bypassed its permission or approval")
        imported = execute(executor, "knowledge.import_file", {"path": "guide.txt"}, approved=True)
        if imported["document_sha256"] != hashlib.sha256(original.encode("utf-8")).hexdigest():
            raise RuntimeError("installed knowledge import recorded an incorrect source hash")

    with closing(KnowledgePlugin(workspace, index)) as plugin:
        registry = ToolRegistry()
        plugin.activate(registry)
        executor = ToolExecutor(registry)
        original_hit = verify_hit(execute(executor, "knowledge.search", {"query": "Orchid"}), original, "731")
        document.write_bytes(updated.encode("utf-8"))
        stale = execute(executor, "knowledge.search", {"query": "Orchid"})
        if stale["hits"] or stale["stale_count"] < 1:
            raise RuntimeError("installed knowledge search returned stale source text")
        execute(executor, "knowledge.import_file", {"path": "guide.txt"}, approved=True)
        updated_hit = verify_hit(execute(executor, "knowledge.search", {"query": "Orchid"}), updated, "942")
        remove = ToolCall("knowledge-unapproved-remove", "knowledge.remove", {"path": "guide.txt"})
        if executor.execute(remove, permissions).error_code != "approval_required":
            raise RuntimeError("installed knowledge removal bypassed approval")
        removed = execute(executor, "knowledge.remove", {"path": "guide.txt"}, approved=True)
        final = execute(executor, "knowledge.search", {"query": "Orchid"})
        if removed["removed"] is not True or final["hits"] or final["stale_count"] != 0:
            raise RuntimeError("installed knowledge removal left searchable data")
    return {"permission_required": True, "approval_required": True, "reopened_index": True,
            "original_citation": original_hit, "stale_text_excluded": True,
            "updated_citation": updated_hit, "removed_without_hits": True}


def verify_knowledge_collections():
    """Exercise installed host operations, durable approval and collection isolation."""
    from contextlib import contextmanager
    from astra_harness.application import Application
    from astra_harness.builtin.knowledge_collections import KnowledgeCollectionManager, load_knowledge_config
    from astra_harness.contracts import ToolCall
    from astra_harness.tools import ToolExecutor, ToolRegistry

    class NoModel:
        calls = 0

        def complete(self, request):
            self.calls += 1
            raise AssertionError("a host-selected tool operation reached the model")

    root = Path("knowledge-collections").resolve()
    workspace = root / "workspace"
    sources = {"product": "Orchid product access code 731.\n",
               "notes": "Orchid notes access code 942.\n"}
    digests = {key: hashlib.sha256(text.encode("utf-8")).hexdigest() for key, text in sources.items()}
    for key, text in sources.items():
        directory = workspace / key
        directory.mkdir(parents=True)
        (directory / "guide.txt").write_bytes(text.encode("utf-8"))
    config = root / "knowledge.json"
    config.write_text(json.dumps({"collections": [
        {"id": key, "label": key, "path": key, "index": f"indexes/{key}.sqlite3"}
        for key in sources]}), encoding="utf-8")
    database = root / "runs.sqlite3"
    specs = load_knowledge_config(config, workspace=workspace, database=database)
    models, runs = [], []

    @contextmanager
    def host():
        with closing(KnowledgeCollectionManager(specs)) as manager:
            registry = ToolRegistry()
            manager.activate(registry)
            model = NoModel()
            models.append(model)
            with Application(model, registry.tools(), database=database,
                             permissions=manager.permissions, workspace=workspace) as app:
                yield app, manager, ToolExecutor(registry)

    def operation(app, name, arguments, *, approve=False):
        state = app.wait(app.submit_tool(name, arguments)["id"])
        if approve:
            if state["status"] != "waiting_approval" or (state["model_calls"], state["tool_calls"]) != (0, 0):
                raise RuntimeError("installed collection mutation bypassed approval")
            app.approve(state["id"])
            state = app.wait(state["id"])
        if (state["kind"], state["status"], state["model_calls"], state["tool_calls"]) != ("tool", "completed", 0, 1):
            raise RuntimeError("installed host-selected operation did not complete without a model")
        result = state["tool_result"]
        if result["error_code"] or not result["value"].get("ok"):
            raise RuntimeError("installed collection operation returned an error")
        runs.append(state["id"])
        return result["value"]

    arguments = {"path": "guide.txt", "expected_source_sha256": digests["product"], "expected_index_sha256": ""}
    with host() as (app, manager, executor):
        catalog = operation(app, "knowledge.product.catalog", {})
        inspected = operation(app, "knowledge.product.inspect", {"path": "guide.txt"})
        if (catalog["documents"] or catalog["available_files"] != [{"path": "guide.txt", "bytes": len(sources["product"].encode("utf-8"))}]
                or inspected["source_sha256"] != digests["product"] or inspected["indexed_sha256"] != ""):
            raise RuntimeError("installed collection metadata does not match the source")
        limited = frozenset({"files.read", "knowledge.product.read", "knowledge.product.write"})
        for tool, args in (("import_file", {"path": "guide.txt"}), ("search", {"query": "Orchid"})):
            if executor.execute(ToolCall("wrong-collection-" + tool, "knowledge.notes." + tool, args), limited, approved=True).error_code != "permission_denied":
                raise RuntimeError("installed collection accepted another collection's permission")
        waiting = app.wait(app.submit_tool("knowledge.product.import_file", arguments)["id"])
        if waiting["status"] != "waiting_approval" or (waiting["model_calls"], waiting["tool_calls"]) != (0, 0):
            raise RuntimeError("installed manual import did not persist an approval")
        if operation(app, "knowledge.product.search", {"query": "Orchid"})["hits"]:
            raise RuntimeError("installed collection wrote before approval")

    with host() as (app, manager, executor):
        restored = app.get_run(waiting["id"])
        if (restored["status"] != "waiting_approval" or restored["approval"] != waiting["approval"]
                or restored["operation"] != waiting["operation"]
                or (restored["model_calls"], restored["tool_calls"]) != (0, 0)):
            raise RuntimeError("installed host operation changed across application/index reopen")
        app.approve(restored["id"])
        done = app.wait(restored["id"])
        if ((done["status"], done["model_calls"], done["tool_calls"]) != ("completed", 0, 1)
                or done["tool_result"]["value"].get("document_sha256") != digests["product"]):
            raise RuntimeError("installed restored approval did not import the bound source")
        runs.append(done["id"])
        operation(app, "knowledge.notes.import_file", {"path": "guide.txt",
                  "expected_source_sha256": digests["notes"], "expected_index_sha256": ""}, approve=True)
        for name, args, code in (
                ("import_file", {"path": "guide.txt", "expected_source_sha256": digests["notes"]}, "source_changed"),
                ("remove", {"path": "guide.txt", "expected_index_sha256": digests["notes"]}, "index_changed")):
            result = executor.execute(ToolCall("digest-guard-" + name, "knowledge.product." + name, args),
                                      frozenset(manager.permissions), approved=True)
            if result.error_code or result.value != {"ok": False, "error_code": code}:
                raise RuntimeError("installed collection did not enforce its digest guard")
        for key, text in sources.items():
            result = operation(app, "knowledge." + key + ".search", {"query": "Orchid"})
            hits = result["hits"]
            if (len(hits) != 1 or hits[0]["path"] != "guide.txt" or hits[0]["document_sha256"] != digests[key]
                    or hits[0]["text"] != text[hits[0]["start_offset"]:hits[0]["end_offset"]]
                    or ("731" if key == "product" else "942") not in hits[0]["text"]):
                raise RuntimeError("installed collection search crossed a source/index boundary")
        if any(event["type"].startswith("model.") for identifier in runs for event in app.events(identifier)):
            raise RuntimeError("installed host operations recorded an unexpected model event")
    if any(model.calls for model in models):
        raise RuntimeError("installed host operations invoked a model")
    return {"collections": sorted(sources), "same_path": "guide.txt", "document_sha256": digests,
            "collection_permissions": True, "approval_survived_reopen": True,
            "source_and_index_guards": True, "isolated_results": True, "model_calls": 0,
            "completed_host_operations": len(runs)}


def verify(wheel, report_path, *, dependency_manifest, basic=False):
    if not sys.flags.isolated or sys.prefix == sys.base_prefix:
        raise RuntimeError("requires an isolated Python in a dedicated virtual environment")
    import astra_harness

    installed = Path(astra_harness.__file__).resolve().parent
    if not installed.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("package was not imported from the virtual environment")
    venv_config = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8").lower()
    if not re.search(r"^include-system-site-packages\s*=\s*false\s*$", venv_config, re.MULTILINE):
        raise RuntimeError("virtual environment must exclude system site packages")
    package = distribution("astra-harness")
    manifest = json.loads(Path(dependency_manifest).read_text(encoding="utf-8"))
    wheel_sha256 = hashlib.sha256(Path(wheel).read_bytes()).hexdigest()
    if (manifest["project_wheel_sha256"] != wheel_sha256
            or exact_pins(package.requires or []) != manifest["direct_dependencies"]):
        raise RuntimeError("installed dependency metadata differs from the audited source/wheel")
    expected_distributions = {**manifest["distributions"], "astra-harness": package.version}
    installed_versions = verify_distributions(distributions(), expected_distributions, sys.prefix)
    installed_distributions = sorted(installed_versions)
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise RuntimeError("wheel must have exactly one metadata record")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        if metadata["Version"] != package.version or exact_pins(metadata.get_all("Requires-Dist", [])) != manifest["direct_dependencies"]:
            raise RuntimeError("wheel dependency metadata differs from the installed package")
        runtime = [name for name in archive.namelist() if name.startswith("astra_harness/") and not name.endswith("/")]
        knowledge_resources = ["astra_harness/static/knowledge.mjs", "astra_harness/static/knowledge-state.mjs"]
        if not set(knowledge_resources) <= set(runtime):
            raise RuntimeError("wheel is missing the knowledge workbench modules")
        for name in runtime:
            if (installed.parent / name).read_bytes() != archive.read(name):
                raise RuntimeError("installed runtime differs from wheel: " + name)
    for name in ("fastapi", "pydantic", "uvicorn", "starlette", "httpx"):
        module = importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise RuntimeError("runtime dependency imported from outside the virtual environment")
    # Even the secondary-interpreter smoke must load the actual web adapter.
    from astra_harness.web import create_server

    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("ASTRA_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    records = []

    def command(args, *, expected=0, executable=None, decode=True):
        argv = ([sys.executable, "-I", "-X", "utf8", "-m", "astra_harness"] if executable is None else [str(executable)]) + args
        result = subprocess.run(argv, cwd=Path.cwd(), env=environment, capture_output=True,
                                text=True, encoding="utf-8", timeout=90)
        records.append({"args": args, "entry": "python -I -m" if executable is None else "console",
                        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        Path(report_path).with_suffix(".commands.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if result.returncode != expected:
            raise RuntimeError("installed CLI returned an unexpected exit code: " + str(args[0]))
        if "Traceback" in result.stdout + result.stderr:
            raise RuntimeError("installed CLI leaked a traceback")
        return json.loads(result.stdout) if decode else result.stdout + result.stderr

    executable = Path(sys.prefix) / ("Scripts/astra-harness.exe" if os.name == "nt" else "bin/astra-harness")
    command(["--help"], executable=executable, decode=False)
    before = sorted(str(path) for path in Path.cwd().rglob("*"))
    doctor = command(["doctor"])
    if not doctor["ok"] or before != sorted(str(path) for path in Path.cwd().rglob("*")):
        raise RuntimeError("doctor failed or wrote to the empty working directory")
    add = command(["run", "2 + 3", "--no-files"])
    if add["status"] != "completed" or add["tool_calls"] != 1:
        raise RuntimeError("installed addition did not execute")
    with closing(sqlite3.connect(".local/harness.sqlite3")) as connection:
        state = json.loads(connection.execute("SELECT state FROM runs WHERE id=?", (add["id"],)).fetchone()[0])
    results = [message["tool_result"] for message in state["messages"] if message.get("tool_result")]
    if len(results) != 1 or results[0]["value"] != 5:
        raise RuntimeError("installed addition returned an incorrect tool result")

    checks = {"console_entry": True, "isolated_import": True, "installed_bytes_match": len(runtime),
              "doctor_read_only": True, "addition": True, "locked_dependencies": True, "web_adapter_import": True}
    # Exercise the newly packaged public tool through the same admission path.
    from astra_harness.builtin.files import FilePlugin
    from astra_harness.contracts import ToolCall
    from astra_harness.tools import ToolExecutor, ToolRegistry
    from astra_harness import tool_from_function
    from astra_harness.tooling import tool_from_function as module_helper

    if tool_from_function is not module_helper:
        raise RuntimeError("installed package root does not export its typed-tool helper")

    typed_calls = []

    def summarize(values: list[int], prefix: str = "total") -> str:
        typed_calls.append((values, prefix))
        return f"{prefix}: {sum(values)}"

    typed_tool = tool_from_function(
        summarize, name="package.summarize", description="Summarize integer values.",
        permissions=frozenset({"package.inspect"}), side_effect=False,
        requires_approval=True, implementation_version="1")
    schema = typed_tool.spec().schema()
    if (typed_tool.handler is not summarize or schema.get("type") != "object"
            or schema.get("required") != ["values"] or schema.get("additionalProperties") is not False
            or schema["properties"]["values"].get("type") != "array"
            or schema["properties"]["values"]["items"].get("type") != "integer"
            or schema["properties"]["prefix"].get("type") != "string"):
        raise RuntimeError("installed typed-tool helper generated an incorrect contract")
    typed_executor = ToolExecutor(ToolRegistry([typed_tool]))
    typed_call = ToolCall("installed-typed", "package.summarize", {"values": [17, 25]})
    permission = frozenset({"package.inspect"})
    if (typed_executor.execute(typed_call).error_code != "permission_denied"
            or typed_executor.execute(typed_call, permission).error_code != "approval_required"
            or typed_executor.execute(
                ToolCall("installed-invalid", "package.summarize", {"values": [True]}),
                permission, approved=True).error_code != "invalid_arguments"
            or typed_calls):
        raise RuntimeError("installed typed tool bypassed its admission checks")
    typed_result = typed_executor.execute(typed_call, permission, approved=True)
    if typed_result.error_code or typed_result.value != "total: 42" or typed_calls != [([17, 25], "total")]:
        raise RuntimeError("installed typed tool failed to execute exactly one approved call with its default")
    checks["typed_tool_public_helper"] = {
        "generated_contract": True, "permission_required": True, "approval_required": True,
        "strict_arguments": True, "default_applied": True, "handler_calls": len(typed_calls),
        "result": typed_result.value}
    checks["mcp_service_public_api"] = verify_mcp_service()
    checks["mcp_http_public_api"] = verify_http_mcp()
    checks["knowledge_index_public_api"] = verify_knowledge_index()
    checks["knowledge_collections_public_api"] = verify_knowledge_collections()
    checks["knowledge_static_resources"] = knowledge_resources

    range_root = Path("range-workspace")
    range_root.mkdir()
    (range_root / "notes.txt").write_bytes("标题\r\nowner: 林舟\r\nquota: 7\n".encode("utf-8"))
    executor = ToolExecutor(ToolRegistry(FilePlugin(range_root).tools))
    call = ToolCall("installed-range", "files.read_range", {"path": "notes.txt", "start_line": 2, "line_count": 1})
    if executor.execute(call).error_code != "permission_denied":
        raise RuntimeError("installed range tool bypassed read permission")
    ranged = executor.execute(call, frozenset({"files.read"}))
    value = ranged.value
    if (ranged.error_code or not value.get("ok")
            or value["fragments"] != [{"line": 2, "start_column": 1, "text": "owner: 林舟", "partial": False}]
            or value["total_lines"] != 3 or not value["truncated"]
            or value["next_cursor"] != {"start_line": 3, "start_column": 1}):
        raise RuntimeError("installed range tool returned incorrect content or cursor")
    checks["file_range_permission_and_result"] = True
    from astra_harness.benchmark import default_tool_factory
    from astra_harness.tool_identity import build_tool_identity, presentation_fingerprint
    template = default_tool_factory()
    identity = build_tool_identity(template)
    checks["tool_identity_sha256"] = identity["sha256"]
    checks["tool_presentation_sha256"] = presentation_fingerprint(template)
    if not basic:
        options = ["--workspace", "meeting", "--database", "meeting.sqlite3"]
        waiting = command(["run", "整理会议待办并保存 todos.json", "--demo-files", *options])
        if waiting["status"] != "waiting_approval" or Path("meeting/todos.json").exists():
            raise RuntimeError("meeting must await approval without writing output")
        run_id = waiting["id"]
        inspected = command(["inspect", run_id, *options])
        paused = command(["pause", run_id, *options])
        resumed = command(["resume", run_id, *options])
        if (inspected["approval"] != waiting["approval"] or paused["status"] != "paused"
                or resumed["status"] != "waiting_approval" or resumed["approval"] != waiting["approval"]
                or (resumed["model_calls"], resumed["tool_calls"]) != (waiting["model_calls"], waiting["tool_calls"])
                or Path("meeting/todos.json").exists()):
            raise RuntimeError("approval or execution counters changed across separate CLI processes")
        approved = command(["approve", run_id, *options])
        expected = [
            {"task": "整理首版需求清单", "owner": "林舟", "due": "2026-11-05"},
            {"task": "完成交互原型", "owner": "陈澄", "due": "2026-11-07"},
            {"task": "补充接口错误码", "owner": "周宁", "due": "2026-11-06"},
            {"task": "验证任务取消行为", "owner": "吴桐", "due": "2026-11-08"},
            {"task": "编写安装说明", "owner": "许安", "due": "2026-11-09"},
            {"task": "检查演示数据", "owner": "沈禾", "due": "2026-11-10"},
        ]
        if approved["status"] != "completed" or json.loads(Path("meeting/todos.json").read_text(encoding="utf-8")) != expected:
            raise RuntimeError("approved installed meeting artifact did not match the independent fixture")
        benchmark = command(["benchmark", "--output-dir", "benchmarks"])
        acceptance = command(["eval"])
        if (benchmark["schema_version"] != 3 or benchmark["tool_identity"] != identity
                or benchmark["tool_presentation_sha256"] != checks["tool_presentation_sha256"]):
            raise RuntimeError("installed benchmark omitted or changed its tool identity")
        if not benchmark["passed"] or benchmark["summary"]["total"] != 9 or not acceptance["passed"]:
            raise RuntimeError("installed evaluation failed")
        checks.update(meeting_approval_across_processes=True, pause_resume=True, six_todos_match=True,
                      benchmark=benchmark["summary"], evaluation=True)

        # The only network access in this script is to this ephemeral loopback server.
        from astra_harness.application import Application
        from astra_harness.builtin.models import DemoModel
        from astra_harness.web import create_server
        resources = {"/": "static/index.html", "/static/app.mjs": "static/app.mjs",
                     "/static/styles.css": "static/styles.css", "/static/markdown.mjs": "static/markdown.mjs",
                     "/static/state.mjs": "static/state.mjs", "/static/knowledge.mjs": "static/knowledge.mjs",
                     "/static/knowledge-state.mjs": "static/knowledge-state.mjs"}
        with Application(DemoModel(), database="http.sqlite3") as app:
            server = create_server(app, port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                opener = build_opener(ProxyHandler({}))
                for route, resource in resources.items():
                    with opener.open(f"http://127.0.0.1:{server.server_port}{route}", timeout=5) as response:
                        expected = (installed / resource).read_bytes()
                        if route == "/":
                            nonce = re.search(r"'nonce-([^']+)'", response.headers.get("Content-Security-Policy", ""))
                            if nonce is None:
                                raise RuntimeError("installed HTML is missing its CSP nonce")
                            expected = (installed / resource).read_text(encoding="utf-8").replace("__NONCE__", nonce.group(1)).encode("utf-8")
                        if response.status != 200 or response.read() != expected:
                            raise RuntimeError("installed static HTTP resource mismatch")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)
                if worker.is_alive():
                    raise RuntimeError("temporary local HTTP server did not stop")
        checks["http_resources"] = list(resources)

    marker = "package-private-error-sentinel"
    Path("invalid.json").write_text('{"' + marker + '": 1}', encoding="utf-8")
    error = command(["run", "2 + 3", "--config", "invalid.json", "--workspace", "invalid-work",
                     "--database", "invalid.sqlite3"], expected=1, decode=False)
    if marker in error or Path("invalid-work").exists() or Path("invalid.sqlite3").exists():
        raise RuntimeError("invalid configuration leaked content or created runtime resources")
    missing_live = command(["doctor", "--live"], expected=1)
    if missing_live["ok"] or marker in json.dumps(missing_live):
        raise RuntimeError("unconfigured live diagnosis did not fail safely")
    checks["sanitized_errors"] = True
    loaded = [module for name, module in sys.modules.items() if name == "astra_harness" or name.startswith("astra_harness.")]
    if not all(Path(module.__file__).resolve().is_relative_to(installed) for module in loaded if getattr(module, "__file__", None)):
        raise RuntimeError("a source checkout shadowed the installed package")
    report = {"passed": True, "python": sys.version.split()[0], "version": package.version, "basic": basic,
              "isolated": bool(sys.flags.isolated), "runtime_dependencies": package.requires or [],
               "environment_distributions": installed_distributions, "environment_versions": installed_versions,
               "dependency_manifest_sha256": hashlib.sha256(Path(dependency_manifest).read_bytes()).hexdigest(),
               "provider_requests": 0, "wheel_sha256": wheel_sha256, "checks": checks,
              "commands": records}
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--dependency-manifest", required=True)
    parser.add_argument("--basic", action="store_true")
    args = parser.parse_args()
    result = verify(Path(args.wheel).resolve(), Path(args.report).resolve(),
                    dependency_manifest=Path(args.dependency_manifest).resolve(), basic=args.basic)
    print(json.dumps({"passed": result["passed"], "python": result["python"], "checks": result["checks"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
