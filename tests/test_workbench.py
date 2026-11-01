from contextlib import closing
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from astra_harness.application import Application
from astra_harness.builtin.files import FilePlugin, MAX_SEARCH_MATCHES
from astra_harness.builtin.models import OpenAICompatibleModel
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.store import Store
from astra_harness.tools import Tool


class ChatModel:
    def complete(self, request):
        return ModelResponse(text="收到")


class FileWriteModel:
    def complete(self, request):
        if request.messages[-1].role == "tool":
            return ModelResponse(text="已保存")
        return ModelResponse(tool_calls=(ToolCall("save", "files.write", {
            "path": "result.md", "content": "# 新结果\n"}),))


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.database = self.root / "runs.sqlite3"

    def app(self, model=None, **options):
        app = Application(model or ChatModel(), database=self.database, **options)
        self.addCleanup(app.close)
        return app

    def file_app(self, model=None, **options):
        return self.app(model, tools=FilePlugin(self.workspace).tools, workspace=self.workspace,
                        permissions=["files.read", "files.write"], **options)

    def test_rename_archive_restore_preserves_history(self):
        app = self.app()
        first = app.wait(app.submit("第一轮")["id"])
        identifier = first["session_id"]
        changed = app.update_session(identifier, title="需求讨论", archived=True)
        self.assertEqual(changed["title"], "需求讨论")
        self.assertTrue(changed["archived"])
        self.assertEqual(len(changed["runs"]), 1)
        self.assertEqual(app.list_sessions(), [])
        self.assertTrue(app.list_sessions(include_archived=True)[0]["archived"])
        with self.assertRaises(RuntimeError):
            app.submit("归档中", session_id=identifier)
        app.update_session(identifier, archived=False)
        app.wait(app.submit("第二轮", session_id=identifier)["id"])
        self.assertEqual(len(app.get_session(identifier)["messages"]), 4)

    def test_delete_removes_history_runs_and_events_atomically(self):
        app = self.app()
        run = app.wait(app.submit("可删除")["id"])
        self.assertGreater(len(app.events(run["id"])), 0)
        self.assertEqual(app.delete_session(run["session_id"]), {"id": run["session_id"], "deleted": True})
        self.assertEqual(app.list_runs(), [])
        self.assertEqual(app.list_sessions(include_archived=True), [])
        self.assertEqual(app.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        with self.assertRaises(KeyError):
            app.get_run(run["id"])

    def test_active_session_cannot_be_deleted_or_archived(self):
        app = self.app(autostart=False)
        run = app.submit("尚未执行")
        for action in (lambda: app.delete_session(run["session_id"]),
                       lambda: app.update_session(run["session_id"], archived=True)):
            with self.assertRaises(RuntimeError):
                action()
        app.cancel(run["id"])
        app.delete_session(run["session_id"])

    def test_session_field_validation(self):
        app = self.app(autostart=False)
        run = app.submit("会话")
        for title in (" ", "x" * 121, 4, "\ud800"):
            with self.subTest(title=repr(title)), self.assertRaises((ValueError, UnicodeError)):
                app.update_session(run["session_id"], title=title)
        with self.assertRaises(ValueError):
            app.update_session(run["session_id"], archived="yes")
        with self.assertRaises(ValueError):
            app.list_sessions(include_archived=1)

    def test_old_database_migrates_without_erasing_session(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT NOT NULL,history TEXT NOT NULL DEFAULT '[]',created REAL NOT NULL)")
            db.execute("INSERT INTO sessions(id,title,created) VALUES('old','旧会话',1)")
        store = Store(self.database)
        self.addCleanup(store.close)
        self.assertEqual(store.session("old")["title"], "旧会话")
        self.assertFalse(store.session("old")["archived"])
        store.update_session("old", "旧会话", True)
        self.assertEqual(store.sessions(), [])

    def test_model_endpoint_change_rejects_pending_resume(self):
        first = OpenAICompatibleModel("test-key", "same-model", "https://first.example")
        app = self.app(first, autostart=False, mode="live")
        run = app.submit("尚未发送到供应商")
        app.close()
        other = OpenAICompatibleModel("test-key", "same-model", "https://other.example")
        reopened = self.app(other, autostart=False, mode="live")
        state = reopened.get_run(run["id"])
        self.assertEqual((state["status"], state["error"]), ("failed", "configuration_changed"))

    def test_timeout_unknown_cannot_be_resolved_by_reopening_same_process(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class WriteModel:
            def complete(self, request):
                return ModelResponse(tool_calls=(ToolCall("write", "write", {}),))

        tool = Tool("write", "慢写入", (), lambda: release.wait(3), side_effect=True)
        app = self.app(WriteModel(), tools=(tool,), limits={"tool_timeout": 0.02})
        run = app.wait(app.submit("写入")["id"])
        self.assertEqual(run["status"], "unknown")
        self.assertFalse(app.get_run(run["id"])["can_resolve_unknown"])
        with self.assertRaises(RuntimeError):
            app.resolve_unknown(run["id"], "检查过了")
        with self.assertRaises(RuntimeError):
            app.delete_session(run["session_id"])
        with self.assertRaises(RuntimeError):
            app.update_session(run["session_id"], archived=True)
        self.assertEqual(app.list_sessions()[0]["id"], run["session_id"])
        self.assertEqual(app.info()["unresolved_runs"], [{
            "id": run["id"], "session_id": run["session_id"], "error": run["error"],
            "can_resolve_unknown": False}])
        app.close()
        reopened = self.app()
        with self.assertRaises(RuntimeError):
            reopened.resolve_unknown(run["id"], "仅重开应用不够")
        release.set()

    def test_previous_process_unknown_can_be_reviewed_without_replaying_tool(self):
        # 子进程退出是关键证据；不是仅关闭一个 Application 对象。
        script = '''
import sys, time
from astra_harness.application import Application
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.tools import Tool
class Model:
    def complete(self, request):
        return ModelResponse(tool_calls=(ToolCall("late", "write", {}),))
def late_write():
    time.sleep(10)
    raise AssertionError("late function should end with its process")
with Application(Model(), tools=(Tool("write", "write", (), late_write, side_effect=True),),
                 database=sys.argv[1], limits={"tool_timeout":0.02}) as app:
    run = app.wait(app.submit("write")["id"])
    assert run["status"] == "unknown"
    print(run["id"])
'''
        child = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(self.database)],
                               cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        identifier = child.stdout.strip()
        app = self.app()
        self.assertTrue(app.get_run(identifier)["can_resolve_unknown"])
        with self.assertRaises(ValueError):
            app.resolve_unknown(identifier, " ")
        resolved = app.resolve_unknown(identifier, "已核对工作区，未发现产物；后续另建任务")
        self.assertEqual(resolved["status"], "unknown")
        self.assertFalse(resolved["can_resolve_unknown"])
        self.assertIn("已核对", resolved["resolution"]["note"])
        self.assertEqual(app.info()["unresolved_runs"], [])
        self.assertTrue(app.update_session(resolved["session_id"], archived=True)["archived"])
        events = app.events(identifier)
        self.assertEqual(sum(e["type"] == "run.resolved" for e in events), 1)
        app.resolve_unknown(identifier, "重复请求不重复写审计")
        self.assertEqual(len(app.events(identifier)), len(events))
        self.assertEqual(app.wait(app.submit("继续正常聊天")["id"])["status"], "completed")
        self.assertEqual(app.get_run(identifier)["tool_calls"], 1)
        app.close()
        reopened = self.app()
        self.assertEqual(reopened.get_run(identifier)["resolution"], resolved["resolution"])
        self.assertEqual(reopened.wait(reopened.submit("重启后仍解除阻断")["id"])["status"], "completed")

    def test_file_browse_read_and_permission_boundary(self):
        (self.workspace / "notes").mkdir()
        (self.workspace / "notes" / "a.md").write_text("你好", encoding="utf-8")
        app = self.file_app()
        entries = app.list_files()["entries"]
        self.assertEqual(entries[0]["type"], "directory")
        self.assertEqual(app.list_files("notes")["entries"][0]["path"], "notes/a.md")
        self.assertEqual(app.read_file("notes/a.md"), {"path": "notes/a.md", "content": "你好", "size": 6})
        with self.assertRaises(ValueError):
            app.read_file("../runs.sqlite3")
        app.close()
        disabled = self.app(workspace=self.workspace)
        with self.assertRaises(RuntimeError):
            disabled.list_files()
        with self.assertRaises(RuntimeError):
            disabled.read_file("notes/a.md")

    def test_preview_shows_before_after_without_writing(self):
        target = self.workspace / "result.md"
        target.write_text("旧结果", encoding="utf-8")
        app = self.file_app(FileWriteModel())
        run = app.wait(app.submit("整理结果")["id"])
        preview = app.preview_write(run["id"])
        self.assertEqual(preview, {"path": "result.md", "before": "旧结果", "after": "# 新结果\n",
                                   "exists": True, "changed": True})
        self.assertEqual(target.read_text(encoding="utf-8"), "旧结果")
        app.approve(run["id"])
        self.assertEqual(app.wait(run["id"])["status"], "completed")
        self.assertEqual(app.read_file("result.md")["content"], "# 新结果\n")
        with self.assertRaises(RuntimeError):
            app.preview_write(run["id"])

    def test_preview_of_new_file_does_not_create_it(self):
        plugin = FilePlugin(self.workspace)
        preview = plugin.preview_write("new.txt", "content")
        self.assertFalse(preview["exists"])
        self.assertEqual(preview["before"], "")
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_search_nested_text_case_insensitive_with_line_numbers(self):
        (self.workspace / "nested").mkdir()
        (self.workspace / "nested" / "notes.md").write_text("第一行\nAgent harness\n第三行", encoding="utf-8")
        (self.workspace / "binary.dat").write_bytes(b"\xff\xfe")
        app = self.file_app()
        result = app.search_files("AGENT")
        self.assertEqual(result["matches"], [{"path": "nested/notes.md", "line": 2, "text": "Agent harness"}])
        self.assertFalse(result["truncated"])
        self.assertEqual(app.search_files("AGENT", "nested")["scanned_files"], 1)
        self.assertIn("files.search", app.info()["tools"])
        self.assertFalse(next(t for t in app.info()["tool_details"] if t["name"] == "files.search")["side_effect"])

    def test_search_limits_and_invalid_path(self):
        (self.workspace / "many.txt").write_text("match\n" * (MAX_SEARCH_MATCHES + 1), encoding="utf-8")
        plugin = FilePlugin(self.workspace)
        result = plugin.search("match")
        self.assertEqual(len(result["matches"]), MAX_SEARCH_MATCHES)
        self.assertTrue(result["truncated"])
        for query, path in ((" ", ""), ("x" * 201, ""), ("match", "../")):
            with self.assertRaises(ValueError):
                plugin.search(query, path)

    def test_search_counts_bytes_of_non_text_files(self):
        for index in range(9):
            (self.workspace / f"binary-{index}.dat").write_bytes(b"\xff" * 125000)
        result = FilePlugin(self.workspace).search("absent")
        self.assertEqual(result["matches"], [])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["scanned_files"], 9)

    def test_search_never_follows_directory_links(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("do-not-find", encoding="utf-8")
        link = self.workspace / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("host does not permit symlink creation")
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest("host does not permit symlink or junction creation")
        try:
            plugin = FilePlugin(self.workspace)
            self.assertEqual(plugin.search("do-not-find")["matches"], [])
            with self.assertRaises(ValueError):
                plugin.search("do-not-find", "linked")
            with self.assertRaises(ValueError):
                plugin.browse("linked")
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                link.rmdir()


if __name__ == "__main__":
    unittest.main()
