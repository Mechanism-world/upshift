"""JSON Schema transformations upshift performs on an agent's own schemas.

Two of them, both driven by a rescue-ops case, both deliberately narrow: upshift edits an
agent's schemas only where a documented provider rule or a documented loss of meaning says it
must, and never "improves" one.

* `canonicalise_nullable` — a property that MAY be null keeps a null branch. `ghisdk-052`:
  `recall_from_scratchpad(key: str | None = None)` reached the model as `{"type": "string"}`
  because the generator dropped `None` from the union ("optionality is carried by `required`,
  not the type"). In 2 of 5 reps the model wanted to say "return everything", could not
  express it, and sent `""` — which the tool read as an ordinary lookup and answered
  `not_found`. The model then told the user nothing was stored, incorrectly. Optionality and
  nullability are different statements and upshift keeps both.

* `make_strict` / `is_strict` — OpenAI's strict structured-output subset, applied to ONE named
  schema by the `schema-strict-compat` repair candidate. The rules are OpenAI's, quoted at
  `make_strict`: <https://platform.openai.com/docs/guides/structured-outputs> ("all fields
  must be required", "additionalProperties: false"). Making a schema strict CHANGES it — an
  optional property becomes required-and-nullable — which is why the repair that uses this
  carries a disclosure.

Everything here is pure and copies its input: an agent's schema on disk is never mutated as a
side effect of being inspected.
"""

from __future__ import annotations

import copy
from typing import Any

NULL = {"type": "null"}

#: Keys whose value is a single subschema.
_SUBSCHEMA_KEYS = ("items", "additionalItems", "contains", "not", "if", "then", "else")
#: Keys whose value is a mapping of name -> subschema.
_SUBSCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
#: Keys whose value is a list of subschemas.
_SUBSCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")


def _is_schema(value: Any) -> bool:
    return isinstance(value, dict)


def _walk(schema: dict[str, Any], transform) -> dict[str, Any]:
    """Apply `transform` to `schema` and to every subschema under it, bottom-up."""
    out = dict(schema)
    for key in _SUBSCHEMA_KEYS:
        if _is_schema(out.get(key)):
            out[key] = _walk(out[key], transform)
    for key in _SUBSCHEMA_MAPS:
        node = out.get(key)
        if isinstance(node, dict):
            out[key] = {
                name: _walk(sub, transform) if _is_schema(sub) else sub
                for name, sub in node.items()
            }
    for key in _SUBSCHEMA_LISTS:
        node = out.get(key)
        if isinstance(node, list):
            out[key] = [_walk(sub, transform) if _is_schema(sub) else sub for sub in node]
    return transform(out)


def is_nullable(schema: Any) -> bool:
    """True when this schema already admits `null`, in any of the three spellings."""
    if not _is_schema(schema):
        return False
    declared = schema.get("type")
    if declared == "null" or (isinstance(declared, list) and "null" in declared):
        return True
    return any(
        _is_schema(branch) and branch.get("type") == "null"
        for branch in schema.get("anyOf") or schema.get("oneOf") or []
    )


def _canonicalise_one(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("nullable") is True:
        rest = {k: v for k, v in schema.items() if k != "nullable"}
        return {"anyOf": [rest, dict(NULL)]} if not is_nullable(rest) else rest
    declared = schema.get("type")
    if isinstance(declared, list) and "null" in declared:
        others = [t for t in declared if t != "null"]
        rest = {k: v for k, v in schema.items() if k != "type"}
        if len(others) == 1 and not rest:
            return {"anyOf": [{"type": others[0]}, dict(NULL)]}
        branches: list[dict[str, Any]] = [{**rest, "type": t} for t in others]
        return {"anyOf": [*branches, dict(NULL)]}
    return schema


def canonicalise_nullable(schema: Any) -> Any:
    """Every "may be null" spelling rewritten as `anyOf: [<the type>, {"type": "null"}]`.

    The three spellings upshift meets are OpenAPI 3.0's `"nullable": true`, JSON Schema's
    `"type": ["string", "null"]`, and the `anyOf` form pydantic emits for `str | None`. They
    mean the same thing to a model only if they all reach it; `anyOf` is the one every
    provider's structured-output subset documents, so it is the canonical one here.

    Not a validator and not a linter: a schema that says nothing about null is returned
    unchanged, and nothing else about it is touched.
    """
    if not _is_schema(schema):
        return schema
    return _walk(copy.deepcopy(schema), _canonicalise_one)


# ---------------------------------------------------------------------------
# OpenAI strict structured-output subset
# ---------------------------------------------------------------------------


def _object_violations(schema: dict[str, Any]) -> list[str]:
    if schema.get("type") != "object":
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    problems = []
    if schema.get("additionalProperties") is not False:
        problems.append("additionalProperties")
    required = schema.get("required")
    if not isinstance(required, list) or set(required) != set(properties):
        problems.append("required")
    return problems


def strict_violations(schema: Any) -> list[str]:
    """The strict-subset rules this schema breaks, as the API's own words for them.

    Empty means the schema is already strict, and a repair candidate that would "fix" it is
    not generated: emitting a patch that changes nothing wastes a screen run and, worse,
    tells a reader the schema was the problem.
    """
    if not _is_schema(schema):
        return []
    found: list[str] = []

    def collect(node: dict[str, Any]) -> dict[str, Any]:
        found.extend(_object_violations(node))
        return node

    _walk(copy.deepcopy(schema), collect)
    return sorted(set(found))


def is_strict(schema: Any) -> bool:
    return not strict_violations(schema)


def make_strict(schema: Any) -> Any:
    """`schema` rewritten to OpenAI's strict structured-output subset.

    Three documented rules (<https://platform.openai.com/docs/guides/structured-outputs>,
    "Supported schemas" / "All fields must be required"), applied to every object in the
    schema:

    1. every object carries `additionalProperties: false`;
    2. every property name appears in that object's `required` list;
    3. a property that was NOT required becomes nullable — `anyOf: [<it>, {"type": "null"}]` —
       because rule 2 removes the only way the model had to omit it. This is the step that
       CHANGES the agent: downstream code that read a missing key must now accept an explicit
       null, which is why the repair candidate built on this function is disclosed.

    Nullability spellings are canonicalised on the way through, so a property that was already
    `str | None` is not wrapped twice.
    """
    if not _is_schema(schema):
        return schema

    def strictify(node: dict[str, Any]) -> dict[str, Any]:
        if node.get("type") != "object" or not isinstance(node.get("properties"), dict):
            return node
        properties = dict(node["properties"])
        previously_required = set(node.get("required") or [])
        for name, sub in properties.items():
            if name in previously_required or not _is_schema(sub):
                continue
            # Optional -> required-and-nullable. Rule 3.
            properties[name] = sub if is_nullable(sub) else {"anyOf": [sub, dict(NULL)]}
        node["properties"] = properties
        node["required"] = list(properties)
        node["additionalProperties"] = False
        return node

    return _walk(canonicalise_nullable(schema), strictify)
