"""Stage 1: walk the repo, rank files by static signal, find the call sites, slice evidence.

Nothing here calls a model. Its whole job is to turn "a repo" into a small, ordered,
citable bundle of source windows — every slice carries `path:start-end` so everything the
extraction stage says can be traced back to a real line of a real file.

Python files get an AST pass (call sites with their keyword arguments, string constants that
look like prompts, `{"type": "function"}` dict literals). Everything else falls back to the
same signal regexes without structure.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Walk policy
# ---------------------------------------------------------------------------

SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
        ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
        "dist", "build", ".idea", ".vscode", "vendor", "third_party", ".eggs",
        "htmlcov", "coverage", ".next", "target",
        # Recorded API traffic: request bodies in there are full of tool schemas and system
        # messages, so they outrank the source that produced them unless we skip them.
        "runs", "cassettes", "recordings", "snapshots", "__snapshots__",
    }
)

TEXT_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".md", ".txt", ".rst", ".toml", ".cfg", ".ini", ".yaml", ".yml",
        ".json", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java", ".sh", ".jinja",
        ".j2", ".tmpl", ".env.example",
    }
)

MAX_FILE_BYTES = 400_000
#: Data files are read (an agent's tools can live in a .json) but a big one is a dump, not
#: source, and reading thousands of them is what makes a walk slow.
DATA_SUFFIXES = frozenset({".json", ".jsonl", ".yaml", ".yml"})
MAX_DATA_BYTES = 100_000

#: A file containing any of these is a recorded API response, not source that defines an
#: agent. It is excluded from ranking however strong its other signals look.
TRANSCRIPT_RE = re.compile(
    r'"finish_reason"|"prompt_tokens"|"completion_tokens"|"chatcmpl-|"object"\s*:\s*"'
    r'chat\.completion'
)
#: Files ranked into the evidence bundle. Beyond this the tail is noise for every repo we
#: have looked at, and the token budget would cut it anyway.
DEFAULT_MAX_FILES = 15
#: len/4 token estimate; the extraction request must stay well inside a single context.
DEFAULT_MAX_EVIDENCE_TOKENS = 120_000

# ---------------------------------------------------------------------------
# Static signals: regex -> (weight, label). Line numbers of every match are kept so the
# slicer can window around them.
# ---------------------------------------------------------------------------

SIGNALS: list[tuple[str, float, str]] = [
    (r"\bchat\.completions\.create\b", 10.0, "chat.completions.create call"),
    (r"\bclient\.responses\.create\b|\bopenai\.responses\.create\b", 10.0, "responses.create call"),
    (r"\bresponses\.create\b", 6.0, "responses.create call"),
    (r"\blitellm\.(a?completion)\b", 9.0, "litellm.completion call"),
    (r"\bacompletion\s*\(|\bcompletion\s*\(", 3.0, "completion() call"),
    (r"^\s*(from|import)\s+openai\b|^\s*from\s+openai\b", 5.0, "openai import"),
    (r"^\s*(from|import)\s+litellm\b", 5.0, "litellm import"),
    (r"\btools\s*=", 6.0, "tools= kwarg"),
    (r"\"type\"\s*:\s*\"function\"|'type'\s*:\s*'function'", 7.0, "function tool schema literal"),
    (r"\btool_choice\b", 4.0, "tool_choice"),
    (r"\bparallel_tool_calls\b", 3.0, "parallel_tool_calls"),
    (r"\bfunction_call\b|\btool_calls\b", 3.0, "tool-call handling"),
    (r"\breasoning_effort\b|\breasoning\s*=\s*\{", 4.0, "reasoning params"),
    (r"\bmodel\s*=\s*[\"'][\w.\-:]+[\"']", 3.0, "model literal"),
    (r"\b(SYSTEM|DEFAULT)_?(PROMPT|ROLE|MESSAGE|TEMPLATE)\b", 5.0, "prompt constant"),
    (r"\b[A-Z_]*(PROMPT|INSTRUCTIONS?|PERSONA)[A-Z_]*\s*=", 4.0, "prompt constant"),
    (r"\"role\"\s*:\s*\"system\"|'role'\s*:\s*'system'", 5.0, "system message literal"),
    (r"\bYou are\b", 3.0, "prompt-shaped text"),
    (r"\bmax_turns\b|\bmax_iterations\b|\bMAX_STEPS\b", 2.0, "turn cap"),
]

_COMPILED = [
    (re.compile(pattern, re.MULTILINE), weight, label) for pattern, weight, label in SIGNALS
]

#: Filename bonuses (substring match on the posix relative path, lowercased).
NAME_BONUS: list[tuple[str, float, str]] = [
    ("prompt", 4.0, "prompt-named file"),
    ("role", 2.0, "role-named file"),
    ("tool", 3.0, "tool-named file"),
    ("function", 2.0, "function-named file"),
    ("agent", 3.0, "agent-named file"),
    ("handler", 2.0, "handler-named file"),
    ("client", 1.5, "client-named file"),
    ("llm", 2.0, "llm-named file"),
    ("readme", 3.0, "README"),
    ("example", 2.0, "example"),
    ("/test", 1.5, "test"),
    ("test_", 1.5, "test"),
    ("config", 1.0, "config"),
    ("main.py", 1.5, "entry point"),
    ("cli.py", 1.0, "entry point"),
]

#: Dotted call names that mean "this is the model call".
CALL_PATTERNS = (
    "chat.completions.create",
    "responses.create",
    "litellm.completion",
    "litellm.acompletion",
    "completion",
    "acompletion",
    "create_completion",
)

PROMPT_NAME_RE = re.compile(
    r"(PROMPT|ROLE|SYSTEM|INSTRUCTION|PERSONA|TEMPLATE|DESCRIPTION)", re.IGNORECASE
)
PROMPT_TEXT_RE = re.compile(r"\byou are\b|\byou're a\b|\bact as\b|\byour task\b", re.IGNORECASE)
PROMPT_MIN_CHARS = 60

GIT_URL_RE = re.compile(r"^(https?://|git@|ssh://|git://)")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class SourceRepo:
    root: Path
    origin: str  # exactly what the user passed
    commit: str | None  # resolved HEAD, when the source is a git checkout
    is_clone: bool


@dataclass
class CallSite:
    path: str
    line: int
    callee: str
    kwargs: dict[str, str] = field(default_factory=dict)
    how: str = "ast"  # "ast" | "regex"

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass
class PromptConstant:
    path: str
    line: int
    name: str
    n_chars: int
    preview: str

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass
class FileSignal:
    path: str
    score: float
    reasons: list[str]
    lines: list[int]
    n_lines: int


@dataclass
class Slice:
    path: str
    start: int  # 1-based, inclusive
    end: int  # 1-based, inclusive
    text: str

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"


@dataclass
class Inventory:
    repo: SourceRepo
    files: list[FileSignal]
    call_sites: list[CallSite]
    prompt_constants: list[PromptConstant]
    slices: list[Slice]
    evidence_tokens: int
    scanned_files: int
    candidate_files: int = 0  # files that carried any signal at all
    truncated: bool = False  # the token budget dropped at least one slice
    #: The cap `take_inventory` was given. Kept so a later stage (extraction round 2) can
    #: append evidence to the SAME budget instead of inventing a second one.
    max_tokens: int = DEFAULT_MAX_EVIDENCE_TOKENS

    @property
    def slice_paths(self) -> set[str]:
        """Files the extraction actually saw. A file with a signal that lost the ranking, or
        whose slices the budget dropped, is NOT in here — which is the point."""
        return {piece.path for piece in self.slices}

    @property
    def slice_ranges(self) -> dict[str, list[tuple[int, int]]]:
        """Line ranges the extraction actually saw, per file.

        Ranking a file is not the same as showing all of it: a big file goes in as windows
        around its signal lines, so the gaps between those windows are unread. Round 2 needs
        this to tell "you already read that" from "you never saw those lines".
        """
        ranges: dict[str, list[tuple[int, int]]] = {}
        for piece in self.slices:
            ranges.setdefault(piece.path, []).append((piece.start, piece.end))
        return ranges


# ---------------------------------------------------------------------------
# Source resolution (local path or git URL)
# ---------------------------------------------------------------------------


def is_git_url(source: str) -> bool:
    """True for things `git clone` takes and `Path` does not."""
    s = source.strip()
    if GIT_URL_RE.match(s):
        return True
    return s.endswith(".git") and "/" in s


def _git(
    args: list[str], cwd: Path | None = None, timeout: int = 300
) -> subprocess.CompletedProcess:
    # Fixed argv, never a shell string: the only untrusted value is the clone URL, which git
    # itself parses.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _default_clone(url: str, dest: Path) -> str | None:
    """Shallow-clone `url` into `dest`; returns the resolved commit sha."""
    result = _git(["clone", "--depth", "1", url, str(dest)])
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"git exited {result.returncode}"
        raise ValueError(f"could not clone {url}: {detail}")
    return head_commit(dest)


def head_commit(root: Path) -> str | None:
    """Resolved HEAD of a git checkout, or None when the directory is not one."""
    result = _git(["rev-parse", "HEAD"], cwd=root, timeout=30)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def resolve_source(
    source: str,
    workdir: Path,
    *,
    clone_fn: Callable[[str, Path], str | None] | None = None,
) -> SourceRepo:
    """A local path or a git URL -> a SourceRepo rooted at a real directory.

    `clone_fn` exists so the clone path is testable without a network (tests hand it a
    function that copies a local fixture and returns a fake sha).
    """
    if is_git_url(source):
        dest = Path(workdir) / "clone"
        clone = clone_fn or _default_clone
        commit = clone(source, dest)
        if not dest.is_dir():
            raise ValueError(f"clone of {source} produced no directory at {dest}")
        return SourceRepo(root=dest, origin=source, commit=commit, is_clone=True)
    root = Path(source).expanduser()
    if not root.exists():
        raise ValueError(f"{source} does not exist (pass a directory or a git URL)")
    if not root.is_dir():
        raise ValueError(f"{source} is not a directory (adapt reads a whole agent repo)")
    return SourceRepo(root=root.resolve(), origin=source, commit=head_commit(root), is_clone=False)


# ---------------------------------------------------------------------------
# Walk + rank
# ---------------------------------------------------------------------------


def readable_source(path: Path) -> bool:
    """A text file small enough to be worth reading. Also the gate on any file a pointer
    names in extraction round 2, so a pointer cannot pull in a 40MB blob."""
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES:
        return False
    limit = MAX_DATA_BYTES if suffix in DATA_SUFFIXES else MAX_FILE_BYTES
    try:
        return path.stat().st_size <= limit
    except OSError:
        return False


def walk_repo(root: Path) -> list[Path]:
    """Every candidate text file, sorted, vendored/build directories skipped."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if readable_source(path):
            found.append(path)
    return found


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def score_text(rel_path: str, text: str) -> tuple[float, list[str], list[int]]:
    """(score, reasons, 1-based line numbers that fired a signal)."""
    score = 0.0
    reasons: list[str] = []
    lines: set[int] = set()
    for pattern, weight, label in _COMPILED:
        hits = list(pattern.finditer(text))
        if not hits:
            continue
        # Diminishing returns: the 30th `tools=` says nothing the first three did not.
        score += weight * min(len(hits), 3)
        reasons.append(f"{label} x{len(hits)}")
        for hit in hits[:40]:
            lines.add(text.count("\n", 0, hit.start()) + 1)
    lowered = rel_path.lower()
    for needle, weight, label in NAME_BONUS:
        if needle in lowered:
            score += weight
            reasons.append(label)
    return score, reasons, sorted(lines)


