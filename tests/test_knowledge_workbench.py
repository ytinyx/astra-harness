"""Preparation is exclusive, offline, and leaves indexing/approval to the user."""
from contextlib import redirect_stdout
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from examples.knowledge_workbench import main, prepare_workbench


class KnowledgeWorkbenchTests(unittest.TestCase):
    def test_two_different_sources_share_filename_but_have_separate_index_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "prepared"
            result = prepare_workbench(directory)
            config = json.loads((directory / "knowledge.json").read_text(encoding="utf-8"))
            self.assertEqual(config, {"collections": [
                {"id": "product", "label": "产品手册", "path": "product", "index": "indexes/product.sqlite3"},
                {"id": "notes", "label": "实验笔记", "path": "notes", "index": "indexes/notes.sqlite3"},
            ]})
            product = (directory / "workspace/product/guide.md").read_text(encoding="utf-8")
            notes = (directory / "workspace/notes/guide.md").read_text(encoding="utf-8")
            self.assertIn("Service level: Gold.\nRetry delay: 17 seconds.", product)
            self.assertIn("Service level: Silver.\nRetry delay: 43 seconds.", notes)
            self.assertNotEqual(product, notes)
            self.assertNotIn("43 seconds", product)
            self.assertNotIn("17 seconds", notes)
            for material in result["materials"]:
                self.assertEqual(material["sha256"], hashlib.sha256((directory / material["path"]).read_bytes()).hexdigest())
            self.assertFalse(result["service_started"])
            self.assertFalse(result["documents_imported"])
            self.assertEqual(list((directory / "indexes").iterdir()), [])
            self.assertFalse((directory / "runs.sqlite3").exists())
            self.assertEqual(sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()),
                             ["knowledge.json", "prepared.json", "workspace/notes/guide.md", "workspace/product/guide.md"])

    def test_repeat_preparation_preserves_all_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "existing"
            prepare_workbench(directory)
            sentinel = directory / "keep.txt"
            sentinel.write_text("existing work", encoding="utf-8")
            before = {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
            with self.assertRaises(FileExistsError):
                prepare_workbench(directory)
            after = {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_printed_command_quotes_paths_and_contains_no_live_or_auto_import_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "O'Brien demo $literal"
            result = prepare_workbench(directory, python_executable="C:\\Synthetic Python O'Brien\\python.exe")
            self.assertTrue(result["serve_powershell"].startswith("& 'C:\\Synthetic Python O''Brien\\python.exe' -X utf8"))
            self.assertIn("O''Brien demo $literal", result["serve_powershell"])
            argv = result["serve_argv"]
            self.assertEqual(argv[1:6], ["-X", "utf8", "-m", "astra_harness", "serve"])
            self.assertEqual(argv[argv.index("--workspace") + 1], str(directory.resolve() / "workspace"))
            self.assertEqual(argv[argv.index("--knowledge-config") + 1], str(directory.resolve() / "knowledge.json"))
            self.assertNotIn("--live", argv)
            self.assertNotIn("--env-file", argv)
            self.assertNotIn("--knowledge-index", argv)

    def test_cli_requires_explicit_new_output_and_only_prints_start_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "cli"
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--output-dir", str(directory)])
            self.assertEqual(code, 0)
            self.assertIn("No service was started", output.getvalue())
            self.assertIn("--knowledge-config", output.getvalue())
            self.assertFalse((directory / "runs.sqlite3").exists())
            self.assertEqual(list((directory / "indexes").iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "PowerShell path quoting is a Windows-specific contract")
    def test_printed_commands_parse_as_one_command_with_exact_arguments_for_smart_quotes(self):
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not executable.is_file():
            self.skipTest("Windows PowerShell parser is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            cases = []
            for index, quote in enumerate([*"'\u2018\u2019\u201a\u201b", "'\u2018\u2019\u201a\u201b"]):
                directory = Path(temporary) / f"demo-{index} {quote} ; Write-Output NEVER_EXECUTED ; #"
                cases.append(prepare_workbench(directory,
                    python_executable=f"C:\\Synthetic O{quote} ; Write-Output NEVER_EXECUTED ; #\\python.exe"))
            payload = base64.b64encode(json.dumps([case["serve_powershell"] for case in cases], ensure_ascii=False)
                                       .encode("utf-8")).decode("ascii")
            # Only parse data with the system parser. Never invoke a printed
            # command or interpolate a path into executable PowerShell source.
            script = r"""
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$inputs = ConvertFrom-Json ([System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
$results = @()
foreach ($inputText in $inputs) {
    $tokens = $null; $parseErrors = $null
    $tree = [System.Management.Automation.Language.Parser]::ParseInput($inputText, [ref]$tokens, [ref]$parseErrors)
    $commands = @($tree.FindAll({param($node) $node -is [System.Management.Automation.Language.CommandAst]}, $true))
    $arguments = @($commands[0].CommandElements | ForEach-Object {
        if ($_ -is [System.Management.Automation.Language.CommandParameterAst] -and $null -eq $_.Argument) {
            '-' + $_.ParameterName
        } elseif ($_ -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
            $_.Value
        } else {
            throw 'Unexpected command element: only literal arguments are allowed'
        }
    })
    $results += @{errors=@($parseErrors).Count; statements=@($tree.EndBlock.Statements).Count;
                  commands=$commands.Count; arguments=$arguments}
}
ConvertTo-Json -InputObject @($results) -Depth 5 -Compress
""".replace("PAYLOAD", payload)
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            parsed = subprocess.run([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            results = json.loads(parsed.stdout)
            self.assertEqual(len(results), len(cases))
            for result, case in zip(results, cases):
                self.assertEqual((result["errors"], result["statements"], result["commands"]), (0, 1, 1))
                self.assertEqual(result["arguments"], case["serve_argv"])

    def test_prepared_config_passes_real_static_loader_without_creating_databases(self):
        from astra_harness.builtin.knowledge_collections import load_knowledge_config
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "loader"
            prepare_workbench(directory)
            before = {p.relative_to(directory).as_posix() for p in directory.rglob("*")}
            configs = load_knowledge_config(directory / "knowledge.json", workspace=directory / "workspace",
                                            database=directory / "runs.sqlite3")
            self.assertEqual(len(configs), 2)
            self.assertEqual({p.relative_to(directory).as_posix() for p in directory.rglob("*")}, before)
            self.assertEqual(list((directory / "indexes").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
