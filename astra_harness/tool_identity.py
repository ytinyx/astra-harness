"""Versioned benchmark identities for explicitly declared local tool implementations.

This is a comparison contract, not a sandbox, signature, or automatic proof of
all dependencies. Bound state, helpers and constants remain host declarations;
stdlib, operating-system behavior and external services are outside this hash.
"""

import ast
from copy import deepcopy
import hashlib
import inspect
import json
import math
import re
import textwrap

from . import tools as tool_module
from .contracts import ToolSpec
from .tools import Tool, check_schema


ALGORITHM = "astra-tool-identity-v1"
_EXECUTION_NAMES = ("check_schema", "validate", "is_number", "Tool.spec", "ToolSpec.schema",
                    "ToolExecutor.check", "ToolExecutor.result", "ToolExecutor.execute", "ToolExecutor.aexecute")


def _json_value(value, depth=0):
    if depth > 128:
        raise ValueError("identity JSON is too deeply nested")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise ValueError("identity requires finite JSON values and string object keys")


def _canonical(value):
    _json_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def canonical_schema(schema, *, presentation=False):
    """Normalize schema nodes, never business keys/arrays in enum or default."""
    try:
        check_schema(schema)
    except (TypeError, KeyError, RecursionError):
        raise ValueError("invalid tool schema structure") from None
    result = {}
    for key, value in schema.items():
        if key in {"description", "title"} and not presentation:
            continue
        if key == "properties" and schema["type"] == "object":
            result[key] = {name: canonical_schema(child, presentation=presentation) for name, child in value.items()}
        elif key == "items" and schema["type"] == "array":
            result[key] = canonical_schema(value, presentation=presentation)
        elif key == "required" and schema["type"] == "object":
            result[key] = sorted(value)
        elif key == "enum":
            # Validation compares types as well as values. JSON encodings keep
            # False/0 and 1/1.0 distinct; only the outer membership list is a set.
            items = {_canonical(item): deepcopy(item) for item in value}
            result[key] = [items[encoded] for encoded in sorted(items)]
        else:
            result[key] = deepcopy(value)
    _canonical(result)
    return result


def _ast_value(node):
    if isinstance(node, ast.AST):
        fields = {}
        for name, value in ast.iter_fields(node):
            # Empty type_params was added in Python 3.12; it does not change
            # these Python 3.10-compatible implementations.
            if name == "type_params" and not value:
                continue
            if (name == "body" and isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and value and isinstance(value[0], ast.Expr)
                    and isinstance(value[0].value, ast.Constant) and isinstance(value[0].value.value, str)):
                value = value[1:]
            fields[name] = _ast_value(value)
        return {"node": type(node).__name__, "fields": fields}
    if isinstance(node, list):
        return [_ast_value(item) for item in node]
    if isinstance(node, bytes):
        return {"literal": "bytes", "hex": node.hex()}
    if isinstance(node, complex):
        return {"literal": "complex", "real": node.real, "imag": node.imag}
    if node is Ellipsis:
        return {"literal": "ellipsis"}
    return node


def _function(value):
    function = value.__func__ if inspect.ismethod(value) else value
    if (not inspect.isfunction(function) or function.__name__ == "<lambda>"
            or function.__closure__):
        raise ValueError("tool identity requires named source functions without captured closures")
    return function


def _default_value(value, depth=0):
    """Encode supported runtime defaults with types, never an unstable repr()."""
    if depth > 32:
        raise ValueError("implementation defaults are too deeply nested")
    if value is None or type(value) in (str, bool, int, float):
        _json_value(value)
        return {"type": type(value).__name__, "value": value}
    if type(value) in (list, tuple, set, frozenset):
        if len(value) > 10_000:
            raise ValueError("implementation default container is too large")
        items = [_default_value(item, depth + 1) for item in value]
        if type(value) in (set, frozenset):
            items.sort(key=_canonical)
        return {"type": type(value).__name__, "items": items}
    if type(value) is dict and all(isinstance(key, str) for key in value):
        return {"type": "dict", "items": {key: _default_value(item, depth + 1) for key, item in value.items()}}
    raise ValueError("unsupported implementation default value")


