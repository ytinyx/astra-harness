"""Explicit local guidance, frozen approvals and independent CSV report checks.

The model is a deliberately small script that reads the current task guidance.
This verifies harness integration; it does not measure a language model's skill.
"""
import argparse
from contextlib import contextmanager
import csv
import io
import json
from pathlib import Path
import re
import tempfile

from astra_harness.application import Application
from astra_harness.builtin.files import FilePlugin, inspect_workspace_directory
from astra_harness.builtin.local_skills import LocalSkillLoader
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.skill_catalog import SkillCatalog
from astra_harness.workspace_binding import WorkspaceBinding


class GuidedCSVModel:
    """Reads request/tool data only: no filesystem or expected-result access."""
    def __init__(self):
        self.observed_guidance = []

    def complete(self, request):
        user = next(message.text for message in reversed(request.messages) if message.role == "user")
        task, marker, framed = user.partition("\n\n[Explicit task guidance; applies only to this task]\n")
        guidance = json.loads(framed.split("\n[End guidance.", 1)[0])["text"] if marker else None
        self.observed_guidance.append(guidance)
        fields = ["rows", "total"]
        if guidance:
            match = re.search(r"```json\s*(\{.*?\})\s*```", guidance, re.S)
            if match:
                fields = json.loads(match[1])["report_fields"]
        target = re.search(r"into ([a-z0-9_-]+\.json)", task)
        if target is None:
            raise ValueError("scripted demo expects a relative JSON report target")
        last = request.messages[-1]
        if last.role == "user":
            return ModelResponse(tool_calls=(ToolCall("read-records", "files.read", {"path": "records.csv"}),))
        if last.role != "tool" or last.tool_result is None:
            raise ValueError("unexpected scripted demo step")
        result = last.tool_result
        if result.error_code:
            return ModelResponse(text="Tool rejected: " + result.error_code)
        if result.name == "files.read":
            rows = list(csv.DictReader(io.StringIO(result.value)))
            aggregates = {"rows": len(rows), "total": sum(int(row["amount"]) for row in rows),
                "open_items": sum(row["status"] == "open" for row in rows),
                "missing_owner": sum(not row["owner"].strip() for row in rows)}
            report = {field: aggregates[field] for field in fields}
            return ModelResponse(tool_calls=(ToolCall("write-report", "files.write", {
                "path": target[1], "content": json.dumps(report, ensure_ascii=False, indent=2) + "\n"}),))
        if result.name == "files.write":
            return ModelResponse(text=json.dumps({"checked_fields": fields}))
        raise ValueError("unexpected demo tool")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


@contextmanager
def open_host(directory, *, model=None, permissions=("files.read", "files.write")):
    workspace = directory / "workspace"
    workspace.mkdir(exist_ok=True)
    files = FilePlugin(workspace)
    scope = {"id": "skills-demo", **inspect_workspace_directory(workspace)}
    holder = {}

    def bind(spec):
        require(spec == scope, "unexpected demo workspace")
        return WorkspaceBinding(**spec, tools=files.tools, permissions=tuple(sorted(permissions)), file_plugin=files)

    app = Application(model or GuidedCSVModel(), files.tools, database=directory / "runs.sqlite3",
        permissions=permissions, workspace=workspace, workspace_spec=scope, workspace_factory=bind,
        task_context_preparer=lambda selection, selected_scope: holder["catalog"].prepare(selection, selected_scope),
        mode="local-skill-demo", autostart=False)
    def loader(selected_scope):
        require(selected_scope == scope, "unexpected skill scope")
        return LocalSkillLoader(files)
    catalog = SkillCatalog(app.store, loader_factory=loader)
    holder["catalog"] = catalog
    try:
        app.start()
        yield app, catalog, scope
    finally:
        app.close()


def enable(catalog, scope):
    before = catalog.list(scope)
    item = before["items"][0]
    result = catalog.set_enabled(item["id"], True, before["revision"], item["identity"], scope)
    require(result["items"][0]["enabled"], "skill did not enable")
    return {"id": item["id"], "expected_identity": item["identity"]}


