"""受根目录约束的文件工具。针对可信本机环境，不抵御恶意进程的并发换链。"""

import codecs
from dataclasses import replace
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path, PureWindowsPath

from ..tools import Tool

MAX_FILE_BYTES = 128_000
MAX_ENTRIES = 200
MAX_SEARCH_FILES = 100
MAX_SEARCH_BYTES = 1_000_000
MAX_SEARCH_MATCHES = 50
MAX_RANGE_CHARS = 2_000
MAX_RANGE_LINES = 100
RANGE_CHUNK_BYTES = 4_096
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_PRIVATE_NAMES = frozenset({"secrets", "secret", "credentials", "credential", "password", "passwords",
                          "tokens", "token", "api_key", "api_keys", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
_RANGE_HINTS = {
    "invalid_range": "行号和列号须为 1 至 128000 的整数，行数须为 1 至 100 的整数。",
    "range_out_of_bounds": "请选用文件内的行号和列号；空行只能从第 1 列读取。",
    "file_limit": "请选择不超过 128000 字节的文件；分段读取不能绕过整文件上限。",
    "invalid_utf8": "请选择完整且有效的 UTF-8 文本文件。",
    "binary_file": "请选择不含 NUL 字符的 UTF-8 文本文件。",
    "file_unreadable": "文件当前无法读取；请检查其存在性及本地读取权限。",
    "invalid_path": "请使用工作区内的普通相对路径，不使用点段或特殊名称。",
    "absolute_path_not_allowed": "请使用工作区内的相对路径。",
    "links_not_allowed": "请使用工作区内的普通文件，不通过链接访问。",
    "path_missing": "路径不存在；请先列出工作区文件并选择已有路径。",
    "path_outside_workspace": "请选择工作区内的文件。",
    "parent_missing": "父目录不存在；请选择已有目录中的文件。",
    "not_file": "请选择普通文件，而不是目录。",
    "forbidden_path": "隐藏文件和常见凭据路径不作为工作区材料；请显式准备可处理的普通文件。",
    "workspace_changed": "工作区目录已变化或不可访问；请停止旧任务并重新核对工作区，不自动重试。",
}


def _range_error(code, **details):
    return {"ok": False, "error_code": code, "hint": _RANGE_HINTS[code], **details}


def _reserved(part: str) -> bool:
    checker = getattr(os.path, "isreserved", None)
    return checker(part) if checker is not None else PureWindowsPath(part).is_reserved()


def _reject_link(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("links_not_allowed")


def _private_name(name: str) -> bool:
    # This is a conservative filename rule, not secret-content classification.
    return name.startswith(".") or name.casefold() in _PRIVATE_NAMES or Path(name).stem.casefold() in _PRIVATE_NAMES


def _reject_private_entry(path: Path, info=None) -> None:
    if _private_name(path.name):
        raise ValueError("forbidden_path")
    info = path.lstat() if info is None else info
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("links_not_allowed")
    if getattr(info, "st_file_attributes", 0) & 0x2:
        raise ValueError("forbidden_path")
    if stat.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) > 1:
        raise ValueError("links_not_allowed")


def _workspace_candidate(path) -> Path:
    """Reject network/device spelling before any filesystem access."""
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\0" in raw:
            raise ValueError
        raw.encode("utf-8")
        normalized = raw.replace("\\", "/")
        windows = PureWindowsPath(raw)
        if (normalized.startswith("//") or ".." in normalized.split("/")
                or windows.drive.startswith("\\\\") or windows.drive and not windows.root):
            raise ValueError
        return Path(os.path.abspath(raw))
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("workspace_invalid_path") from None


def inspect_workspace_directory(path) -> dict:
    """Inspect an existing local directory without reading or enumerating files.

    Check the lexical ancestor chain before resolve can hide a junction. The
    physical identity is separate from the globally comparable tool contract.
    It detects ordinary directory replacement, not hostile concurrent swapping.
    """
    candidate = _workspace_candidate(path)
    try:
        # Walk from the volume/root downward: never inspect a child through an
        # ancestor already known to be a junction or symbolic link.
        for component in reversed((candidate, *candidate.parents)):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("links_not_allowed")
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("workspace_not_directory")
        _reject_private_entry(candidate, info)
        if type(info.st_ino) is not int or info.st_ino <= 0 or type(info.st_dev) is not int:
            raise ValueError("workspace_identity_unavailable")
        root = candidate.resolve(strict=True)
        binding = {"path": os.path.normcase(str(root)), "device": info.st_dev, "inode": info.st_ino}
        identity = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {"path": str(root), "identity": identity}
    except FileNotFoundError:
        raise ValueError("workspace_missing") from None
    except OSError:
        raise ValueError("workspace_unavailable") from None


def _relative_file_parts(relative: str) -> list[str]:
    if not isinstance(relative, str) or not relative or len(relative) > 240:
        raise ValueError("invalid_path")
    try:
        relative.encode("utf-8")
    except UnicodeError:
        raise ValueError("invalid_path") from None
    normalized = relative.replace("\\", "/")
    parts = normalized.split("/")
    windows = PureWindowsPath(relative)
    if windows.drive or windows.root or normalized.startswith("/"):
        raise ValueError("absolute_path_not_allowed")
    if any(part in ("", ".", "..") or any(ord(c) < 32 or c in ':<>|?*"' for c in part)
           or part.endswith((" ", ".")) or _reserved(part) for part in parts):
        raise ValueError("invalid_path")
    if any(_private_name(part) for part in parts):
        raise ValueError("forbidden_path")
    return parts


class FilePlugin:
    def __init__(self, root: str | Path) -> None:
        self._lexical_root = _workspace_candidate(root)
        binding = inspect_workspace_directory(self._lexical_root)
        self.root = Path(binding["path"])
        self._workspace_identity = binding["identity"]
        self.tools = tuple(replace(tool, handler=tool.handler.__get__(self, type(self)))
                           for tool in self.tool_definitions())

    @property
    def workspace_identity(self):
        return self._workspace_identity

    def validate_workspace(self) -> None:
        try:
            observed = inspect_workspace_directory(self._lexical_root)
            if (observed["identity"] != self._workspace_identity
                    or os.path.normcase(observed["path"]) != os.path.normcase(str(self.root))):
                raise ValueError("workspace_changed")
        except (OSError, ValueError, TypeError, RuntimeError):
            raise ValueError("workspace_changed") from None

    @classmethod
    def tool_definitions(cls) -> tuple[Tool, ...]:
        """Describe unbound tools without opening or even resolving a workspace.

        These definitions are for inspection, not execution. Instances bind the
        same handlers after validating the trusted host's workspace as before.
        """
        path_schema = {"type": "string", "maxLength": 240}
        guards = (cls.validate_workspace, inspect_workspace_directory, _workspace_candidate,
                  _private_name, _reject_private_entry, _relative_file_parts, _reserved, _reject_link)
        path_dependencies = (cls._path, *guards)
        boundary_constants = {"private_names": sorted(_PRIVATE_NAMES), "workspace_identity": "canonical-path-device-inode-v1"}
        return (
            Tool("files.list", "列出工作区根目录内的普通文件", (), cls.list_files,
                 input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                 permissions=frozenset({"files.read"}), implementation_version="2",
                 implementation_dependencies=guards, implementation_constants={"MAX_ENTRIES": MAX_ENTRIES, **boundary_constants}),
            Tool("files.read", "读取工作区内 UTF-8 文本文件", (), cls.read_file,
                 input_schema={"type": "object", "properties": {"path": path_schema},
                               "required": ["path"], "additionalProperties": False},
                 permissions=frozenset({"files.read"}), implementation_version="2",
                 implementation_dependencies=(cls._read_bytes, *path_dependencies),
                 implementation_constants={"MAX_FILE_BYTES": MAX_FILE_BYTES, **boundary_constants}),
            Tool("files.search", "按关键词搜索工作区文本，返回文件路径与行号；不是语义检索", (), cls.search,
                 input_schema={"type": "object", "properties": {
                     "query": {"type": "string", "minLength": 1, "maxLength": 200}, "path": path_schema},
                     "required": ["query"], "additionalProperties": False},
                 permissions=frozenset({"files.read"}), implementation_version="2",
                 implementation_dependencies=(cls._directory, cls._read_bytes, *path_dependencies),
                 implementation_constants={"MAX_ENTRIES": MAX_ENTRIES, "MAX_SEARCH_FILES": MAX_SEARCH_FILES,
                     "MAX_SEARCH_BYTES": MAX_SEARCH_BYTES, "MAX_SEARCH_MATCHES": MAX_SEARCH_MATCHES,
                     "MAX_FILE_BYTES": MAX_FILE_BYTES, **boundary_constants}),
            Tool("files.read_range", "读取 UTF-8 文本片段；先检查返回 ok，fragments 保留行列，按 next_cursor 续读；每次全文件校验，最多 2000 文本字符", (), cls.read_range,
                 input_schema={"type": "object", "properties": {
                     "path": path_schema,
                     "start_line": {"type": "integer", "minimum": 1, "maximum": MAX_FILE_BYTES, "default": 1,
                                    "description": "起始行号，从 1 开始。"},
                     "start_column": {"type": "integer", "minimum": 1, "maximum": MAX_FILE_BYTES, "default": 1,
                                      "description": "仅起始行的 Unicode 码点列号（不是字节），从 1 开始。"},
                     "line_count": {"type": "integer", "minimum": 1, "maximum": MAX_RANGE_LINES, "default": 40,
                                    "description": "最多返回的逻辑行数；空行也计数。"}},
                     "required": ["path"], "additionalProperties": False},
                 permissions=frozenset({"files.read"}), implementation_version="2",
                 implementation_dependencies=(cls._scan_range, _range_error, *path_dependencies),
                 implementation_constants={"_RANGE_HINTS": _RANGE_HINTS, "_LINE_BREAKS": sorted(_LINE_BREAKS),
                     "RANGE_CHUNK_BYTES": RANGE_CHUNK_BYTES, "MAX_RANGE_CHARS": MAX_RANGE_CHARS,
                     "MAX_RANGE_LINES": MAX_RANGE_LINES, "MAX_FILE_BYTES": MAX_FILE_BYTES, **boundary_constants}),
            Tool("files.write", "经审批后原子保存工作区 UTF-8 文本文件", (), cls.write_file,
                 input_schema={"type": "object", "properties": {
                     "path": path_schema, "content": {"type": "string", "maxLength": MAX_FILE_BYTES}},
                     "required": ["path", "content"], "additionalProperties": False},
                 permissions=frozenset({"files.write"}), side_effect=True, requires_approval=True,
                 implementation_version="2", implementation_dependencies=path_dependencies,
                 implementation_constants={"MAX_FILE_BYTES": MAX_FILE_BYTES, **boundary_constants}),
        )

    def activate(self, registry):
        for tool in self.tools:
            registry.register(tool)
        return None

    def _path(self, relative: str, *, writing: bool = False) -> Path:
        self.validate_workspace()
        parts = _relative_file_parts(relative)
        target = self.root
        for index, part in enumerate(parts):
            target = target / part
            try:
                info = target.lstat()
            except FileNotFoundError:
                if not (writing and index == len(parts) - 1):
                    raise ValueError("path_missing") from None
            else:
                _reject_private_entry(target, info)
        resolved = target.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("path_outside_workspace")
        if not target.parent.is_dir():
            raise ValueError("parent_missing")
        return target

    def list_files(self) -> list[str]:
        self.validate_workspace()
        result = []
        # 限制扫描的总条目数，避免目录里大量被跳过条目造成无界遍历。
        with os.scandir(self.root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ENTRIES:
                    raise ValueError("directory_limit")
                path = Path(entry.path)
                try:
                    _reject_private_entry(path)
                except (OSError, ValueError):
                    continue
                if entry.is_file(follow_symlinks=False):
                    result.append(entry.name)
        return sorted(result)

    def read_file(self, path: str) -> str:
        return self._read_bytes(path).decode("utf-8")

    def read_range(self, path: str, start_line: int = 1, start_column: int = 1, line_count: int = 40) -> dict:
        # Only this new tool returns actionable, fixed error codes. Existing read
        # behavior and the executor's schema/permission errors remain unchanged.
        if (any(type(value) is not int or not 1 <= value <= MAX_FILE_BYTES for value in (start_line, start_column))
                or type(line_count) is not int or not 1 <= line_count <= MAX_RANGE_LINES):
            return _range_error("invalid_range")
        try:
            target = self._path(path)
            if not target.is_file():
                return _range_error("not_file")
            return self._scan_range(target, start_line, start_column, line_count)
        except UnicodeError:
            return _range_error("invalid_utf8")
        except ValueError as error:
            if str(error) not in _RANGE_HINTS:
                raise
            return _range_error(str(error))
        except OSError:
            return _range_error("file_unreadable")

    def _scan_range(self, target, start_line, start_column, line_count):
        fragments = []
        next_cursor = None
        used = total_lines = column = 0
        line = 1
        line_length = None
        skip_lf = False

        def finish_line():
            nonlocal total_lines, line_length, next_cursor
            total_lines = line
            if line == start_line:
                line_length = column
            # A real empty line must survive paging, but a trailing separator
            # does not create another line (the same rule as str.splitlines).
            if column == 0 and line >= start_line and next_cursor is None:
                if len(fragments) < line_count:
                    fragments.append({"line": line, "start_column": 1, "text": [], "partial": False})
                else:
                    next_cursor = {"start_line": line, "start_column": 1}

        def chunks(stream):
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            consumed = 0
            while True:
                data = stream.read(min(RANGE_CHUNK_BYTES, MAX_FILE_BYTES + 1 - consumed))
                consumed += len(data)
                if consumed > MAX_FILE_BYTES:
                    raise ValueError("file_limit")
                text = decoder.decode(data, final=not data)
                if "\x00" in text:
                    raise ValueError("binary_file")
                yield text
                if not data:
                    break

        # Always validate through EOF, even after the response is full. Keeping
        # only a decoder chunk and bounded fragments prevents a long-line buffer
        # or pagination from bypassing the whole-file byte/UTF-8/NUL checks.
        with target.open("rb") as stream:
            for text in chunks(stream):
                for char in text:
                    if skip_lf:
                        skip_lf = False
                        if char == "\n":
                            continue
                    if char in _LINE_BREAKS:
                        finish_line()
                        line += 1
                        column = 0
                        skip_lf = char == "\r"
                        continue
                    column += 1
                    if line < start_line or (line == start_line and column < start_column) or next_cursor is not None:
                        continue
                    same_line = bool(fragments and fragments[-1]["line"] == line)
                    if used == MAX_RANGE_CHARS or (not same_line and len(fragments) == line_count):
                        next_cursor = {"start_line": line, "start_column": column}
                        if same_line:
                            fragments[-1]["partial"] = True
                        continue
                    if not same_line:
                        fragments.append({"line": line, "start_column": column, "text": [], "partial": False})
                    fragments[-1]["text"].append(char)
                    used += 1
        if column:
            finish_line()
        if not (total_lines == 0 and start_line == start_column == 1) and (
                line_length is None or start_column > max(1, line_length)):
            return _range_error("range_out_of_bounds", total_lines=total_lines, line_length=line_length)
        for fragment in fragments:
            fragment["text"] = "".join(fragment["text"])
        return {"ok": True, "path": target.relative_to(self.root).as_posix(), "fragments": fragments,
                "total_lines": total_lines, "truncated": next_cursor is not None, "next_cursor": next_cursor}

    def _read_bytes(self, path: str, limit=MAX_FILE_BYTES) -> bytes:
        target = self._path(path)
        if not target.is_file():
            raise ValueError("not_file")
        with target.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file_limit")
        return data

    def _directory(self, path: str = "") -> Path:
        self.validate_workspace()
        target = self.root if path == "" else self._path(path)
        if not target.is_dir():
            raise ValueError("not_directory")
        return target

    def browse(self, path: str = "") -> dict:
        directory = self._directory(path)
        result = []
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ENTRIES:
                    raise ValueError("directory_limit")
                target = Path(entry.path)
                try:
                    _reject_private_entry(target)
                except (OSError, ValueError):
                    continue
                if not (entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)):
                    continue
                result.append({"name": entry.name, "path": target.relative_to(self.root).as_posix(),
                               "type": "directory" if entry.is_dir(follow_symlinks=False) else "file",
                               "size": 0 if entry.is_dir(follow_symlinks=False) else entry.stat(follow_symlinks=False).st_size})
        result.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
        relative = directory.relative_to(self.root).as_posix()
        return {"path": "" if relative == "." else relative, "entries": result}

    def preview_write(self, path: str, content: str) -> dict:
        target = self._path(path, writing=True)
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("file_limit")
        exists = target.exists()
        before = self.read_file(path) if exists else ""
        return {"path": path, "before": before, "after": content, "exists": exists,
                "changed": not exists or before != content}

    def search(self, query: str, path: str = "") -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("invalid_query")
        query.encode("utf-8")
        directories = [self._directory(path)]
        matches = []
        scanned = visited = read_bytes = 0
        truncated = False
        needle = query.casefold()
        # 同时限制目录条目、文件数、字节和结果，避免只限制返回值却无界扫描。
        while directories and not truncated:
            directory = directories.pop()
            self.validate_workspace()
            try:
                _reject_private_entry(directory)
            except (OSError, ValueError):
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES or scanned >= MAX_SEARCH_FILES:
                        truncated = True
                        break
                    target = Path(entry.path)
                    try:
                        _reject_private_entry(target)
                    except (OSError, ValueError):
                        # Excluded entries are not returned or traversed, even
                        # when the query would match a private filename/content.
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(target)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    scanned += 1
                    size = entry.stat(follow_symlinks=False).st_size
                    if size > MAX_FILE_BYTES:
                        continue
                    if read_bytes + size > MAX_SEARCH_BYTES:
                        truncated = True
                        break
                    relative = target.relative_to(self.root).as_posix()
                    remaining = min(MAX_FILE_BYTES, MAX_SEARCH_BYTES - read_bytes)
                    try:
                        data = self._read_bytes(relative, remaining)
                    except ValueError:
                        # 并发增长的文件同样有读取上界；保守停止本轮扫描。
                        truncated = True
                        break
                    read_bytes += len(data)
                    try:
                        content = data.decode("utf-8")
                    except UnicodeError:
                        continue
                    for number, line in enumerate(content.splitlines(), 1):
                        if needle in line.casefold():
                            matches.append({"path": relative, "line": number, "text": line[:500]})
                            if len(matches) >= MAX_SEARCH_MATCHES:
                                truncated = True
                                break
                    if truncated:
                        break
        return {"matches": matches, "scanned_files": scanned, "truncated": truncated}

    def write_file(self, path: str, content: str) -> dict:
        target = self._path(path, writing=True)
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file_limit")
        if target.exists() and not target.is_file():
            raise ValueError("not_file")
        descriptor, temporary = tempfile.mkstemp(prefix=".astra-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._path(path, writing=True)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"path": path, "bytes": len(data)}


def file_tools(root: str | Path) -> tuple[Tool, ...]:
    return FilePlugin(root).tools
