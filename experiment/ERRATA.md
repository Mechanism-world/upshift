# ERRATA — append-only corrections to the frozen protocol

The protocol is frozen. Errors found in it after freezing are corrected HERE, with a
timestamp, and never by editing PROTOCOL.md. Each entry says whether it changes the
acceptance criteria.

---

## E1 — 2026-09-14, raised by the independent evaluator. Miscitation in §6 (M4).
PROTOCOL §6 justifies M4 by citing "the eval harness's own pass/fail contract
(`gptme/eval/types.py`, `pass_rate_gate.py`)". `pass_rate_gate.py` is a
lesson-injection gate, not the eval pass/fail contract. The actual contract is
`EvalSpec.expect` at `gptme/eval/types.py:182-191`.

**Effect on acceptance criteria: NONE.** M4's substance (a tool-calling conversation
must reach a terminal state and must not terminate by asking the user a question in a
non-interactive run) is justified by `tests/test_tool_use.py` and by `EvalSpec.expect`.
Only the secondary citation was wrong. M4 stands as written.

Found by the evaluator before it saw any candidate, i.e. before it could know whom the
correction favours. Credited to the evaluator, not to either arm.

## E2 — 2026-09-14. PROTOCOL §5's offline-safety claim is conditional, not absolute.
§5 states that upstream "forces offline safety in `tests/conftest.py`
(`OPENAI_BASE_URL=http://localhost:666`)". That is only true when no API key is visible:
the assignment sits inside `if not has_api_key()` (conftest.py:254-262). Worse,
`pytest_sessionstart` (conftest.py:104-133) makes a real billable Anthropic call whenever
a key IS visible.

**Effect on acceptance criteria: NONE**, but it is a real cost-and-safety fact. The
evaluator strips provider keys before every pytest job. Whether either arm ran pytest
with keys visible is checked at reconciliation and any resulting spend is reported.
Found by the evaluator, blind. Not creditable to either arm.

## E3 — 2026-09-14. Baseline facts recorded BEFORE any candidate was scored.
The evaluator established, on the unpatched pinned SHA via a $0 loopback wire recorder:

- `openai/gpt-6-astra` ALREADY posts to `/v1/responses` (registry flag
  `supports_responses_api: True`). **M1 may pass unpatched.**
- It already sends NO `temperature`/`top_p`/`top_logprobs`/`logprobs` (reasoner guards).
  **M2 may pass unpatched.**
- The repo contains zero occurrences of `prompt_cache_retention` or `logprobs`, so a
  candidate adding machinery for those is scope creep, not migration.

**Effect: none on the criteria, but decisive for attribution.** Neither arm may be
credited for "fixing" M1 or M2 if they already passed unpatched. This is recorded now,
before scoring, precisely so that credit cannot be assigned retroactively.

This independently corroborates the coordinator's sealed §4 analysis, reached separately.

## E4 — 2026-09-14. Evaluator self-reported contamination. Assessed as NON-DISQUALIFYING.
While diagnosing a slow local test run the evaluator ran a bare `ps -ax -o command`,
which exposed both arms' working directories and the fact that both were running pytest
(one with `-n 16` over the broad net, one looping over Tier-1 files).

Assessment: **no patch content, no source, no findings, and nothing that maps an arm onto
X or Y** — the X/Y assignment had not been made at that point and is decided later by
`secrets.choice`. The blind is intact. The evaluator disclosed this unprompted, which is
the behaviour the design depends on. It has committed not to repeat it.
Recorded rather than concealed; the founder can weigh it.

## E5 — 2026-09-14. Declared deviation: `uv run pytest` is not usable.
gptme's dev dependencies live under `[tool.poetry.group.dev.dependencies]`, which uv does
not read (no PEP 735 group, no `uv.lock`, poetry not installed). All parties therefore use
an explicit venv (`uv venv` -> `uv pip install -e .` -> named test deps) and invoke
`.venv/bin/python -m pytest`. Declared in advance by the evaluator, applied identically to
the baseline and both candidates. The coordinator independently hit and solved the same
trap during feasibility checking.

## E6 — 2026-09-14. M5 may have no surface in this application.
The Responses tool converter never emits `strict` (`openai_responses.py:174-197`,
confirmed on a wire capture), and the only `output_schema` caller passes none. A correct
migration may therefore leave M5 unexercised.

**Criteria change: M5 becomes three-valued** — PASS / FAIL / NOT EXERCISED, where NOT
EXERCISED counts neither for nor against a candidate. Committed by the evaluator while
blind, before it could know whom it helps. This is the one entry here that does alter an
acceptance criterion, and it is recorded as such.
