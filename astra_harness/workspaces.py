"""Host-owned directory selection; no scanning, file moves or service discovery."""
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import stat

from .workspace_binding import WorkspaceBinding, WorkspaceError


def _home_directory():
    try:
        return Path.home()
    except RuntimeError:
        if os.name != "nt":
            raise WorkspaceError("workspace_unavailable") from None
    # Windows expanduser relies on environment variables, which a service or
    # isolated launcher can omit. Ask the OS for FOLDERID_Profile; do not skip
    # the home-directory exclusion or guess a profile from the current path.
    import ctypes
    from uuid import UUID
    folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(UUID("5e6c858f-0e22-4760-9afe-ea3317b67173").bytes_le)
    output = ctypes.c_wchar_p()
    query = ctypes.windll.shell32.SHGetKnownFolderPath
    query.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
    query.restype = ctypes.c_long
    release = ctypes.windll.ole32.CoTaskMemFree
    release.argtypes = (ctypes.c_void_p,)
    release.restype = None
    try:
        # KF_FLAG_DONT_VERIFY avoids checking or creating the profile directory.
        if query(ctypes.byref(folder_id), 0x4000, None, ctypes.byref(output)) != 0 or not output.value:
            raise WorkspaceError("workspace_unavailable")
        result = Path(output.value)
        if not result.is_absolute():
            raise WorkspaceError("workspace_unavailable")
        return result
    finally:
        release(ctypes.cast(output, ctypes.c_void_p))


def local_workspace_path(value, *, absolute=True):
    """Reject network/device syntax before any filesystem metadata is read."""
    try:
        raw = os.fspath(value)
    except TypeError:
        raise WorkspaceError("invalid_workspace") from None
    if (not isinstance(raw, str) or not raw or len(raw) > 1000
            or any(ord(char) < 32 for char in raw)):
        raise WorkspaceError("invalid_workspace")
    normalized = raw.replace("\\", "/")
    winpath = PureWindowsPath(raw)
    if (normalized.startswith("//") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw)
            or any(part == ".." for part in normalized.split("/"))
            or winpath.drive.startswith("\\\\") or (winpath.drive and not winpath.root)
            or (os.name == "nt" and normalized.startswith("/") and not winpath.drive)):
        raise WorkspaceError("invalid_workspace")
    if absolute and not Path(raw).is_absolute():
        raise WorkspaceError("invalid_workspace")
    candidate = Path(os.path.abspath(raw))
    if os.name == "nt":
        import ctypes
        # GetDriveType reads the selected volume mapping, not directory content.
        # A mapped SMB drive is still remote even without UNC spelling.
        if ctypes.windll.kernel32.GetDriveTypeW(str(candidate.anchor)) in {0, 1, 4}:
            raise WorkspaceError("invalid_workspace")
    return candidate


def workspace_id(path):
    return hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:32]


def _overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def _database_files(path):
    target = Path(os.path.abspath(path))
    return {Path(str(base) + suffix) for base in (target, target.resolve())
            for suffix in ("", ".lock", "-wal", "-shm", "-journal")}


