"""Conditional UTF-8 edits with bounded, local recovery copies.

The host owns the history location. These checks detect ordinary changes in a
trusted local environment; path checks are not an operating-system sandbox.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid

from .files import FilePlugin, MAX_FILE_BYTES, _relative_file_parts, _workspace_candidate
from ..tools import Tool, ToolExecutionUnknown, ToolUnavailable


MAX_BACKUPS = 100
MAX_RECORD_BYTES = MAX_FILE_BYTES * 4 // 3 + 4096
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BACKUP = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_ERRORS = frozenset({"invalid_edit", "invalid_sha256", "stale_file", "match_not_found",
    "ambiguous_match", "no_change", "file_limit", "invalid_utf8", "binary_file",
    "file_unreadable", "invalid_path", "absolute_path_not_allowed", "links_not_allowed",
    "path_missing", "path_outside_workspace", "parent_missing", "not_file", "forbidden_path",
    "workspace_changed", "file_changed", "history_unavailable", "history_changed",
    "history_invalid", "history_full", "backup_missing", "invalid_backup_id", "backup_invalid",
    "backup_write_failed", "write_failed"})


class _Rejected(ValueError):
    pass


def _reject(code, **details):
    raise ToolUnavailable("file_edit_rejected", code="edit_rejected",
                          details={"reason": code, "applied": False, **details})


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _record_id(record):
    # Bind the approved identifier to path, scope and all recovery bytes. A
    # random nonce keeps repeated identical edits as independent records.
    body = {key: value for key, value in record.items() if key != "backup_id"}
    return _sha(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8"))


def _decode(data):
    if len(data) > MAX_FILE_BYTES:
        raise _Rejected("file_limit")
    if b"\0" in data:
        raise _Rejected("binary_file")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise _Rejected("invalid_utf8") from None


def _identity(info):
    return (info.st_dev, info.st_ino)


def _ordinary(path, *, directory):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise _Rejected("history_invalid")
    if (directory and not stat.S_ISDIR(info.st_mode)) or (not directory and
            (not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1)):
        raise _Rejected("history_invalid")
    return info


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


class FileEditPlugin:
    """An optional adapter; constructing it never creates history directories."""
    name = "file-editing"

    def __init__(self, file_plugin: FilePlugin, history_dir, *, max_backups=MAX_BACKUPS):
        if not isinstance(file_plugin, FilePlugin):
            raise TypeError("an existing FilePlugin is required")
        if type(max_backups) is not int or not 1 <= max_backups <= MAX_BACKUPS:
            raise ValueError("invalid history limit")
        self.files = file_plugin
        self.history_dir = _workspace_candidate(history_dir)
        if (self.history_dir.is_relative_to(self.files.root)
                or self.files.root.is_relative_to(self.history_dir)):
            raise ValueError("history must be outside the workspace")
        self.max_backups = max_backups
        self._anchors = {}
        self._lock = threading.RLock()
        self.files.validate_workspace()
        self._history(create=False)
        self.permissions = frozenset({"files.read", "files.write"})
        self.recovery_identity = _sha(json.dumps({"format": 1,
            "workspace": self.files.workspace_identity, "history": os.path.normcase(str(self.history_dir)),
            "max_backups": self.max_backups, "max_file_bytes": MAX_FILE_BYTES},
            sort_keys=True, separators=(",", ":")).encode("utf-8"))
        self.tools = self._tools()

    def _tools(self):
        path = {"type": "string", "minLength": 1, "maxLength": 240}
        digest = {"type": "string", "minLength": 64, "maxLength": 64}
        text = {"type": "string", "maxLength": MAX_FILE_BYTES}
        definitions = (
            ("files.inspect", "检查普通 UTF-8 文件的原始字节 SHA256 和大小，不返回正文；编辑前先检查 ok。",
             self.inspect, {"path": path}, False),
            ("files.edit", "按 SHA256 条件精确替换唯一旧文本；审批后保存本地备份再替换，必须检查返回 ok。",
             self.edit, {"path": path, "old_text": {**text, "minLength": 1}, "new_text": text,
                         "expected_sha256": digest}, True),
            ("files.restore", "审批后按当前 SHA256 恢复本工作区指定备份；先备份当前文件，必须检查返回 ok。",
             self.restore, {"backup_id": {"type": "string", "minLength": 64, "maxLength": 64},
                            "expected_sha256": digest}, True),
        )
        dependencies = (self._read, self._history, self._entries, self._load_backup, self._save_backup,
            self._replace, self._apply, self._prepare_edit, self._prepare_restore, self._attempt,
            _decode, _sha, _record_id, _identity, _ordinary, _unique_object, _relative_file_parts, _reject,
            FilePlugin.validate_workspace, FilePlugin._path, FilePlugin._read_bytes)
        return tuple(Tool(name, description, (), handler,
            input_schema={"type": "object", "properties": properties, "required": list(properties),
                          "additionalProperties": False},
            permissions=self.permissions if writing else frozenset({"files.read"}),
            side_effect=writing, requires_approval=writing, implementation_version="1",
            implementation_dependencies=dependencies,
            implementation_constants={"MAX_FILE_BYTES": MAX_FILE_BYTES, "MAX_RECORD_BYTES": MAX_RECORD_BYTES,
                                      "max_backups": self.max_backups, "record_format": 1},
            recovery_identity=self.recovery_identity)
            for name, description, handler, properties, writing in definitions)

    def activate(self, registry):
        for tool in self.tools:
            registry.register(tool)
        return None

    def _history(self, *, create):
        self.files.validate_workspace()
        missing = False
        try:
            for component in reversed((self.history_dir, *self.history_dir.parents)):
                try:
                    info = _ordinary(component, directory=True)
                except FileNotFoundError:
                    if component in self._anchors:
                        raise _Rejected("history_changed") from None
                    missing = True
                    if not create:
                        continue
                    component.mkdir()
                    info = _ordinary(component, directory=True)
                prior = self._anchors.get(component)
                if prior is not None and prior != _identity(info):
                    raise _Rejected("history_changed")
                self._anchors[component] = _identity(info)
            return not missing or create
        except OSError:
            raise _Rejected("history_unavailable") from None

    def _entries(self):
        if not self._history(create=False):
            return []
        entries = []
        try:
            with os.scandir(self.history_dir) as iterator:
                for item in iterator:
                    if len(entries) >= self.max_backups:
                        raise _Rejected("history_full")
                    if item.name.endswith(".json") and _BACKUP.fullmatch(item.name[:-5]):
                        info = _ordinary(Path(item.path), directory=False)
                        if info.st_size > MAX_RECORD_BYTES:
                            raise _Rejected("history_invalid")
                        entries.append(item.name)
                    else:
                        raise _Rejected("history_invalid")
        except OSError:
            raise _Rejected("history_unavailable") from None
        return entries

    def _read(self, path):
        target = self.files._path(path)
        before = target.lstat()
        data = self.files._read_bytes(path)
        after = self.files._path(path).lstat()
        if (_identity(before), before.st_size, before.st_mtime_ns) != (
                _identity(after), after.st_size, after.st_mtime_ns):
            raise _Rejected("file_changed")
        return data, _decode(data)

    def _attempt(self, operation):
        with self._lock:
            try:
                return operation()
            except ToolExecutionUnknown:
                raise
            except UnicodeError:
                return _reject("invalid_utf8")
            except ValueError as error:
                return _reject(str(error) if str(error) in _ERRORS else "invalid_edit")
            except OSError:
                return _reject("file_unreadable")

    @staticmethod
    def _expected(value):
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise _Rejected("invalid_sha256")

    def inspect(self, path):
        def action():
            data, _ = self._read(path)
            return {"ok": True, "path": "/".join(_relative_file_parts(path)), "bytes": len(data),
                    "sha256": _sha(data), "encoding": "utf-8"}
        return self._attempt(action)

    def edit(self, path, old_text, new_text, expected_sha256):
        def action():
            before, after = self._prepare_edit(path, old_text, new_text, expected_sha256)
            return self._apply(path, before, after, operation="edit")
        return self._attempt(action)

    def _prepare_edit(self, path, old_text, new_text, expected_sha256):
        self._expected(expected_sha256)
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            raise _Rejected("invalid_edit")
        if len(old_text.encode("utf-8")) > MAX_FILE_BYTES or len(new_text.encode("utf-8")) > MAX_FILE_BYTES:
            raise _Rejected("file_limit")
        if "\0" in old_text or "\0" in new_text:
            raise _Rejected("binary_file")
        before, content = self._read(path)
        if _sha(before) != expected_sha256:
            raise _Rejected("stale_file")
        first = content.find(old_text)
        if first < 0:
            raise _Rejected("match_not_found")
        # Overlapping matches also make the requested location ambiguous.
        if content.find(old_text, first+1) >= 0:
            raise _Rejected("ambiguous_match")
        after = (content[:first]+new_text+content[first+len(old_text):]).encode("utf-8")
        _decode(after)
        if before == after:
            raise _Rejected("no_change")
        return before, after

    def restore(self, backup_id, expected_sha256):
        def action():
            path, before, replacement = self._prepare_restore(backup_id, expected_sha256)
            return self._apply(path, before, replacement, operation="restore")
        return self._attempt(action)

    def _prepare_restore(self, backup_id, expected_sha256):
        self._expected(expected_sha256)
        record, replacement = self._load_backup(backup_id)
        before, _ = self._read(record["path"])
        if _sha(before) != expected_sha256:
            raise _Rejected("stale_file")
        if before == replacement:
            raise _Rejected("no_change")
        return record["path"], before, replacement

    @staticmethod
    def _preview(path, before, after):
        return {"path": "/".join(_relative_file_parts(path)), "exists": True,
                "before": before.decode("utf-8"), "after": after.decode("utf-8"),
                "changed": before != after, "before_sha256": _sha(before), "after_sha256": _sha(after)}

    def preview_edit(self, path, old_text, new_text, expected_sha256):
        return self._attempt(lambda: self._preview(path, *self._prepare_edit(path, old_text, new_text, expected_sha256)))

    def preview_restore(self, backup_id, expected_sha256):
        def action():
            path, before, after = self._prepare_restore(backup_id, expected_sha256)
            return {**self._preview(path, before, after), "backup_id": backup_id}
        return self._attempt(action)

    def _load_backup(self, backup_id):
        if not isinstance(backup_id, str) or not _BACKUP.fullmatch(backup_id):
            raise _Rejected("invalid_backup_id")
        if not self._history(create=False):
            raise _Rejected("backup_missing")
        target = self.history_dir/(backup_id+".json")
        try:
            info = _ordinary(target, directory=False)
            if info.st_size > MAX_RECORD_BYTES:
                raise _Rejected("backup_invalid")
            with target.open("rb") as stream:
                raw = stream.read(MAX_RECORD_BYTES+1)
            if len(raw) > MAX_RECORD_BYTES:
                raise _Rejected("backup_invalid")
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            fields = {"format", "backup_id", "nonce", "workspace_identity", "path", "operation", "before_sha256", "after_sha256", "data_base64"}
            if (not isinstance(record, dict) or set(record) != fields or type(record["format"]) is not int
                    or record["format"] != 1 or any(type(record[key]) is not str for key in fields-{"format"})):
                raise _Rejected("backup_invalid")
            if (record["backup_id"] != backup_id
                    or record["workspace_identity"] != self.files.workspace_identity
                    or record["operation"] not in {"edit", "restore"}
                    or not _NONCE.fullmatch(record["nonce"])):
                raise _Rejected("backup_invalid")
            if record["path"] != "/".join(_relative_file_parts(record["path"])):
                raise _Rejected("backup_invalid")
            self._expected(record["before_sha256"]); self._expected(record["after_sha256"])
            if _record_id(record) != backup_id:
                raise _Rejected("backup_invalid")
            data = base64.b64decode(record["data_base64"], validate=True)
            _decode(data)
            if _sha(data) != record["before_sha256"]:
                raise _Rejected("backup_invalid")
            self._history(create=False)
            if _identity(_ordinary(target, directory=False)) != _identity(info):
                raise _Rejected("backup_invalid")
            return record, data
        except FileNotFoundError:
            raise _Rejected("backup_missing") from None
        except (ValueError, TypeError, UnicodeError, KeyError):
            raise _Rejected("backup_invalid") from None
        except OSError:
            raise _Rejected("history_unavailable") from None

    def _save_backup(self, path, before, after, operation):
        if len(self._entries()) >= self.max_backups:
            raise _Rejected("history_full")
        self._history(create=True)
        record = {"format": 1, "nonce": uuid.uuid4().hex, "workspace_identity": self.files.workspace_identity,
            "path": "/".join(_relative_file_parts(path)), "operation": operation,
            "before_sha256": _sha(before), "after_sha256": _sha(after),
            "data_base64": base64.b64encode(before).decode("ascii")}
        backup_id = _record_id(record)
        record["backup_id"] = backup_id
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_RECORD_BYTES:
            raise _Rejected("backup_write_failed")
        target = self.history_dir/(backup_id+".json")
        created = False
        try:
            # Exclusive creation never overwrites an old backup. A crash can
            # leave an incomplete record; it remains local, occupies capacity,
            # and cannot be restored unless its entire payload verifies.
            with target.open("xb") as stream:
                created = True
                stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
            saved, data = self._load_backup(backup_id)
            if saved != record or data != before:
                raise _Rejected("backup_write_failed")
        except (OSError, ValueError):
            if created:
                _reject("backup_write_failed", backup_id=backup_id, backup_confirmed=False)
            raise _Rejected("backup_write_failed") from None
        return backup_id

    def _apply(self, path, before, after, *, operation):
        backup_id = self._save_backup(path, before, after, operation)
        try:
            self._replace(path, before, after)
        except ToolExecutionUnknown:
            raise ToolExecutionUnknown("edit_result_unknown", details={"reason": "edit_result_unknown",
                "backup_id": backup_id, "path": "/".join(_relative_file_parts(path)),
                "before_sha256": _sha(before), "after_sha256": _sha(after)}) from None
        except (ValueError, OSError) as error:
            # A known target rejection can still leave the verified recovery
            # copy created above. Report it rather than claiming zero effects.
            code = str(error) if str(error) in _ERRORS else "write_failed"
            _reject(code, backup_id=backup_id, backup_confirmed=True)
        return {"ok": True, "applied": True, "path": "/".join(_relative_file_parts(path)),
                "backup_id": backup_id, "before_sha256": _sha(before), "after_sha256": _sha(after),
                "bytes": len(after), "operation": operation}

    def _replace(self, path, before, after):
        target = self.files._path(path)
        temporary = None
        dispatched = False
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".astra-edit-", dir=target.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(after); stream.flush(); os.fsync(stream.fileno())
            # Recheck immediately before replacement, after backup and staging.
            # There is still a filesystem race window: this is not an OS CAS.
            current, _ = self._read(path)
            if current != before:
                raise _Rejected("stale_file")
            self._history(create=False)
            self.files._path(path)
            dispatched = True
            os.replace(temporary, target)
            verified, _ = self._read(path)
            if verified != after:
                raise ToolExecutionUnknown("edit_result_unknown")
        except BaseException as error:
            if dispatched:
                raise ToolExecutionUnknown("edit_result_unknown") from None
            if isinstance(error, OSError):
                raise _Rejected("write_failed") from None
            raise
        finally:
            if temporary is not None:
                try:
                    self.files._path(path)
                    Path(temporary).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