def start_report(app, output, *, selection=None, session_id=None):
    state = app.submit("Summarize records.csv into " + output, session_id=session_id,
                       task_context_selection=selection)
    waiting = app.wait(state["id"])
    require(waiting["status"] == "waiting_approval", "report did not wait for write approval")
    return waiting


def finish_report(app, waiting):
    app.approve(waiting["id"])
    result = app.wait(waiting["id"])
    require(result["status"] == "completed", "report task failed")
    return result


def execute(directory, retained):
    directory.mkdir(parents=True, exist_ok=False)
    workspace = directory / "workspace"
    skill_file = workspace / "skills/report-check/SKILL.md"
    skill_file.parent.mkdir(parents=True)
    sample = Path(__file__).parent / "skills/report-check/SKILL.md"
    original = sample.read_text(encoding="utf-8")
    skill_file.write_text(original, encoding="utf-8")
    (workspace / "records.csv").write_text("id,owner,status,amount\n1,Mei,open,7\n2,,done,5\n3,Lin,open,8\n", encoding="utf-8")
    with open_host(directory) as (app, catalog, scope):
        selection = enable(catalog, scope)
        waiting = start_report(app, "reviewed.json", selection=selection)
        require(not (workspace / "reviewed.json").exists(), "write happened before approval")
        frozen_text = waiting["task_context"]["text"]
        revised = original.replace('["rows", "total", "open_items", "missing_owner"]', '["rows", "total", "open_items"]')
        revised = revised.replace('- `missing_owner`: number of rows with an empty `owner`.\n', '')
        require(revised != original, "sample fields were not located")
        skill_file.write_text(revised, encoding="utf-8")
        changed = catalog.list(scope)["items"][0]
        require(changed["needs_review"] and not changed["enabled"], "changed file retained old approval")
    # The old task must retain its selected bytes across a real host reopen.
    reopened_model = GuidedCSVModel()
    with open_host(directory, model=reopened_model) as (app, catalog, scope):
        restored = app.get_run(waiting["id"])
        require(restored["status"] == "waiting_approval" and restored["task_context"]["text"] == frozen_text,
                "reopen replaced the frozen skill guidance")
        reviewed = finish_report(app, restored)
        require(json.loads((workspace / "reviewed.json").read_text()) == {
            "rows": 3, "total": 20, "open_items": 2, "missing_owner": 1}, "reviewed report failed independent oracle")
        require(reopened_model.observed_guidance[0] == frozen_text, "continuation did not use original guidance")
        baseline = finish_report(app, start_report(app, "plain.json", session_id=reviewed["session_id"]))
        require("task_context" not in baseline, "next task inherited a skill selection")
        require(json.loads((workspace / "plain.json").read_text()) == {"rows": 3, "total": 20}, "plain report failed oracle")
        new_selection = enable(catalog, scope)
        updated = finish_report(app, start_report(app, "updated.json", selection=new_selection))
        require(json.loads((workspace / "updated.json").read_text()) == {
            "rows": 3, "total": 20, "open_items": 2}, "updated report failed independent oracle")
        require(updated["task_context"]["selection"] != reviewed["task_context"]["selection"], "new content identity did not change")
        runs = [reviewed, baseline, updated]
        summary = {"passed": True, "retained": retained, "runs": [{key: run[key] for key in
            ("status", "model_calls", "tool_calls")} for run in runs],
            "model_calls": sum(run["model_calls"] for run in runs), "tool_calls": sum(run["tool_calls"] for run in runs),
            "external_model_requests": 0, "scripts_executed_from_skill": 0,
            "frozen_guidance_survived_restart": True, "next_task_inherited_skill": False,
            "revised_content_required_enable": True, "artifact_oracle_passed": True,
            "boundary": "Deterministic guidance-aware script and synthetic CSV, not a language-model quality benchmark."}
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def run_demo(output_dir=None):
    if output_dir is None:
        with tempfile.TemporaryDirectory(prefix="astra-local-skills-") as temporary:
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
