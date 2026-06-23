"""Schema profile system with per-provider normalization for tool schemas.

Each provider (Anthropic, Gemini, OpenAI, Grok/xAI) has a ``SchemaProfile``
that controls which normalisation transforms are applied.  The top-level
``normalize_schema`` function reads the registry and applies transforms in a
consistent order.  ``json_schema_to_openai`` is a convenience wrapper that
produces a fully OpenAI-compatible schema from any input.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict


# ---------------------------------------------------------------------------
# SchemaProfile
# ---------------------------------------------------------------------------


@dataclass
class SchemaProfile:
    """Normalisation flags for a provider's schema dialect.

    Attributes:
        resolve_refs:
            Resolve ``$ref`` pointers by looking up ``$defs`` /
            ``definitions``.
        flatten_unions:
            Flatten nested ``oneOf`` / ``anyOf`` lists into a single
            level.
        convert_const_to_enum:
            Replace ``const: X`` with ``enum: [X]``.
        strict_mode:
            Make all properties required and set
            ``additionalProperties: false``.
        strip_keys:
            Keys to remove from every schema node (e.g. ``minLength``,
            ``maxLength``).
        inject_type:
            Add ``"type": "object"`` to object schemas that are missing
            an explicit ``type``.
    """

    resolve_refs: bool = False
    flatten_unions: bool = False
    convert_const_to_enum: bool = False
    strict_mode: bool = False
    strip_keys: list[str] = field(default_factory=list)
    inject_type: bool = False


# ---------------------------------------------------------------------------
# Normalization registry
# ---------------------------------------------------------------------------

NORMALIZATION_REGISTRY: Dict[str, SchemaProfile] = {
    "anthropic": SchemaProfile(
        resolve_refs=True,
    ),
    "gemini": SchemaProfile(
        resolve_refs=True,
        flatten_unions=True,
        convert_const_to_enum=True,
        strip_keys=["minLength", "maxLength"],
    ),
    "openai": SchemaProfile(
        strict_mode=True,
    ),
    "grok/xai": SchemaProfile(
        resolve_refs=True,
        flatten_unions=True,
        inject_type=True,
        strip_keys=["minLength", "maxLength"],
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_schema(schema: dict, provider: str) -> dict:
    """Apply the normalization profile for *provider* to *schema*.

    Transforms are applied in a fixed order: ``$ref`` resolution, union
    flattening, const-to-enum conversion, key stripping, strict mode,
    and type injection.

    Args:
        schema: The JSON Schema dict to normalise.
        provider: A key in ``NORMALIZATION_REGISTRY`` (e.g. ``"openai"``).

    Returns:
        A new schema dict (the input is not mutated).
    """
    profile = NORMALIZATION_REGISTRY.get(provider)
    if profile is None:
        return copy.deepcopy(schema)

    result = copy.deepcopy(schema)

    if profile.resolve_refs:
        result = _resolve_schema_refs(result)
    if profile.flatten_unions:
        result = _flatten_schema_unions(result)
    if profile.convert_const_to_enum:
        result = _convert_const_to_enum(result)
    if profile.strip_keys:
        result = _strip_schema_keys(result, profile.strip_keys)
    if profile.strict_mode:
        result = _apply_strict_mode(result)
    if profile.inject_type:
        result = _inject_type(result)

    return result


def json_schema_to_openai(schema: dict) -> dict:
    """Convert any JSON Schema to an OpenAI-compatible format.

    OpenAI tool-calling expects a strict subset of JSON Schema:

    * All ``$ref`` pointers resolved so the schema is self-contained.
    * Nested ``oneOf`` / ``anyOf`` flattened.
    * ``additionalProperties: false`` and all properties listed in
      ``required`` (strict mode).
    * A top-level ``type: "object"`` with a ``properties`` map.

    Args:
        schema: Any valid JSON Schema dict.

    Returns:
        A new schema dict in OpenAI-compatible form.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    result = copy.deepcopy(schema)
    result = _resolve_schema_refs(result)
    result = _flatten_schema_unions(result)
    result = _apply_strict_mode(result)

    # Ensure top-level is a proper object schema.
    if result.get("type") != "object":
        result["type"] = "object"
    result.setdefault("properties", {})
    result["additionalProperties"] = False

    return result


# ---------------------------------------------------------------------------
# Helper functions  (all return new dicts, input is never mutated)
# ---------------------------------------------------------------------------


