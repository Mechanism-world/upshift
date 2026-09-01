"""Stage 3: the anti-hallucination gate. Mechanical, not judgemental.

Every claim the extraction marked `verbatim` must literally appear — modulo whitespace — in
the file it cites. A claim that does not is downgraded to `inferred` and flagged; a claim
whose file or line does not exist is downgraded too. Confidence for every generated artifact
is computed HERE, from what checked out, and is never read off the model's own reply
(DESIGN.md: "Confidence is derived from the verification gate, never self-reported").
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HIGH, MEDIUM, LOW = "high", "medium", "low"

#: Endpoint claims are checked against the call markers that decide the endpoint.
ENDPOINT_MARKERS = {
    "chat_completions": ("chat.completions.create", "litellm.completion", "litellm.acompletion",
                         "completion(", "ChatCompletion"),
    "responses": ("responses.create", "client.responses", "responses.parse"),
    # The Anthropic Messages API. Deliberately disjoint from the chat_completions markers:
    # `messages.create` is evidence for "messages" and for nothing else, so an Anthropic
    # call site can never be used to confirm a claimed chat_completions endpoint.
    "messages": ("messages.create", "client.messages", "beta.messages.create"),
}

#: Canonical parameter -> the spellings that count as evidence for it in the source, per
#: endpoint. A canonical name upshift invented (`reasoning_effort` on the Messages API, where
#: the wire spelling is `output_config={"effort": ...}`) is not in the file it comes from, so
#: the gate looks for the wire name instead of dropping a parameter that is really there.
PARAM_SOURCE_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "messages": {"reasoning_effort": ("reasoning_effort", "output_config", "effort")},
}

CITATION_RE = re.compile(r"^(?P<path>[^\s:][^:]*):(?P<start>\d+)(?:-(?P<end>\d+))?$")

#: How far from the cited line a verbatim match may sit before we call the citation sloppy.
NEAR_BEFORE = 6
NEAR_AFTER = 120

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass
class Flag:
    artifact: str  # system_prompt | tools | agent.json | cases | backend.py
    what: str  # which claim
    citation: str
    severity: str
    reason: str


@dataclass
class Verification:
    data: dict[str, Any]  # the extraction, with verified/kind fields settled
    flags: list[Flag] = field(default_factory=list)
    confidence: dict[str, str] = field(default_factory=dict)
    dropped_params: list[str] = field(default_factory=list)
    checked: int = 0
    downgraded: int = 0

    def flags_for(self, artifact: str) -> list[Flag]:
        return [f for f in self.flags if f.artifact == artifact]


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def normalize_ws(text: str) -> str:
    """Collapse every whitespace run to one space. The one normalisation the gate allows —
    source is wrapped, indented and re-indented; characters are otherwise compared exactly."""
    return re.sub(r"\s+", " ", text).strip()


def parse_citation(citation: str) -> tuple[str, int, int] | None:
    match = CITATION_RE.match(str(citation or "").strip())
    if not match:
        return None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    return match.group("path"), start, max(start, end)


class FileCache:
    """Reads each cited file once; unreadable files resolve to None forever."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._cache: dict[str, list[str] | None] = {}

    def lines(self, rel: str) -> list[str] | None:
        if rel not in self._cache:
            path = self.root / rel
            try:
                self._cache[rel] = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError, ValueError):
                self._cache[rel] = None
        return self._cache[rel]

    def whole(self, rel: str) -> str | None:
        lines = self.lines(rel)
        return None if lines is None else "\n".join(lines)

    def window(self, rel: str, start: int, end: int) -> str | None:
        lines = self.lines(rel)
        if lines is None:
            return None
        low = max(0, start - 1 - NEAR_BEFORE)
        high = min(len(lines), end + NEAR_AFTER)
        return "\n".join(lines[low:high])


#: A templated chunk (placeholders substituted) can never match literally, so it is checked
#: for an anchor instead: a run of at least this many consecutive words that IS in the file.
MIN_ANCHOR_WORDS = 5