def check_existing_ancestors(path):
    """Check an optional startup directory before mkdir or demo seeding."""
    candidate = local_workspace_path(path, absolute=False)
    for part in reversed((candidate, *candidate.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            return candidate
        except OSError:
            raise WorkspaceError("workspace_unavailable") from None
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISDIR(info.st_mode)):
            raise WorkspaceError("invalid_workspace")
    return candidate


class WorkspacePolicy:
    """Only explicit user-selected existing directories may become new scopes.

    The startup directory is a trusted compatibility exception inside the host
    project/data parent, but it must not contain executable host files or state.
    Local processes are trusted: metadata checks are not an OS sandbox.
    """
    def __init__(self, startup, *, database, config_paths=(), index_paths=(), protected_directories=(), enabled=True,
                 project_root=None):
        self.startup = local_workspace_path(startup, absolute=False)
        self.enabled = bool(enabled)
        self.project_root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        self.home = _home_directory().resolve()
        target = Path(os.path.abspath(database))
        self.data_roots = {target.parent, target.resolve().parent}
        self.protected_files = _database_files(database)
        # Host-only history must remain outside every task root, including the
        # normally permitted startup directory below the service data parent.
        self.protected_directories = {candidate for path in protected_directories
                                      for candidate in (Path(os.path.abspath(path)), Path(path).resolve())}
        for path in index_paths:
            self.protected_files.update(_database_files(path))
            self.data_roots.add(Path(os.path.abspath(path)).parent)
            self.data_roots.add(Path(path).resolve().parent)
        for path in config_paths:
            if path is not None:
                self.protected_files.update((Path(os.path.abspath(path)), Path(path).resolve()))
        # These are known host paths, not a recursive search for user projects.
        self.host_code = {self.project_root / name for name in
                          ("astra_harness", "tests", "scripts", "examples", "docs", ".git", ".venv", ".agents", ".codex")}
        self.check_location(self.startup)
        check_existing_ancestors(self.startup)
        # Expand an existing Windows short-name prefix only after checking the
        # original ancestor chain. Both spellings name the same startup exception.
        self._startup_canonical = self.startup.resolve()
        self.check_location(self._startup_canonical)

    def check_location(self, candidate):
        candidate = Path(candidate)
        if candidate == Path(candidate.anchor) or candidate == self.home:
            raise WorkspaceError("protected_workspace")
        if candidate == self.project_root or any(_overlaps(candidate, code) for code in self.host_code):
            raise WorkspaceError("protected_workspace")
        if any(_overlaps(candidate, directory) for directory in self.protected_directories):
            raise WorkspaceError("protected_workspace")
        # An explicitly configured startup subdirectory may sit below .local;
        # an arbitrary selector may never expose the surrounding service data.
        if candidate not in (self.startup, getattr(self, "_startup_canonical", self.startup)) and (_overlaps(candidate, self.project_root)
                                         or any(_overlaps(candidate, root) for root in self.data_roots)):
            raise WorkspaceError("protected_workspace")
        if any(path == candidate or path.is_relative_to(candidate) for path in self.protected_files):
            raise WorkspaceError("protected_workspace")

    def inspect(self, value, *, items=(), allow_startup=False):
        if not self.enabled and not allow_startup:
            raise WorkspaceError("workspace_disabled")
        candidate = local_workspace_path(value, absolute=not allow_startup)
        self.check_location(candidate)
        from .builtin.files import inspect_workspace_directory
        try:
            inspected = inspect_workspace_directory(candidate)
        except (OSError, ValueError, TypeError) as error:
            code = str(error)
            if code == "workspace_identity_unavailable":
                raise WorkspaceError(code) from None
            raise WorkspaceError("invalid_workspace") from None
        path = Path(inspected["path"])
        self.check_location(path)
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise WorkspaceError("workspace_unavailable")
        identifier = workspace_id(path)
        for item in items:
            old = Path(item["path"])
            if path != old and _overlaps(path, old):
                raise WorkspaceError("workspace_overlap")
            if path == old and item["identity"] != inspected["identity"]:
                raise WorkspaceError("workspace_changed")
        return {"id": identifier, "path": str(path), "identity": inspected["identity"]}

    def validate_spec(self, spec):
        current = self.inspect(spec["path"], allow_startup=True)
        if current != spec:
            raise WorkspaceError("workspace_changed")

    def describe(self, application):
        result = application.workspaces()
        return {**result, "enabled": self.enabled,
                **({"reason": "files_disabled"} if not self.enabled else {})}

    def select(self, application, *, path, expected_identity, expected_revision):
        if not self.enabled:
            raise WorkspaceError("workspace_disabled")
        spec = self.inspect(path, items=application.workspaces()["items"])
        if spec["identity"] != expected_identity:
            raise WorkspaceError("workspace_changed")
        application.select_workspace(spec, expected_revision)
        return self.describe(application)


def workspace_factory(policy, startup_spec, startup_tools, startup_permissions, startup_file_plugin,
                      *, file_extensions=None):
    """Keep fixed integrations on startup; assemble host-approved file extensions per root."""
    startup_tools, startup_permissions = tuple(startup_tools), tuple(startup_permissions)
    base_tools = tuple(tool for tool in startup_tools if tool.name == "add")

    def build(spec):
        policy.validate_spec(spec)
        if spec["id"] == startup_spec["id"]:
            if spec != startup_spec:
                raise WorkspaceError("workspace_changed")
            return WorkspaceBinding(**spec, tools=startup_tools, permissions=startup_permissions,
                                    file_plugin=startup_file_plugin,
                                    validate=lambda: policy.validate_spec(spec))
        if not policy.enabled:
            raise WorkspaceError("workspace_disabled")
        from .builtin.files import FilePlugin
        plugin = FilePlugin(spec["path"])
        extra = tuple(file_extensions(plugin, spec)) if file_extensions is not None else ()
        permissions = tuple(sorted({"files.read", "files.write"}
                                   | {permission for tool in extra for permission in tool.permissions}))
        return WorkspaceBinding(**spec, tools=base_tools + plugin.tools + extra,
                                permissions=permissions, file_plugin=plugin,
                                validate=lambda: policy.validate_spec(spec))
    return build
