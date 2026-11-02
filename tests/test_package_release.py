"""Negative archive checks complement the separately executed real build/install."""

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from scripts.package_release import (audit_requirements, audit_sdist, audit_wheel, copy_wheelhouse,
                                     dependency_installation, exact_pins, permitted_name, sha256, source_files)
from scripts.package_smoke import verify_distributions


class PackageReleaseTests(unittest.TestCase):
    def test_source_package_includes_documented_lab_entries(self):
        # The CLI is intentionally source-only: omitting it breaks the sdist tutorial.
        root = Path(__file__).resolve().parents[1]
        included = source_files(root)
        for name in ("scripts/file_range_lab.py", "scripts/tool_identity_lab.py", "uv.lock", "requirements.lock"):
            with self.subTest(entry=name):
                self.assertIn(name, included)

    def test_private_path_rejection_is_case_insensitive(self):
        for name in (".ENV", "docs/.ENV.local", ".LOCAL/file.md", ".VENV/pyvenv.cfg", "A.SQLITE3", "cache/__PYCACHE__/entry.pyc"):
            with self.subTest(name=name):
                self.assertFalse(permitted_name(name))
        self.assertTrue(permitted_name(".env.example"))

    def test_source_manifest_never_implicitly_collects_private_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("public", encoding="utf-8")
            (root / ".env").write_text("private sentinel", encoding="utf-8")
            (root / "MANIFEST.in").write_text("include README.md\n", encoding="utf-8")
            self.assertEqual(source_files(root), ["README.md"])
            (root / "MANIFEST.in").write_text("include README.md .env\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                source_files(root)

    def test_wheel_rejects_changed_runtime_private_files_and_runtime_dependencies(self):
        original = b"known source"
        metadata = b"Metadata-Version: 2.1\nName: astra-harness\nVersion: 0.5.0\n"
        for variant in ("valid", "changed", "private", "dependency"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                wheel = Path(temporary) / "example.whl"
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr("astra_harness/__init__.py", b"changed" if variant == "changed" else original)
                    archive.writestr("astra_harness-0.5.0.dist-info/METADATA",
                                     metadata + (b"Requires-Dist: unwanted\n" if variant == "dependency" else b""))
                    if variant == "private":
                        archive.writestr("astra_harness/.env", "private sentinel")
                expected = {"astra_harness/__init__.py": sha256(original)}
                if variant == "valid":
                    self.assertEqual(audit_wheel(wheel, expected)["runtime_files"], 1)
                else:
                    with self.assertRaises(ValueError):
                        audit_wheel(wheel, expected)

    def test_sdist_validates_every_member_before_extracting(self):
        for variant in ("valid", "traversal", "link", "private", "missing", "changed"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path, extracted = root / "source.tar.gz", root / "extracted"
                with tarfile.open(archive_path, "w:gz") as archive:
                    if variant != "missing":
                        content = b"changed" if variant == "changed" else b"public"
                        member = tarfile.TarInfo("project/README.md")
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
                    if variant in {"traversal", "link", "private"}:
                        name = {"traversal": "project/../escape", "link": "project/link", "private": "project/.env"}[variant]
                        member = tarfile.TarInfo(name)
                        if variant == "link":
                            member.type, member.linkname = tarfile.SYMTYPE, "../escape"
                        archive.addfile(member)
                if variant == "valid":
                    audit_sdist(archive_path, {"README.md": sha256(b"public")}, extracted)
                    self.assertEqual((extracted / "README.md").read_bytes(), b"public")
                else:
                    with self.assertRaises(ValueError):
                        audit_sdist(archive_path, {"README.md": sha256(b"public")}, extracted)
                    self.assertFalse(extracted.exists())


    def test_declared_dependencies_match_wheel_metadata_exactly(self):
        dependencies = {"fastapi": "0.162.0", "httpx": "0.28.1"}
        variants = {
            "valid": ["FastAPI==0.162.0", "httpx == 0.28.1"],
            "missing": ["fastapi==0.162.0"],
            "extra": ["fastapi==0.162.0", "httpx==0.28.1", "unwanted==1.0"],
            "changed": ["fastapi==0.161.0", "httpx==0.28.1"],
            "duplicate": ["fastapi==0.162.0", "FastAPI==0.162.0", "httpx==0.28.1"],
            "marker": ["fastapi==0.162.0; python_version>'3.10'", "httpx==0.28.1"],
            "url": ["fastapi @ https://example.invalid/pkg.whl", "httpx==0.28.1"],
        }
        for variant, requirements in variants.items():
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                wheel = Path(temporary) / "test.whl"
                metadata = "Metadata-Version: 2.1\nName: astra-harness\nVersion: 0.5.0\n"
                metadata += "".join("Requires-Dist: " + value + "\n" for value in requirements)
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr("astra_harness/__init__.py", b"known source")
                    archive.writestr("astra_harness-0.5.0.dist-info/METADATA", metadata)
                if variant == "valid":
                    self.assertEqual(audit_wheel(wheel, {"astra_harness/__init__.py": sha256(b"known source")}, dependencies)["runtime_dependencies"], dependencies)
                else:
                    with self.assertRaises(ValueError):
                        audit_wheel(wheel, {"astra_harness/__init__.py": sha256(b"known source")}, dependencies)

    def test_normalized_dependency_names_cannot_hide_duplicate_pins(self):
        with self.assertRaises(ValueError):
            exact_pins(["typing_extensions==4.16.0", "Typing-Extensions==4.16.0"])
        for value in ["httpx>=0.28.1", "httpx==0.28.*", "httpx[socks]==0.28.1"]:
            with self.subTest(requirement=value), self.assertRaises(ValueError):
                exact_pins([value])

    def test_hashed_requirements_reject_network_options_and_changed_lock(self):
        digest = "a" * 64
        locked = {("httpx", "0.28.1"): {digest}}
        valid = 'httpx==0.28.1 ; python_version >= "3.10" \\\n    --hash=sha256:' + digest + '\n'
        variants = {"valid": valid, "network": "--extra-index-url https://example.invalid\n" + valid,
                    "nested": "-r other.txt\n" + valid, "url": "httpx @ https://example.invalid/a.whl --hash=sha256:" + digest,
                    "missing_hash": "httpx==0.28.1", "changed_hash": valid.replace(digest, "b" * 64),
                    "wrong_version": valid.replace("0.28.1", "0.28.0"), "missing": "# no dependencies\n",
                    "duplicate": valid + valid}
        for variant, content in variants.items():
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "requirements.lock"
                path.write_text(content, encoding="utf-8")
                if variant == "valid":
                    self.assertEqual(audit_requirements(path, locked)["locked_packages"], 1)
                else:
                    with self.assertRaises(ValueError):
                        audit_requirements(path, locked)

    def test_wheelhouse_requires_locked_filename_and_content(self):
        content = b"locked wheel fixture"
        name = "httpx-0.28.1-py3-none-any.whl"
        locked = {name: {"name": "httpx", "version": "0.28.1", "sha256": sha256(content)}}
        for variant in ("valid", "changed", "unlisted", "empty"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                source, destination = Path(temporary) / "input", Path(temporary) / "frozen"
                source.mkdir()
                if variant != "empty":
                    (source / ("unlisted.whl" if variant == "unlisted" else name)).write_bytes(b"changed" if variant == "changed" else content)
                if variant == "valid":
                    self.assertEqual(copy_wheelhouse(source, destination, locked), locked)
                    self.assertEqual((destination / name).read_bytes(), content)
                else:
                    with self.assertRaises(ValueError):
                        copy_wheelhouse(source, destination, locked)

    def test_pip_report_must_reference_the_audited_local_wheel(self):
        digest, filename = "a" * 64, "httpx-0.28.1-py3-none-any.whl"
        wheels = {filename: {"name": "httpx", "version": "0.28.1", "sha256": digest}}
        for variant in ("valid", "legacy_hash", "remote", "outside", "hash", "version", "inherited", "conflicting_hashes"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                wheelhouse = root / "wheelhouse"
                url = (wheelhouse / filename).as_uri()
                if variant == "remote":
                    url = "https://example.invalid/" + filename
                if variant == "outside":
                    url = (root / "elsewhere" / filename).as_uri()
                report = {"install": [{"metadata": {"name": "httpx", "version": "0.28.0" if variant == "version" else "0.28.1"},
                                      "download_info": {"url": url, "archive_info": {"hashes": {"sha256": "b" * 64 if variant == "hash" else digest}}}}]}
                if variant == "legacy_hash":
                    report["install"][0]["download_info"]["archive_info"] = {"hash": "sha256=" + digest}
                if variant == "conflicting_hashes":
                    report["install"][0]["download_info"]["archive_info"]["hash"] = "sha256=" + "b" * 64
                path = root / "pip-report.json"
                path.write_text(json.dumps(report), encoding="utf-8")
                bootstrap = {"pip": "25.0", **({"unrelated": "1.0"} if variant == "inherited" else {})}
                if variant in {"valid", "legacy_hash"}:
                    result = dependency_installation(path, wheelhouse, wheels, bootstrap, {"httpx": "0.28.1"})
                    self.assertEqual(result["distributions"], {"pip": "25.0", "httpx": "0.28.1"})
                else:
                    with self.assertRaises(ValueError):
                        dependency_installation(path, wheelhouse, wheels, bootstrap, {"httpx": "0.28.1"})

    def test_installed_dependency_audit_rejects_global_extra_or_changed_packages(self):
        class Distribution:
            def __init__(self, name, version, root):
                self.metadata, self.version, self.root = {"Name": name}, version, root

            def locate_file(self, name):
                return self.root / name

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "venv"
            valid = Distribution("httpx", "0.28.1", prefix / "Lib/site-packages")
            self.assertEqual(verify_distributions([valid], {"httpx": "0.28.1"}, prefix), {"httpx": "0.28.1"})
            for items in ([], [valid, valid], [Distribution("httpx", "0.28.0", valid.root)],
                          [Distribution("httpx", "0.28.1", root / "global")],
                          [valid, Distribution("unrelated", "1.0", valid.root)]):
                with self.subTest(items=len(items)), self.assertRaises(RuntimeError):
                    verify_distributions(items, {"httpx": "0.28.1"}, prefix)


if __name__ == "__main__":
    unittest.main()
