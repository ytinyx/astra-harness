import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from astra_harness.application import Application
from astra_harness.builtin.files import FilePlugin, MAX_FILE_BYTES
from astra_harness.builtin.models import OpenAICompatibleModel
from astra_harness.contracts import Message, ModelRequest, ModelResponse, ToolCall
from astra_harness.engine import Engine
from astra_harness.tools import ToolExecutor, ToolRegistry


class RangeModel:
    def __init__(self, count=0):
        self.count, self.requests = count, []

    def complete(self, request):
        self.requests.append(request)
        if self.count and request.messages[-1].role != "tool":
            return ModelResponse(tool_calls=tuple(ToolCall(f"read-{i}", "files.read_range", {"path": "input.txt"})
                                                 for i in range(self.count)))
        return ModelResponse(text="done")


class FileRangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plugin = FilePlugin(self.root)

    def source(self, content, name="input.txt"):
        (self.root / name).write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return name

    def read(self, **arguments):
        return self.plugin.read_range("input.txt", **arguments)

    def test_registration_preserves_old_read_and_last_write(self):
        self.source("first\r\nsecond\x00")
        tools = {tool.name: tool for tool in self.plugin.tools}
        self.assertEqual(self.plugin.tools[-1].name, "files.write")
        self.assertEqual(tools["files.read"].input_schema,
                         {"type": "object", "properties": {"path": {"type": "string", "maxLength": 240}},
                          "required": ["path"], "additionalProperties": False})
        self.assertEqual(tools["files.read"].handler("input.txt"), "first\r\nsecond\x00")
        range_tool = tools["files.read_range"]
        self.assertEqual(range_tool.permissions, frozenset({"files.read"}))
        self.assertFalse(range_tool.side_effect or range_tool.requires_approval)
        self.assertEqual(self.read()["error_code"], "binary_file")

    def test_empty_files_empty_lines_and_eof_do_not_create_phantom_lines(self):
        for content, lines in (("", []), ("\n", [""]), ("\r\n", [""]), ("x\n", ["x"]),
                               ("x\n\n", ["x", ""]), ("\r\n\r", ["", ""])):
            with self.subTest(content=repr(content)):
                self.source(content)
                result = self.read()
                self.assertTrue(result["ok"])
                self.assertEqual(result["total_lines"], len(lines))
                self.assertEqual([item["text"] for item in result["fragments"]], lines)
                self.assertFalse(result["truncated"])
                self.assertIsNone(result["next_cursor"])
                self.assertTrue(all(not item["partial"] for item in result["fragments"]))

    def test_defaults_and_line_limit_produce_real_continuations(self):
        self.source("\n".join(str(i) for i in range(1, 102)))
        first = self.read()
        self.assertEqual(len(first["fragments"]), 40)
        self.assertEqual(first["next_cursor"], {"start_line": 41, "start_column": 1})
        page = self.read(line_count=100)
        self.assertEqual(len(page["fragments"]), 100)
        last = self.read(**page["next_cursor"])
        self.assertEqual(last["fragments"], [{"line": 101, "start_column": 1, "text": "101", "partial": False}])
        self.assertFalse(last["truncated"])

    def test_all_splitlines_separators_and_multibyte_chunk_boundaries(self):
        # The CR is the final byte of a 4096-byte chunk; the emoji crosses the next boundary.
        content = "a" * 4095 + "\r\n" + "b" * 4094 + "😀中\n\v\f\x1c\x1d\x1e\x85\u2028\u2029尾\ufeff"
        self.source(content)
        actual, cursor = [], {}
        for _ in range(20):
            page = self.read(line_count=3, **cursor)
            self.assertTrue(page["ok"])
            self.assertEqual(page["total_lines"], len(content.splitlines()))
            actual.extend(page["fragments"])
            if not page["truncated"]:
                break
            self.assertNotEqual(cursor, page["next_cursor"])
            cursor = page["next_cursor"]
        else:
            self.fail("cursor did not reach EOF")
        rebuilt = [""] * len(content.splitlines())
        for fragment in actual:
            index = fragment["line"] - 1
            self.assertEqual(fragment["start_column"], len(rebuilt[index]) + 1)
            rebuilt[index] += fragment["text"]
        self.assertEqual(rebuilt, content.splitlines())

    def test_long_unicode_line_pages_without_lost_or_repeated_codepoints(self):
        content = "🙂a\u0301" * 1700
        self.source(content)
        rebuilt, cursor = "", {}
        while True:
            page = self.read(**cursor)
            self.assertTrue(page["ok"])
            self.assertEqual(len(page["fragments"]), 1)
            fragment = page["fragments"][0]
            self.assertEqual(fragment["start_column"], len(rebuilt) + 1)
            self.assertLessEqual(len(fragment["text"]), 2000)
            rebuilt += fragment["text"]
            self.assertEqual(fragment["partial"], len(rebuilt) < len(content))
            if page["next_cursor"] is None:
                break
            self.assertEqual(page["next_cursor"], {"start_line": 1, "start_column": len(rebuilt) + 1})
            cursor = page["next_cursor"]
        self.assertEqual(rebuilt, content)

    def test_character_limit_uses_lookahead_for_partial_and_empty_continuations(self):
        for suffix, expected_next, partial in (("", None, False), ("\r\n", None, False),
                                               ("x", {"start_line": 1, "start_column": 2001}, True),
                                               ("\nnext", {"start_line": 2, "start_column": 1}, False),
                                               ("\n\nnext", {"start_line": 2, "start_column": 1}, False)):
            with self.subTest(suffix=repr(suffix)):
                self.source("a" * 2000 + suffix)
                page = self.read(line_count=1)
                self.assertEqual(page["next_cursor"], expected_next)
                self.assertEqual(page["fragments"][0]["partial"], partial)
                if suffix == "\n\nnext":
                    blank = self.read(line_count=1, **expected_next)
                    self.assertEqual(blank["fragments"][0]["text"], "")
                    self.assertEqual(blank["next_cursor"], {"start_line": 3, "start_column": 1})

    def test_explicit_columns_and_range_errors_are_actionable_and_bounded(self):
        self.source("abc\n\n末尾")
        result = self.read(start_line=1, start_column=2, line_count=1)
        self.assertEqual(result["fragments"][0], {"line": 1, "start_column": 2, "text": "bc", "partial": False})
        for arguments, line_length in (({"start_line": 4}, None), ({"start_column": 4}, 3),
                                        ({"start_line": 2, "start_column": 2}, 0)):
            with self.subTest(arguments=arguments):
                result = self.read(**arguments)
                self.assertFalse(result["ok"])
                self.assertEqual((result["error_code"], result["total_lines"], result["line_length"]),
                                 ("range_out_of_bounds", 3, line_length))
                self.assertNotIn(str(self.root), json.dumps(result))
        for name in ("start_line", "start_column", "line_count"):
            for value in (False, True, 1.0, 0, -1, 10**100):
                with self.subTest(name=name, value=value):
                    self.assertEqual(self.read(**{name: value})["error_code"], "invalid_range")
        self.assertEqual(self.read(line_count=101)["error_code"], "invalid_range")
        self.source("")
        self.assertEqual(self.read(start_line=2)["error_code"], "range_out_of_bounds")

    def test_tail_validation_cannot_be_bypassed_by_a_small_first_page(self):
        for content, code in ((b"ok\n" + b"x" * 9000 + b"\xff", "invalid_utf8"),
                              (b"ok\n" + b"x" * 9000 + b"\xe4\xb8", "invalid_utf8"),
                              (b"ok\n" + b"x" * 9000 + b"\0", "binary_file"),
                              (b"ok\n" + b"x" * (MAX_FILE_BYTES - 2), "file_limit")):
            with self.subTest(code=code, size=len(content)):
                self.source(content)
                result = self.read(line_count=1)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], code)
                self.assertNotIn("fragments", result)

    def test_scan_reads_fixed_chunks_through_eof_without_whole_file_helpers(self):
        self.source(b"x" * MAX_FILE_BYTES)
        original_open = Path.open
        observed, sizes = [], []

        class Reader:
            def __enter__(inner):
                inner.stream = original_open(self.root / "input.txt", "rb")
                return inner

            def __exit__(inner, *args):
                inner.stream.close()

            def read(inner, size):
                self.assertGreater(size, 0)
                self.assertLessEqual(size, 4096)
                sizes.append(size)
                data = inner.stream.read(size)
                observed.append(len(data))
                return data

        with patch.object(Path, "open", return_value=Reader()), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")), \
                patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")):
            result = self.read()
        self.assertTrue(result["ok"])
        self.assertEqual(sum(observed), MAX_FILE_BYTES)
        self.assertEqual(observed[-1], 0)
        self.assertGreater(len(sizes), 2)
        self.assertEqual(len(result["fragments"][0]["text"]), 2000)
        self.assertTrue(result["fragments"][0]["partial"])

    def test_maximum_text_and_empty_line_metadata_fit_executor_result_budget(self):
        self.source("\n" * 119900 + ("😀" * 20 + "\n") * 100)
        result = self.read(start_line=119901, line_count=100)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["fragments"]), 100)
        self.assertEqual(sum(len(item["text"]) for item in result["fragments"]), 2000)
        # Include worst allowed path/column lengths without requiring a host to
        # support a 240-codepoint filesystem name. ASCII JSON is the larger form.
        result["path"] = "😀" * 240
        for item in result["fragments"]:
            item["start_column"] = MAX_FILE_BYTES
        self.assertLess(len(json.dumps(result, ensure_ascii=True).encode("utf-8")), 40000)
        self.source("\n" * 101)
        blanks = self.read(line_count=100)
        self.assertEqual(len(blanks["fragments"]), 100)
        self.assertTrue(all(item["text"] == "" for item in blanks["fragments"]))
        self.assertEqual(blanks["next_cursor"], {"start_line": 101, "start_column": 1})

    def test_schema_permission_and_tool_budget_reject_before_handler(self):
        self.source("allowed")
        original = next(tool for tool in self.plugin.tools if tool.name == "files.read_range")
        handler = Mock(wraps=original.handler)
        tool = replace(original, handler=handler)
        executor = ToolExecutor(ToolRegistry((tool,)))
        permission = frozenset({"files.read"})
        invalid = ({"path": "input.txt", "start_line": True}, {"path": "input.txt", "start_column": 1.0},
                   {"path": "input.txt", "line_count": 101}, {"path": "input.txt", "max_chars": 5000})
        for arguments in invalid:
            self.assertEqual(executor.execute(ToolCall("x", tool.name, arguments), permission).error_code, "invalid_arguments")
        self.assertEqual(executor.execute(ToolCall("x", tool.name, {"path": "input.txt"})).error_code, "permission_denied")
        handler.assert_not_called()
        engine = Engine(RangeModel(count=2), (tool,))
        state = engine.create("read", permissions=permission, limits={"max_tool_calls": 1})
        asyncio.run(engine.advance(state))
        self.assertEqual((state["status"], state["error"]), ("failed", "tool_limit"))
        handler.assert_not_called()
        result = executor.execute(ToolCall("x", tool.name, {"path": "input.txt"}), permission)
        self.assertIsNone(result.error_code)
        self.assertTrue(result.value["ok"])

    def test_paths_reuse_workspace_boundary_and_do_not_echo_private_locations(self):
        self.source("unchanged")
        for path in ("../outside", "/outside", "C:\\outside", "missing", ".", "a:stream"):
            with self.subTest(path=path):
                result = self.plugin.read_range(path)
                self.assertFalse(result["ok"])
                self.assertNotIn(str(self.root), json.dumps(result))
        (self.root / "folder").mkdir()
        (self.root / "folder" / "text.txt").write_text("normal", encoding="utf-8")
        self.assertEqual(self.plugin.read_range("folder\\text.txt")["path"], "folder/text.txt")
        self.assertEqual(self.plugin.read_range("folder")["error_code"], "not_file")
        link = self.root / "link"
        try:
            link.symlink_to(self.root / "folder", target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("host does not permit symlinks")
            created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(self.root / "folder")],
                                     capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if created.returncode:
                self.skipTest("host does not permit links")
        try:
            self.assertEqual(self.plugin.read_range("link/missing.txt")["error_code"], "links_not_allowed")
        finally:
            link.unlink() if link.is_symlink() else link.rmdir()

    def test_new_tool_snapshot_rejects_old_pending_and_paused_but_keeps_terminal_history(self):
        old_tools = tuple(tool for tool in self.plugin.tools if tool.name != "files.read_range")
        database = self.root / "runs.sqlite3"
        with Application(RangeModel(), old_tools, database=database, autostart=False) as app:
            pending = app.submit("pending")
            paused = app.submit("paused")
            app.pause(paused["id"])
            terminal = app.submit("cancelled")
            terminal = app.cancel(terminal["id"])
            saved_terminal = app.store.get(terminal["id"])
        new_model = RangeModel()
        with Application(new_model, self.plugin.tools, database=database, autostart=False) as app:
            for run in (pending, paused):
                state = app.get_run(run["id"])
                self.assertEqual((state["status"], state["error"], state["model_calls"], state["tool_calls"]),
                                 ("failed", "tool_snapshot_changed", 0, 0))
            self.assertEqual(app.get_run(terminal["id"])["status"], "cancelled")
            self.assertEqual(app.store.get(terminal["id"]), saved_terminal)
            self.assertEqual(new_model.requests, [])

    def test_chat_completions_maps_range_and_old_read_without_network(self):
        model = OpenAICompatibleModel("fixture-placeholder", "fixture", "https://example.invalid/v1")
        tools = tuple(tool.spec() for tool in self.plugin.tools)
        with patch.object(model.opener, "open", side_effect=AssertionError("network forbidden")):
            payload, inverse = model._payload(ModelRequest((Message("user", "read"),), tools))
            functions = {inverse[item["function"]["name"]]: item["function"] for item in payload["tools"]}
            self.assertEqual(functions["files.read"]["parameters"]["required"], ["path"])
            self.assertEqual(set(functions["files.read"]["parameters"]["properties"]), {"path"})
            arguments = {"path": "input.txt", "start_line": 2, "start_column": 3, "line_count": 4}
            restored = model._calls([{"id": "range-1", "function": {
                "name": functions["files.read_range"]["name"], "arguments": json.dumps(arguments)}}], inverse)
            self.assertEqual(restored, (ToolCall("range-1", "files.read_range", arguments),))


if __name__ == "__main__":
    unittest.main()
