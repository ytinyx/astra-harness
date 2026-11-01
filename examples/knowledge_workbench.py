"""Prepare two synthetic collections; print an existing CLI command, never start it."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


COLLECTIONS = (
    ("product", "产品手册", (
        "# 合成产品手册\n"
        "Collection: product.\n"
        "Service level: Gold.\n"
        "Retry delay: 17 seconds.\n"
        "This guide describes a synthetic product, not a real service.\n"
    )),
    ("notes", "实验笔记", (
        "# 合成实验笔记\n"
        "Collection: notes.\n"
        "Service level: Silver.\n"
        "Retry delay: 43 seconds.\n"
        "This guide describes a synthetic experiment, not a real service.\n"
    )),
)


def _powershell_literal(value):
    """Escape every single-quote character recognized by PowerShell's parser."""
    text = str(value)
    # Smart quotes are also delimiters in PowerShell, and are legal in Windows
    # paths. Escaping only ASCII apostrophes could turn a printed path into code.
    for quote in "'\u2018\u2019\u201a\u201b":
        text = text.replace(quote, quote * 2)
    return "'" + text + "'"


def prepare_workbench(output_dir, *, python_executable=None):
    executable = sys.executable if python_executable is None else python_executable
    if not isinstance(executable, str) or not executable or "\0" in executable:
        raise ValueError("python_executable must be a nonempty path")
    directory = Path(output_dir).resolve()
    # Exclusive creation happens before writing anything. A second invocation
    # cannot overwrite source material, approval state, or a caller's index.
    directory.mkdir(parents=True, exist_ok=False)
    workspace = directory / "workspace"
    workspace.mkdir()
    (directory / "indexes").mkdir()
    configuration = {"collections": []}
    materials = []
    for identifier, label, content in COLLECTIONS:
        source_root = workspace / identifier
        source_root.mkdir()
        source = source_root / "guide.md"
        encoded = content.encode("utf-8")
        source.write_bytes(encoded)
        configuration["collections"].append({"id": identifier, "label": label, "path": identifier,
                                               "index": f"indexes/{identifier}.sqlite3"})
        materials.append({"collection": identifier, "label": label, "path": f"workspace/{identifier}/guide.md",
                          "sha256": hashlib.sha256(encoded).hexdigest()})
    config = directory / "knowledge.json"
    config.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    database = directory / "runs.sqlite3"
    command = [executable, "-X", "utf8", "-m", "astra_harness", "serve", "--workspace", str(workspace),
               "--database", str(database), "--knowledge-config", str(config)]
    powershell = (
        f"& {_powershell_literal(executable)} -X utf8 -m astra_harness serve "
        f"--workspace {_powershell_literal(workspace)} --database {_powershell_literal(database)} "
        f"--knowledge-config {_powershell_literal(config)}"
    )
    manifest = {"prepared": True, "service_started": False, "documents_imported": False,
                "workspace": "workspace", "knowledge_config": "knowledge.json", "runtime_database": "runs.sqlite3",
                "materials": materials, "indexes": [item["index"] for item in configuration["collections"]]}
    (directory / "prepared.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**manifest, "serve_argv": command, "serve_powershell": powershell}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Exclusively create a new directory for synthetic collections and configuration.")
    args = parser.parse_args(argv)
    try:
        result = prepare_workbench(args.output_dir)
    except FileExistsError:
        parser.exit(2, "Output already exists; choose a new --output-dir. Existing files were not changed.\n")
    print("Prepared two synthetic collections. No service was started, and no document was imported or approved.")
    print("From the project root, run this PowerShell command when you want to open the workbench:")
    print(result["serve_powershell"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
