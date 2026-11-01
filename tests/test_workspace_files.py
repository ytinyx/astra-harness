"""Synthetic local fixtures for directory identity and file-access boundaries."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from astra_harness.builtin.files import FilePlugin, MAX_FILE_BYTES, inspect_workspace_directory
from astra_harness.contracts import ToolCall
from astra_harness.tool_identity import build_tool_identity
from astra_harness.tools import ToolExecutor, ToolRegistry


class WorkspaceFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="astra-files-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "工作 空间"
        self.root.mkdir()
        self.plugin = FilePlugin(self.root)

    def link_directory(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("directory links unavailable")
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest("directory links unavailable")
        self.addCleanup(lambda: link.unlink() if link.is_symlink() else link.rmdir())

    def assert_error(self, code, function, *args):
        with self.assertRaisesRegex(ValueError, "^" + code + "$"):
            function(*args)

    def assert_direct_denied(self, name, code):
        for function, args in ((self.plugin.read_file, (name,)),
                               (self.plugin.write_file, (name, "replacement")),
                               (self.plugin.preview_write, (name, "replacement"))):
            with self.subTest(entrypoint=function.__name__, path=name):
                self.assert_error(code, function, *args)
        result = self.plugin.read_range(name)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], code)
        self.assertTrue(result["hint"])

    def test_metadata_only_identity_and_normal_chinese_spaces(self):
        with patch.object(Path, "open", side_effect=AssertionError("must not read content")), \
                patch("os.scandir", side_effect=AssertionError("must not enumerate")):
            first = inspect_workspace_directory(self.root)
        self.assertEqual(first["path"], str(self.root))
        self.assertRegex(first["identity"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.plugin.workspace_identity, first["identity"])
        (self.root / "子目录 空格").mkdir()
        self.plugin.write_file("子目录 空格/材料 一.txt", "你好\nsecond line")
        self.assertEqual(self.plugin.read_file("子目录 空格/材料 一.txt"), "你好\nsecond line")
        self.assertEqual(inspect_workspace_directory(self.root)["identity"], first["identity"])
        self.assertIsNone(self.plugin.validate_workspace())
        self.assertEqual(self.plugin.search("你好")["matches"][0]["path"], "子目录 空格/材料 一.txt")
        self.assertEqual(self.plugin.read_range("子目录 空格/材料 一.txt")["fragments"][0]["text"], "你好")

    def test_remote_device_and_ambiguous_paths_are_rejected_before_stat(self):
        candidates = (r"\\server\share\folder", "//server/share/folder", r"\\?\C:\data", r"\\.\pipe\name",
                      "C:relative", str(self.root / ".." / self.root.name), "bad\0name", "\ud800")
        with patch.object(Path, "lstat", side_effect=AssertionError("must reject before stat")):
            for candidate in candidates:
                with self.subTest(candidate=repr(candidate)):
                    self.assert_error("workspace_invalid_path", inspect_workspace_directory, candidate)

    def test_missing_regular_file_and_unavailable_identity_rejected(self):
        self.assert_error("workspace_missing", inspect_workspace_directory, self.root / "missing")
        filename = self.root / "normal.txt"
        filename.write_text("synthetic", encoding="utf-8")
        self.assert_error("workspace_not_directory", inspect_workspace_directory, filename)
        original = Path.lstat
        def missing_identity(path):
            observed = original(path)
            if path == self.root:
                return SimpleNamespace(st_mode=observed.st_mode, st_file_attributes=0,
                                       st_nlink=observed.st_nlink, st_dev=observed.st_dev, st_ino=0)
            return observed
        with patch.object(Path, "lstat", missing_identity):
            self.assert_error("workspace_identity_unavailable", inspect_workspace_directory, self.root)

    def test_all_entries_reject_replaced_root_and_do_not_touch_new_directory(self):
        (self.root / "sample.txt").write_text("old original", encoding="utf-8")
        moved = self.base / "original-retained"
        self.root.rename(moved)
        self.root.mkdir()
        (self.root / "sample.txt").write_text("new directory", encoding="utf-8")
        self.assertNotEqual(inspect_workspace_directory(self.root)["identity"], self.plugin.workspace_identity)
        for function, args in ((self.plugin.validate_workspace, ()), (self.plugin.list_files, ()),
                               (self.plugin.browse, ()), (self.plugin.search, ("directory",)),
                               (self.plugin.read_file, ("sample.txt",)),
                               (self.plugin.preview_write, ("sample.txt", "edited")),
                               (self.plugin.write_file, ("sample.txt", "edited"))):
            with self.subTest(entrypoint=function.__name__):
                self.assert_error("workspace_changed", function, *args)
        self.assertEqual(self.plugin.read_range("sample.txt")["error_code"], "workspace_changed")
        self.assertEqual((self.root / "sample.txt").read_text(), "new directory")
        self.assertEqual((moved / "sample.txt").read_text(), "old original")

    def test_ancestor_junction_is_not_hidden_by_resolve_at_construction(self):
        real = self.base / "real-parent"
        (real / "child").mkdir(parents=True)
        alias = self.base / "alias"
        self.link_directory(alias, real)
        self.assert_error("links_not_allowed", inspect_workspace_directory, alias / "child")
        self.assert_error("links_not_allowed", FilePlugin, alias / "child")

    def test_replaced_ancestor_is_rechecked_at_original_lexical_location(self):
        parent = self.base / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        plugin = FilePlugin(child)
        parent.rename(self.base / "retained-parent")
        replacement = self.base / "replacement-parent"
        (replacement / "child").mkdir(parents=True)
        (replacement / "child" / "data.txt").write_text("synthetic replacement", encoding="utf-8")
        self.link_directory(parent, replacement)
        self.assert_error("workspace_changed", plugin.read_file, "data.txt")
        self.assert_error("workspace_changed", plugin.write_file, "data.txt", "must not write")
        self.assertEqual((replacement / "child" / "data.txt").read_text(), "synthetic replacement")

    def test_sensitive_files_and_directories_are_not_listed_searched_or_previewed(self):
        (self.root / "normal.txt").write_text("needle public", encoding="utf-8")
        for name in (".env", "secrets.txt", "TOKEN.json", "id_ed25519"):
            (self.root / name).write_text("needle private fixture", encoding="utf-8")
            self.assert_direct_denied(name, "forbidden_path")
        for name in (".git", "credentials"):
            (self.root / name).mkdir()
            (self.root / name / "data.txt").write_text("needle private fixture", encoding="utf-8")
            self.assert_direct_denied(name + "/data.txt", "forbidden_path")
            self.assert_error("forbidden_path", self.plugin.browse, name)
            self.assert_error("forbidden_path", self.plugin.search, "needle", name)
        self.assertEqual(self.plugin.list_files(), ["normal.txt"])
        self.assertEqual([entry["name"] for entry in self.plugin.browse()["entries"]], ["normal.txt"])
        self.assertEqual([match["path"] for match in self.plugin.search("needle")["matches"]], ["normal.txt"])

    def test_new_sensitive_filename_is_rejected_before_creating_temporary_file(self):
        with patch("tempfile.mkstemp", side_effect=AssertionError("must not create temporary file")):
            self.assert_error("forbidden_path", self.plugin.write_file, ".env.local", "synthetic")
            self.assert_error("forbidden_path", self.plugin.preview_write, "passwords.txt", "synthetic")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_real_hardlink_is_rejected_by_all_access_paths(self):
        outside = self.base / "outside.txt"
        outside.write_text("needle outside synthetic", encoding="utf-8")
        shared = self.root / "shared.txt"
        os.link(outside, shared)
        self.assertGreater(shared.stat().st_nlink, 1)
        self.assert_direct_denied("shared.txt", "links_not_allowed")
        self.assertEqual(self.plugin.list_files(), [])
        self.assertEqual(self.plugin.browse()["entries"], [])
        self.assertEqual(self.plugin.search("needle")["matches"], [])
        self.assertEqual(outside.read_text(), "needle outside synthetic")

    @unittest.skipUnless(os.name == "nt", "Windows hidden attributes")
    def test_windows_hidden_attribute_is_excluded(self):
        filename = self.root / "ordinary-name.txt"
        filename.write_text("needle hidden synthetic", encoding="utf-8")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        set_attributes = kernel.SetFileAttributesW
        set_attributes.argtypes, set_attributes.restype = [ctypes.c_wchar_p, ctypes.c_uint32], ctypes.c_int
        original = filename.lstat().st_file_attributes
        self.assertTrue(set_attributes(str(filename), original | 0x2))
        self.addCleanup(lambda: set_attributes(str(filename), original))
        self.assert_direct_denied(filename.name, "forbidden_path")
        self.assertEqual(self.plugin.list_files(), [])
        self.assertEqual(self.plugin.browse()["entries"], [])
        self.assertEqual(self.plugin.search("needle")["matches"], [])

    def test_lists_skip_directory_links_and_never_traverse_them(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sample.txt").write_text("needle outside synthetic", encoding="utf-8")
        self.link_directory(self.root / "linked", outside)
        self.assertEqual(self.plugin.list_files(), [])
        self.assertEqual(self.plugin.browse()["entries"], [])
        self.assertEqual(self.plugin.search("needle")["matches"], [])
        self.assert_direct_denied("linked/sample.txt", "links_not_allowed")

    def test_permissions_approval_and_existing_byte_limits_are_preserved(self):
        self.plugin.write_file("sample.txt", "safe")
        executor = ToolExecutor(ToolRegistry(self.plugin.tools))
        call = ToolCall("read", "files.read", {"path": "sample.txt"})
        with patch.object(Path, "open", side_effect=AssertionError("permission denial must not read")):
            self.assertEqual(executor.execute(call).error_code, "permission_denied")
        self.assertEqual(executor.execute(call, frozenset({"files.read"})).value, "safe")
        write = ToolCall("write", "files.write", {"path": "sample.txt", "content": "edited"})
        self.assertEqual(executor.execute(write, frozenset({"files.write"})).error_code, "approval_required")
        self.assertEqual(self.plugin.read_file("sample.txt"), "safe")
        self.assertIsNone(executor.execute(write, frozenset({"files.write"}), approved=True).error_code)
        self.assertEqual(self.plugin.read_file("sample.txt"), "edited")
        self.assert_error("file_limit", self.plugin.write_file, "big.txt", "中" * MAX_FILE_BYTES)
        self.assertFalse((self.root / "big.txt").exists())

    def test_tool_contract_fingerprint_is_independent_of_workspace_inode(self):
        second = self.base / "second"
        second.mkdir()
        another = FilePlugin(second)
        self.assertNotEqual(self.plugin.workspace_identity, another.workspace_identity)
        expected = build_tool_identity(FilePlugin.tool_definitions())
        self.assertEqual(build_tool_identity(self.plugin.tools), expected)
        self.assertEqual(build_tool_identity(another.tools), expected)
        self.assertTrue(all(tool.recovery_identity is None for tool in self.plugin.tools))
        for tool in self.plugin.tools:
            self.assertIn(FilePlugin.validate_workspace, tool.implementation_dependencies)
            self.assertIn(inspect_workspace_directory, tool.implementation_dependencies)


if __name__ == "__main__":
    unittest.main()