def longest_anchor(cache: FileCache, citation: str, needle: str) -> str | None:
    """The longest run of >= MIN_ANCHOR_WORDS consecutive words of `needle` that appears in
    the cited file, or None. This is what grounds a templated chunk."""
    parsed = parse_citation(citation)
    if parsed is None:
        return None
    whole = cache.whole(parsed[0])
    if whole is None:
        return None
    haystack = normalize_ws(whole)
    words = normalize_ws(needle).split(" ")
    for size in range(len(words), MIN_ANCHOR_WORDS - 1, -1):
        for start in range(len(words) - size + 1):
            run = " ".join(words[start : start + size])
            if run in haystack:
                return run
    return None


def literal_in_file(cache: FileCache, citation: str, needle: str) -> tuple[str, str]:
    """(verdict, detail) where verdict is 'at_citation' | 'in_file' | 'absent' | 'no_file'
    | 'bad_citation'."""
    parsed = parse_citation(citation)
    if parsed is None:
        return "bad_citation", f"citation {citation!r} is not 'path:line'"
    rel, start, end = parsed
    whole = cache.whole(rel)
    if whole is None:
        return "no_file", f"{rel} does not exist in the source repo"
    target = normalize_ws(needle)
    if not target:
        return "absent", "claimed text is empty"
    window = cache.window(rel, start, end) or ""
    if target in normalize_ws(window):
        n_lines = len(cache.lines(rel) or [])
        if start > n_lines:
            return "in_file", f"{rel} has {n_lines} lines; cited line {start} is past the end"
        return "at_citation", ""
    if target in normalize_ws(whole):
        return "in_file", f"text is in {rel} but not near line {start}"
    return "absent", f"text does not appear in {rel}"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def verify(extraction: dict[str, Any], repo_root: str | Path) -> Verification:
    """Check every citation; downgrade what does not check out; score every artifact."""
    data = copy.deepcopy(extraction)
    cache = FileCache(Path(repo_root))
    result = Verification(data=data)

    _verify_prompt(data, cache, result)
    _verify_tools(data, cache, result)
    _verify_agent_config(data, cache, result)
    _verify_cases(data, cache, result)
    _score_backend(data, cache, result)
    return result


def _downgrade(entry: dict[str, Any], result: Verification, flag: Flag) -> None:
    entry["kind"] = "inferred"
    entry["verified"] = False
    result.downgraded += 1
    result.flags.append(flag)


def _verify_prompt(data: dict[str, Any], cache: FileCache, result: Verification) -> None:
    """Three outcomes per chunk, in this order:

    1. the exact text is in the cited file  -> kept, verified;
    2. only an anchor (a long word run) is  -> kept, marked templated, flagged;
    3. neither                              -> `omitted`, so generate.py never writes text
       that is nowhere in the source into the agent's own system prompt.
    """
    prompt = data.get("system_prompt") or {}
    chunks = prompt.get("chunks") or []
    verbatim_ok = grounded = 0
    for index, chunk in enumerate(chunks):
        result.checked += 1
        citation = str(chunk.get("citation") or "")
        text = str(chunk.get("text") or "")
        verdict, detail = literal_in_file(cache, citation, text)
        chunk["verify"] = verdict
        chunk["verify_detail"] = detail
        chunk["omitted"] = False
        claimed = chunk.get("kind")
        where = f"system_prompt.chunks[{index}]"

        if verdict in ("at_citation", "in_file"):
            chunk["verified"] = True
            grounded += 1
            if claimed == "verbatim":
                verbatim_ok += 1
            if verdict == "in_file":
                result.flags.append(Flag("system_prompt", where, citation, WARNING, detail))
            continue

        anchor = longest_anchor(cache, citation, text)
        if anchor:
            chunk["verified"] = False
            chunk["verify"] = "anchored"
            chunk["verify_detail"] = f"only a partial match is in the file: {anchor!r}"
            grounded += 1
            if claimed == "verbatim":
                chunk["kind"] = "templated"
                result.downgraded += 1
                result.flags.append(
                    Flag("system_prompt", where, citation, ERROR,
                         f"claimed verbatim, but only {anchor!r} is actually in the file — "
                         f"downgraded to templated; check the substitution by hand"),
                )
            else:
                result.flags.append(
                    Flag("system_prompt", where, citation, WARNING,
                         f"{claimed} text anchored on {anchor!r}; the rest is the model's "
                         f"substitution and is not verified"),
                )
            continue

        chunk["omitted"] = True
        _downgrade(
            chunk, result,
            Flag("system_prompt", where, citation, ERROR,
                 f"claimed {claimed}, but {detail} — omitted from the generated prompt rather "
                 f"than written into the agent under test"),
        )

    total = len(chunks)
    if total == 0 or prompt.get("status") == "undetermined":
        result.confidence["system_prompt.txt"] = LOW
    elif verbatim_ok == total:
        result.confidence["system_prompt.txt"] = HIGH
    elif grounded:
        result.confidence["system_prompt.txt"] = MEDIUM
    else:
        result.confidence["system_prompt.txt"] = LOW


