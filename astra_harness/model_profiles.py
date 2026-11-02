"""Host-owned model catalog with separate, protected credential records.

Editing sends no provider requests. Only bind/test resolves credentials; the
runtime caches a binding so a running task retains its original credential.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid

from .builtin.model_profile_adapter import build_profile_model, validate_profile_config
from .builtin import model_profile_adapter
from .builtin.models import ModelAdapterError
from .model_binding import ModelBinding, ModelProfileError
from . import model_credentials

CATALOG_KEY = "model_profiles.catalog_v1"
SEAL_KEY = "model_profiles.seal_v1"
CREDENTIAL_PREFIX = "model_profiles.credential."
MAX_PROFILES = 16
MAX_REVISION = 9007199254740991
ADAPTER_REVISION = "1"
_ADAPTER_FIELDS = {"provider", "base_url", "model", "key_env", "timeout", "max_output_tokens"}
_SNAPSHOT_FIELDS = _ADAPTER_FIELDS | {"id", "name", "revision", "adapter_revision"}
_TEST_ERRORS = {"model_credentials_missing", "model_credentials_invalid", "model_endpoint_not_public",
                "model_dns_failed", "model_request_too_large", "model_authentication_failed", "model_rate_limited",
                "model_transport_error", "model_response_too_large", "model_protocol_error", "model_timeout",
                "model_test_unexpected_tools"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise ModelProfileError("invalid_model_profile")
    return value


def _id(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ModelProfileError("invalid_model_profile")
    return value


def _name(value):
    if not isinstance(value, str):
        raise ModelProfileError("invalid_model_profile")
    value = value.strip()
    if not 1 <= len(value) <= 80 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ModelProfileError("invalid_model_profile")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ModelProfileError("invalid_model_profile") from None
    return value


def _snapshot(value):
    if type(value) is not dict or set(value) != _SNAPSHOT_FIELDS:
        raise ModelProfileError("invalid_model_profile")
    try:
        result = {"id": _id(value["id"]), "name": _name(value["name"]),
                  **validate_profile_config({key: value[key] for key in _ADAPTER_FIELDS}),
                  "revision": _revision(value["revision"]), "adapter_revision": ADAPTER_REVISION}
        if not result["revision"] or _json(result) != _json(value):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError):
        raise ModelProfileError("invalid_model_profile") from None


def profile_identity(snapshot):
    return hashlib.sha256(_json(_snapshot(snapshot)).encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class ModelProfiles:
    def __init__(self, store, *, environment=None):
        self._store = store
        self._environment = environment if environment is not None else lambda: os.environ
        if not callable(self._environment):
            raise TypeError("environment must be callable")
        self._lock = threading.RLock()
        self._test_lock = threading.Lock()

    def _read(self):
        try:
            raw = self._store.host_setting(CATALOG_KEY)
            if raw is None:
                return raw, {"format": 1, "revision": 0, "selected_id": None, "items": []}
            value = json.loads(raw, object_pairs_hook=_unique_object)
            if (type(value) is not dict or set(value) != {"format", "revision", "selected_id", "items"}
                    or type(value["format"]) is not int or value["format"] != 1
                    or type(value["items"]) is not list or len(value["items"]) > MAX_PROFILES):
                raise ValueError
            _revision(value["revision"])
            items = [_snapshot(item) for item in value["items"]]
            ids = [item["id"] for item in items]
            if len(set(ids)) != len(ids) or (value["selected_id"] is not None and value["selected_id"] not in ids):
                raise ValueError
            return raw, {**value, "items": items}
        except (ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error, ModelProfileError):
            # A malformed catalog must not silently reset selection or recovery.
            raise ModelProfileError("model_profile_unavailable") from None

    def _view(self, value):
        items = [{**item, "identity": profile_identity(item)} for item in value["items"]]
        for item in items:
            if item["key_env"] is None:
                item["has_api_key"] = self._stored_key(item["id"]) is not None
        selected = next((item for item in items if item["id"] == value["selected_id"]), None)
        return {"revision": value["revision"], "selected_id": value["selected_id"],
                "selected_identity": selected["identity"] if selected is not None else "startup",
                "items": items, "protocol": "chat_completions", "startup_label": "启动配置",
                "can_store_api_key": model_credentials.available()}

    def _stored_key(self, identifier):
        try:
            return self._store.host_setting(CREDENTIAL_PREFIX + identifier)
        except (ValueError, UnicodeError, sqlite3.Error):
            raise ModelProfileError("model_key_storage_unavailable") from None

    def list(self):
        with self._lock:
            return self._view(self._read()[1])

    def selected_snapshot(self):
        with self._lock:
            value = self._read()[1]
            return next((dict(item) for item in value["items"] if item["id"] == value["selected_id"]), None)

    def _checked(self, expected_revision):
        _revision(expected_revision)
        raw, value = self._read()
        if value["revision"] != expected_revision or expected_revision == MAX_REVISION:
            raise ModelProfileError("model_profile_conflict")
        return raw, value

    def _write(self, raw, value, *, credentials=None):
        value["revision"] += 1
        try:
            options = {"updates": credentials} if credentials else {}
            if not self._store.compare_and_swap_host_setting(CATALOG_KEY, raw, _json(value), **options):
                raise ModelProfileError("model_profile_conflict")
        except (ValueError, UnicodeError, sqlite3.Error):
            raise ModelProfileError("model_profile_unavailable") from None
        return self._view(value)

    def save(self, profile, expected_revision):
        if type(profile) is not dict or set(profile) - (_ADAPTER_FIELDS | {"id", "name", "api_key"}):
            raise ModelProfileError("invalid_model_profile")
        try:
            config = validate_profile_config({key: value for key, value in profile.items() if key in _ADAPTER_FIELDS})
            name = _name(profile.get("name"))
        except (ValueError, TypeError, UnicodeError):
            raise ModelProfileError("invalid_model_profile") from None
        # None explicitly selects local storage. Existing environment profiles
        # keep their original snapshot/identity and never accept a secret field.
        if "api_key" in profile and config["key_env"] is not None:
            raise ModelProfileError("invalid_model_profile")
        api_key = profile.get("api_key", "")
        if api_key != "":
            model_credentials.validate_key(api_key)
        with self._lock:
            raw, value = self._checked(expected_revision)
            old = None
            if "id" in profile:
                identifier = _id(profile["id"])
                old = next((item for item in value["items"] if item["id"] == identifier), None)
                if old is None:
                    raise ModelProfileError("model_profile_not_found")
            else:
                if len(value["items"]) >= MAX_PROFILES:
                    raise ModelProfileError("model_profile_conflict")
                identifier = uuid.uuid4().hex
            credentials = {}
            if config["key_env"] is None:
                if api_key:
                    credentials[CREDENTIAL_PREFIX + identifier] = model_credentials.protect_key(api_key, identifier)
                elif old is None or old["key_env"] is not None:
                    raise ModelProfileError("model_credentials_missing")
            elif old is not None and old["key_env"] is None:
                if self._test_lock.locked():
                    raise ModelProfileError("model_profile_in_use")
                credentials[CREDENTIAL_PREFIX + identifier] = None
            item = {"id": identifier, "name": name, **config, "revision": old["revision"] if old else 1,
                    "adapter_revision": ADAPTER_REVISION}
            if old is not None:
                if item != old or api_key:
                    if old["revision"] == MAX_REVISION:
                        raise ModelProfileError("model_profile_conflict")
                    item["revision"] += 1
                value["items"] = [item if entry["id"] == identifier else entry for entry in value["items"]]
            else:
                value["items"].append(item)
            return self._write(raw, value, credentials=credentials)

    def select(self, identifier, expected_revision):
        if identifier is not None:
            _id(identifier)
        with self._lock:
            raw, value = self._checked(expected_revision)
            if identifier is not None and not any(item["id"] == identifier for item in value["items"]):
                raise ModelProfileError("model_profile_not_found")
            value["selected_id"] = identifier
            return self._write(raw, value)

    def delete(self, identifier, expected_revision):
        _id(identifier)
        if not self._test_lock.acquire(blocking=False):
            raise ModelProfileError("model_profile_in_use")
        try:
            return self._delete(identifier, expected_revision)
        finally:
            self._test_lock.release()

    def _delete(self, identifier, expected_revision, *, key_only=False):
        with self._lock:
            raw, value = self._checked(expected_revision)
            item = next((item for item in value["items"] if item["id"] == identifier), None)
            if item is None:
                raise ModelProfileError("model_profile_not_found")
            if key_only:
                if item["key_env"] is not None:
                    raise ModelProfileError("invalid_model_profile")
                if self._stored_key(identifier) is None:
                    return self._view(value)
                if item["revision"] == MAX_REVISION:
                    raise ModelProfileError("model_profile_conflict")
                item["revision"] += 1
            else:
                value["items"] = [item for item in value["items"] if item["id"] != identifier]
                if value["selected_id"] == identifier:
                    value["selected_id"] = None
            return self._write(raw, value, credentials={CREDENTIAL_PREFIX + identifier: None})

    def delete_key(self, identifier, expected_revision):
        _id(identifier)
        if not self._test_lock.acquire(blocking=False):
            raise ModelProfileError("model_profile_in_use")
        try:
            return self._delete(identifier, expected_revision, key_only=True)
        finally:
            self._test_lock.release()

    def _seal_secret(self):
        try:
            value = self._store.host_setting(SEAL_KEY)
            if value is None:
                candidate = secrets.token_hex(32)
                if self._store.compare_and_swap_host_setting(SEAL_KEY, None, candidate):
                    value = candidate
                else:
                    value = self._store.host_setting(SEAL_KEY)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError
            return bytes.fromhex(value)
        except (ValueError, UnicodeError, sqlite3.Error):
            raise ModelProfileError("model_profile_unavailable") from None

    def bind(self, snapshot):
        snapshot = _snapshot(snapshot)
        identity = profile_identity(snapshot)
        try:
            key = (model_credentials.unprotect_key(self._stored_key(snapshot["id"]), snapshot["id"])
                   if snapshot["key_env"] is None else self._environment().get(snapshot["key_env"]))
            model = build_profile_model(key, {field: snapshot[field] for field in _ADAPTER_FIELDS})
        except ModelProfileError:
            raise
        except ModelAdapterError as exc:
            code = "model_credentials_missing" if str(exc) == "model_credentials_missing" else "model_profile_unavailable"
            raise ModelProfileError(code) from None
        except Exception:
            raise ModelProfileError("model_profile_unavailable") from None
        # A DB-local random secret prevents a public run identity from becoming
        # an offline oracle for a low-entropy credential. No plaintext is stored.
        payload = _json(["astra.model-profile.credential.v1", identity, key]).encode("utf-8")
        seal = hmac.new(self._seal_secret(), payload, hashlib.sha256).hexdigest()
        return ModelBinding(snapshot=snapshot, identity=identity, credential_seal=seal, model=model)

    async def test(self, identifier, expected_revision):
        _id(identifier)
        if not self._test_lock.acquire(blocking=False):
            raise ModelProfileError("model_profile_in_use")
        try:
            with self._lock:
                _, value = self._checked(expected_revision)
                snapshot = next((item for item in value["items"] if item["id"] == identifier), None)
                if snapshot is None:
                    raise ModelProfileError("model_profile_not_found")
                binding = self.bind(snapshot)
            # No catalog/Store lock spans the request. Edits can continue while
            # the result remains explicitly associated with the tested snapshot.
            try:
                result = await model_profile_adapter.probe_model(binding.model)
                if type(result) is not dict or type(result.get("ok")) is not bool or type(result.get("request_issued")) is not bool:
                    raise ValueError
                public = {"ok": result["ok"], "request_issued": result["request_issued"],
                          "requested_model": snapshot["model"]}
                if not result["ok"]:
                    public["error_code"] = result.get("error_code") if result.get("error_code") in _TEST_ERRORS else "model_transport_error"
                elif result.get("usage") is not None:
                    usage = result["usage"]
                    if (type(usage) is not dict or any(key not in {"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"}
                            or type(count) is not int or not 0 <= count <= MAX_REVISION for key, count in usage.items())):
                        raise ValueError
                    public["usage"] = dict(usage)
            except Exception:
                raise ModelProfileError("model_profile_unavailable") from None
            return {**public, "id": identifier, "identity": binding.identity,
                    "revision": value["revision"], "profile_revision": snapshot["revision"]}
        finally:
            self._test_lock.release()
