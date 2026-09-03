"""Stage 1: walk the repo, rank files by static signal, find the call sites, slice evidence.

Nothing here calls a model. Its whole job is to turn "a repo" into a small, ordered,
citable bundle of source windows — every slice carries `path:start-end` so everything the
extraction stage says can be traced back to a real line of a real file.

Python files get an AST pass (call sites with their keyword arguments, string constants that
look like prompts, `{"type": "function"}` dict literals). Everything else falls back to the
same signal regexes without structure.

Jupyter notebooks
-----------------
A `.ipynb` is never shown to the pipeline as JSON. `read_text` renders it — deterministically,
and identically everywhere a cited file is re-read (round-1 slicing, round-2 pointer slicing,
the verbatim gate) — into a synthetic text document: the notebook's cells in order, each
preceded by a marker line

    # --- cell <index> (code) ---
    # --- cell <index> (markdown) ---

where `<index>` is the cell's position in the notebook's own `cells` list. Code cells
contribute their source verbatim; markdown (and raw) cells contribute their lines prefixed
with `# `, so a README-style usage example written in a markdown cell stays citable evidence
and the whole document still reads as valid-enough Python for the signal regexes. Outputs,
execution counts and metadata are dropped entirely.

**Line numbers in a citation `path:line` for a notebook are lines of THAT rendered document,
not of the JSON file.** The `# --- cell N ---` markers are what let a human map a cited line
back to a cell: count the lines since the nearest marker above it. Because every consumer
goes through `read_text`, the model's citations and the verbatim gate agree by construction.

The rendered text may not parse as Python (`%magics`, `!shell`), so the AST pass is only used
for a notebook when it parses; otherwise it falls back to the regex call-site scan, exactly as
a non-Python file does.
"""

from __future__ import annotations

import ast
import json
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
        "htmlcov", "coverage", ".next", "target", ".ipynb_checkpoints",
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
        # Read through `render_notebook`, never as raw JSON (see the module docstring).
        ".ipynb",
    }
)

#: Suffix whose bytes are notebook JSON. Not in DATA_SUFFIXES: it is source, and it is never
#: put into the evidence as JSON, so the transcript heuristic and the small data cap that
#: exist for .json dumps do not apply to it.
NOTEBOOK_SUFFIX = ".ipynb"

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
    # --- Anthropic Messages API (DESIGN.md "Anthropic provider (v0.3)") ---------------
    # Weighted to match the OpenAI signals rung for rung: the dedicated call site scores
    # like chat.completions.create, the bare attribute like responses.create, the import
    # like the openai import, so an Anthropic repo ranks the way an OpenAI one does.
    (r"\bclient\.messages\.create\b|\.beta\.messages\.create\b", 10.0, "messages.create call"),
    (r"\bmessages\.create\b", 6.0, "messages.create call"),
    (r"^\s*(from|import)\s+anthropic\b|^\s*from\s+anthropic\b", 5.0, "anthropic import"),
    (r"@anthropic-ai/sdk", 5.0, "@anthropic-ai/sdk import"),
    (r"\bAsyncAnthropic\s*\(|\bAnthropic\s*\(", 6.0, "Anthropic client construction"),
    (r"\binput_schema\b", 7.0, "Anthropic tool schema literal"),
    (r"\boutput_config\b", 4.0, "output_config (effort)"),
    (r"\bthinking\s*=|[\"']thinking[\"']\s*:", 3.0, "thinking param"),
    (r"\bsystem\s*=", 4.0, "system= kwarg"),
    (r"\bmax_tokens\b", 2.0, "max_tokens"),
    (r"anthropic-version", 3.0, "anthropic-version header"),
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
    "messages.create",
    "beta.messages.create",
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


def check_clone_url(url: str) -> None:
    """Raise unless `url` is a clone URL git will treat as a URL and not as an option.

    A source that starts with `-` is argument injection, not a repository: `git clone` would
    parse `--upload-pack=…` / `--separate-git-dir=…` as its own flag. The scheme allowlist is
    the same one `is_git_url` recognises, plus the `.git`-suffixed forms scp-style remotes
    take; anything else is either a local path (handled without git) or not ours to run.
    """
    candidate = str(url or "").strip()
    if not candidate:
        raise ValueError("empty clone URL")
    if candidate.startswith("-"):
        raise ValueError(
            f"refusing to clone {candidate!r}: a source starting with '-' would be read by "
            f"git as an option, not a URL"
        )
    if not GIT_URL_RE.match(candidate) and not candidate.endswith(".git"):
        raise ValueError(
            f"refusing to clone {candidate!r}: expected https://, git://, ssh:// or git@ "
            f"(or a path ending in .git)"
        )
    if "\n" in candidate or "\r" in candidate or "\x00" in candidate:
        raise ValueError(f"refusing to clone {candidate!r}: URL contains a control character")


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
    """Shallow-clone `url` into `dest`; returns the resolved commit sha.

    `--depth 1` does not run the remote repository's hooks and does not initialise its
    submodules, and no other git subcommand runs against the clone (only `rev-parse HEAD`),
    so nothing in a hostile repository is executed by cloning it.
    """
    check_clone_url(url)
    # `--` terminates option parsing: without it a "URL" like `--upload-pack=…` would be
    # read by git as one of its own flags. `check_clone_url` refuses those too; both guards
    # stay, because either one alone is a single point of failure.
    result = _git(["clone", "--depth", "1", "--", url, str(dest)])
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
        check_clone_url(source)
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


