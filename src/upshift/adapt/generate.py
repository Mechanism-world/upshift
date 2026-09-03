"""Stage 4: write the five files (plus PROVENANCE.json) from the verified extraction.

Rules this module obeys, in order:

* Never fabricate. Anything the gate downgraded is written as a TODO with a pointer, not as
  a plausible value.
* `system_prompt.txt` and `tools.json` are sent to the model verbatim, so they carry NO
  provenance comments — a comment there would change the agent under test. Their citations
  live in `PROVENANCE.json` and in ADAPT_REPORT.md. `backend.py` is ours, so it carries the
  file:line provenance inline.
* `backend.py` re-implements a tool only when the extraction described mechanical semantics
  (a lookup, a list, an append with an issued id, a field update, a fixture-file read or
  write) and the citation for it resolves. Everything else is a stub returning
  `{"error": "TODO(adapt): not implemented — <reason>"}`, which still satisfies the
  never-raises contract in ADAPTER.md.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from upshift.adapt.extract import ALLOWED_CHECK_TYPES, DICT_PARAMS, ENDPOINT_VALUES
from upshift.adapt.verify import Verification

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_MAX_TURNS = 12
DEFAULT_ENDPOINT = "chat_completions"

#: Request parameters that would break the eval loop or the recorder if passed through.
#: `system` joins `instructions`/`messages`/`input` here: on the Messages API the system
#: prompt is a request field, and upshift owns it (it lives in system_prompt.txt).
BLOCKED_PARAMS = frozenset(
    {"stream", "stream_options", "messages", "input", "model", "tools", "functions",
     "function_call", "n", "response_format", "store", "instructions", "system"}
)

#: Tool names that look like retrieval. The generated `tool_called` check for such a tool
#: carries `"retrieval": true` — inert for pass/fail (checks.py ignores unknown keys), read
#: by the differ's `reduced_retrieval_calls` signature (DESIGN.md v0.3).
RETRIEVAL_NAME_RE = re.compile(r"search|retriev|lookup|query|fetch|find", re.IGNORECASE)

MECHANICAL_KINDS = frozenset({"lookup", "list", "create", "update", "file_read", "file_write"})

PLACEHOLDER_TOOL_NAME = "TODO_adapt_undetermined_tool"
PLACEHOLDER_PROMPT = (
    "TODO(adapt): no system prompt could be traced to this repository's source. "
    "Paste the agent's real system message here before running upshift."
)


@dataclass
class MustReview:
    file: str  # path relative to the generated agent dir
    lines: str  # "12" or "12-40" or "-" when the whole file is the concern
    reason: str


@dataclass
class GenerationResult:
    out_dir: Path
    files: list[str] = field(default_factory=list)
    must_review: list[MustReview] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    implemented_tools: list[str] = field(default_factory=list)
    stub_tools: list[str] = field(default_factory=list)
    case_ids: list[str] = field(default_factory=list)
    dropped_cases: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def slugify(text: str, fallback: str = "adapted-agent") -> str:
    """Lowercase slug. Underscores survive: case ids seed runs and name directories on disk,
    and `todo_add_one` should stay `todo_add_one`."""
    slug = re.sub(r"[^a-z0-9_]+", "-", str(text or "").lower()).strip("-_")
    return slug or fallback


#: Template placeholders, so a value substituted earlier cannot contain a later one.
_PLACEHOLDERS = ("__ORIGIN__", "__COMMIT__", "__PROVENANCE__", "__TOOL_SPECS__")


def docstring_safe(text: str) -> str:
    """Text that is safe to paste inside a `'''...'''` module docstring.

    Everything interpolated into `backend.py`'s header comes from outside: the origin is a
    CLI argument, the provenance lines carry model-written tool names and citations, and the
    model read a repository that may have been trying to get code into this file. A `'''`
    (or a trailing backslash, or a stray placeholder) in any of them would end the docstring
    and turn whatever follows into statements in a file `upshift upgrade` imports and runs.

    Quotes are neutralised rather than dropped so the text stays readable, and control
    characters other than tab go, so nothing can hide from a reviewer's eye.
    """
    out = str(text or "")
    out = out.replace("\\", "/")  # no trailing backslash can escape the closing quotes
    out = out.replace("'''", "'’'").replace('"""', '"”"')
    for placeholder in _PLACEHOLDERS:
        out = out.replace(placeholder, placeholder.replace("__", "_ _"))
    out = "".join(ch if ch == "\t" or ch >= " " else " " for ch in out.replace("\n", " "))
    return out


