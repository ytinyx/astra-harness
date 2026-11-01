import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from astra_harness.builtin.files import FilePlugin, MAX_FILE_BYTES
from astra_harness.builtin.models import DemoModel
from astra_harness.contracts import Message, ModelRequest, ToolResult


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plugin = FilePlugin(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_read_write_and_no_temporary_residue(self):
        self.plugin.write_file("result.txt", "你好")
        self.assertEqual(self.plugin.read_file("result.txt"), "你好")
        self.assertEqual(self.plugin.list_files(), ["result.txt"])
        self.plugin.write_file("result.txt", "changed")
        self.assertEqual(self.plugin.read_file("result.txt"), "changed")
        write = self.plugin.tools[-1]
        self.assertTrue(write.side_effect and write.requires_approval)
        self.assertEqual(write.permissions, frozenset({"files.write"}))

    def test_path_escape_and_special_names_are_rejected(self):
        for path in ("../outside", "/outside", "C:\\outside", "\\\\host\\share", "a/../b",
                     "a:stream", "NUL", "a.", "a ", "", "a//b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.plugin.write_file(path, "x")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_nested_existing_parent_allowed_missing_parent_rejected(self):
        (self.root / "sub").mkdir()
        self.plugin.write_file("sub/a.txt", "x")
        self.assertEqual(self.plugin.read_file("sub/a.txt"), "x")
        with self.assertRaises(ValueError):
            self.plugin.write_file("missing/a.txt", "x")

    def test_read_and_write_size_limit(self):
        (self.root / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.plugin.read_file("large.txt")
        with self.assertRaises(ValueError):
            self.plugin.write_file("output.txt", "中" * MAX_FILE_BYTES)
        self.assertFalse((self.root / "output.txt").exists())

    def test_directory_enumeration_limit(self):
        for index in range(201):
            (self.root / str(index)).touch()
        with self.assertRaises(ValueError):
            self.plugin.list_files()

    def test_link_and_linked_root_are_rejected(self):
        directory = self.root / "real"
        directory.mkdir()
        target = directory / "data.txt"
        target.write_text("x")
        link = self.root / "link"
        try:
            link.symlink_to(directory, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("host does not permit symlink creation")
            created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(directory)],
                                     capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if created.returncode:
                self.skipTest("host does not permit symlink or junction creation")
        try:
            with self.assertRaises(ValueError):
                self.plugin.read_file("link/data.txt")
            with self.assertRaises(ValueError):
                self.plugin.write_file("link/data.txt", "changed")
            with self.assertRaises(ValueError):
                FilePlugin(link)
            self.assertEqual(target.read_text(), "x")
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                link.rmdir()

    def test_demo_uses_real_file_content_and_is_replayable(self):
        source = Path(__file__).parents[1] / "examples" / "workspace"
        for item in source.glob("*.md"):
            shutil.copyfile(item, self.root / item.name)
        # 修改输入能改变实际产物，排除硬编码最终 JSON。
        first = self.root / "01-product.md"
        first.write_text(first.read_text(encoding="utf-8").replace("整理首版需求清单", "核对改动后的需求"), encoding="utf-8")
        model = DemoModel()
        messages = [Message("user", "整理会议待办")]
        tools = {tool.name: tool for tool in self.plugin.tools}
        calls = []
        for _ in range(10):
            request = ModelRequest(tuple(messages), tuple(tool.spec() for tool in tools.values()))
            response = model.complete(request)
            self.assertEqual(response, DemoModel().complete(request))
            if not response.tool_calls:
                break
            messages.append(Message("assistant", tool_calls=response.tool_calls))
            for call in response.tool_calls:
                calls.append(call.name)
                # 本测试显式执行文件处理函数，审批集成由 Engine 测试验证。
                value = tools[call.name].handler(**call.arguments)
                messages.append(Message("tool", tool_result=ToolResult(call.call_id, call.name, value)))
        todos = json.loads((self.root / "todos.json").read_text(encoding="utf-8"))
        self.assertEqual(len(todos), 6)
        self.assertEqual(todos[0], {"task": "核对改动后的需求", "owner": "林舟", "due": "2026-11-05"})
        self.assertEqual(calls, ["files.list", "files.read", "files.read", "files.read", "files.write"])
        self.assertIn("离线", response.text)