def _verify_tools(data: dict[str, Any], cache: FileCache, result: Verification) -> None:
    tools = data.get("tools") or []
    full = partial = 0
    for index, tool in enumerate(tools):
        result.checked += 1
        citation = str(tool.get("citation") or "")
        name = str(tool.get("name") or "")
        description = str(tool.get("description") or "")
        where = f"tools[{index}] {name}"
        name_verdict, name_detail = literal_in_file(cache, citation, name)
        desc_verdict, desc_detail = (
            literal_in_file(cache, citation, description)
            if description
            else ("absent", "the tool has no description")
        )
        tool["verify"] = {"name": name_verdict, "description": desc_verdict}
        if name_verdict in ("at_citation", "in_file"):
            tool["verified"] = True
            if desc_verdict in ("at_citation", "in_file") and tool.get("kind") == "verbatim":
                full += 1
            else:
                partial += 1
                if tool.get("kind") == "verbatim":
                    tool["kind"] = "templated"
                    result.flags.append(
                        Flag("tools.json", where, citation, WARNING,
                             f"tool name checks out but its description does not ({desc_detail})"
                             f" — kind downgraded to templated"),
                    )
        else:
            _downgrade(
                tool, result,
                Flag("tools.json", where, citation, ERROR,
                     f"tool name {name!r} does not appear at its citation ({name_detail}) — "
                     f"treated as inferred; do not run this schema unreviewed"),
            )

    if not tools:
        result.confidence["tools.json"] = LOW
    elif full == len(tools):
        result.confidence["tools.json"] = HIGH
    elif full + partial == len(tools):
        result.confidence["tools.json"] = MEDIUM
    else:
        result.confidence["tools.json"] = LOW


