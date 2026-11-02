"""Local user-managed collections. Agent calls still use registered tools.

Only the host chooses index locations. Creating a collection publishes a new
tool composition at the existing idle boundary; uploads never import a file.
"""
import base64
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import threading

from .builtin.files import MAX_FILE_BYTES, inspect_workspace_directory
from .builtin.knowledge import MAX_DOCUMENTS, MAX_TOTAL_BYTES, _relative_key, _reject_hidden
from .builtin.knowledge_collections import KnowledgeCollectionManager, MAX_COLLECTIONS, _parse_knowledge_config
from .config_file import JSONConfigurationFile, ConfigurationConflict
from .tools import ToolRegistry
from .workspace_binding import WorkspaceError


EMPTY_REVISION = hashlib.sha256(b"").hexdigest()
_UPLOAD_LOCK = threading.RLock()


class KnowledgeLibraryError(RuntimeError):
    STATUS = {"knowledge_invalid_file": 400, "knowledge_invalid_collection": 400,
              "knowledge_file_exists": 409, "knowledge_limit": 409,
              "knowledge_conflict": 409, "knowledge_unavailable": 409}

    def __init__(self, code):
        self.code = code if code in self.STATUS else "knowledge_unavailable"
        self.status = self.STATUS[self.code]
        super().__init__(self.code)


def library_directory(database):
    return Path(str(Path(database).absolute()) + ".knowledge")


