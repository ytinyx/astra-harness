"""Build an existing Tool from a trusted, explicitly authorized Python function."""

import inspect
import sys
from typing import Literal, get_args, get_origin, get_type_hints

from .tools import Tool, validate


def _schema(annotation, depth=1):
    if depth > 16:
        raise ValueError("tool parameter type exceeds schema nesting limit")
    for python_type, kind in ((str, "string"), (int, "integer"), (float, "number"),
                              (bool, "boolean"), (type(None), "null"), (None, "null")):
        if annotation is python_type:
            return {"type": kind}
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin is list and len(arguments) == 1:
        return {"type": "array", "items": _schema(arguments[0], depth + 1)}
    if origin is Literal and arguments:
        kind = type(arguments[0])
        if kind not in (str, int, bool, type(None)) or any(type(item) is not kind for item in arguments):
            raise ValueError("Literal requires values of one supported scalar type")
        schema = _schema(kind, depth)
        if not all(validate(item, schema) for item in arguments):
            raise ValueError("Literal contains an invalid JSON value")
        return {**schema, "enum": list(arguments)}
    raise ValueError("unsupported tool parameter type; use a scalar, list[T], or homogeneous Literal")


def _signature_and_hints(function):
    options = {"follow_wrapped": False, "eval_str": False}
    if sys.version_info >= (3, 14):
        # Keep evaluated type objects intact while allowing an unresolved return.
        from annotationlib import Format
        options["annotation_format"] = Format.FORWARDREF
    signature = inspect.signature(function, **options)

    def input_hints():
        pass

    # get_type_hints is the standard resolver, including future annotations.
    # Only inputs form the tool schema; the existing executor gates JSON results.
    input_hints.__annotations__ = {name: parameter.annotation for name, parameter in signature.parameters.items()
                                   if parameter.annotation is not inspect.Parameter.empty}
    try:
        hints = get_type_hints(input_hints, globalns=function.__globals__, include_extras=True)
    except Exception:
        raise ValueError("cannot resolve tool parameter annotations") from None
    return signature, hints


def tool_from_function(function, *, permissions, side_effect, requires_approval, implementation_version,
                       name=None, description=None) -> Tool:
    """Describe a trusted named sync/async function without wrapping or executing it.

    All policy fields and the implementation version are explicit. Input types:
    str/int/float/bool/None, list[T], and homogeneous Literal[str/int/bool/None].
    Only matching immutable JSON scalar defaults are accepted; Python supplies
    omitted defaults. JSON numbers retain their original int/float type.

    Standard annotation resolution can execute Python expressions: registration
    is for trusted host code, never model-supplied code or annotation strings.
    Return annotations are not an output schema. Existing JSON result limits,
    permission/approval checks, and benchmark identity restrictions still apply.
    """
    if (not inspect.isfunction(function) or function.__name__ == "<lambda>"
            or inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function)):
        raise ValueError("tool requires a named Python function returning one result")
    if hasattr(function, "__signature__"):
        raise ValueError("custom function signatures are not supported")
    if not isinstance(implementation_version, str) or not implementation_version.strip():
        raise ValueError("tool requires an explicit implementation version")
    signature, hints = _signature_and_hints(function)
    properties, required = {}, []
    for parameter in signature.parameters.values():
        if parameter.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            raise ValueError("tool parameters must accept named arguments; positional-only and variadic inputs are unsupported")
        if parameter.name not in hints:
            raise ValueError("every tool parameter requires a supported type annotation")
        schema = _schema(hints[parameter.name])
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
        else:
            default = parameter.default
            if type(default) not in (str, int, float, bool, type(None)):
                raise ValueError("tool defaults must be immutable JSON scalars")
            if not validate(default, schema):
                raise ValueError("tool default does not match its parameter schema")
            schema["default"] = default
        properties[parameter.name] = schema
    return Tool(name=function.__name__ if name is None else name,
                description=(inspect.getdoc(function) or function.__name__) if description is None else description,
                parameters=tuple(properties), handler=function,
                input_schema={"type": "object", "properties": properties, "required": required,
                              "additionalProperties": False},
                permissions=permissions, side_effect=side_effect, requires_approval=requires_approval,
                implementation_version=implementation_version)