def function_fingerprint(value):
    function = _function(value)
    try:
        source = textwrap.dedent(inspect.getsource(function))
        if len(source) > 128_000:
            raise ValueError("implementation source is too large")
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError) as error:
        raise ValueError("implementation source is unavailable") from error
    if (len(tree.body) != 1 or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef))
            or sum(1 for _ in ast.walk(tree)) > 10_000):
        raise ValueError("unsupported implementation source")
    # Evaluated defaults can differ even when an AST refers to the same constant.
    return _hash({"ast": _ast_value(tree), "defaults": [_default_value(item) for item in function.__defaults__ or ()],
                  "keyword_defaults": {name: _default_value(item) for name, item in (function.__kwdefaults__ or {}).items()}})


def _tools(tools):
    result = tuple(tools)
    if len(result) > 1000 or any(not isinstance(tool, Tool) for tool in result):
        raise ValueError("tool identity requires at most 1000 Tool definitions")
    if len({tool.name for tool in result}) != len(result):
        raise ValueError("tool identity requires unique names")
    return sorted(result, key=lambda tool: tool.name)


def build_tool_identity(tools):
    entries, cached = [], {}

    def implementation_hash(value):
        function = _function(value)
        if function not in cached:
            cached[function] = function_fingerprint(function)
        return cached[function]

    for tool in _tools(tools):
        version = tool.implementation_version
        if not isinstance(version, str) or not version.strip() or len(version) > 128:
            raise ValueError("tool identity requires an explicit implementation version")
        dependencies = {}
        for dependency in tool.implementation_dependencies:
            function = _function(dependency)
            name = function.__module__ + "." + function.__qualname__
            if name in dependencies:
                raise ValueError("duplicate implementation dependency")
            dependencies[name] = implementation_hash(function)
        entries.append({"name": tool.name, "input_schema": canonical_schema(tool.spec().schema()),
                        "result_contract": "numeric" if tool.input_schema is None else "json",
                        "permissions": sorted(tool.permissions), "side_effect": tool.side_effect,
                        "requires_approval": tool.requires_approval,
                        **({"recovery_identity": tool.recovery_identity} if tool.recovery_identity is not None else {}),
                        "implementation": {"version": version, "handler_sha256": implementation_hash(tool.handler),
                                           "dependencies": dependencies,
                                           "constants_sha256": _hash(tool.implementation_constants)}})
    # Resolve current callables, not a cached tuple: a host replacing a validator
    # or result gate must not silently keep the old executor fingerprint.
    execution_functions = {"check_schema": tool_module.check_schema, "validate": tool_module.validate,
                           "is_number": tool_module.is_number, "Tool.spec": Tool.spec, "ToolSpec.schema": ToolSpec.schema,
                           "ToolExecutor.check": tool_module.ToolExecutor.check,
                           "ToolExecutor.result": tool_module.ToolExecutor.result,
                           "ToolExecutor.execute": tool_module.ToolExecutor.execute,
                           "ToolExecutor.aexecute": tool_module.ToolExecutor.aexecute}
    contract = {"version": "1", "functions": {name: implementation_hash(value) for name, value in execution_functions.items()}}
    identity = {"algorithm": ALGORITHM, "execution_contract": contract, "tools": entries}
    identity["sha256"] = _hash(identity)
    validate_tool_identity(identity)
    return identity


def presentation_fingerprint(tools):
    """Track tool/model-visible wording separately from the hard comparison gate."""
    return _hash([{"name": tool.name, "description": tool.description,
                   "input_schema": canonical_schema(tool.spec().schema(), presentation=True)} for tool in _tools(tools)])


