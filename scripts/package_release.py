"""Build and verify this project's local wheel/sdist without downloading packages."""

import argparse
from email.parser import BytesParser
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
import zipfile


VERSION = "0.4.0"
GENERATED_SDIST = {"PKG-INFO", "setup.cfg", *(
    "astra_harness.egg-info/" + name for name in (
        "PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "top_level.txt", "requires.txt"))}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def exact_pins(requirements):
    """This release declares unconditional exact pins, not URLs/extras/ranges."""
    result = {}
    for requirement in requirements:
        match = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([A-Za-z0-9][A-Za-z0-9.!+_-]*)\s*", requirement)
        if not match:
            raise ValueError("runtime dependencies must be unconditional exact pins")
        name, value = canonical_name(match[1]), match[2]
        if name in result:
            raise ValueError("duplicate runtime dependency")
        result[name] = value
    return result


def read_toml(path):
    try:
        import tomllib
    except ImportError:  # Python 3.10 build tooling normally provides tomli.
        try:
            import tomli as tomllib
        except ImportError:
            raise RuntimeError("package build tooling requires Python 3.11+ or tomli") from None
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def project_dependencies(root):
    project = read_toml(Path(root) / "pyproject.toml")["project"]
    if project["name"] != "astra-harness" or project["version"] != VERSION or project.get("optional-dependencies"):
        raise ValueError("unexpected project metadata")
    return exact_pins(project.get("dependencies", []))


def lock_artifacts(path):
    """Only the explicitly locked official registry artifacts may be installed."""
    packages, wheels = {}, {}
    for package in read_toml(path)["package"]:
        name, value = canonical_name(package["name"]), package["version"]
        if name == "astra-harness":
            if value != VERSION or package["source"] != {"editable": "."}:
                raise ValueError("unexpected locked project")
            continue
        if package["source"] != {"registry": "https://pypi.org/simple"} or (name, value) in packages:
            raise ValueError("unexpected or duplicate lock source")
        hashes = set()
        for artifact in [*package.get("wheels", []), *([package["sdist"]] if "sdist" in package else [])]:
            url = urlsplit(artifact["url"])
            digest = artifact["hash"]
            if (url.scheme != "https" or url.netloc != "files.pythonhosted.org" or url.query or url.fragment
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
                raise ValueError("lock artifact is not an official hashed PyPI file")
            hashes.add(digest[7:])
            filename = unquote(PurePosixPath(url.path).name)
            if filename.endswith(".whl"):
                if not permitted_name(filename) or "/" in filename or filename in wheels:
                    raise ValueError("unexpected or duplicate locked wheel filename")
                wheels[filename] = {"name": name, "version": value, "sha256": digest[7:]}
        if not hashes:
            raise ValueError("locked dependency has no artifact hashes")
        packages[name, value] = hashes
    return packages, wheels


def audit_requirements(path, locked_packages):
    """Accept uv's hashed exact-pin export, never pip options or direct URLs."""
    statements, pending = [], ""
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending += " " + line.removesuffix("\\").rstrip()
        if not line.endswith("\\"):
            statements.append(pending.strip())
            pending = ""
    if pending:
        raise ValueError("unfinished requirements continuation")
    found = set()
    for statement in statements:
        hashes = re.findall(r"(?:^|\s)--hash=sha256:([0-9a-f]{64})(?=\s|$)", statement)
        requirement = re.sub(r"(?:^|\s)--hash=sha256:[0-9a-f]{64}(?=\s|$)", "", statement).strip()
        if not hashes or any(value in requirement for value in ("--", "@", "://", "\\", "#")):
            raise ValueError("requirements must contain only hashed exact pins and markers")
        pin, separator, marker = requirement.partition(";")
        if separator and not marker.strip():
            raise ValueError("empty dependency marker")
        name, value = next(iter(exact_pins([pin]).items()))
        key = name, value
        if key in found or key not in locked_packages or not set(hashes).issubset(locked_packages[key]):
            raise ValueError("requirements differ from uv.lock")
        found.add(key)
    if found != set(locked_packages):
        raise ValueError("requirements omit locked dependencies")
    return {"sha256": sha256(Path(path).read_bytes()), "locked_packages": len(found)}


def copy_wheelhouse(source, destination, locked_wheels):
    source, destination = Path(source).resolve(), Path(destination)
    if not source.is_dir():
        raise ValueError("local wheelhouse directory is required")
    verified = {}
    destination.mkdir()
    for path in sorted(source.glob("*.whl")):
        if path.is_symlink() or not path.is_file() or path.name not in locked_wheels:
            raise ValueError("wheelhouse contains an unlisted wheel")
        data, expected = path.read_bytes(), locked_wheels[path.name]
        if sha256(data) != expected["sha256"]:
            raise ValueError("wheelhouse hash differs from uv.lock")
        target = destination / path.name
        target.write_bytes(data)
        verified[path.name] = expected
    if not verified:
        raise ValueError("wheelhouse contains no locked wheels")
    return verified


def dependency_installation(report_path, wheelhouse, wheels, bootstrap, direct):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    expected = dict(bootstrap)
    if set(expected) - {"pip", "setuptools"}:
        raise ValueError("new virtual environment inherited third-party packages")
    dependencies = {}
    for item in report["install"]:
        name, value = canonical_name(item["metadata"]["name"]), item["metadata"]["version"]
        download = item["download_info"]
        url = urlsplit(download["url"])
        filename = unquote(PurePosixPath(url.path).name)
        digest = download["archive_info"].get("hashes", {}).get("sha256")
        legacy_hash = download["archive_info"].get("hash")
        if legacy_hash is not None:
            if not re.fullmatch(r"sha256=[0-9a-f]{64}", legacy_hash) or (digest and digest != legacy_hash[7:]):
                raise ValueError("pip report contains inconsistent artifact hashes")
            digest = legacy_hash[7:]
        allowed = wheels.get(filename)
        local = Path(url2pathname(url.path)).resolve()
        if (url.scheme != "file" or url.netloc not in {"", "localhost"}
                or local != (Path(wheelhouse) / filename).resolve()
                or not allowed or allowed != {"name": name, "version": value, "sha256": digest}
                or name in expected):
            raise ValueError("dependency installation differs from the audited wheelhouse")
        expected[name] = value
        dependencies[name] = value
    if any(dependencies.get(name) != value for name, value in direct.items()):
        raise ValueError("installation omitted or changed a direct dependency")
    return {"direct_dependencies": direct, "distributions": expected, "dependencies": dependencies}


def permitted_name(name):
    """Reject private/build artifacts even if an accidental manifest edit adds one."""
    path = PurePosixPath(name)
    forbidden = {".local", ".git", ".aws", ".codex", ".agents", ".venv", "__pycache__", "node_modules", "build", "dist"}
    return bool(name) and not path.is_absolute() and "\\" not in name and ":" not in name and all(
        part.casefold() not in forbidden and part not in {".", ".."}
        and (not part.casefold().startswith(".env") or part.casefold() == ".env.example") for part in path.parts
    ) and not any(name.casefold().endswith(suffix) for suffix in (".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log"))


def source_files(root):
    """Use only the explicit, shallow include patterns in the checked-in manifest."""
    root, result = Path(root), set()
    for line in (root / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        words = line.split()
        if not words or words[0] != "include":
            continue
        for pattern in words[1:]:
            if "**" in pattern or not permitted_name(pattern):
                raise ValueError("unsafe or recursive source include")
            matches = [path for path in root.glob(pattern) if path.is_file()]
            if not matches:
                raise ValueError("manifest include has no matching file: " + pattern)
            for path in matches:
                name = path.relative_to(root).as_posix()
                if path.is_symlink() or not permitted_name(name):
                    raise ValueError("unsafe source path")
                # A manifest is not permission to follow a parent directory link.
                if path.resolve() != root.resolve().joinpath(*PurePosixPath(name).parts):
                    raise ValueError("source path traverses a link")
                result.add(name)
    return sorted(result)


def audit_wheel(path, expected, dependencies=()):
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if len(names) != len(set(names)) or not all(permitted_name(name) for name in names):
            raise ValueError("wheel contains duplicate or forbidden paths")
        runtime = {name: archive.read(name) for name in names if name.startswith("astra_harness/")}
        if set(runtime) != set(expected) or any(sha256(data) != expected[name] for name, data in runtime.items()):
            raise ValueError("wheel runtime differs from the source snapshot")
        metadata_prefix = f"astra_harness-{VERSION}.dist-info/"
        if any(not name.startswith(("astra_harness/", metadata_prefix)) for name in names):
            raise ValueError("wheel contains unexpected non-runtime files")
        metadata = BytesParser().parsebytes(archive.read(metadata_prefix + "METADATA"))
        declared = dependencies if isinstance(dependencies, dict) else exact_pins(dependencies)
        if (metadata["Name"] != "astra-harness" or metadata["Version"] != VERSION
                or exact_pins(metadata.get_all("Requires-Dist", [])) != declared):
            raise ValueError("unexpected version or runtime dependency")
    return {"runtime_files": len(runtime), "archive_files": len(names), "sha256": sha256(Path(path).read_bytes()),
            "runtime_dependencies": declared}


def audit_sdist(path, source_hashes, extracted):
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names, roots = set(), set()
        for member in members:
            if not permitted_name(member.name) or not (member.isfile() or member.isdir()):
                raise ValueError("sdist contains an unsafe path or non-regular member")
            parts = PurePosixPath(member.name).parts
            roots.add(parts[0])
            if member.isdir():
                continue
            name = PurePosixPath(*parts[1:]).as_posix()
            if name in names or name not in set(source_hashes) | GENERATED_SDIST:
                raise ValueError("sdist contains an unexpected or duplicate file: " + name)
            names.add(name)
            content = archive.extractfile(member).read()
            if name in source_hashes and sha256(content) != source_hashes[name]:
                raise ValueError("sdist source differs: " + name)
        if len(roots) != 1 or not set(source_hashes).issubset(names):
            raise ValueError("sdist omits source/demo material or has multiple roots")
        # Copy only validated regular files. Never extract archive paths or links blindly.
        for member in members:
            if member.isfile():
                target = Path(extracted).joinpath(*PurePosixPath(member.name).parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    return {"source_files": len(source_hashes), "archive_files": len(names), "sha256": sha256(Path(path).read_bytes())}


def _environment():
    result = {key: value for key, value in os.environ.items()
              if not key.startswith(("ASTRA_", "PIP_")) and key not in {"PYTHONPATH", "PYTHONHOME"}}
    result.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull,
                  PYTHONUTF8="1")
    return result


def build_release(output_dir=".local/milestone5-package", additional_pythons=(), *, wheelhouse=None, requirements_lock=None):
    root = Path(__file__).resolve().parents[1]
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="package-", dir=output))
    report = {"schema_version": 1, "version": VERSION, "passed": False, "directory": run.name,
              "provider_requests": 0, "downloads": 0, "published": False, "phase": "source_snapshot",
              "build_environment": {},
              "commands": [], "installations": []}

    def execute(command, cwd, label, timeout=180):
        result = subprocess.run([str(item) for item in command], cwd=cwd, env=_environment(), capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout)
        (run / (label + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        report["commands"].append({"label": label, "exit_code": result.returncode, "log": label + ".log"})
        if result.returncode:
            raise RuntimeError("local package step failed: " + label)
        return result.stdout

    try:
        report["phase"] = "local_build_dependencies"
        report["build_environment"] = {name: version(name) for name in ("build", "setuptools", "wheel", "pip")}
        report["phase"] = "source_snapshot"
        source = run / "source"
        source.mkdir()
        source_hashes = {}
        for name in source_files(root):
            content = (root / name).read_bytes()
            source_hashes[name] = sha256(content)
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        if any(sha256((root / name).read_bytes()) != value for name, value in source_hashes.items()):
            raise RuntimeError("source changed during snapshot")
        report["source_files_sha256"] = source_hashes
        direct = project_dependencies(source)
        if not wheelhouse or not requirements_lock:
            raise ValueError("explicit --wheelhouse and --requirements-lock are required for offline dependency installation")
        requirement_path = Path(requirements_lock)
        requirement_path = (root / requirement_path).resolve() if not requirement_path.is_absolute() else requirement_path.resolve()
        if not requirement_path.is_relative_to(root):
            raise ValueError("requirements lock must be a public file in the source tree")
        requirement_name = requirement_path.relative_to(root).as_posix()
        if requirement_name not in source_hashes or "uv.lock" not in source_hashes:
            raise ValueError("MANIFEST.in must explicitly include uv.lock and the requirements lock")
        frozen_requirements = source / requirement_name
        locked_packages, locked_wheels = lock_artifacts(source / "uv.lock")
        if any((name, value) not in locked_packages for name, value in direct.items()):
            raise ValueError("project dependency pins differ from uv.lock")
        report["dependency_lock"] = {"path": requirement_name, **audit_requirements(frozen_requirements, locked_packages),
                                     "uv_lock_sha256": source_hashes["uv.lock"], "direct_dependencies": direct}
        local_wheels = run / "dependency-wheelhouse"
        report["dependency_wheels"] = copy_wheelhouse(wheelhouse, local_wheels, locked_wheels)
        # Calculate in a fresh process from the frozen snapshot, independent of
        # preloaded caller modules or subsequent edits in the original checkout.
        identity_code = (
            "import json; from astra_harness.benchmark import default_tool_factory; "
            "from astra_harness.tool_identity import build_tool_identity, presentation_fingerprint; "
            "tools = default_tool_factory(); "
            "print(json.dumps({'identity':build_tool_identity(tools)['sha256'], "
            "'presentation':presentation_fingerprint(tools)}))")
        snapshot_identity = json.loads(execute([sys.executable, "-X", "utf8", "-c", identity_code],
                                               source, "source-tool-identity"))
        report["source_tool_identity_sha256"] = snapshot_identity["identity"]
        report["source_tool_presentation_sha256"] = snapshot_identity["presentation"]
        runtime = {name: value for name, value in source_hashes.items() if name.startswith("astra_harness/")}
        report["phase"] = "build_wheel_and_sdist"
        artifacts = run / "artifacts"
        execute([sys.executable, "-X", "utf8", "-m", "build", "--no-isolation", "--outdir", artifacts], source, "build")
        wheels, sdists = list(artifacts.glob("*.whl")), list(artifacts.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise RuntimeError("expected one wheel and one genuine sdist")
        wheel, sdist = wheels[0], sdists[0]
        report["phase"] = "archive_audit"
        report["wheel"] = {"path": wheel.relative_to(run).as_posix(), **audit_wheel(wheel, runtime, direct)}
        extracted = run / "sdist-source"
        report["sdist"] = {"path": sdist.relative_to(run).as_posix(), **audit_sdist(sdist, source_hashes, extracted)}
        report["phase"] = "rebuild_from_sdist"
        rebuilt = run / "rebuilt"
        execute([sys.executable, "-X", "utf8", "-m", "build", "--no-isolation", "--wheel", "--outdir", rebuilt],
                extracted, "sdist-rebuild")
        rebuilt_wheel = next(rebuilt.glob("*.whl"))
        report["rebuilt_wheel"] = {"path": rebuilt_wheel.relative_to(run).as_posix(), **audit_wheel(rebuilt_wheel, runtime, direct)}
        report["runtime_bytes_identical"] = True
        # A valid archive can still omit a new source-only documented entry.
        # Execute that entry from the extracted sdist, with independent output.
        report["phase"] = "sdist_documented_range_demo"
        source_demo = json.loads(execute(
            [sys.executable, "-X", "utf8", "-m", "scripts.file_range_lab", "--output-dir", run / "source-lab"],
            extracted, "sdist-range-lab"))
        if not source_demo["passed"]:
            raise RuntimeError("sdist range demonstration failed")
        report["sdist_range_lab"] = {"passed": True, "report": "source-lab/" + source_demo["directory"] + "/report.json"}
        report["phase"] = "sdist_documented_identity_demo"
        identity_demo = json.loads(execute(
            [sys.executable, "-X", "utf8", "-m", "scripts.tool_identity_lab", "--output-dir", run / "identity-lab"],
            extracted, "sdist-tool-identity-lab"))
        if not identity_demo["passed"]:
            raise RuntimeError("sdist tool identity demonstration failed")
        report["sdist_tool_identity_lab"] = {"passed": True, "report": "identity-lab/" + identity_demo["directory"] + "/report.json"}

        for index, interpreter in enumerate((sys.executable, *additional_pythons)):
            report["phase"] = "offline_install_and_smoke_" + str(index)
            environment = run / f"venv-{index}"
            execute([interpreter, "-m", "venv", environment], run, f"venv-{index}")
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            bootstrap = json.loads(execute([python, "-I", "-c",
                "import json,importlib.metadata as m; print(json.dumps({d.metadata['Name'].lower():d.version for d in m.distributions()}))"],
                run, f"bootstrap-{index}"))
            dependency_report = run / f"dependencies-{index}.json"
            # The export already pins the complete closure. Do not let metadata
            # resolve extra requirements (including direct URLs) behind our back.
            execute([python, "-I", "-m", "pip", "--disable-pip-version-check", "install", "--no-index",
                     "--find-links", local_wheels, "--require-hashes", "--only-binary=:all:", "--no-cache-dir",
                     "--no-deps", "--report", dependency_report, "-r", frozen_requirements], run, f"dependencies-{index}")
            dependency_manifest = dependency_installation(dependency_report, local_wheels, report["dependency_wheels"], bootstrap, direct)
            dependency_manifest["project_wheel_sha256"] = report["wheel"]["sha256"]
            dependency_manifest_path = run / f"dependency-manifest-{index}.json"
            dependency_manifest_path.write_text(json.dumps(dependency_manifest, indent=2) + "\n", encoding="utf-8")
            execute([python, "-I", "-m", "pip", "--disable-pip-version-check", "install", "--no-index", "--no-deps",
                     "--no-cache-dir", wheel], run, f"install-{index}")
            execute([python, "-I", "-m", "pip", "check"], run, f"pip-check-{index}")
            work = run / f"installed-run-{index}"
            work.mkdir()
            result_path = run / f"installed-{index}.json"
            arguments = [python, "-I", "-X", "utf8", source / "scripts/package_smoke.py", "--wheel", wheel,
                         "--report", result_path, "--dependency-manifest", dependency_manifest_path]
            if index:
                arguments.append("--basic")
            execute(arguments, work, f"smoke-{index}")
            installed = json.loads(result_path.read_text(encoding="utf-8"))
            if (installed["checks"]["tool_identity_sha256"] != report["source_tool_identity_sha256"]
                    or installed["checks"]["tool_presentation_sha256"] != report["source_tool_presentation_sha256"]):
                raise RuntimeError("installed tool fingerprint differs across source or interpreter")
            report["installations"].append({"python": installed["python"], "basic": installed["basic"],
                                             "passed": installed["passed"], "report": result_path.name})
        report.update(passed=True, phase="complete")
    except Exception as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        (run / "package-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=".local/milestone5-package")
    parser.add_argument("--additional-python", action="append", default=[])
    parser.add_argument("--wheelhouse", required=True, help="Existing local dependency wheels; each must match uv.lock")
    parser.add_argument("--requirements-lock", required=True, help="Public hashed uv export included in MANIFEST.in")
    args = parser.parse_args()
    report = build_release(args.output_dir, args.additional_python, wheelhouse=args.wheelhouse, requirements_lock=args.requirements_lock)
    print(json.dumps({key: report[key] for key in ("directory", "passed", "wheel", "sdist", "installations")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
