"""Bounded host-selected JSON configuration with optimistic revision checks.

The caller validates its own schema. Clients never select filesystem paths.
The process lock serializes cooperating host writers; outside editors must use
the returned revision and can still race the final filesystem replacement.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading


MAX_CONFIG_BYTES = 65_536
_WRITE_LOCK = threading.RLock()


class ConfigurationConflict(RuntimeError):
    """A fixed diagnostic; do not expose paths or source configuration text."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("configuration_duplicate_key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("configuration_nonfinite_number")


class JSONConfigurationFile:
    def __init__(self, path):
        self.path = Path(os.path.abspath(path))

    def _check_path(self):
        for candidate in (self.path, *self.path.parents):
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("configuration_link")
            if candidate == self.path:
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) > 1:
                    raise ValueError("configuration_not_regular")
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("configuration_parent_invalid")

    def _read_bytes(self):
        try:
            self._check_path()
            with self.path.open("rb") as source:
                raw = source.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise ValueError("configuration_too_large")
            return raw
        except (OSError, ValueError, TypeError):
            raise ValueError("configuration_not_readable") from None

    @staticmethod
    def _decode(raw):
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
            if type(value) is not dict:
                raise ValueError
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("configuration_invalid_json") from None

    def read(self):
        with _WRITE_LOCK:
            raw = self._read_bytes()
            return self._decode(raw), hashlib.sha256(raw).hexdigest()

    def update(self, expected_revision, update, validate):
        """Validate complete prospective JSON before atomically replacing bytes."""
        if not isinstance(expected_revision, str) or re.fullmatch(r"[0-9a-f]{64}", expected_revision) is None:
            raise ValueError("configuration_invalid_revision")
        with _WRITE_LOCK:
            original = self._read_bytes()
            if hashlib.sha256(original).hexdigest() != expected_revision:
                raise ConfigurationConflict("configuration_changed")
            value = update(self._decode(original))
            validate(value)
            try:
                raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise ValueError("configuration_invalid_json") from None
            if len(raw) > MAX_CONFIG_BYTES:
                raise ValueError("configuration_too_large")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".astra-config-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Recheck path identity and bytes after preparation, never use a
                # stale UI form to knowingly overwrite a concurrent editor.
                if self._read_bytes() != original:
                    raise ConfigurationConflict("configuration_changed")
                os.replace(temporary, self.path)
                temporary = None
                if self._read_bytes() != raw:
                    raise ConfigurationConflict("configuration_changed_after_save")
            except OSError:
                raise ConfigurationConflict("configuration_save_failed") from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return hashlib.sha256(raw).hexdigest()
