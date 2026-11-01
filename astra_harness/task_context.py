"""Bounded, host-prepared guidance for one task; never execution authority.

This module knows neither local Skill files nor their enabling policy. The host
prepares selected content once. Recovery uses the saved bytes, not a live source.
"""
from dataclasses import dataclass, field
import hashlib
import json
import re

MAX_TEXT_BYTES = 16_384
MAX_METADATA_BYTES = 4_096
MAX_SNAPSHOT_BYTES = 49_152
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class TaskContextError(RuntimeError):
    STATUS = {"task_context_invalid": 400, "task_context_unavailable": 409,
              "task_context_changed": 409}

    def __init__(self, code):
        self.code = code if code in self.STATUS else "task_context_unavailable"
        self.status = self.STATUS[self.code]
        super().__init__(self.code)


@dataclass(frozen=True)
class PreparedTaskContext:
    selection: dict
    text: str = field(repr=False)
    metadata: dict = field(default_factory=dict)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def normalize_selection(value):
    """Validate the small user selection; callers cannot supply content here."""
    if value is None:
        return None
    if (type(value) is not dict or set(value) != {"id", "expected_identity"}
            or type(value["id"]) is not str or _ID.fullmatch(value["id"]) is None
            or type(value["expected_identity"]) is not str
            or _SHA.fullmatch(value["expected_identity"]) is None):
        raise TaskContextError("task_context_invalid")
    return dict(value)


def _text(value, maximum, *, nonempty=False):
    if type(value) is not str or (nonempty and not value.strip()):
        raise ValueError("invalid text")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ValueError("invalid text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError("text too large")
    return value


def _metadata(value):
    if type(value) is not dict:
        raise ValueError("metadata must be an object")
    remaining = 64

    def check(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 3:
            raise ValueError("metadata too complex")
        if item is None or type(item) is bool:
            return
        if type(item) is int and abs(item) <= 9_007_199_254_740_991:
            return
        if type(item) is str:
            _text(item, 2_048)
            return
        if type(item) is list and len(item) <= 16:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and len(item) <= 16:
            for key, child in item.items():
                if type(key) is not str or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key) is None:
                    raise ValueError("invalid metadata key")
                check(child, depth + 1)
            return
        raise ValueError("invalid metadata value")

    check(value)
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("metadata too large")
    return json.loads(encoded)


def _scope(state):
    identifier, identity = state.get("workspace_id"), state.get("workspace_identity")
    if identifier is None and identity is None:
        return None
    if (type(identifier) is not str or _ID.fullmatch(identifier) is None
            or type(identity) is not str or _SHA.fullmatch(identity) is None):
        raise ValueError("invalid workspace scope")
    return {"id": identifier, "identity": identity}


def render_guidance(prompt, snapshot):
    # One user message keeps selected guidance in the current indivisible turn.
    # JSON framing is unambiguous data formatting, not a prompt-injection sandbox.
    guidance = _json({"text": snapshot["text"], "metadata": snapshot["metadata"]})
    return (prompt + "\n\n[Explicit task guidance; applies only to this task]\n" + guidance
            + "\n[End guidance. Tools, permissions, approvals and limits remain unchanged.]")


def attach_task_context(state, prepared, selection):
    """Freeze a host response without changing the prompt, system or permissions."""
    try:
        requested = normalize_selection(selection)
        if requested is None or type(prepared) is not PreparedTaskContext:
            raise ValueError("invalid prepared context")
        if normalize_selection(prepared.selection) != requested:
            raise TaskContextError("task_context_changed")
        payload = {"format": 1, "selection": requested,
                   "text": _text(prepared.text, MAX_TEXT_BYTES, nonempty=True),
                   "metadata": _metadata(prepared.metadata), "workspace": _scope(state)}
        raw = _json(payload).encode("utf-8")
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot too large")
        snapshot = {**payload, "sha256": hashlib.sha256(raw).hexdigest()}
        position = state["turn_start"]
        if type(position) is not int or state["messages"][position]["role"] != "user":
            raise ValueError("invalid current turn")
        text = render_guidance(state["prompt"], snapshot)
        state["messages"][position]["text"] = text
        state["task_context"] = snapshot
    except TaskContextError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
        raise TaskContextError("task_context_invalid") from None


def validate_task_context(state):
    """Check stored content and its current-turn use, without a source lookup.

    The digest detects mismatched/corrupt records; it does not authenticate a
    malicious local database writer who can replace both content and its digest.
    """
    try:
        if "task_context" not in state:
            # Old/unselected Application states use their literal prompt. A
            # missing snapshot must not turn an enhanced user turn into legacy.
            position = state["turn_start"]
            if (type(position) is not int or not 0 <= position < len(state["messages"])
                    or state["messages"][position].get("role") != "user"
                    or state["messages"][position].get("text") != state["prompt"]):
                raise ValueError("missing task snapshot")
            return
        snapshot = state["task_context"]
        fields = {"format", "selection", "text", "metadata", "workspace", "sha256"}
        if (state.get("kind") == "tool" or type(snapshot) is not dict or set(snapshot) != fields
                or type(snapshot["format"]) is not int or snapshot["format"] != 1
                or type(snapshot["sha256"]) is not str or _SHA.fullmatch(snapshot["sha256"]) is None):
            raise ValueError("invalid snapshot")
        selection = normalize_selection(snapshot["selection"])
        if selection is None or snapshot["workspace"] != _scope(state):
            raise ValueError("snapshot scope changed")
        payload = {"format": 1, "selection": selection,
                   "text": _text(snapshot["text"], MAX_TEXT_BYTES, nonempty=True),
                   "metadata": _metadata(snapshot["metadata"]), "workspace": _scope(state)}
        raw = _json(payload).encode("utf-8")
        if len(raw) > MAX_SNAPSHOT_BYTES or hashlib.sha256(raw).hexdigest() != snapshot["sha256"]:
            raise ValueError("snapshot digest changed")
        messages, position = state["messages"], state["turn_start"]
        if (type(messages) is not list or type(position) is not int or not 0 <= position < len(messages)
                or messages[position].get("role") != "user"
                or any(item.get("role") == "user" for item in messages[position + 1:])
                or messages[position].get("text") != render_guidance(state["prompt"], payload)):
            raise ValueError("current turn differs from snapshot")
    except (TaskContextError, ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError, RecursionError):
        raise TaskContextError("task_context_changed") from None