def _locate(text: str, needle: str) -> str:
    """1-based line of the first line containing `needle`, as a string, else '-'."""
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return str(index)
    return "-"


def _write(out_dir: Path, rel: str, content: str, result: GenerationResult) -> str:
    path = out_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.files.append(rel)
    return content


# ---------------------------------------------------------------------------
# system_prompt.txt
# ---------------------------------------------------------------------------


def build_system_prompt(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The assembled prompt plus one provenance entry per chunk, with its line span.

    Chunks are joined with a single newline, not a blank line: the common case is one source
    literal per prompt line (`"line one\\n" "line two\\n"`), and inserting paragraph breaks
    there would change the prompt the agent under test actually sends. A chunk that needs a
    blank line after it carries it in its own text.

    Text that the gate could not find ANYWHERE in the source repo is omitted rather than
    written into the prompt: an invented rule in a system prompt silently changes the agent
    under test, which is the exact failure this feature must not have. The omission is
    reported with the text, so a human can put it back if the extraction was right and the
    citation merely wrong.
    """
    chunks = (data.get("system_prompt") or {}).get("chunks") or []
    pieces: list[str] = []
    provenance: list[dict[str, Any]] = []
    line = 1
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        if not text:
            continue
        omitted = bool(chunk.get("omitted"))
        span = text.rstrip("\n").count("\n") + 1
        provenance.append(
            {
                "lines": "-" if omitted
                else (f"{line}-{line + span - 1}" if span > 1 else str(line)),
                "citation": chunk.get("citation", ""),
                "kind": chunk.get("kind", "inferred"),
                "verified": bool(chunk.get("verified")),
                "omitted": omitted,
                "text": text if omitted else "",
                "detail": chunk.get("verify_detail", ""),
                "note": chunk.get("note", ""),
            }
        )
        if omitted:
            continue
        pieces.append(text)
        line += span
    if not pieces:
        return PLACEHOLDER_PROMPT + "\n", provenance
    return "\n".join(piece.rstrip("\n") for piece in pieces).rstrip() + "\n", provenance


# ---------------------------------------------------------------------------
# tools.json
# ---------------------------------------------------------------------------


def build_tools(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tools: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for tool in data.get("tools") or []:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        parameters = tool.get("parameters")
        if not isinstance(parameters, dict):
            # Anthropic tools are {name, description, input_schema}. tools.json is chat-style
            # whatever the endpoint is (DESIGN.md: the loop converts parameters ->
            # input_schema on the wire), so an extraction that reported the wire name is
            # normalised here rather than written out in a shape upshift cannot load.
            raw_schema = tool.get("input_schema")
            parameters = (
                raw_schema if isinstance(raw_schema, dict)
                else {"type": "object", "properties": {}, "required": []}
            )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": parameters,
                },
            }
        )
        provenance.append(
            {
                "tool": name,
                "citation": tool.get("citation", ""),
                "kind": tool.get("kind", "inferred"),
                "verified": bool(tool.get("verified")),
                "verify": tool.get("verify", {}),
            }
        )
    if not tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": PLACEHOLDER_TOOL_NAME,
                    "description": (
                        "TODO(adapt): no tool schema could be traced to this repository's "
                        "source. Replace this placeholder with the agent's real tools "
                        "before running upshift."
                    ),
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }
        )
        provenance.append({"tool": PLACEHOLDER_TOOL_NAME, "citation": "", "kind": "inferred",
                           "verified": False, "verify": {}})
    return tools, provenance


# ---------------------------------------------------------------------------
# backend.py
# ---------------------------------------------------------------------------

BACKEND_TEMPLATE = '''"""Deterministic tool backend generated by `upshift adapt`.

Source: __ORIGIN__
Commit: __COMMIT__

REVIEW BEFORE USE. Every implemented tool below was re-implemented from a *description* of
the cited source, not from the source itself — upshift verifies citations mechanically, but
nothing here proves the semantics match upstream. Tools whose behaviour was not mechanically
clear are stubs returning {"error": "TODO(adapt): not implemented — ..."}, which is a valid
tool result under the ADAPTER.md contract (execute never raises).

The backend is a pure function of `initial_state` and the sequence of `execute` calls: no
clock, no network, no randomness (ADAPTER.md requirement 3).

Provenance (tool <- file:line):
__PROVENANCE__
"""

from __future__ import annotations

import copy
from typing import Any

TOOL_SPECS: dict[str, dict[str, Any]] = __TOOL_SPECS__


class Backend:
    """One episode's state. Construct via :func:`create_backend`."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        state = copy.deepcopy(initial_state or {})
        self._state: dict[str, Any] = state if isinstance(state, dict) else {}

    # -- ADAPTER.md contract ------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            spec = TOOL_SPECS.get(name)
            if spec is None:
                return {"error": f"unknown tool: {name}"}
            if not isinstance(arguments, dict):
                return {"error": f"arguments must be an object, got {type(arguments).__name__}"}
            handler = getattr(self, "_" + str(spec.get("kind", "unclear")), None)
            if handler is None:
                return self._unclear(spec, arguments)
            return handler(spec, arguments)
        except Exception as exc:  # noqa: BLE001 - defensive: execute never raises
            return {"error": f"backend failure in {name}: {exc}"}

    def state(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    # -- helpers ------------------------------------------------------------

    def _entries(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        key = str(spec.get("state_key") or "items")
        bucket = self._state.get(key)
        if not isinstance(bucket, list):
            bucket = list(bucket.values()) if isinstance(bucket, dict) else []
            self._state[key] = bucket
        return bucket

    def _files(self) -> dict[str, Any]:
        files = self._state.get("files")
        if not isinstance(files, dict):
            files = {}
            self._state["files"] = files
        return files

    @staticmethod
    def _arg(spec: dict[str, Any], key: str, default: str) -> str:
        value = spec.get(key)
        return str(value) if isinstance(value, str) and value else default

    # -- tool kinds ---------------------------------------------------------

    def _unclear(self, spec: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        return {"error": "TODO(adapt): not implemented — " + str(spec.get("todo") or "the "
                "source did not make this tool's semantics mechanically clear")}

    def _list(self, spec: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        return {"results": copy.deepcopy(self._entries(spec))}

    def _lookup(self, spec: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        fields = [f for f in (spec.get("match_fields") or []) if f in arguments]
        results = [
            entry
            for entry in self._entries(spec)
            if all(str(entry.get(f, "")).lower() == str(arguments[f]).lower() for f in fields)
        ]
        return {"results": copy.deepcopy(results)}

    def _create(self, spec: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        entries = self._entries(spec)
        entry: dict[str, Any] = {k: v for k, v in arguments.items()}
        id_field = self._arg(spec, "id_field", "")
        if id_field:
            prefix = self._arg(spec, "id_prefix", "ID-")
            entry[id_field] = f"{prefix}{1000 + len(entries) + 1}"
        entries.append(entry)
        return copy.deepcopy(entry)

    def _update(self, spec: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        id_field = self._arg(spec, "id_field", "id")
        wanted = arguments.get(id_field)
        if wanted is None:
            return {"error": f"missing required argument: {id_field}"}
        for entry in self._entries(spec):
            if str(entry.get(id_field)) == str(wanted):
                entry.update({k: v for k, v in arguments.items() if k != id_field})
                return copy.deepcopy(entry)
        return {"error": f"no entry with {id_field}={wanted!r}"}

    def _file_read(self, spec: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        path_field = self._arg(spec, "path_field", "path")
        path = arguments.get(path_field)
        if not isinstance(path, str) or not path:
            return {"error": f"missing required argument: {path_field}"}
        files = self._files()
        if path not in files:
            return {"error": f"no such file: {path}"}
        return {"path": path, "content": files[path]}

    def _file_write(self, spec: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        path_field = self._arg(spec, "path_field", "path")
        text_field = self._arg(spec, "text_field", "content")
        path = arguments.get(path_field)
        if not isinstance(path, str) or not path:
            return {"error": f"missing required argument: {path_field}"}
        content = arguments.get(text_field)
        if content is None:
            return {"error": f"missing required argument: {text_field}"}
        self._files()[path] = str(content)
        return {"path": path, "written": True, "bytes": len(str(content))}


def create_backend(initial_state: dict[str, Any]) -> Backend:
    """Build a backend seeded from a case's ``initial_state`` (ADAPTER.md)."""
    return Backend(initial_state)
'''


def build_backend(
    data: dict[str, Any], origin: str, commit: str | None
) -> tuple[str, list[str], list[str], list[dict[str, Any]]]:
    """(source, implemented tool names, stub tool names, provenance rows)."""
    specs: dict[str, Any] = {}
    provenance_lines: list[str] = []
    provenance: list[dict[str, Any]] = []
    implemented: list[str] = []
    stubs: list[str] = []

    tools = data.get("tools") or []
    if not tools:
        specs[PLACEHOLDER_TOOL_NAME] = {
            "kind": "unclear",
            "todo": "no tool was extracted from the source",
        }
        stubs.append(PLACEHOLDER_TOOL_NAME)
        provenance_lines.append(f"  {PLACEHOLDER_TOOL_NAME} <- (nothing extracted)")

    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        backend = tool.get("backend") or {}
        kind = str(backend.get("kind") or "unclear")
        citation = str(backend.get("citation") or tool.get("citation") or "")
        reason = str(backend.get("reason") or "")
        if kind in MECHANICAL_KINDS:
            match_fields = [str(f) for f in (backend.get("match_fields") or [])]
            spec: dict[str, Any] = {"kind": kind, "citation": citation}
            if kind in ("file_read", "file_write"):
                spec["path_field"] = match_fields[0] if match_fields else "path"
                if kind == "file_write":
                    spec["text_field"] = str(backend.get("text_field") or "content")
            else:
                spec["state_key"] = str(backend.get("state_key") or "items")
                if kind == "lookup":
                    spec["match_fields"] = match_fields
                if kind in ("create", "update"):
                    spec["id_field"] = str(backend.get("id_field") or "id")
                if kind == "create":
                    spec["id_prefix"] = str(backend.get("id_prefix") or "ID-")
            implemented.append(name)
            provenance_lines.append(f"  {name} <- {citation or '(uncited)'}  [{kind}] REVIEW")
        else:
            spec = {
                "kind": "unclear",
                "todo": reason or f"{name}'s semantics were not mechanically clear in the source",
                "citation": citation,
            }
            stubs.append(name)
            provenance_lines.append(f"  {name} <- {citation or '(uncited)'}  [stub: TODO]")
        specs[name] = spec
        provenance.append({"tool": name, "kind": spec["kind"], "citation": citation,
                           "reason": reason})

    source = (
        BACKEND_TEMPLATE.replace("__ORIGIN__", docstring_safe(origin))
        .replace("__COMMIT__", docstring_safe(commit or "(not a git checkout)"))
        .replace(
            "__PROVENANCE__",
            "\n".join(docstring_safe(line) for line in provenance_lines) or "  (none)",
        )
        # json.dumps, never raw: every string here is escaped, so a tool name or citation
        # cannot close the literal it sits in.
        .replace("__TOOL_SPECS__", json.dumps(specs, indent=4, sort_keys=True))
    )
    if not _parses(source):
        # Belt and braces: `docstring_safe` is what makes this impossible, so reaching here
        # is a bug in it. Emit a file that is still valid Python rather than one that runs
        # something the target repository chose.
        source = (
            BACKEND_TEMPLATE.replace("__ORIGIN__", "(omitted: unrenderable)")
            .replace("__COMMIT__", "(omitted: unrenderable)")
            .replace("__PROVENANCE__", "  (omitted: unrenderable — see PROVENANCE.json)")
            .replace("__TOOL_SPECS__", json.dumps(specs, indent=4, sort_keys=True))
        )
    return source, implemented, stubs, provenance


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# cases/cases.json
# ---------------------------------------------------------------------------


# Exact parameter vocabulary of checks.py, plus the synonyms extraction models actually
# emit. Anything that survives normalization but is not in the allowed set drops the check:
# a check the engine "could not evaluate" fails every rep for a reason that has nothing to
# do with any model (found live on the first real adapt run — shell_gpt, 2026-09-01).
_CHECK_PARAM_SYNONYMS: dict[str, dict[str, str]] = {
    "response_contains": {"value": "text", "substring": "text", "string": "text",
                          "contains": "text"},
    "response_not_contains": {"value": "text", "substring": "text", "string": "text"},
    "response_matches": {"value": "regex", "pattern": "regex"},
    "final_state": {"key": "path", "value": "equals", "expected": "equals"},
    "state_count": {"value": "equals", "expected": "equals", "count": "equals"},
    "tool_called": {"tool": "name", "times": "min_times"},
    "tool_not_called": {"tool": "name"},
    "no_tool_calls_after_success": {"tool": "name"},
}
_CHECK_ALLOWED_PARAMS: dict[str, set[str]] = {
    "no_api_error": set(),
    "tool_called": {"name", "args_subset", "exact_args", "min_times", "max_times",
                    "retrieval"},
    "tool_not_called": {"name"},
    "no_tool_calls_after_success": {"name"},
    "final_state": {"path", "equals"},
    "state_count": {"path", "equals", "where"},
    "bookings_count": {"equals"},
    "confirmation_id_valid": {"pattern", "state_path", "id_field", "known_from"},
    "response_contains": {"text"},
    "response_not_contains": {"text"},
    "response_matches": {"regex"},
}
_CHECK_REQUIRED_PARAMS: dict[str, set[str]] = {
    "tool_called": {"name"},
    "tool_not_called": {"name"},
    "no_tool_calls_after_success": {"name"},
    "final_state": {"path", "equals"},
    "state_count": {"path", "equals"},
    "bookings_count": {"equals"},
    "response_contains": {"text"},
    "response_not_contains": {"text"},
    "response_matches": {"regex"},
}


def _normalize_check(check: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Map synonym params onto checks.py's exact vocabulary; reject what can't map.

    Returns (normalized_check, None) or (None, reason_dropped)."""
    kind = check["type"]
    synonyms = _CHECK_PARAM_SYNONYMS.get(kind, {})
    allowed = _CHECK_ALLOWED_PARAMS[kind]
    out: dict[str, Any] = {"type": kind}
    for key, value in check.items():
        if key == "type":
            continue
        key = synonyms.get(key, key)
        if key not in allowed:
            return None, f"{kind} has unknown parameter {key!r}"
        out.setdefault(key, value)
    missing = _CHECK_REQUIRED_PARAMS.get(kind, set()) - set(out)
    if missing:
        return None, f"{kind} missing required parameter(s) {sorted(missing)}"
    return out, None


def _clean_checks(
    raw: Any, tool_names: set[str], expected: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    checks: list[dict[str, Any]] = [{"type": "no_api_error"}]
    dropped: list[str] = []
    for check in raw if isinstance(raw, list) else []:
        if not isinstance(check, dict):
            dropped.append("non-object check")
            continue
        kind = check.get("type")
        if kind not in ALLOWED_CHECK_TYPES:
            dropped.append(f"unsupported check type {kind!r}")
            continue
        if kind == "no_api_error":
            continue
        check, drop_reason = _normalize_check(check)
        if check is None:
            dropped.append(drop_reason)
            continue
        name = check.get("name")
        tool_scoped = ("tool_called", "tool_not_called", "no_tool_calls_after_success")
        if kind in tool_scoped and name not in tool_names:
            dropped.append(f"{kind} names unknown tool {name!r}")
            continue
        checks.append(check)
    named = {c.get("name") for c in checks if c.get("type") == "tool_called"}
    for call in expected:
        name = call.get("name")
        if name in tool_names and name not in named:
            checks.append({"type": "tool_called", "name": name})
            named.add(name)
    for check in checks:
        if check.get("type") == "tool_called" and RETRIEVAL_NAME_RE.search(
            str(check.get("name") or "")
        ):
            check["retrieval"] = True
    return checks, dropped


def _oracle_plan(
    expected: list[dict[str, Any]], final_message: str, n_segments: int = 1
) -> list[dict[str, Any]]:
    """The sim consumes plan entries per user-message segment, each segment ending at a
    final_message entry — so a case with N user messages needs N terminators (found live:
    a 2-message case with a 1-terminator plan replays 'Done.' as the real final message
    and fails its own response checks). Extraction doesn't attribute tool calls to
    segments, so earlier segments are plain acknowledgments and the last segment carries
    every expected call plus the aligned final message."""
    plan: list[dict[str, Any]] = []
    for _ in range(max(1, n_segments) - 1):
        plan.append({"final_message": "Okay."})
    for call in expected:
        arguments = call.get("arguments")
        plan.append(
            {
                "tool_calls": [
                    {
                        "name": call.get("name"),
                        "arguments": arguments if isinstance(arguments, dict) else {},
                    }
                ]
            }
        )
    plan.append({"final_message": final_message})
    return plan


def build_cases(
    data: dict[str, Any], tool_names: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[str, str]], list[str]]:
    """(cases, provenance rows, dropped [(id, why)], notes)."""
    cases: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    dropped: list[tuple[str, str]] = []
    notes: list[str] = []
    seen: set[str] = set()

    for raw in data.get("cases") or []:
        case_id = slugify(raw.get("id"), fallback="")
        if not case_id or case_id in seen:
            dropped.append((str(raw.get("id")), "missing or duplicate id"))
            continue
        if not raw.get("verified", True):
            dropped.append((case_id, "citation did not resolve to a readable file"))
            continue
        messages = [str(m) for m in (raw.get("user_messages") or []) if str(m).strip()]
        if not messages:
            dropped.append((case_id, "no user messages"))
            continue
        expected = [c for c in (raw.get("expected_tool_calls") or []) if isinstance(c, dict)]
        unknown = [str(c.get("name")) for c in expected if c.get("name") not in tool_names]
        if unknown:
            dropped.append((case_id, f"expects tools that are not in tools.json: {unknown}"))
            continue
        checks, dropped_checks = _clean_checks(raw.get("checks"), tool_names, expected)
        for reason in dropped_checks:
            notes.append(f"case {case_id}: dropped a check ({reason})")

        final_message = str(raw.get("final_message") or "").strip() or "Done."
        # Sim-only nicety: the oracle plan is what `--provider sim` replays as the baseline,
        # so a response_contains check that the plan's own message does not satisfy would
        # fail the baseline for a reason that has nothing to do with any model. Sim results
        # are never evidence (DESIGN.md), so aligning the script is free.
        for check in checks:
            if check.get("type") == "response_contains":
                text = str(check.get("text") or "")
                if text and text.lower() not in final_message.lower():
                    final_message = f"{final_message} {text}".strip()
                    notes.append(
                        f"case {case_id}: appended {text!r} to the sim oracle message so the "
                        f"sim baseline can satisfy its own response_contains check"
                    )
            if check.get("type") == "response_matches":
                notes.append(
                    f"case {case_id}: response_matches cannot be satisfied mechanically by the "
                    f"sim oracle — the sim baseline may fail this case; review the regex"
                )

        initial_state = raw.get("initial_state")
        case = {
            "id": case_id,
            "description": (
                f"{str(raw.get('description') or '').strip()} "
                f"[adapt: derived from {raw.get('citation') or 'uncited'}]"
            ).strip(),
            "initial_state": initial_state if isinstance(initial_state, dict) else {},
            "user_messages": messages,
            "checks": checks,
            "sim": {"oracle_plan": _oracle_plan(expected, final_message, len(messages))},
        }
        if expected:
            case["sim"]["critical_tool"] = expected[-1].get("name")
        cases.append(case)
        seen.add(case_id)
        provenance.append(
            {
                "case": case_id,
                "citation": raw.get("citation", ""),
                "note": raw.get("note", ""),
                "expected_tool_calls": [c.get("name") for c in expected],
            }
        )
    return cases, provenance, dropped, notes


# ---------------------------------------------------------------------------
# agent.json params
# ---------------------------------------------------------------------------


def build_params(
    raw: Any, endpoint: str, notes: list[str]
) -> dict[str, Any]:
    """The canonical `params` block of agent.json.

    Two jobs, in order:

    * **canonicalise the Anthropic wire shape.** upshift's config is provider-neutral: the
      agent loop maps `reasoning_effort` onto `output_config.effort` on its way out
      (DESIGN.md v0.3). An extraction that reported the wire name anyway is translated here
      rather than written into agent.json, where nothing downstream would understand it.
      `max_tokens`, `tool_choice` (Anthropic shape) and `thinking` are already canonical and
      pass straight through.
    * **keep the blocklist.** Anything upshift owns — transport, messages, tools, the system
      prompt — is dropped with a note, and so is any non-scalar value outside the two
      documented object parameters.
    """
    params: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return params
    source = dict(raw)

    if endpoint == "messages":
        output_config = source.pop("output_config", None)
        if isinstance(output_config, dict):
            effort = output_config.get("effort")
            if isinstance(effort, str) and effort:
                if "reasoning_effort" in source:
                    notes.append(
                        f"output_config.effort was {effort!r} and reasoning_effort was already "
                        f"reported: kept reasoning_effort={source['reasoning_effort']!r}"
                    )
                else:
                    source["reasoning_effort"] = effort
                    notes.append(
                        "mapped output_config.effort -> the canonical reasoning_effort "
                        "parameter (the messages loop writes it back to output_config)"
                    )
            else:
                notes.append(
                    "dropped output_config: it carried no 'effort' value to canonicalise"
                )
        elif output_config is not None:
            notes.append(f"dropped output_config: expected an object, got {output_config!r}")

    for key, value in source.items():
        if key in BLOCKED_PARAMS:
            notes.append(
                f"dropped request parameter {key!r}: upshift owns it (transport, messages, "
                f"the system prompt or tools)"
            )
            continue
        if isinstance(value, dict) and key not in DICT_PARAMS:
            notes.append(
                f"dropped request parameter {key!r}: only {'/'.join(DICT_PARAMS)} may be an "
                f"object, and this one is a {type(value).__name__}"
            )
            continue
        if isinstance(value, list):
            notes.append(f"dropped request parameter {key!r}: a list is not a request parameter")
            continue
        params[key] = value
    return params


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def generate(
    verification: Verification,
    out_dir: str | Path,
    *,
    origin: str,
    commit: str | None = None,
) -> GenerationResult:
    """Write agent.json, system_prompt.txt, tools.json, backend.py, cases/cases.json."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = verification.data
    result = GenerationResult(out_dir=out_dir)

    # -- system_prompt.txt --------------------------------------------------
    prompt_text, prompt_provenance = build_system_prompt(data)
    _write(out_dir, "system_prompt.txt", prompt_text, result)
    if prompt_text.startswith("TODO(adapt)"):
        result.must_review.append(
            MustReview("system_prompt.txt", "1", "no prompt was extracted — write it by hand")
        )
    for entry in prompt_provenance:
        if entry["omitted"]:
            result.must_review.append(
                MustReview(
                    "system_prompt.txt", "-",
                    f"OMITTED from the prompt — the extraction claimed this line came from "
                    f"{entry['citation'] or 'no citation'} but it is nowhere in the source "
                    f"({entry['detail']}): {entry['text'].strip()!r}. Add it back only if you "
                    f"can point at the real line.",
                )
            )
        elif not entry["verified"]:
            result.must_review.append(
                MustReview(
                    "system_prompt.txt", entry["lines"],
                    f"{entry['kind']} text from {entry['citation'] or 'no citation'}: "
                    f"{entry['detail'] or 'not verified against source'}",
                )
            )
        elif entry["kind"] != "verbatim":
            result.must_review.append(
                MustReview(
                    "system_prompt.txt", entry["lines"],
                    f"{entry['kind']} from {entry['citation']}"
                    + (f" — {entry['note']}" if entry["note"] else ""),
                )
            )

    # -- tools.json ---------------------------------------------------------
    tools, tools_provenance = build_tools(data)
    tools_text = _write(out_dir, "tools.json", json.dumps(tools, indent=2) + "\n", result)
    result.tool_names = [t["function"]["name"] for t in tools]
    for entry in tools_provenance:
        if entry["tool"] == PLACEHOLDER_TOOL_NAME:
            result.must_review.append(
                MustReview("tools.json", _locate(tools_text, PLACEHOLDER_TOOL_NAME),
                           "placeholder tool: no tool schema was traceable to the source")
            )
        elif not entry["verified"]:
            result.must_review.append(
                MustReview("tools.json", _locate(tools_text, f'"{entry["tool"]}"'),
                           f"tool name not found at {entry['citation'] or 'no citation'} — "
                           f"schema is inferred, not extracted")
            )
        elif entry["kind"] != "verbatim":
            result.must_review.append(
                MustReview("tools.json", _locate(tools_text, f'"{entry["tool"]}"'),
                           f"{entry['kind']} schema, assembled from {entry['citation']}")
            )

    # -- backend.py ---------------------------------------------------------
    backend_text, implemented, stubs, backend_provenance = build_backend(data, origin, commit)
    _write(out_dir, "backend.py", backend_text, result)
    result.implemented_tools, result.stub_tools = implemented, stubs
    for name in implemented:
        result.must_review.append(
            MustReview("backend.py", _locate(backend_text, f'"{name}"'),
                       f"{name} is a re-implementation from a description of the source; "
                       f"confirm it matches upstream before trusting a run")
        )
    for name in stubs:
        result.must_review.append(
            MustReview("backend.py", _locate(backend_text, f'"{name}"'),
                       f"{name} is a TODO stub: every call returns an error result")
        )

    # -- cases/cases.json ---------------------------------------------------
    cases, cases_provenance, dropped, case_notes = build_cases(data, set(result.tool_names))
    result.notes.extend(case_notes)
    result.dropped_cases = dropped
    cases_text = _write(
        out_dir, "cases/cases.json", json.dumps(cases, indent=2) + "\n", result
    )
    result.case_ids = [c["id"] for c in cases]
    for case in cases:
        result.must_review.append(
            MustReview("cases/cases.json", _locate(cases_text, f'"id": "{case["id"]}"'),
                       "drafted from cited usage evidence — the checks are upshift's yardstick, "
                       "so confirm they encode behaviour you actually require")
        )
    if not cases:
        result.must_review.append(
            MustReview("cases/cases.json", "1",
                       "no case survived: the suite is empty and upshift will refuse to run")
        )

    # -- agent.json ---------------------------------------------------------
    endpoint_claim = data.get("endpoint") or {}
    endpoint = endpoint_claim.get("value")
    if endpoint not in ENDPOINT_VALUES:
        endpoint = DEFAULT_ENDPOINT
        result.notes.append(
            "endpoint was undetermined; defaulted to chat_completions (the endpoint the "
            "documented gpt-5.6 break fires on) — confirm it"
        )
    model_claim = data.get("model") or {}
    model = model_claim.get("value")
    if not isinstance(model, str) or not model.strip():
        model = DEFAULT_MODEL
        result.notes.append(f"model was undetermined; wrote the upshift default {DEFAULT_MODEL!r}")
    params_value = (data.get("params") or {}).get("value")
    params = build_params(params_value, endpoint, result.notes)
    max_turns = (data.get("max_turns") or {}).get("value")
    config = {
        "name": slugify(data.get("agent_name")),
        "endpoint": endpoint,
        "model": model,
        "params": params,
        "system_prompt_file": "system_prompt.txt",
        "tools_file": "tools.json",
        "max_turns": max_turns if isinstance(max_turns, int) and max_turns > 0
        else DEFAULT_MAX_TURNS,
    }
    agent_text = _write(out_dir, "agent.json", json.dumps(config, indent=2) + "\n", result)
    if not endpoint_claim.get("verified"):
        result.must_review.append(
            MustReview("agent.json", _locate(agent_text, '"endpoint"'),
                       f"endpoint {endpoint!r} is not confirmed by a call site in the source")
        )
    if not model_claim.get("verified"):
        result.must_review.append(
            MustReview("agent.json", _locate(agent_text, '"model"'),
                       f"model {model!r} was not found as a literal in the cited file")
        )
    for key in verification.dropped_params:
        result.notes.append(
            f"dropped parameter {key!r}: it does not appear in the file the extraction cited"
        )

    # -- PROVENANCE.json ----------------------------------------------------
    provenance = {
        "generated_by": "upshift adapt",
        "source": {"origin": origin, "commit": commit},
        "confidence": verification.confidence,
        "system_prompt": prompt_provenance,
        "tools": tools_provenance,
        "backend": backend_provenance,
        "cases": cases_provenance,
        "dropped_cases": [{"id": i, "why": why} for i, why in dropped],
        "undetermined": data.get("undetermined") or [],
        "flags": [
            {"artifact": f.artifact, "what": f.what, "citation": f.citation,
             "severity": f.severity, "reason": f.reason}
            for f in verification.flags
        ],
    }
    _write(out_dir, "PROVENANCE.json", json.dumps(provenance, indent=2) + "\n", result)
    return result
