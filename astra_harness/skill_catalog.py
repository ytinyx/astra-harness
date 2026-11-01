"""Workspace-local consent to Skill content; no tool or filesystem authority.

Only approved identities are stored here. Documents remain user-edited files;
the host prepares a selected body once and the runtime freezes it per task.
"""
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
import threading

from .store import Store
from .task_context import PreparedTaskContext
from .workspace_binding import WorkspaceError

MAX_APPROVALS = 32
MAX_REVISION = 9007199254740991
_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class SkillCatalogError(RuntimeError):
    STATUS = {"skill_catalog_unavailable": 409, "skill_catalog_conflict": 409,
              "invalid_skill_selection": 400, "skill_disabled": 409,
              "skill_changed": 409, "skill_not_found": 404, "skill_catalog_limit": 409}

    def __init__(self, code):
        self.code = code if code in self.STATUS else "skill_catalog_unavailable"
        self.status = self.STATUS[self.code]
        super().__init__(self.code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _selection(identifier, identity):
    if (type(identifier) is not str or _ID.fullmatch(identifier) is None
            or type(identity) is not str or _SHA.fullmatch(identity) is None):
        raise SkillCatalogError("invalid_skill_selection")


def _revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise SkillCatalogError("invalid_skill_selection")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


class SkillCatalog:
    def __init__(self, store, *, loader_factory):
        if not callable(loader_factory):
            raise TypeError("loader_factory must be callable")
        self._store, self._loader_factory = store, loader_factory
        self._lock = threading.RLock()

    @staticmethod
    def _scope(scope):
        try:
            return Store.workspace_spec(scope)
        except WorkspaceError:
            raise SkillCatalogError("skill_catalog_unavailable") from None

    @staticmethod
    def _key(scope):
        # Include logical and physical scope; a replacement at the same path
        # cannot inherit approval. The setting name never exposes that path.
        return "skills." + hashlib.sha256(_json(scope).encode("utf-8")).hexdigest()

    def _read(self, scope):
        try:
            raw = self._store.host_setting(self._key(scope))
            if raw is None:
                return raw, {"format": 1, "revision": 0, "approved": {}}
            value = json.loads(raw, object_pairs_hook=_unique)
            if (type(value) is not dict or set(value) != {"format", "revision", "approved"}
                    or type(value["format"]) is not int or value["format"] != 1
                    or type(value["approved"]) is not dict or len(value["approved"]) > MAX_APPROVALS):
                raise ValueError()
            _revision(value["revision"])
            for identifier, identity in value["approved"].items():
                _selection(identifier, identity)
            return raw, value
        except (ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error, SkillCatalogError):
            raise SkillCatalogError("skill_catalog_unavailable") from None

    def _write(self, scope, raw, value):
        if len(value["approved"]) > MAX_APPROVALS:
            raise SkillCatalogError("skill_catalog_limit")
        value["revision"] += 1
        try:
            if not self._store.compare_and_swap_host_setting(self._key(scope), raw, _json(value)):
                raise SkillCatalogError("skill_catalog_conflict")
        except (ValueError, UnicodeError, sqlite3.Error):
            raise SkillCatalogError("skill_catalog_unavailable") from None

    @staticmethod
    def _item(catalog, identifier, expected_identity):
        item = next((item for item in catalog["items"] if item["id"] == identifier), None)
        if item is None:
            # Malformed/duplicate documents have issues, never usable metadata.
            raise SkillCatalogError("skill_not_found")
        if item["identity"] != expected_identity:
            raise SkillCatalogError("skill_changed")
        return item

    @staticmethod
    def _metadata(item, approved):
        previous = approved.get(item["id"])
        return {**deepcopy(item), "enabled": previous == item["identity"],
                "needs_review": previous is not None and previous != item["identity"]}

    @classmethod
    def _view(cls, scope, catalog, value):
        return {**deepcopy(catalog), "workspace_id": scope["id"], "workspace_identity": scope["identity"],
                "revision": value["revision"],
                "items": [cls._metadata(item, value["approved"]) for item in catalog["items"]]}

    def list(self, scope):
        scope = self._scope(scope)
        # Loader validation precedes access to approvals: an unavailable scope
        # is never replaced by a different directory or a cached old listing.
        loader = self._loader_factory(dict(scope))
        catalog = loader.catalog()
        with self._lock:
            value = self._read(scope)[1]
            return self._view(scope, catalog, value)

    def view(self, identifier, expected_identity, scope):
        _selection(identifier, expected_identity)
        scope = self._scope(scope)
        loader = self._loader_factory(dict(scope))
        self._item(loader.catalog(), identifier, expected_identity)
        document = loader.load(identifier, expected_identity)
        with self._lock:
            value = self._read(scope)[1]
            return {**self._metadata(document.public_metadata(), value["approved"]), "body": document.body,
                    "revision": value["revision"], "workspace_id": scope["id"], "workspace_identity": scope["identity"]}

    def set_enabled(self, identifier, enabled, expected_revision, expected_identity, scope):
        _selection(identifier, expected_identity)
        _revision(expected_revision)
        if type(enabled) is not bool:
            raise SkillCatalogError("invalid_skill_selection")
        scope = self._scope(scope)
        loader = self._loader_factory(dict(scope))
        with self._lock:
            raw, value = self._read(scope)
            if value["revision"] != expected_revision or expected_revision == MAX_REVISION:
                raise SkillCatalogError("skill_catalog_conflict")
            catalog = loader.catalog()
            self._item(catalog, identifier, expected_identity)
            loader.load(identifier, expected_identity)
            # Explicit changes can retire approval for removed directories. GET
            # never writes, and changed/invalid documents at an existing id keep
            # their reviewed identity until the user chooses a new one.
            known = {item["id"] for item in catalog["items"]} | {issue["id"] for issue in catalog["issues"]}
            value["approved"] = {key: identity for key, identity in value["approved"].items() if key in known}
            if enabled:
                value["approved"][identifier] = expected_identity
            else:
                value["approved"].pop(identifier, None)
            self._write(scope, raw, value)
            return self._view(scope, catalog, value)

    def prepare(self, selection, scope):
        if type(selection) is not dict or set(selection) != {"id", "expected_identity"}:
            raise SkillCatalogError("invalid_skill_selection")
        identifier, identity = selection["id"], selection["expected_identity"]
        _selection(identifier, identity)
        scope = self._scope(scope)
        loader = self._loader_factory(dict(scope))
        with self._lock:
            value = self._read(scope)[1]
            item = self._item(loader.catalog(), identifier, identity)
            if value["approved"].get(identifier) != identity:
                raise SkillCatalogError("skill_disabled")
            document = loader.load(identifier, identity)
            # No permissions, tools or adapter identities are taken from a
            # document. The core validates and persists this bounded plain text.
            return PreparedTaskContext(selection=dict(selection), text=document.body,
                metadata={key: item[key] for key in ("name", "description", "path")})