def _new_file(path, raw):
    """Publish complete bytes without replacing an existing destination.

    A same-directory hard link is an atomic no-clobber publication on the
    supported local filesystems. Remove the temporary name before any reader
    receives success, since normal material reads reject multiple hard links.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".astra-upload-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def upload_material(plugin, filename, content_base64):
    """Copy a user-selected UTF-8 material into a collection, never overwrite."""
    try:
        if (not isinstance(filename, str) or len(filename) > 120
                or any(char in filename for char in '/\\<>:"|?*')
                or any(ord(char) < 32 for char in filename)):
            raise ValueError()
        key = _relative_key(filename)
        if not isinstance(content_base64, str) or len(content_base64) > 170_668:
            raise ValueError()
        raw = base64.b64decode(content_base64, validate=True)
        if not 0 < len(raw) <= MAX_FILE_BYTES or "\0" in raw.decode("utf-8"):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise KnowledgeLibraryError("knowledge_invalid_file") from None
    with _UPLOAD_LOCK:
        try:
            plugin.files.validate_workspace()
            _reject_hidden(plugin.root)
            target = plugin.root / key
            if target.exists() or target.is_symlink():
                # The exact same retry is harmless; a changed file is never replaced.
                if plugin._source(key)[1] != raw:
                    raise KnowledgeLibraryError("knowledge_file_exists")
                return {"ok": True, "path": key, "bytes": len(raw), "unchanged": True}
            count, size, visited = 0, 0, 0
            pending = [(plugin.root, 0)]
            while pending:
                directory, depth = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > 200:
                            raise KnowledgeLibraryError("knowledge_limit")
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if depth >= 8:
                                raise KnowledgeLibraryError("knowledge_limit")
                            pending.append((Path(entry.path), depth + 1))
                        elif stat.S_ISREG(info.st_mode):
                            count += 1
                            size += info.st_size
            if count >= MAX_DOCUMENTS or size + len(raw) > MAX_TOTAL_BYTES:
                raise KnowledgeLibraryError("knowledge_limit")
            plugin.files.validate_workspace()
            _new_file(target, raw)
            return {"ok": True, "path": key, "bytes": len(raw), "unchanged": False}
        except FileExistsError:
            raise KnowledgeLibraryError("knowledge_file_exists") from None
        except (OSError, ValueError):
            raise KnowledgeLibraryError("knowledge_unavailable") from None


class KnowledgeLibrary:
    name = "knowledge-library"

    def __init__(self, workspace, database):
        self.workspace, self.database = Path(workspace), Path(database)
        self.directory = library_directory(database)
        self._configuration = JSONConfigurationFile(self.directory / "collections.json")
        self._lock = threading.RLock()
        self._managers = []
        self._app = self._publisher = None
        self._closed = False
        value, _ = self._read()
        self.configs = self._parse(value)
        self.permissions = ()

    def _parse(self, value):
        if value == {"collections": []}:
            return ()
        configs = _parse_knowledge_config(value, self._configuration.path,
                                         workspace=self.workspace, database=self.database)
        # Managed paths are derived from the ID, including when reloading disk.
        for config in configs:
            if (not re.fullmatch(r"kb_[a-f0-9]{24}", config.id)
                    or config.path != "knowledge/" + config.id
                    or config.index_path != (self.directory / (config.id + ".sqlite3")).resolve()):
                raise KnowledgeLibraryError("knowledge_invalid_collection")
        return configs

    def _read(self):
        from .workspaces import check_existing_ancestors
        check_existing_ancestors(self._configuration.path.parent)
        if not self._configuration.path.exists():
            return {"collections": []}, EMPTY_REVISION
        return self._configuration.read()

    def activate(self, registry):
        if self.configs:
            manager = KnowledgeCollectionManager(self.configs, config_path=self._configuration.path,
                                                workspace=self.workspace, database=self.database)
            manager.activate(registry)
            self._managers.append(manager)
            self.permissions = manager.permissions
        return self.close

    def bind(self, app, publisher):
        self._app, self._publisher = app, publisher

    def management(self):
        with self._lock:
            value, revision = self._read()
            self._parse(value)
            return {"can_create": True, "revision": revision, "max_collections": MAX_COLLECTIONS}

    def collections(self):
        with self._lock:
            return [item for manager in self._managers for item in manager.collections()]

    def get(self, identifier):
        with self._lock:
            return next((manager.get(identifier) for manager in self._managers if manager.get(identifier)), None)

    def _owner(self, identifier):
        return next(manager for manager in self._managers if manager.get(identifier))

    def embedding_configuration(self, identifier):
        with self._lock:
            return self._owner(identifier).embedding_configuration(identifier)

    def save_embedding_configuration(self, identifier, embedding, expected_revision):
        with self._lock:
            return self._owner(identifier).save_embedding_configuration(identifier, embedding, expected_revision)

    def create(self, *, id, label, expected_revision, workspace_id):
        if (not isinstance(id, str) or not re.fullmatch(r"kb_[a-f0-9]{24}", id)
                or not isinstance(label, str) or not label.strip() or label != label.strip()
                or len(label) > 64 or any(ord(char) < 32 for char in label)):
            raise KnowledgeLibraryError("knowledge_invalid_collection")
        with self._lock:
            if self._closed or self._publisher is None:
                raise KnowledgeLibraryError("knowledge_unavailable")
            token = self._app.reserve_tool_update()
            manager = None
            try:
                if workspace_id != self._app.startup_workspace_id or workspace_id != self._app.selected_workspace_id:
                    raise WorkspaceError("workspace_not_configured")
                binding = self._app.require_workspace(workspace_id)
                binding.validate()
                value, revision = self._read()
                configs = self._parse(value)
                existing = next((item for item in configs if item.id == id), None)
                if existing is not None:
                    if existing.label != label or self.get(id) is None:
                        raise KnowledgeLibraryError("knowledge_conflict")
                    return {"ok": True, "id": id, "management": self.management()}
                if revision != expected_revision:
                    raise KnowledgeLibraryError("knowledge_conflict")
                if len(configs) >= MAX_COLLECTIONS:
                    raise KnowledgeLibraryError("knowledge_limit")
                # A changed saved embedding profile must be loaded before a new
                # composition can claim that the complete catalog is current.
                if any(self.embedding_configuration(item.id)["restart_required"] for item in configs):
                    raise KnowledgeLibraryError("knowledge_conflict")
                parent = self.workspace / "knowledge"
                if not parent.exists():
                    parent.mkdir()
                inspect_workspace_directory(parent)
                _reject_hidden(parent)
                root = parent / id
                if not root.exists():
                    root.mkdir()
                inspect_workspace_directory(root)
                _reject_hidden(root)
                self.directory.mkdir(parents=True, exist_ok=True)
                inspect_workspace_directory(self.directory)
                entry = {"id": id, "label": label, "path": "knowledge/" + id, "index": id + ".sqlite3"}
                updated = {"collections": [*value["collections"], entry]}
                prospective = self._parse(updated)
                config = next(item for item in prospective if item.id == id)
                manager = KnowledgeCollectionManager((config,), config_path=self._configuration.path,
                                                     workspace=self.workspace, database=self.database)
                registry = ToolRegistry()
                manager.activate(registry)
                registry.freeze()
                extra = tuple(registry.get(spec.name) for spec in registry.specs())

                def commit():
                    if revision == EMPTY_REVISION:
                        import json
                        _new_file(self._configuration.path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                    else:
                        self._configuration.update(revision, lambda _: updated, self._parse)

                self._publisher.extend_base(token, extra, manager.permissions, commit)
                self._managers.append(manager)
                manager = None
                self.configs = prospective
                self.permissions = tuple(sorted({p for item in self._managers for p in item.permissions}))
                return {"ok": True, "id": id, "management": self.management()}
            except (OSError, ValueError, ConfigurationConflict):
                raise KnowledgeLibraryError("knowledge_unavailable") from None
            finally:
                if manager is not None:
                    manager.close()
                self._app.release_tool_update(token)

    def close(self):
        with self._lock:
            self._closed = True
            for manager in reversed(self._managers):
                manager.close()
