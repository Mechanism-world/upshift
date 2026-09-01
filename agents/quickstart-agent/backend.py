"""Deterministic, in-memory reimplementation of the four bundled quickstart agent tools.

Upstream (anthropics/claude-quickstarts @ 3313e97, ``agents/``) runs these tools against the
real machine: ``FileReadTool``/``FileWriteTool`` touch the user's filesystem
(``agents/tools/file_tools.py``), ``calculator`` is a FastMCP server spawned over stdio
(``agents/tools/calculator_mcp.py``), and ``ThinkTool`` returns a constant
(``agents/tools/think.py``). An eval suite cannot use a real filesystem — it would be neither
safe nor deterministic — so the file tree lives in ``initial_state`` and every operation is a
pure function of it (ADAPTER.md requirement 3: no clock, no network, no randomness).

What is mirrored, string for string, from upstream:

* ``file_read``  — ``FileReadTool.execute`` (file_tools.py:49-135). Same operation dispatch,
  same ``max_lines`` truncation, same ``📄``/``📁`` listing format, same error sentences
  ("Error: File not found at {path}", "Error: Directory not found at {directory}",
  "No files found matching {directory}/{pattern}", "Error: Unsupported operation '{op}'").
* ``file_write`` — ``FileWriteTool.execute`` (file_tools.py:180-277). Same required-argument
  guards, the same "Successfully wrote {n} characters to {path}" / "Successfully edited
  {path}" / "Warning: Found {n} occurrences. All were replaced in {path}" replies, and the
  same "Error: The specified text was not found in {path}" miss.
* ``calculator`` — ``calculator_mcp.calculator`` (calculator_mcp.py:12-51), operator table,
  division-by-zero and negative-sqrt guards, the ``float -> int`` tidy-up, and the
  ``"Result: {result}"`` envelope.
* ``think``      — ``ThinkTool.execute`` (think.py:30-32) returns the constant
  ``"Thinking complete!"``.

The one shape delta (identical to agents/shell_gpt): upstream hands the model
``str(result)`` as the ``tool_result`` content (``agents/utils/tool_util.py:16``), while
upshift JSON-encodes whatever ``execute`` returns, so upstream's bare string arrives as
``{"output": "..."}``. Upstream's own *error strings* stay inside ``output``, exactly as
upstream sends them; only failures upstream would raise on (unknown tool, non-object
arguments, a missing required argument — a ``TypeError`` caught at tool_util.py:20-22 and
returned with ``is_error``) come back as ``{"error": ...}`` per the ADAPTER.md contract.

``initial_state`` schema::

    {"files": {"data/a.txt": "12\\n", "notes.txt": "..."}}

Paths are relative, ``/``-separated, and directories are implied by them. ``state()``::

    {"files":       {relpath: content},      # the tree after the episode
     "paths":       [relpath, ...],          # sorted
     "writes":      [relpath, ...],          # every path file_write changed, in call order
     "write_count": <len(writes)>}           # the no-over-acting assertion

``writes``/``write_count`` are upshift's, not upstream's: upstream has no notion of a write
log. They exist so a case can assert "the model changed nothing" without guessing at file
bytes.
"""

from __future__ import annotations

import copy
import fnmatch
import math
from typing import Any

#: Upstream's ThinkTool.execute return value (think.py:32), verbatim.
THINK_RESULT = "Thinking complete!"


