"""Bounded, read-only task guidance from a workspace's visible skills directory.

The two-field frontmatter is deliberately a plain-text subset, not YAML. Loading
a document neither executes code nor follows Markdown references. Permissions
remain the responsibility of the ordinary registry/executor, never this text.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat

from .files import FilePlugin, _reject_private_entry


MAX_ENTRIES = 64
MAX_SKILLS = 32
MAX_FILE_BYTES = 16_384
MAX_HEADER_BYTES = 2_048
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 240
_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BOUNDARY_ERRORS = frozenset({"workspace_changed", "links_not_allowed", "forbidden_path",
    "path_outside_workspace", "path_missing", "parent_missing", "not_directory"})


class SkillLoadError(ValueError):
    """A fixed public code, with no file contents or operating-system message."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SkillDocument:
    id: str
    identity: str
    name: str
    description: str
    path: str
    sha256: str
    byte_count: int
    body: str

    def public_metadata(self):
        return {key: getattr(self, key) for key in
                ("id", "identity", "name", "description", "path", "sha256", "byte_count")}


def _stamp(info):
    # Windows path stat and fstat can expose different ctime semantics in
    # Python 3.14. Use birthtime there (ctime fallback for Python 3.10), while
    # still comparing file identity, size, modification time and link count.
    stable_time = getattr(info, "st_birthtime_ns", info.st_ctime_ns) if os.name == "nt" else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, stable_time, info.st_nlink)


def _boundary_error(exc):
    code = str(exc)
    return SkillLoadError(code if code in _BOUNDARY_ERRORS else "skill_unavailable")


def _parse(raw):
    if len(raw) > MAX_FILE_BYTES:
        raise SkillLoadError("skill_file_limit")
    if b"\0" in raw:
        raise SkillLoadError("skill_binary")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise SkillLoadError("skill_invalid_utf8") from None
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise SkillLoadError("skill_binary")
    # Only CR/LF are stripped from metadata; Unicode separators remain invalid.
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SkillLoadError("skill_metadata_invalid")
    metadata = {}
    header_bytes = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    for position, line in enumerate(lines):
        header_bytes += len(line.encode("utf-8"))
        if header_bytes > MAX_HEADER_BYTES:
            raise SkillLoadError("skill_header_limit")
        if position == 0:
            continue
        value = line.rstrip("\r\n")
        if value == "---":
            if set(metadata) != {"name", "description"}:
                raise SkillLoadError("skill_metadata_invalid")
            body = "".join(lines[position + 1:])
            if not body.strip():
                raise SkillLoadError("skill_body_empty")
            return metadata, body
        key, separator, value = value.partition(":")
        value = value.strip(" ")
        maximum = {"name": MAX_NAME_CHARS, "description": MAX_DESCRIPTION_CHARS}.get(key)
        if (not separator or maximum is None or key in metadata or not value
                or len(value) > maximum or value[0] in "\"'[{|>&*!%@`"
                or any(ord(char) < 32 or ord(char) == 127 or char in "\x85\u2028\u2029" for char in value)):
            raise SkillLoadError("skill_metadata_invalid")
        metadata[key] = value
    raise SkillLoadError("skill_metadata_invalid")


class LocalSkillLoader:
    """Read through an existing FilePlugin; constructing a loader performs no IO.

    Every direct entry counts toward the traversal bound. Every valid slug is a
    candidate, even if its target is malformed. Limits reject the whole catalog,
    so a truncated scan cannot hide a duplicate display name.
    """

    def __init__(self, file_plugin: FilePlugin):
        if not isinstance(file_plugin, FilePlugin):
            raise TypeError("an existing FilePlugin is required")
        self.files = file_plugin

    def _read(self, relative):
        try:
            target = self.files._path(relative)
            before = target.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise SkillLoadError("skill_not_file")
            if before.st_size > MAX_FILE_BYTES:
                raise SkillLoadError("skill_file_limit")
            with target.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                _reject_private_entry(target, opened)
                if _stamp(before) != _stamp(opened):
                    raise SkillLoadError("skill_changed")
                raw = stream.read(MAX_FILE_BYTES + 1)
                after_read = os.fstat(stream.fileno())
            after = self.files._path(relative).lstat()
            if _stamp(before) != _stamp(after_read) or _stamp(before) != _stamp(after):
                raise SkillLoadError("skill_changed")
            if len(raw) > MAX_FILE_BYTES:
                raise SkillLoadError("skill_file_limit")
            return raw
        except SkillLoadError:
            raise
        except (OSError, ValueError) as exc:
            raise _boundary_error(exc) from None

    def _document(self, slug):
        relative = f"skills/{slug}/SKILL.md"
        raw = self._read(relative)
        metadata, body = _parse(raw)
        digest = hashlib.sha256(raw).hexdigest()
        binding = {"format": 1, "workspace": self.files.workspace_identity,
                   "path": relative, "sha256": digest}
        identity = hashlib.sha256(json.dumps(binding, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return SkillDocument(slug, identity, metadata["name"], metadata["description"],
                             relative, digest, len(raw), body)

    def _scan(self):
        try:
            directory = self.files._directory("skills")
        except ValueError as exc:
            if str(exc) == "path_missing":
                return False, {}, []
            raise _boundary_error(exc) from None
        except OSError:
            raise SkillLoadError("skill_unavailable") from None
        documents, issues, candidates = {}, [], []
        try:
            before = _stamp(directory.lstat())
            with os.scandir(directory) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > MAX_ENTRIES:
                        raise SkillLoadError("skills_limit")
                    if _ID.fullmatch(entry.name):
                        candidates.append(entry.name)
                        if len(candidates) > MAX_SKILLS:
                            raise SkillLoadError("skills_limit")
            for slug in sorted(candidates):
                try:
                    self.files._directory(f"skills/{slug}")
                    documents[slug] = self._document(slug)
                except SkillLoadError as exc:
                    if exc.code == "workspace_changed":
                        raise
                    issues.append({"id": slug, "error_code": exc.code})
                except (OSError, ValueError) as exc:
                    error = _boundary_error(exc)
                    if error.code == "workspace_changed":
                        raise error from None
                    issues.append({"id": slug, "error_code": error.code})
            after = _stamp(self.files._directory("skills").lstat())
            if before != after:
                raise SkillLoadError("skill_changed")
        except SkillLoadError:
            raise
        except (OSError, ValueError) as exc:
            raise _boundary_error(exc) from None
        names = {}
        for slug, document in documents.items():
            names.setdefault(document.name.casefold(), []).append(slug)
        for slugs in names.values():
            if len(slugs) > 1:
                for slug in slugs:
                    del documents[slug]
                    issues.append({"id": slug, "error_code": "skill_duplicate"})
        return True, documents, sorted(issues, key=lambda item: item["id"])

    def catalog(self):
        exists, documents, issues = self._scan()
        return {"directory": "skills", "exists": exists, "limit": MAX_SKILLS,
                "items": [document.public_metadata() for document in documents.values()],
                "issues": issues}

    def load(self, skill_id, expected_identity):
        if not isinstance(skill_id, str) or not _ID.fullmatch(skill_id):
            raise SkillLoadError("invalid_skill_id")
        if not isinstance(expected_identity, str) or not _DIGEST.fullmatch(expected_identity):
            raise SkillLoadError("invalid_skill_identity")
        _, documents, issues = self._scan()
        if skill_id not in documents:
            code = next((item["error_code"] for item in issues if item["id"] == skill_id), "skill_missing")
            raise SkillLoadError(code)
        document = documents[skill_id]
        if document.identity != expected_identity:
            raise SkillLoadError("skill_changed")
        return document
