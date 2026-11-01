import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from astra_harness.builtin.files import FilePlugin
from astra_harness.builtin.local_skills import (
    LocalSkillLoader, SkillLoadError, MAX_ENTRIES, MAX_SKILLS, MAX_FILE_BYTES,
)


class LocalSkillLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.files = FilePlugin(self.root)
        self.loader = LocalSkillLoader(self.files)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, slug="report-check", *, name="Report check", description="Check a local report.",
              body="\nCompare the report with its source.\n", raw=None):
        path = self.root / "skills" / slug / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is None:
            raw = f"---\nname: {name}\ndescription: {description}\n---\n{body}".encode("utf-8")
        path.write_bytes(raw)
        return path

    def error(self, code, operation):
        with self.assertRaises(SkillLoadError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def issue(self, raw, code):
        self.write(raw=raw)
        catalog = self.loader.catalog()
        self.assertEqual(catalog["items"], [])
        self.assertEqual(catalog["issues"], [{"id": "report-check", "error_code": code}])

    def test_constructor_and_missing_catalog_do_not_create_directory(self):
        with patch.object(self.files, "validate_workspace", side_effect=AssertionError("IO")):
            LocalSkillLoader(self.files)
        self.assertEqual(self.loader.catalog(), {
            "directory": "skills", "exists": False, "limit": MAX_SKILLS, "items": [], "issues": []})
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(TypeError):
            LocalSkillLoader(self.root)

    def test_catalog_excludes_body_load_returns_frozen_explicit_document(self):
        body = "\n仅核对用户选定材料。\n```python\nraise RuntimeError('never execute')\n```\n"
        path = self.write(name="报告核对", description="先验证来源，再说明差异。", body=body)
        catalog = self.loader.catalog()
        self.assertTrue(catalog["exists"])
        item, = catalog["items"]
        self.assertNotIn("body", item)
        self.assertNotIn("never execute", json.dumps(catalog))
        self.assertEqual(item["path"], "skills/report-check/SKILL.md")
        self.assertEqual(item["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(item["byte_count"], len(path.read_bytes()))
        document = self.loader.load(item["id"], item["identity"])
        self.assertEqual(document.body, body)
        self.assertEqual(document.public_metadata(), item)
        with self.assertRaises(AttributeError):
            document.body = "changed"

    def test_bom_crlf_and_field_order_are_supported_and_preserved(self):
        body = "\r\n保持换行。\r\n"
        raw = ("\ufeff---\r\ndescription: 中文描述\r\nname: 示例\r\n---\r\n" + body).encode("utf-8")
        self.write(raw=raw)
        item, = self.loader.catalog()["items"]
        self.assertEqual(self.loader.load(item["id"], item["identity"]).body, body)

    def test_raw_newline_changes_invalidate_old_identity(self):
        path = self.write()
        item, = self.loader.catalog()["items"]
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.error("skill_changed", lambda: self.loader.load(item["id"], item["identity"]))

    def test_identity_is_bound_to_workspace_and_relative_path(self):
        path = self.write()
        first, = self.loader.catalog()["items"]
        self.write("second", name="Different")
        second = self.loader.catalog()["items"][1]
        self.assertNotEqual(first["identity"], second["identity"])
        other = self.parent / "other"
        other.mkdir()
        destination = other / "skills/report-check/SKILL.md"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(path.read_bytes())
        other_item, = LocalSkillLoader(FilePlugin(other)).catalog()["items"]
        self.assertEqual(first["sha256"], other_item["sha256"])
        self.assertNotEqual(first["identity"], other_item["identity"])

    def test_changed_missing_and_malformed_documents_never_use_cached_body(self):
        path = self.write()
        item, = self.loader.catalog()["items"]
        self.write(body="New instructions")
        self.error("skill_changed", lambda: self.loader.load(item["id"], item["identity"]))
        path.write_bytes(b"not metadata")
        self.error("skill_metadata_invalid", lambda: self.loader.load(item["id"], item["identity"]))
        path.unlink()
        self.error("path_missing", lambda: self.loader.load(item["id"], item["identity"]))

    def test_ids_and_expected_digests_are_validated_before_any_read(self):
        with patch.object(self.files, "_directory", side_effect=AssertionError("unexpected IO")):
            for slug in ("../escape", ".hidden", "UPPER", "", "a/b", "x" * 49, "a\n", None):
                with self.subTest(slug=slug):
                    self.error("invalid_skill_id", lambda: self.loader.load(slug, "a" * 64))
            for digest in (None, "a", "A" * 64, "a" * 64 + "\n"):
                with self.subTest(digest=digest):
                    self.error("invalid_skill_identity", lambda: self.loader.load("valid", digest))

    def test_duplicate_casefold_names_invalidate_all_not_first_wins(self):
        self.write("first", name="Straße")
        self.write("second", name="STRASSE")
        self.write("third", name="Unique")
        catalog = self.loader.catalog()
        self.assertEqual([item["id"] for item in catalog["items"]], ["third"])
        self.assertEqual(catalog["issues"], [
            {"id": "first", "error_code": "skill_duplicate"},
            {"id": "second", "error_code": "skill_duplicate"}])
        self.error("skill_duplicate", lambda: self.loader.load("first", "a" * 64))

    def test_duplicate_added_after_enable_is_rejected_on_load(self):
        self.write()
        item, = self.loader.catalog()["items"]
        self.write("duplicate")
        self.error("skill_duplicate", lambda: self.loader.load(item["id"], item["identity"]))

    def test_malformed_metadata_isolated_without_raw_content_or_path(self):
        cases = [
            b"name: private-text\ndescription: d\nbody", b"---\nname: n\n---\nbody",
            b"---\nname: n\nname: x\ndescription: d\n---\nbody",
            b"---\nname: n\ndescription: d\ntools: all\n---\nbody",
            b"---\n name: n\ndescription: d\n---\nbody",
            b"---\nname: n\ndescription: |\n  multiline\n---\nbody",
            b"---\nname: [one, two]\ndescription: d\n---\nbody",
            b'---\nname: "quoted"\ndescription: d\n---\nbody',
            b"---\nname: n\ndescription: \tcontrol\n---\nbody",
            b"---\nname: n\ndescription: d\n", b"---\nname: \ndescription: d\n---\nbody",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.issue(raw, "skill_metadata_invalid")

    def test_metadata_unicode_limits_and_empty_body(self):
        self.write(name="名" * 64, description="述" * 240)
        self.assertEqual(len(self.loader.catalog()["items"]), 1)
        self.write(name="名" * 65)
        self.assertEqual(self.loader.catalog()["issues"][0]["error_code"], "skill_metadata_invalid")
        self.write(description="述" * 241)
        self.assertEqual(self.loader.catalog()["issues"][0]["error_code"], "skill_metadata_invalid")
        self.write(body=" \r\n\t")
        self.assertEqual(self.loader.catalog()["issues"][0]["error_code"], "skill_body_empty")

    def test_header_byte_limit_not_only_character_limit(self):
        self.issue(b"---\nname:" + b" " * 2048 + b"n\ndescription: d\n---\nbody", "skill_header_limit")

    def test_control_bytes_rejected_before_catalog_enable_or_task_preparation(self):
        for control in range(32):
            if control in (9, 10, 13):
                continue
            with self.subTest(control=control):
                self.issue(b"---\nname: n\ndescription: d\n---\nbody" + bytes([control]), "skill_binary")
                self.error("skill_binary", lambda: self.loader.load("report-check", "a" * 64))
        self.write(body="Text\twith\r\nallowed whitespace")
        self.assertEqual(len(self.loader.catalog()["items"]), 1)

    def test_utf8_binary_and_file_byte_limits(self):
        self.issue(b"---\nname: n\ndescription: d\n---\n\xff", "skill_invalid_utf8")
        self.issue(b"---\nname: n\ndescription: d\n---\n\0", "skill_binary")
        self.issue(b"x" * (MAX_FILE_BYTES + 1), "skill_file_limit")
        header = b"---\nname: n\ndescription: d\n---\n"
        self.write(raw=header + b"x" * (MAX_FILE_BYTES - len(header)))
        self.assertEqual(self.loader.catalog()["items"][0]["byte_count"], MAX_FILE_BYTES)

    def test_invalid_entry_names_ignored_but_all_count_toward_bound(self):
        directory = self.root / "skills"
        directory.mkdir()
        for index in range(MAX_ENTRIES):
            (directory / f"IGNORED-{index}").touch()
        self.assertEqual(self.loader.catalog()["items"], [])
        (directory / "IGNORED-extra").touch()
        self.error("skills_limit", self.loader.catalog)

    def test_candidate_limit_fails_closed_without_truncation(self):
        for index in range(MAX_SKILLS):
            self.write(f"skill-{index}", name=f"Name {index}")
        self.assertEqual(len(self.loader.catalog()["items"]), MAX_SKILLS)
        self.write("skill-extra", name="Name extra")
        self.error("skills_limit", self.loader.catalog)

    def test_candidate_non_directory_and_skill_non_file_are_issues(self):
        directory = self.root / "skills"
        directory.mkdir()
        (directory / "ordinary").write_text("Not a skill directory", encoding="utf-8")
        (directory / "nested" / "SKILL.md").mkdir(parents=True)
        self.assertEqual(self.loader.catalog()["items"], [])
        self.assertEqual([item["id"] for item in self.loader.catalog()["issues"]], ["nested", "ordinary"])

    def test_no_recursion_no_reference_loading_no_code_execution(self):
        body = "Read [outside](../../outside.txt).\n!include secret.txt\n```python\nraise Exception()\n```"
        self.write(body=body)
        nested = self.root / "skills" / "report-check" / "nested"
        nested.mkdir()
        (nested / "SKILL.md").write_bytes(b"ignored malformed file")
        with patch("subprocess.Popen", side_effect=AssertionError("execution forbidden")):
            item, = self.loader.catalog()["items"]
            self.assertEqual(self.loader.load(item["id"], item["identity"]).body, body)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_hardlinked_skill_is_rejected(self):
        path = self.write()
        os.link(path, self.parent / "second-link.md")
        self.assertEqual(self.loader.catalog()["issues"], [
            {"id": "report-check", "error_code": "links_not_allowed"}])

    def test_private_slug_does_not_bypass_file_policy(self):
        self.write("secrets", name="Hidden instructions")
        self.assertEqual(self.loader.catalog()["issues"], [
            {"id": "secrets", "error_code": "forbidden_path"}])

    def test_linked_skill_directory_is_rejected_without_reading_target(self):
        self.write()
        target = self.root / "skills" / "report-check"
        link = self.root / "skills" / "linked"
        junction = False
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("symlinks unavailable")
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest("symlinks and junctions unavailable")
            junction = True
        try:
            catalog = self.loader.catalog()
            self.assertEqual([item["id"] for item in catalog["items"]], ["report-check"])
            self.assertEqual(catalog["issues"], [{"id": "linked", "error_code": "links_not_allowed"}])
        finally:
            os.rmdir(link) if junction else link.unlink()

    def test_replaced_workspace_is_rejected(self):
        self.write()
        self.root.rename(self.parent / "old-workspace")
        self.root.mkdir()
        self.error("workspace_changed", self.loader.catalog)

    def test_linked_top_level_skills_directory_rejected_before_content_read(self):
        target = self.parent / "outside-skills"
        target.mkdir()
        link = self.root / "skills"
        junction = False
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("symlinks unavailable")
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest("symlinks and junctions unavailable")
            junction = True
        try:
            with patch.object(Path, "open", side_effect=AssertionError("must not read linked content")):
                self.error("links_not_allowed", self.loader.catalog)
        finally:
            os.rmdir(link) if junction else link.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows hidden-file attributes")
    def test_hidden_skill_file_is_rejected_by_existing_policy(self):
        import ctypes
        path = self.write()
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        api.SetFileAttributesW.restype = ctypes.c_int
        original = path.lstat().st_file_attributes
        self.assertTrue(api.SetFileAttributesW(str(path), original | 0x2))
        try:
            self.assertEqual(self.loader.catalog()["issues"], [
                {"id": "report-check", "error_code": "forbidden_path"}])
        finally:
            self.assertTrue(api.SetFileAttributesW(str(path), original))

    def test_same_bytes_file_replaced_during_read_is_rejected(self):
        target = self.write()
        original = self.files._path
        calls = 0
        def change_on_recheck(relative, **kwargs):
            nonlocal calls
            if relative.endswith("SKILL.md"):
                calls += 1
                if calls == 2:
                    replacement = target.with_name("replacement.md")
                    replacement.write_bytes(target.read_bytes())
                    os.replace(replacement, target)
            return original(relative, **kwargs)
        with patch.object(self.files, "_path", side_effect=change_on_recheck):
            self.assertEqual(self.loader.catalog()["issues"], [
                {"id": "report-check", "error_code": "skill_changed"}])

    def test_catalog_directory_changes_during_scan_fail_closed(self):
        self.write()
        original = self.loader._document
        def mutate(slug):
            document = original(slug)
            self.write("new-entry", name="Added")
            return document
        with patch.object(self.loader, "_document", side_effect=mutate):
            self.error("skill_changed", self.loader.catalog)

    def test_unreadable_file_error_is_sanitized_and_other_skill_survives(self):
        self.write()
        self.write("good", name="Good")
        original = Path.open
        def inaccessible(path, *args, **kwargs):
            if "report-check" in path.parts:
                raise PermissionError("private OS path and credentials must not appear")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", inaccessible):
            catalog = self.loader.catalog()
        self.assertEqual([item["id"] for item in catalog["items"]], ["good"])
        self.assertEqual(catalog["issues"], [{"id": "report-check", "error_code": "skill_unavailable"}])
        self.assertNotIn("private OS", json.dumps(catalog))


if __name__ == "__main__":
    unittest.main()