class Backend:
    """One episode's file tree. Construct via :func:`create_backend`."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        state = copy.deepcopy(initial_state or {})
        files = state.get("files") if isinstance(state, dict) else None
        self._files: dict[str, str] = (
            {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {}
        )
        self._writes: list[str] = []

    # -- ADAPTER.md contract ------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(arguments, dict):
                return {"error": f"arguments must be an object, got {type(arguments).__name__}"}
            handler = {
                "file_read": self._file_read,
                "file_write": self._file_write,
                "calculator": self._calculator,
                "think": self._think,
            }.get(name)
            if handler is None:
                return {"error": f"unknown tool: {name}"}
            return handler(arguments)
        except Exception as exc:  # noqa: BLE001 - defensive: execute never raises
            return {"error": f"backend failure in {name}: {exc}"}

    def state(self) -> dict[str, Any]:
        return {
            "files": dict(sorted(self._files.items())),
            "paths": sorted(self._files),
            "writes": list(self._writes),
            "write_count": len(self._writes),
        }

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _norm(path: str) -> str:
        """``./data/`` and ``data`` are the same directory; ``.`` and ``""`` are the root."""
        cleaned = str(path).strip()
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        cleaned = cleaned.rstrip("/")
        return "" if cleaned == "." else cleaned

    def _is_dir(self, path: str) -> bool:
        return path == "" or any(p.startswith(path + "/") for p in self._files)

    @staticmethod
    def _require(arguments: dict[str, Any], key: str) -> tuple[str | None, dict | None]:
        value = arguments.get(key)
        if not isinstance(value, str) or not value:
            return None, {"error": f"missing required argument: {key}"}
        return value, None

    # -- tools --------------------------------------------------------------

    def _file_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        operation, err = self._require(arguments, "operation")
        if err:
            return err
        path, err = self._require(arguments, "path")
        if err:
            return err
        if operation == "read":
            return {"output": self._read_file(path, arguments.get("max_lines") or 0)}
        if operation == "list":
            return {"output": self._list_files(path, str(arguments.get("pattern") or "*"))}
        return {"output": f"Error: Unsupported operation '{operation}'"}

    def _read_file(self, path: str, max_lines: Any) -> str:
        key = self._norm(path)
        if key not in self._files:
            # Upstream distinguishes "no such path" from "a directory" (file_tools.py:84-87).
            if self._is_dir(key):
                return f"Error: {path} is not a file"
            return f"Error: File not found at {path}"
        content = self._files[key]
        try:
            limit = int(max_lines)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0:
            lines = content.splitlines(keepends=True)[:limit]
            return "".join(lines)
        return content

    def _list_files(self, directory: str, pattern: str) -> str:
        key = self._norm(directory)
        if key not in self._files and not self._is_dir(key):
            return f"Error: Directory not found at {directory}"
        if key in self._files:
            return f"Error: {directory} is not a directory"
        prefix = f"{key}/" if key else ""
        # Upstream globs `f"{directory}/{pattern}"`, which matches direct children only
        # (a pattern carrying "/" reaches deeper). Same rule, over the in-memory tree.
        names: set[str] = set()
        for path in self._files:
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            names.add(rest if "/" in pattern else rest.split("/", 1)[0])
        matched = sorted(n for n in names if fnmatch.fnmatch(n, pattern))
        if not matched:
            return f"No files found matching {directory}/{pattern}"
        return "\n".join(
            f"📁 {name}/" if self._is_dir(f"{prefix}{name}") else f"📄 {name}" for name in matched
        )

    def _file_write(self, arguments: dict[str, Any]) -> dict[str, Any]:
        operation, err = self._require(arguments, "operation")
        if err:
            return err
        path, err = self._require(arguments, "path")
        if err:
            return err
        if operation == "write":
            content = arguments.get("content") or ""
            if not content:
                return {"output": "Error: content parameter is required"}
            key = self._norm(path)
            self._files[key] = str(content)
            self._writes.append(key)
            return {"output": f"Successfully wrote {len(str(content))} characters to {path}"}
        if operation == "edit":
            old_text = arguments.get("old_text") or ""
            new_text = arguments.get("new_text") or ""
            if not old_text or not new_text:
                return {
                    "output": "Error: both old_text and new_text parameters "
                    "are required for edit operation"
                }
            return {"output": self._edit_file(path, str(old_text), str(new_text))}
        return {"output": f"Error: Unsupported operation '{operation}'"}

    def _edit_file(self, path: str, old_text: str, new_text: str) -> str:
        key = self._norm(path)
        if key not in self._files:
            if self._is_dir(key):
                return f"Error: {path} is not a file"
            return f"Error: File not found at {path}"
        content = self._files[key]
        if old_text not in content:
            return f"Error: The specified text was not found in {path}"
        count = content.count(old_text)
        self._files[key] = content.replace(old_text, new_text)
        self._writes.append(key)
        if count > 1:
            return f"Warning: Found {count} occurrences. All were replaced in {path}"
        return f"Successfully edited {path}"

    def _calculator(self, arguments: dict[str, Any]) -> dict[str, Any]:
        for key in ("number1", "number2", "operator"):
            if key not in arguments:
                return {"error": f"missing required argument: {key}"}
        operator = arguments["operator"]
        try:
            number1 = float(arguments["number1"])
            number2 = float(arguments["number2"])
        except (TypeError, ValueError):
            return {"error": "number1 and number2 must be numbers"}
        if operator == "+":
            result: float = number1 + number2
        elif operator == "-":
            result = number1 - number2
        elif operator == "*":
            result = number1 * number2
        elif operator == "/":
            if number2 == 0:
                return {"output": "Error: Division by zero"}
            result = number1 / number2
        elif operator == "^":
            result = number1**number2
        elif operator == "sqrt":
            if number1 < 0:
                return {"output": "Error: Cannot take square root of negative number"}
            result = math.sqrt(number1)
        else:
            return {"output": f"Error: Unsupported operator '{operator}'"}
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return {"output": f"Result: {result}"}

    def _think(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if "thought" not in arguments:
            return {"error": "missing required argument: thought"}
        return {"output": THINK_RESULT}


def create_backend(initial_state: dict[str, Any]) -> Backend:
    """Build a backend seeded from a case's ``initial_state`` (ADAPTER.md)."""
    return Backend(initial_state)
