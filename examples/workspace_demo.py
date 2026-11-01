"""Offline proof that changing the selected folder cannot retarget an old run.

Only synthetic directories and ordinary file tools are used. The host, registry,
executor, approvals and SQLite store are the same components as the local UI.
"""
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path
import tempfile

from astra_harness.cli import build_application
from astra_harness.workspace_binding import WorkspaceError


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def open_host(base):
    args = argparse.Namespace(command="serve", config=None, mcp_config=None, env_file=None,
        workspace=base / "工作区 A", database=base / "state/runs.sqlite3",
        knowledge_index=None, knowledge_config=None, no_files=False, live=False, demo_files=False)
    with ExitStack() as resources:
        services = {}
        app = build_application(args, resources, web_services=services)
        yield app, services["workspaces"]


def select(app, policy, path):
    before = app.workspaces()
    preview = policy.inspect(str(path), items=before["items"])
    policy.select(app, path=preview["path"], expected_identity=preview["identity"],
                  expected_revision=before["revision"])
    return preview


def waiting_write(app, workspace_id, path, content):
    run = app.submit_tool("files.write", {"path": path, "content": content}, workspace_id=workspace_id)
    waiting = app.wait(run["id"])
    assert waiting["status"] == "waiting_approval" and waiting["tool_calls"] == 0
    return waiting


def approve(app, run):
    app.approve(run["id"])
    result = app.wait(run["id"])
    assert result["status"] == "completed" and not result["tool_result"]["error_code"]
    return result


def execute(base):
    a, b = base / "工作区 A", base / "工作区 B"
    a.mkdir(); b.mkdir(); (base / "state").mkdir()
    (a / "value.txt").write_bytes(b"A original")
    (b / "value.txt").write_bytes(b"B original")
    with open_host(base) as (app, policy):
        a_id = app.info()["workspace_id"]
        a_write = waiting_write(app, a_id, "value.txt", "A approved")
        assert (a / "value.txt").read_bytes() == b"A original"
        selected = select(app, policy, b)
        b_id = selected["id"]
        assert b_id != a_id and app.info()["workspace_id"] == b_id
        # The visible folder changed, but this old approval still previews A.
        assert app.preview_write(a_write["id"])["before"] == "A original"
        b_write = waiting_write(app, b_id, "value.txt", "B approved")
        approve(app, b_write)
        approve(app, a_write)
        assert (a / "value.txt").read_bytes() == b"A approved"
        assert (b / "value.txt").read_bytes() == b"B approved"
        try:
            app.submit("This must not cross workspace history", session_id=a_write["session_id"], workspace_id=b_id)
        except WorkspaceError as error:
            assert error.code == "workspace_session_mismatch"
        else:
            raise AssertionError("a session crossed workspace boundaries")
        select(app, policy, a)
        restart_write = waiting_write(app, a_id, "after-restart.txt", "Still A")
        select(app, policy, b)

    with open_host(base) as (app, policy):
        assert app.info()["workspace_id"] == b_id
        assert app.get_run(restart_write["id"])["status"] == "waiting_approval"
        approve(app, restart_write)
        assert (a / "after-restart.txt").read_bytes() == b"Still A"
        assert not (b / "after-restart.txt").exists()
        replaced_write = waiting_write(app, b_id, "must-not-exist.txt", "Old approval")

    # Rename only this explicitly created fixture, preserving its old contents.
    old_b = base / "original B preserved"
    b.rename(old_b)
    b.mkdir()
    (b / "sentinel.txt").write_bytes(b"New directory: do not touch")
    with open_host(base) as (app, policy):
        failed = app.get_run(replaced_write["id"])
        assert failed["status"] == "failed" and failed["error"] in {"workspace_changed", "workspace_unavailable"}
        assert app.info()["workspace_id"] == b_id and not app.info()["workspace_available"]
        assert not (b / "must-not-exist.txt").exists()
        assert (b / "sentinel.txt").read_bytes() == b"New directory: do not touch"
        select(app, policy, a)
        assert app.info()["workspace_available"] and app.info()["workspace_id"] == a_id
        runs = app.list_runs()
        assert all(run["model_calls"] == 0 for run in runs)
        save(base / "runs.json", runs)
        save(base / "trace.json", {run["id"]: app.events(run["id"]) for run in runs})

    artifacts = {"A/value.txt": a / "value.txt", "A/after-restart.txt": a / "after-restart.txt",
                 "original B/value.txt": old_b / "value.txt", "new B/sentinel.txt": b / "sentinel.txt"}
    summary = {"passed": True, "mode": "offline_tool_operations", "model_calls": 0, "provider_requests": 0,
        "task_runs": len(runs), "tool_calls": sum(run["tool_calls"] for run in runs),
        "old_approval_stayed_in_A": True, "selection_survived_restart": True,
        "cross_workspace_session_rejected": True, "directory_replacement_rejected": True,
        "unavailable_selection_did_not_fallback": True,
        "artifacts": {name: {"text": path.read_text(encoding="utf-8"),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for name, path in artifacts.items()},
        "boundary": "Directory metadata and host binding checks are not an operating system sandbox or a proof against malicious concurrent file replacement."}
    save(base / "summary.json", summary)
    return summary


def run_demo(output_dir=None):
    if output_dir is None:
        with tempfile.TemporaryDirectory(prefix="astra-workspace-demo-") as directory:
            return {**execute(Path(directory)), "retained": False}
    target = Path(output_dir).absolute()
    source_root = Path(__file__).resolve().parents[1]
    if target.resolve().is_relative_to(source_root):
        raise ValueError("choose a new directory outside the Astra source tree, for example under your temporary directory")
    target.mkdir(parents=True, exist_ok=False)
    return {**execute(target), "retained": True, "directory": str(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="new fixture directory outside the Astra source tree")
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
