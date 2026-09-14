#!/usr/bin/env python3
"""Randomly assign the two arms to Candidate X / Candidate Y for blinded scoring.

Written and committed BEFORE either arm produced a patch. The mapping is decided by
secrets.choice at run time, written to a sealed file the evaluator cannot read, and
revealed only after the evaluator's objective scoring is on disk.
"""
import json, secrets, shutil, subprocess, sys
from pathlib import Path

EXP = Path.home() / "Desktop" / "exp914"
SHA = "3411afec50c9e1124441bec1b00c0831400c8592"
SEALED = EXP / "SEALED_MAPPING.json"          # coordinator only
CAND = EXP / "candidates"                      # evaluator reads this

def main() -> int:
    if SEALED.exists():
        print(f"refusing to re-randomise: {SEALED} already exists", file=sys.stderr)
        return 1
    arms = {"A": EXP / "w1", "B": EXP / "w2"}
    for arm, d in arms.items():
        if not (d / "final.patch").is_file():
            print(f"arm {arm}: {d/'final.patch'} missing — not ready", file=sys.stderr)
            return 1

    first = secrets.choice(["A", "B"])
    mapping = {"X": first, "Y": "B" if first == "A" else "A"}

    if CAND.exists():
        shutil.rmtree(CAND)
    for label, arm in mapping.items():
        dest = CAND / label
        dest.mkdir(parents=True)
        # a clean checkout at the pinned SHA with ONLY the patch applied
        subprocess.run(["git", "clone", "-q", str(EXP / "upstream" / "gptme"),
                        str(dest / "gptme")], check=True)
        subprocess.run(["git", "checkout", "-q", SHA], cwd=dest / "gptme", check=True)
        shutil.copy2(arms[arm] / "final.patch", dest / "candidate.patch")
        # NOTE: the patch is deliberately NOT applied here — the evaluator applies it
        # itself, per its own frozen plan, so application is part of what it verifies.

    SEALED.write_text(json.dumps(mapping, indent=2))
    print(f"sealed mapping written to {SEALED}")
    print(f"candidates prepared in {CAND} (X and Y) — arm identity NOT present there")
    for label in ("X", "Y"):
        leaked = [p for p in (CAND / label).rglob("*")
                  if p.name in {"RESULT.md", "metrics.json", "UPSHIFT_CONTRIBUTION.md"}]
        if leaked:
            print(f"LEAK in {label}: {leaked}", file=sys.stderr)
            return 1
    print("leak check: PASS — no arm artifacts inside candidate trees")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