def inside_root(path: Path, root: Path) -> bool:
    """Whether `path` really lives under `root` once every symlink is followed.

    The target repository is untrusted input: a checked-in symlink named `config.py` that
    points at `~/.ssh/id_rsa` or `/etc/passwd` would otherwise be read and sent to the model
    as evidence. Anything whose resolved path leaves the repo is not repository source.
    """
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:  # broken link, permission denied, resolution loop
        return False


def walk_repo(root: Path) -> list[Path]:
    """Every candidate text file, sorted, vendored/build directories skipped.

    Symlinks that leave the repository are skipped — see `inside_root`. A symlink pointing
    *within* the repo is kept: it names source the repo itself contains.
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        # Every path, not just `path.is_symlink()`: a symlinked *directory* would escape the
        # same way, and whether `rglob` descends into one is a Python-version detail.
        if not inside_root(path, root):
            continue
        if readable_source(path):
            found.append(path)
    return found


def _cell_lines(cell: dict) -> list[str]:
    source = cell.get("source")
    if isinstance(source, str):
        return source.splitlines()
    if isinstance(source, list):
        return "".join(part for part in source if isinstance(part, str)).splitlines()
    return []


def render_notebook(raw: str) -> str | None:
    """Notebook JSON -> the synthetic text document the whole pipeline cites.

    Cells in notebook order, each under a `# --- cell <index> (<type>) ---` marker; code cell
    source verbatim, every other cell type commented with `# `. Outputs are ignored. None
    when the bytes are not a notebook (malformed JSON, no `cells` list) — the caller treats
    that exactly like an unreadable file and skips it.

    Deterministic: same bytes in, same text out, so a `path:line` citation means the same
    thing in the evidence bundle, in a round-2 slice and in the verbatim gate.
    """
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("cells"), list):
        return None
    lines: list[str] = []
    for index, cell in enumerate(doc["cells"]):
        if not isinstance(cell, dict):
            continue
        kind = str(cell.get("cell_type") or "raw")
        lines.append(f"# --- cell {index} ({kind}) ---")
        body = _cell_lines(cell)
        if kind == "code":
            lines.extend(body)
        else:
            lines.extend(f"# {line}" if line.strip() else "#" for line in body)
    return "\n".join(lines) + "\n" if lines else ""


def read_text(path: Path) -> str | None:
    """The text of a source file as every stage of adapt sees it.

    The one place notebooks are decoded: for a `.ipynb` this returns the rendered cell
    document (see `render_notebook` and the module docstring), never the raw JSON. Every
    consumer that re-reads a cited file goes through here, which is what keeps the model's
    line numbers and the verbatim gate talking about the same document.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if path.suffix.lower() == NOTEBOOK_SUFFIX:
        return render_notebook(raw)
    return raw


def parses_as_python(text: str) -> bool:
    """Whether the AST pass can be used on this text at all."""
    try:
        ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return True


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


#: A keyword argument's rendered value is evidence, not data: past this it is a hostile
#: literal, and the slice it lands in has a token budget to respect.
MAX_KWARG_CHARS = 4_000


def _kwarg_source(node: ast.AST, text: str) -> str:
    try:
        value = ast.literal_eval(node)
        rendered = repr(value)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        # ValueError also covers CPython's int -> str digit limit, which `repr` raises on a
        # huge hex literal in the target repo: a file we merely read must never abort adapt.
        segment = ast.get_source_segment(text, node)
        rendered = (segment or "<expr>").strip()
    if len(rendered) > MAX_KWARG_CHARS:
        rendered = rendered[:MAX_KWARG_CHARS] + "…<truncated>"
    return rendered


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
    r"(?P<callee>[\w.]*(?:chat\.completions\.create|responses\.create|messages\.create|"
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
        # A rendered notebook is analysed as Python when it parses (the common case: the
        # cell markers and commented markdown are comments), and falls back to the regex
        # call-site scan when a magic or a shell escape makes it unparseable.
        use_ast = path.suffix == ".py" or (
            path.suffix.lower() == NOTEBOOK_SUFFIX and parses_as_python(text)
        )
        if use_ast:
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