def _resolve_schema_refs(schema: dict) -> dict:
    """Resolve all ``$ref`` pointers in *schema*.

    Definition maps are collected from the ``$defs`` and ``definitions``
    keys of the top-level schema, including their full path prefix so
    that refs like ``#/$defs/User`` resolve correctly.  Circular
    references are left unresolved to prevent infinite recursion.
    """
    # Build a flat path-to-value lookup from $defs / definitions.
    ref_map: dict[str, Any] = {}

    def _collect(path: str, node: Any) -> None:
        if isinstance(node, dict):
            ref_map[path] = node
            for key, val in node.items():
                _collect(f"{path}/{key}", val)
        elif isinstance(node, list):
            for i, val in enumerate(node):
                _collect(f"{path}/{i}", val)

    containers = ["$defs", "definitions"]
    for key in containers:
        raw = schema.get(key)
        if isinstance(raw, dict):
            _collect(f"#/{key}", raw)

    def _resolve_value(value: Any) -> Any:
        if isinstance(value, dict):
            return _resolve_node(value)
        if isinstance(value, list):
            return [_resolve_value(item) for item in value]
        return value

    def _resolve_node(node: dict) -> dict:
        ref = node.get("$ref")
        if isinstance(ref, str) and ref in ref_map:
            resolved = copy.deepcopy(ref_map[ref])
            # Merge sibling keys with resolved definition content.
            merged: dict = {k: _resolve_value(v) for k, v in resolved.items()}
            for k, v in node.items():
                if k != "$ref":
                    merged[k] = _resolve_value(v)
            return merged
        return {k: _resolve_value(v) for k, v in node.items()}

    return _resolve_node(copy.deepcopy(schema))


def _flatten_schema_unions(schema: dict) -> dict:
    """Flatten nested ``oneOf`` / ``anyOf`` lists.

    An inner ``oneOf`` appearing inside an outer ``oneOf`` is hoisted
    to the outer list.  The same applies to ``anyOf``.
    """
    def _flatten_node(node: dict) -> dict:
        result: dict = {}
        for key, value in node.items():
            if isinstance(value, dict):
                result[key] = _flatten_node(value)
            elif isinstance(value, list):
                result[key] = [
                    _flatten_node(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value

        for union_key in ("oneOf", "anyOf"):
            items = result.get(union_key)
            if not isinstance(items, list):
                continue
            flattened: list[Any] = []
            for item in items:
                if isinstance(item, dict) and union_key in item:
                    inner = item[union_key]
                    if isinstance(inner, list):
                        flattened.extend(inner)
                    else:
                        flattened.append(_flatten_node(item))
                else:
                    flattened.append(_flatten_node(item) if isinstance(item, dict) else item)
            result[union_key] = flattened

        return result

    return _flatten_node(copy.deepcopy(schema))


def _convert_const_to_enum(schema: dict) -> dict:
    """Replace every ``const: X`` with ``enum: [X]`` throughout *schema*.

    This is helpful for providers that do not support the ``const``
    keyword (e.g. Gemini).
    """
    def _convert_node(node: dict) -> dict:
        result: dict = {}
        for key, value in node.items():
            if isinstance(value, dict):
                result[key] = _convert_node(value)
            elif isinstance(value, list):
                result[key] = [
                    _convert_node(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value

        if "const" in result:
            result["enum"] = [result.pop("const")]

        return result

    return _convert_node(copy.deepcopy(schema))


def _apply_strict_mode(schema: dict) -> dict:
    """Make *schema* strict.

    Declares all properties required and forbids additional properties.
    Applied to every node that has a ``properties`` map (or is of type
    ``object``).
    """
    def _apply_node(node: dict) -> dict:
        result: dict = {}
        for key, value in node.items():
            if isinstance(value, dict):
                result[key] = _apply_node(value)
            elif isinstance(value, list):
                result[key] = [
                    _apply_node(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value

        is_object = result.get("type") == "object" or "properties" in result
        if is_object and "$ref" not in result:
            props = result.get("properties")
            if isinstance(props, dict):
                result["required"] = list(props.keys())
            result["additionalProperties"] = False

        return result

    return _apply_node(copy.deepcopy(schema))


def _strip_schema_keys(schema: dict, keys: list[str]) -> dict:
    """Remove all occurrences of *keys* from every node in *schema*.

    Used to drop keywords that a provider's schema parser rejects
    (e.g. ``minLength``, ``maxLength`` for Gemini).
    """
    if not keys:
        return copy.deepcopy(schema)

    keys_set = frozenset(keys)

    def _strip_node(node: dict) -> dict:
        result: dict = {}
        for key, value in node.items():
            if key in keys_set:
                continue
            if isinstance(value, dict):
                result[key] = _strip_node(value)
            elif isinstance(value, list):
                result[key] = [
                    _strip_node(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value
        return result

    return _strip_node(copy.deepcopy(schema))


def _inject_type(schema: dict) -> dict:
    """Add ``"type": "object"`` to schemas that have ``properties`` but
    no explicit ``type``.

    Some providers (e.g. xAI / Grok) require ``type`` on every object
    schema; this fills in the missing value.
    """
    def _inject_node(node: dict) -> dict:
        result: dict = {}
        for key, value in node.items():
            if isinstance(value, dict):
                result[key] = _inject_node(value)
            elif isinstance(value, list):
                result[key] = [
                    _inject_node(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                result[key] = value

        if "properties" in result and "type" not in result:
            result["type"] = "object"

        return result

    return _inject_node(copy.deepcopy(schema))