def validate_tool_identity(identity):
    if (not isinstance(identity, dict) or set(identity) != {"algorithm", "execution_contract", "tools", "sha256"}
            or identity["algorithm"] != ALGORITHM or not _digest(identity["sha256"])
            or not isinstance(identity["tools"], list) or len(identity["tools"]) > 1000):
        raise ValueError("report requires a valid versioned tool identity")
    contract = identity["execution_contract"]
    if (not isinstance(contract, dict) or set(contract) != {"version", "functions"}
            or not isinstance(contract["version"], str) or not contract["version"].strip() or len(contract["version"]) > 128
            or not isinstance(contract["functions"], dict) or set(contract["functions"]) != set(_EXECUTION_NAMES)
            or not all(_digest(value) for value in contract["functions"].values())):
        raise ValueError("invalid tool execution contract")
    names = []
    descriptor_fields = {"name", "input_schema", "result_contract", "permissions", "side_effect",
                         "requires_approval", "implementation"}
    for item in identity["tools"]:
        if (not isinstance(item, dict) or set(item) not in (descriptor_fields, descriptor_fields | {"recovery_identity"})
                or not isinstance(item["name"], str) or not item["name"].strip()
                or not isinstance(item["permissions"], list)
                or any(not isinstance(value, str) for value in item["permissions"])
                or item["permissions"] != sorted(set(item["permissions"]))
                or not isinstance(item["result_contract"], str) or item["result_contract"] not in {"numeric", "json"}
                or type(item["side_effect"]) is not bool or type(item["requires_approval"]) is not bool):
            raise ValueError("invalid canonical tool descriptor")
        if "recovery_identity" in item and (
                not isinstance(item["recovery_identity"], str) or not item["recovery_identity"].strip()
                or len(item["recovery_identity"]) > 128):
            raise ValueError("invalid tool recovery identity")
        schema = item["input_schema"]
        if not isinstance(schema, dict) or schema.get("type") != "object" or _canonical(schema) != _canonical(canonical_schema(schema)):
            raise ValueError("tool schema is not canonical")
        implementation = item["implementation"]
        if (not isinstance(implementation, dict)
                or set(implementation) != {"version", "handler_sha256", "dependencies", "constants_sha256"}
                or not isinstance(implementation["version"], str) or not implementation["version"].strip()
                or len(implementation["version"]) > 128
                or not _digest(implementation["handler_sha256"]) or not _digest(implementation["constants_sha256"])
                or not isinstance(implementation["dependencies"], dict)
                or any(not isinstance(name, str) or not name.strip() or len(name) > 512 or not _digest(value)
                       for name, value in implementation["dependencies"].items())):
            raise ValueError("invalid tool implementation identity")
        names.append(item["name"])
    if names != sorted(set(names)) or identity["sha256"] != _hash({"algorithm": ALGORITHM, "execution_contract": contract,
                                                                "tools": identity["tools"]}):
        raise ValueError("tool identity is unordered, duplicated, or has an invalid hash")


def identity_difference(current, previous):
    """Explain comparison gates using field labels, never schema/source values."""
    before = {item["name"]: item for item in previous["tools"]}
    after = {item["name"]: item for item in current["tools"]}
    label = lambda name: name if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) else "<nonstandard tool name>"
    differences = []
    for category, names in (("added", set(after) - set(before)), ("removed", set(before) - set(after))):
        if names:
            differences.append(category + "=" + ",".join(label(name) for name in sorted(names)[:10]))
    for name in sorted(set(before) & set(after)):
        fields = ["schema" if key == "input_schema" else key for key in (
            "input_schema", "permissions", "side_effect", "requires_approval", "result_contract", "recovery_identity")
            if _canonical(before[name].get(key)) != _canonical(after[name].get(key))]
        fields.extend("implementation." + key.removesuffix("_sha256") for key in (
            "version", "handler_sha256", "dependencies", "constants_sha256")
            if before[name]["implementation"][key] != after[name]["implementation"][key])
        if fields:
            differences.append("changed " + label(name) + "(" + ",".join(fields) + ")")
    if current["execution_contract"] != previous["execution_contract"]:
        differences.append("execution_contract changed")
    return "; ".join(differences[:20]) or "canonical tool contract changed"
