"""Synthetic conditional edits, exact bytes, approvals and crash uncertainty."""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from astra_harness.application import Application
from astra_harness.builtin.editing import FileEditPlugin, MAX_BACKUPS, MAX_RECORD_BYTES
from astra_harness.builtin.files import FilePlugin, MAX_FILE_BYTES
from astra_harness.contracts import ToolCall
from astra_harness.tools import ToolExecutor, ToolRegistry, ToolExecutionUnknown, ToolUnavailable


def sha(data):
    return hashlib.sha256(data).hexdigest()


class NoModel:
    def complete(self, request):
        raise AssertionError("file operation must not call a model")


class EditingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base/"workspace"
        self.root.mkdir()
        self.history = self.base/"runtime"/"history"/"scope"
        self.files = FilePlugin(self.root)
        self.plugin = FileEditPlugin(self.files, self.history)
        self.path = self.root/"note.txt"
        self.original = "第一行\r\nunique target\r\n末尾\r\n".encode("utf-8")
        self.path.write_bytes(self.original)

    def edit(self, **kwargs):
        arguments = dict(path="note.txt", old_text="unique target", new_text="更正内容", expected_sha256=sha(self.original))
        arguments.update(kwargs)
        return self.plugin.edit(**arguments)

    def rejected(self, reason, function, *args, **kwargs):
        with self.assertRaises(ToolUnavailable) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, "edit_rejected")
        self.assertEqual(caught.exception.details["reason"], reason)
        self.assertIs(caught.exception.details["applied"], False)
        return caught.exception.details

    def backups(self):
        return list(self.history.glob("*.json"))

    def test_constructor_inspection_and_preview_never_create_history(self):
        inspected = self.plugin.inspect("note.txt")
        self.assertEqual(inspected, {"ok": True, "path": "note.txt", "bytes": len(self.original),
                                    "sha256": sha(self.original), "encoding": "utf-8"})
        preview = self.plugin.preview_edit("note.txt", "unique target", "更正内容", sha(self.original))
        self.assertEqual(preview["before"].encode(), self.original)
        self.assertEqual(preview["after"], "第一行\r\n更正内容\r\n末尾\r\n")
        self.assertTrue(preview["exists"] and preview["changed"])
        self.assertFalse(self.history.parent.parent.exists())
        self.assertNotIn("before", inspected)

    def test_edit_preserves_crlf_utf8_and_immutable_before_bytes(self):
        result = self.edit()
        expected = "第一行\r\n更正内容\r\n末尾\r\n".encode()
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(result["before_sha256"], sha(self.original))
        self.assertEqual(result["after_sha256"], sha(expected))
        self.assertEqual(result["bytes"], len(expected))
        self.assertTrue(result["ok"] and result["applied"])
        backup = self.history/(result["backup_id"]+".json")
        saved = json.loads(backup.read_text(encoding="utf-8"))
        self.assertEqual(base64.b64decode(saved["data_base64"]), self.original)
        self.assertNotIn(self.original.decode(), json.dumps(result, ensure_ascii=False))
        self.assertEqual(list(self.root.glob(".astra-edit-*")), [])

    def test_utf8_bom_and_exact_raw_digest(self):
        data = b"\xef\xbb\xbfone\r\ntwo\r\n"
        self.path.write_bytes(data)
        self.assertEqual(self.plugin.inspect("note.txt")["sha256"], sha(data))
        self.edit(old_text="two", new_text="三", expected_sha256=sha(data))
        self.assertEqual(self.path.read_bytes(), b"\xef\xbb\xbfone\r\n"+"三\r\n".encode())

    def test_stale_hash_rejected_without_backup_or_original_write(self):
        self.rejected("stale_file", self.edit, expected_sha256="0"*64)
        self.assertFalse(self.history.exists())
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_invalid_digest_is_rejected_before_read(self):
        for value in (None, "A"*64, "abc", 5):
            with self.subTest(value=value), patch.object(self.plugin, "_read", side_effect=AssertionError("read")):
                self.rejected("invalid_sha256", self.edit, expected_sha256=value)

    def test_missing_ambiguous_and_overlapping_matches(self):
        self.rejected("match_not_found", self.edit, old_text="not present")
        for data, old in ((b"aaa", "aa"), (b"token token", "token")):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                self.rejected("ambiguous_match", self.edit, old_text=old, expected_sha256=sha(data))
                self.assertEqual(self.path.read_bytes(), data)
        self.assertFalse(self.history.exists())

    def test_empty_old_no_change_and_invalid_unicode(self):
        self.rejected("invalid_edit", self.edit, old_text="")
        self.rejected("no_change", self.edit, new_text="unique target")
        self.rejected("invalid_utf8", self.edit, new_text="\ud800")
        self.rejected("binary_file", self.edit, new_text="\0")
        self.assertFalse(self.history.exists())

    def test_empty_new_text_is_an_explicit_deletion(self):
        result = self.edit(new_text="")
        self.assertEqual(self.path.read_bytes(), "第一行\r\n\r\n末尾\r\n".encode())
        self.assertEqual(result["operation"], "edit")

    def test_byte_limit_applies_to_input_and_expanded_output(self):
        self.rejected("file_limit", self.edit, new_text="中"*MAX_FILE_BYTES)
        self.path.write_bytes(b"a"+b"x"*(MAX_FILE_BYTES-1))
        self.rejected("file_limit", self.edit, old_text="a", new_text="abc", expected_sha256=sha(self.path.read_bytes()))
        self.path.write_bytes(b"a"*(MAX_FILE_BYTES+1))
        self.rejected("file_limit", self.plugin.inspect, "note.txt")
        self.assertFalse(self.history.exists())

    def test_binary_invalid_utf8_and_empty_files(self):
        for data, reason in ((b"a\0b", "binary_file"), (b"\xff", "invalid_utf8")):
            with self.subTest(reason=reason):
                self.path.write_bytes(data)
                self.rejected(reason, self.plugin.inspect, "note.txt")
                self.rejected(reason, self.edit, expected_sha256=sha(data))
        self.path.write_bytes(b"")
        self.assertEqual(self.plugin.inspect("note.txt")["sha256"], sha(b""))
        self.rejected("match_not_found", self.edit, expected_sha256=sha(b""))

    def test_file_boundaries_are_the_actual_file_plugin_boundaries(self):
        for name in ("../outside", "C:\\outside", ".env", "secrets.txt", "a/../note.txt", "NUL"):
            with self.subTest(name=name), self.assertRaises(ToolUnavailable):
                self.plugin.inspect(name)
        (self.root/"folder").mkdir()
        self.rejected("not_file", self.plugin.inspect, "folder")
        self.rejected("path_missing", self.edit, path="missing.txt")
        self.assertFalse(self.history.exists())

    def test_executor_checks_read_write_permissions_and_approval_before_any_history(self):
        executor = ToolExecutor(ToolRegistry(self.plugin.tools))
        call = ToolCall("edit", "files.edit", {"path": "note.txt", "old_text": "unique target",
            "new_text": "changed", "expected_sha256": sha(self.original)})
        for permissions, approved in ((frozenset(), True), (frozenset({"files.write"}), True),
                (frozenset({"files.read"}), True), (self.plugin.permissions, False)):
            result = executor.execute(call, permissions, approved=approved)
            self.assertIsNotNone(result.error_code)
            self.assertFalse(self.history.exists())
        result = executor.execute(call, self.plugin.permissions, approved=True)
        self.assertIsNone(result.error_code)
        self.assertTrue(result.value["ok"])
        inspection = executor.execute(ToolCall("inspect", "files.inspect", {"path": "note.txt"}), frozenset({"files.read"}))
        self.assertIsNone(inspection.error_code)

    def test_restore_has_own_approval_and_preserves_current_bytes_as_new_backup(self):
        edited = self.edit()
        changed = self.path.read_bytes()
        backup_file = self.history/(edited["backup_id"]+".json")
        original_record = backup_file.read_bytes()
        before_entries = self.backups()
        preview = self.plugin.preview_restore(edited["backup_id"], sha(changed))
        self.assertEqual(preview["after"].encode(), self.original)
        self.assertEqual(self.backups(), before_entries)
        executor = ToolExecutor(ToolRegistry(self.plugin.tools))
        call = ToolCall("restore", "files.restore", {"backup_id": edited["backup_id"], "expected_sha256": sha(changed)})
        self.assertIsNotNone(executor.execute(call, self.plugin.permissions).error_code)
        restored = executor.execute(call, self.plugin.permissions, approved=True)
        self.assertIsNone(restored.error_code)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(backup_file.read_bytes(), original_record)
        new_record = json.loads((self.history/(restored.value["backup_id"]+".json")).read_text())
        self.assertEqual(base64.b64decode(new_record["data_base64"]), changed)

    def test_history_survives_reopen_and_restore_can_be_explicitly_undone(self):
        edited = self.edit()
        changed = self.path.read_bytes()
        reopened = FileEditPlugin(FilePlugin(self.root), self.history)
        self.assertEqual(reopened.recovery_identity, self.plugin.recovery_identity)
        restored = reopened.restore(edited["backup_id"], sha(changed))
        reopened.restore(restored["backup_id"], sha(self.original))
        self.assertEqual(self.path.read_bytes(), changed)

    def test_stale_restore_never_overwrites_later_user_changes(self):
        edited = self.edit()
        self.path.write_bytes(b"external user change")
        self.rejected("stale_file", self.plugin.restore, edited["backup_id"], edited["after_sha256"])
        self.assertEqual(self.path.read_bytes(), b"external user change")
        self.assertEqual(len(self.backups()), 1)

    def test_history_limit_never_deletes_old_records(self):
        self.plugin = FileEditPlugin(self.files, self.history, max_backups=2)
        first = self.edit()
        second = self.plugin.restore(first["backup_id"], first["after_sha256"])
        saved = {p.name: p.read_bytes() for p in self.backups()}
        self.rejected("history_full", self.plugin.restore, second["backup_id"], second["after_sha256"])
        self.assertEqual({p.name: p.read_bytes() for p in self.backups()}, saved)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_backup_scope_and_invalid_identifier_are_rejected(self):
        result = self.edit()
        other = self.base/"other-workspace"
        other.mkdir(); (other/"note.txt").write_bytes(self.path.read_bytes())
        plugin = FileEditPlugin(FilePlugin(other), self.history)
        self.assertNotEqual(plugin.recovery_identity, self.plugin.recovery_identity)
        self.rejected("backup_invalid", plugin.restore, result["backup_id"], result["after_sha256"])
        for value in ("../outside", "f"*33, None):
            self.rejected("invalid_backup_id", self.plugin.restore, value, result["after_sha256"])
        self.rejected("backup_missing", self.plugin.restore, "0"*64, result["after_sha256"])

    def test_backup_identifier_binds_previewed_path_and_all_record_metadata(self):
        edited = self.edit()
        changed = self.path.read_bytes()
        other = self.root/"other.txt"
        other.write_bytes(changed)
        self.assertEqual(self.plugin.preview_restore(edited["backup_id"], sha(changed))["path"], "note.txt")
        target = self.history/(edited["backup_id"]+".json")
        original = json.loads(target.read_text(encoding="utf-8"))
        body = {key: value for key, value in original.items() if key != "backup_id"}
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(sha(canonical), edited["backup_id"])
        for field, value in (("path", "other.txt"), ("after_sha256", "0"*64), ("operation", "restore"),
                ("nonce", "0"*32)):
            with self.subTest(field=field):
                target.write_text(json.dumps(dict(original, **{field: value})), encoding="utf-8")
                self.rejected("backup_invalid", self.plugin.restore, edited["backup_id"], sha(changed))
                self.assertEqual(self.path.read_bytes(), changed)
                self.assertEqual(other.read_bytes(), changed)

    def test_corrupted_record_fields_and_bytes_fail_closed(self):
        result = self.edit()
        target = self.history/(result["backup_id"]+".json")
        original = json.loads(target.read_text(encoding="utf-8"))
        cases = [dict(original, data_base64=base64.b64encode(b"wrong bytes").decode()),
                 dict(original, path="../outside"), dict(original, format=True),
                 dict(original, extra="not allowed"), dict(original, after_sha256="bad")]
        for record in cases:
            with self.subTest(fields=list(record)):
                target.write_text(json.dumps(record), encoding="utf-8")
                self.rejected("backup_invalid", self.plugin.restore, result["backup_id"], result["after_sha256"])
        target.write_bytes(b"x"*(MAX_RECORD_BYTES+1))
        self.rejected("backup_invalid", self.plugin.restore, result["backup_id"], result["after_sha256"])
        self.assertEqual(self.path.read_bytes().decode(), "第一行\r\n更正内容\r\n末尾\r\n")

    def test_history_location_and_limits_are_validated_without_creating_directories(self):
        for path in (self.root, self.root/"history", self.base):
            with self.subTest(path=path), self.assertRaises(ValueError):
                FileEditPlugin(self.files, path)
        for cap in (0, True, MAX_BACKUPS+1):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                FileEditPlugin(self.files, self.history, max_backups=cap)
        self.assertFalse(self.history.exists())

    def test_replaced_workspace_is_rejected_without_touching_new_directory(self):
        self.root.rename(self.base/"preserved-workspace")
        self.root.mkdir(); self.path.write_bytes(b"replacement")
        self.rejected("workspace_changed", self.edit)
        self.assertEqual(self.path.read_bytes(), b"replacement")
        self.assertFalse(self.history.exists())

    def test_replaced_history_is_rejected_with_existing_instance(self):
        first = self.edit()
        self.history.rename(self.history.with_name("preserved-history"))
        self.history.mkdir()
        self.rejected("history_changed", self.plugin.restore, first["backup_id"], first["after_sha256"])
        self.assertEqual(list(self.history.iterdir()), [])

    def test_target_hardlink_and_backup_hardlink_are_rejected(self):
        alias = self.root/"alias.txt"
        try:
            os.link(self.path, alias)
        except OSError:
            self.skipTest("host cannot create a fixture hard link")
        self.rejected("links_not_allowed", self.edit)
        alias.unlink()
        result = self.edit()
        backup = self.history/(result["backup_id"]+".json")
        alias = self.base/"backup-alias.json"
        os.link(backup, alias)
        self.rejected("backup_invalid", self.plugin.restore, result["backup_id"], result["after_sha256"])

    def test_symlink_history_and_target_are_rejected(self):
        def link_directory(link, directory):
            try:
                link.symlink_to(directory, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    self.skipTest("host does not allow a fixture directory link")
                made = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(directory)],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                if made.returncode:
                    self.skipTest("host does not allow a fixture symlink or junction")
            def remove():
                if link.is_symlink():
                    link.unlink()
                else:
                    link.rmdir()
            self.addCleanup(remove)
        linked = self.base/"history-link"
        outside = self.base/"ordinary-history"
        outside.mkdir()
        link_directory(linked, outside)
        with self.assertRaises(ValueError):
            FileEditPlugin(self.files, linked)
        link = self.root/"linked-directory"
        link_directory(link, outside)
        self.rejected("links_not_allowed", self.edit, path="linked-directory/note.txt")
        self.assertEqual(list(outside.iterdir()), [])

    def test_unknown_history_entry_prevents_edit_without_removing_it(self):
        self.history.mkdir(parents=True)
        entry = self.history/"unrecognized.txt"
        entry.write_bytes(b"keep")
        self.rejected("history_invalid", self.edit)
        self.assertEqual(entry.read_bytes(), b"keep")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_backup_fsync_failure_keeps_original_and_any_partial_record(self):
        with patch("astra_harness.builtin.editing.os.fsync", side_effect=OSError("private sentinel")):
            details = self.rejected("backup_write_failed", self.edit)
        self.assertFalse(details["backup_confirmed"])
        self.assertEqual(self.backups()[0].stem, details["backup_id"])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(list(self.root.glob(".astra-edit-*")), [])

    def test_failure_before_replace_is_known_and_keeps_backup(self):
        with patch("astra_harness.builtin.editing.tempfile.mkstemp", side_effect=OSError("private sentinel")):
            self.rejected("write_failed", self.edit)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(len(self.backups()), 1)

    def test_source_change_after_backup_before_replace_is_rejected(self):
        save = self.plugin._save_backup
        def concurrent_change(*args):
            backup_id = save(*args)
            self.path.write_bytes(b"user changed while staging")
            return backup_id
        with patch.object(self.plugin, "_save_backup", side_effect=concurrent_change):
            details = self.rejected("stale_file", self.edit)
        self.assertEqual(self.path.read_bytes(), b"user changed while staging")
        self.assertEqual(len(self.backups()), 1)
        self.assertTrue(details["backup_confirmed"])
        self.assertEqual(self.backups()[0].stem, details["backup_id"])
        backup = json.loads(self.backups()[0].read_text(encoding="utf-8"))
        self.assertEqual(base64.b64decode(backup["data_base64"]), self.original)
        self.assertLessEqual(self.backups()[0].stat().st_size, MAX_RECORD_BYTES)
        self.assertEqual(list(self.root.glob(".astra-edit-*")), [])

    def test_replace_error_is_conservatively_unknown_with_backup_identity(self):
        with patch("astra_harness.builtin.editing.os.replace", side_effect=OSError("private sentinel")):
            with self.assertRaises(ToolExecutionUnknown) as caught:
                self.edit()
        self.assertEqual(caught.exception.details["reason"], "edit_result_unknown")
        self.assertEqual(caught.exception.details["before_sha256"], sha(self.original))
        self.assertNotIn("private sentinel", str(caught.exception.details))
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_replace_completed_but_result_lost_preserves_recovery_copy(self):
        replace = os.replace
        def lost_result(source, target):
            replace(source, target)
            raise OSError("simulated lost acknowledgement")
        with patch("astra_harness.builtin.editing.os.replace", side_effect=lost_result):
            with self.assertRaises(ToolExecutionUnknown) as caught:
                self.edit()
        details = caught.exception.details
        self.assertEqual(sha(self.path.read_bytes()), details["after_sha256"])
        self.plugin.restore(details["backup_id"], details["after_sha256"])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_post_replace_verification_failure_is_unknown(self):
        read = self.plugin._read
        calls = 0
        def fail_verify(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("verification failed")
            return read(path)
        with patch.object(self.plugin, "_read", side_effect=fail_verify):
            with self.assertRaises(ToolExecutionUnknown) as caught:
                self.edit()
        self.assertEqual(sha(self.path.read_bytes()), caught.exception.details["after_sha256"])

    def test_same_plugin_serializes_concurrent_conditional_edits(self):
        def perform():
            try:
                return self.edit()["ok"]
            except ToolUnavailable as error:
                return error.details["reason"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: perform(), range(2)))
        self.assertCountEqual(results, [True, "stale_file"])
        self.assertEqual(len(self.backups()), 1)

    def test_pending_approval_rechecks_sha_after_reopen_and_reports_failure(self):
        database = self.base/"runs.sqlite3"
        def app():
            result = Application(NoModel(), self.plugin.tools, database=database, permissions=self.plugin.permissions)
            self.addCleanup(result.close)
            return result
        first = app()
        run = first.wait(first.submit_tool("files.edit", {"path": "note.txt", "old_text": "unique target",
            "new_text": "approved change", "expected_sha256": sha(self.original)})["id"])
        self.assertEqual(run["status"], "waiting_approval")
        self.assertFalse(self.history.exists())
        first.close()
        self.path.write_bytes(b"user changed while waiting")
        reopened = app()
        reopened.approve(run["id"])
        result = reopened.wait(run["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tool_result"]["error_code"], "edit_rejected")
        self.assertEqual(self.path.read_bytes(), b"user changed while waiting")
        self.assertFalse(self.history.exists())


if __name__ == "__main__":
    unittest.main()