# ---------------------------------------------------------------------------
# Python AST analysis
# ---------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append("()")
    return ".".join(reversed(parts))


def _kwarg_source(node: ast.AST, text: str) -> str:
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        segment = ast.get_source_segment(text, node)
        return (segment or "<expr>").strip()
    return repr(value)


def analyze_python(
    rel_path: str, text: str
) -> tuple[list[CallSite], list[PromptConstant], list[int]]:
    """AST pass. Returns (call sites, prompt-shaped constants, extra signal lines).

    A syntax error is not fatal: the caller still has the regex signals.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], [], []
    calls: list[CallSite] = []
    prompts: list[PromptConstant] = []
    extra_lines: list[int] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            tail = name.rsplit("().", 1)[-1]
            if any(tail.endswith(pattern) or name.endswith(pattern) for pattern in CALL_PATTERNS):
                kwargs = {
                    kw.arg: _kwarg_source(kw.value, text)
                    for kw in node.keywords
                    if kw.arg is not None
                }
                calls.append(
                    CallSite(
                        path=rel_path, line=node.lineno, callee=name or "<call>",
                        kwargs=kwargs,
                    )
                )
                extra_lines.append(node.lineno)
        elif isinstance(node, ast.Assign):
            value = node.value
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            names += [t.attr for t in node.targets if isinstance(t, ast.Attribute)]
            if not names:
                continue
            literal = value.value
            named = PROMPT_NAME_RE.search(names[0]) is not None
            shaped = PROMPT_TEXT_RE.search(literal) is not None
            if (named and len(literal) >= PROMPT_MIN_CHARS) or shaped:
                prompts.append(
                    PromptConstant(
                        path=rel_path,
                        line=node.lineno,
                        name=names[0],
                        n_chars=len(literal),
                        preview=literal[:120].replace("\n", " "),
                    )
                )
                extra_lines.append(node.lineno)
        elif isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "type"
                    and isinstance(val, ast.Constant)
                    and val.value == "function"
                ):
                    extra_lines.append(node.lineno)
    return calls, prompts, sorted(set(extra_lines))


REGEX_CALL_RE = re.compile(
    r"(?P<callee>[\w.]*(?:chat\.completions\.create|responses\.create|"
    r"litellm\.a?completion|a?completion))\s*\("
)


def analyze_regex(rel_path: str, text: str) -> list[CallSite]:
    """Call-site fallback for non-Python sources: location only, no keyword arguments."""
    calls: list[CallSite] = []
    for hit in REGEX_CALL_RE.finditer(text):
        line = text.count("\n", 0, hit.start()) + 1
        calls.append(
            CallSite(path=rel_path, line=line, callee=hit.group("callee"), kwargs={}, how="regex")
        )
    return calls


# ---------------------------------------------------------------------------
# Evidence slicing
# ---------------------------------------------------------------------------

WINDOW_BEFORE = 8
WINDOW_AFTER = 24
SMALL_FILE_LINES = 160  # files this short go in whole; windowing them saves nothing
README_LINES = 220

#: Round-2 slicing is deliberately more generous than round 1: a pointer is the model saying
#: "the answer is over there", so the window has to be wide enough to contain a whole
#: docstring, class body or schema literal rather than one call site.
POINTER_WINDOW = 80
POINTER_WHOLE_FILE_LINES = 400


def estimate_tokens(text: str) -> int:
    """len/4. Deliberately crude and deliberately documented as crude."""
    return len(text) // 4


def _merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 3:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_ranges(
    windows: list[tuple[int, int]], covered: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """`windows` minus `covered`: the line ranges left over once what was already read is
    taken out. Both sides are inclusive, 1-based.

    `covered` is merged with the same 3-line slack `_merge` uses, so a two-line hole between
    two round-1 windows counts as read rather than becoming a slice of its own.
    """
    blocked = _merge(list(covered))
    out: list[tuple[int, int]] = []
    for start, end in _merge(list(windows)):
        cursor = start
        for low, high in blocked:
            if high < cursor or low > end:
                continue
            if low > cursor:
                out.append((cursor, low - 1))
            cursor = max(cursor, high + 1)
            if cursor > end:
                break
        if cursor <= end:
            out.append((cursor, end))
    return out


def slice_file(rel_path: str, text: str, signal_lines: list[int]) -> list[Slice]:
    lines = text.splitlines()
    n = len(lines)
    if n == 0:
        return []
    lowered = rel_path.lower()
    if n <= SMALL_FILE_LINES:
        windows = [(1, n)]
    elif "readme" in lowered and not signal_lines:
        windows = [(1, min(README_LINES, n))]
    elif not signal_lines:
        windows = [(1, min(SMALL_FILE_LINES, n))]
    else:
        windows = _merge(
            [
                (max(1, line - WINDOW_BEFORE), min(n, line + WINDOW_AFTER))
                for line in signal_lines
            ]
        )
    out: list[Slice] = []
    for start, end in windows:
        body = "\n".join(lines[start - 1 : end])
        out.append(Slice(path=rel_path, start=start, end=end, text=body))
    return out


def slice_pointer(
    rel_path: str,
    text: str,
    lines_of_interest: list[int],
    *,
    covered: list[tuple[int, int]] | tuple[()] = (),
    radius: int = POINTER_WINDOW,
    whole_below: int = POINTER_WHOLE_FILE_LINES,
) -> list[Slice]:
    """Evidence for a file some claim pointed at, minus whatever round 1 already showed.

    `covered` is the line ranges of that file already in the evidence, and it decides the
    shape of the answer:

    * **unseen file** (`covered` empty) — the whole file when it is small, otherwise a
      generous window around each pointed line (merged when they overlap). A pointer with no
      line number gets the head of the file: that is what "look in this file" can mean
      without guessing.
    * **seen file** — only the pointed-at window (± `radius`), and only the part of it no
      round-1 slice contained. Ranking a file does not mean the model read all of it; the
      gaps between its windows are exactly what a pointer into it can still be about. A
      window that is wholly covered yields nothing, which is how "you already read that"
      turns into "no second round".
    """
    lines = text.splitlines()
    n = len(lines)
    if n == 0:
        return []
    if covered:
        windows = (
            [
                (max(1, line - radius), min(n, line + radius))
                for line in sorted(set(lines_of_interest))
            ]
            if lines_of_interest
            else [(1, min(whole_below, n))]
        )
        windows = subtract_ranges(windows, list(covered))
    elif n <= whole_below:
        windows = [(1, n)]
    elif not lines_of_interest:
        windows = [(1, min(whole_below, n))]
    else:
        windows = _merge(
            [
                (max(1, line - radius), min(n, line + radius))
                for line in sorted(set(lines_of_interest))
            ]
        )
    return [
        Slice(path=rel_path, start=start, end=end, text="\n".join(lines[start - 1 : end]))
        for start, end in windows
    ]


# ---------------------------------------------------------------------------
# The whole stage
# ---------------------------------------------------------------------------


def take_inventory(
    repo: SourceRepo,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_tokens: int = DEFAULT_MAX_EVIDENCE_TOKENS,
) -> Inventory:
    """Rank every file, analyse the top ones, and build the bounded evidence bundle."""
    root = repo.root
    scored: list[tuple[FileSignal, str]] = []
    call_sites: list[CallSite] = []
    prompt_constants: list[PromptConstant] = []
    scanned = 0

    for path in walk_repo(root):
        text = read_text(path)
        if text is None:
            continue
        scanned += 1
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() in DATA_SUFFIXES and TRANSCRIPT_RE.search(text):
            # A recorded API response, not source that defines the agent. Only data files
            # are judged this way: a .py that mentions "prompt_tokens" is the code that
            # PARSES responses, which is exactly what we want to read.
            continue
        score, reasons, lines = score_text(rel, text)
        if path.suffix == ".py":
            calls, prompts, extra = analyze_python(rel, text)
            if calls:
                score += 8.0 * min(len(calls), 3)
                reasons.append(f"ast call sites x{len(calls)}")
            if prompts:
                score += 5.0 * min(len(prompts), 3)
                reasons.append(f"ast prompt constants x{len(prompts)}")
            call_sites.extend(calls)
            prompt_constants.extend(prompts)
            lines = sorted(set(lines) | set(extra))
        else:
            calls = analyze_regex(rel, text)
            call_sites.extend(calls)
        if score <= 0:
            continue
        scored.append(
            (
                FileSignal(
                    path=rel, score=round(score, 2), reasons=reasons, lines=lines,
                    n_lines=text.count("\n") + 1,
                ),
                text,
            )
        )

    scored.sort(key=lambda pair: (-pair[0].score, pair[0].path))
    ranked = scored[:max_files]

    slices: list[Slice] = []
    used = 0
    truncated = False
    for signal, text in ranked:
        for piece in slice_file(signal.path, text, signal.lines):
            cost = estimate_tokens(piece.text) + 16  # header line for the citation
            if used + cost > max_tokens:
                truncated = True
                continue
            slices.append(piece)
            used += cost

    # Call sites and prompt constants are reported for the whole repo, most-relevant first.
    ranked_paths = {signal.path: index for index, (signal, _) in enumerate(ranked)}
    call_sites.sort(key=lambda c: (ranked_paths.get(c.path, 10_000), c.path, c.line))
    prompt_constants.sort(key=lambda p: (ranked_paths.get(p.path, 10_000), p.path, p.line))

    return Inventory(
        repo=repo,
        files=[signal for signal, _ in ranked],
        call_sites=call_sites,
        prompt_constants=prompt_constants,
        slices=slices,
        evidence_tokens=used,
        scanned_files=scanned,
        candidate_files=len(scored),
        truncated=truncated,
        max_tokens=max_tokens,
    )


def render_slices(slices: list[Slice]) -> str:
    """Slices as the extraction prompt sees them: cited, ordered, line-numbered."""
    blocks: list[str] = []
    for piece in slices:
        numbered = "\n".join(
            f"{piece.start + offset:>5}| {line}"
            for offset, line in enumerate(piece.text.splitlines())
        )
        blocks.append(f"===== FILE {piece.path} lines {piece.start}-{piece.end} =====\n{numbered}")
    return "\n\n".join(blocks)


def render_evidence(inventory: Inventory) -> str:
    """The round-1 evidence bundle."""
    return render_slices(inventory.slices)