def _verify_agent_config(data: dict[str, Any], cache: FileCache, result: Verification) -> None:
    verified: list[bool] = []

    endpoint = data.get("endpoint") or {}
    value = endpoint.get("value")
    markers = ENDPOINT_MARKERS.get(str(value), ())
    citation = str(endpoint.get("citation") or "")
    result.checked += 1
    hit = next(
        (
            m for m in markers
            if literal_in_file(cache, citation, m)[0] in ("at_citation", "in_file")
        ),
        None,
    )
    endpoint["verified"] = hit is not None
    endpoint["verify_detail"] = f"matched {hit!r}" if hit else "no matching call marker at citation"
    verified.append(bool(hit))
    if not hit:
        result.flags.append(
            Flag("agent.json", "endpoint", citation, WARNING,
                 f"no {value!r} call marker found at the cited location; "
                 f"generated agent.json keeps it but it must be reviewed"),
        )

    model = data.get("model") or {}
    if isinstance(model.get("value"), str) and model["value"]:
        result.checked += 1
        verdict, detail = literal_in_file(cache, str(model.get("citation") or ""), model["value"])
        model["verified"] = verdict in ("at_citation", "in_file")
        model["verify_detail"] = detail
        verified.append(bool(model["verified"]))
        if not model["verified"]:
            result.flags.append(
                Flag("agent.json", "model", str(model.get("citation") or ""), ERROR,
                     f"model string {model['value']!r} does not appear in the cited file "
                     f"({detail})"),
            )
    else:
        model["verified"] = False

    params = (data.get("params") or {}).get("value")
    kept: dict[str, Any] = {}
    if isinstance(params, dict):
        citation = str((data.get("params") or {}).get("citation") or "")
        aliases = PARAM_SOURCE_ALIASES.get(str((data.get("endpoint") or {}).get("value")), {})
        for key, value in params.items():
            result.checked += 1
            verdict, detail = "absent", ""
            for spelling in aliases.get(str(key), (str(key),)):
                verdict, detail = literal_in_file(cache, citation, spelling)
                if verdict in ("at_citation", "in_file"):
                    break
            if verdict in ("at_citation", "in_file"):
                kept[key] = value
            else:
                result.dropped_params.append(key)
                result.flags.append(
                    Flag("agent.json", f"params.{key}", citation, ERROR,
                         f"parameter {key!r} does not appear in the cited file ({detail}) — "
                         f"dropped from agent.json rather than sent to the API"),
                )
        data["params"]["value"] = kept
        data["params"]["verified"] = not result.dropped_params

    clean = bool(verified) and all(verified) and not result.dropped_params
    result.confidence["agent.json"] = (
        HIGH if clean else (MEDIUM if any(verified) else LOW)
    )


def _verify_cases(data: dict[str, Any], cache: FileCache, result: Verification) -> None:
    cases = data.get("cases") or []
    grounded = 0
    for index, case in enumerate(cases):
        result.checked += 1
        citation = str(case.get("citation") or "")
        parsed = parse_citation(citation)
        where = f"cases[{index}] {case.get('id')}"
        if parsed is None or cache.whole(parsed[0]) is None:
            case["verified"] = False
            result.flags.append(
                Flag("cases/cases.json", where, citation, ERROR,
                     "case cites no readable file — it rests on nothing in the source"),
            )
            continue
        case["verified"] = True
        grounded += 1

    if not cases:
        result.confidence["cases/cases.json"] = LOW
    elif grounded == len(cases):
        # Never high: a case asserts intended behaviour, which no citation can prove.
        result.confidence["cases/cases.json"] = MEDIUM
    else:
        result.confidence["cases/cases.json"] = LOW


def _score_backend(data: dict[str, Any], cache: FileCache, result: Verification) -> None:
    implemented = stubs = 0
    for index, tool in enumerate(data.get("tools") or []):
        backend = tool.get("backend") or {}
        kind = backend.get("kind", "unclear")
        where = f"tools[{index}].backend {tool.get('name')}"
        if kind == "unclear":
            stubs += 1
            continue
        citation = str(backend.get("citation") or tool.get("citation") or "")
        if parse_citation(citation) is None or cache.whole(parse_citation(citation)[0]) is None:
            backend["kind"] = "unclear"
            stubs += 1
            result.flags.append(
                Flag("backend.py", where, citation, ERROR,
                     f"claimed mechanical semantics ({kind}) with no readable citation — "
                     f"generated as a TODO stub instead"),
            )
            continue
        implemented += 1
        result.flags.append(
            Flag("backend.py", where, citation, WARNING,
                 f"re-implemented as {kind!r} from a description of the source, not from the "
                 f"source itself — semantics are NOT machine-verified"),
        )

    # No backend can be `high`: nothing here proves behaviour, only that a citation exists.
    result.confidence["backend.py"] = MEDIUM if implemented and not stubs else LOW
