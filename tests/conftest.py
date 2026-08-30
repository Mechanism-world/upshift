"""Test bootstrap.

On macOS, uv's editable-install .pth file under site-packages can carry the UF_HIDDEN
flag, and CPython skips hidden .pth files — `import upshift` then fails even though the
venv is fine (DESIGN.md, dev-env gotcha). Fall back to importing straight from src/ so
`uv run pytest` always works; `chflags nohidden .venv/lib/python3.12/site-packages/*.pth`
fixes the CLI entry point itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import upshift  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
