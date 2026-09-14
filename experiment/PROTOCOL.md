# PROTOCOL — frozen 2026-09-14, before either migration arm starts

Controlled comparison: migrating a real OpenAI agentic application to GPT-6 Astra
with a normal state-of-the-art workflow (Arm A) versus the same workflow plus Upshift
(Arm B), scored by an independent blinded evaluator (Arm C).

Nothing in this file may change after an arm starts. Departures are recorded, not edited.

---

## 1. Repository

**gptme/gptme** — https://github.com/gptme/gptme — MIT — Python — ~4,414 stars.

A terminal coding agent: it holds a multi-turn conversation, calls local tools (shell,
python, patch, browser, computer), and ships both a mocked unit suite and a live
deterministic eval harness.

### Why this one (and not the others)
Five candidates were verified by reading actual source, not summaries:
gptme, simonw/llm, arc53/DocsGPT, home-assistant/core (`openai_conversation`),
browser-use/browser-use. Scores out of 5:

| | tests/evals | real agent behavior | baseline reproducibility | realistic Astra migration | runs natively | contamination risk |
|---|---:|---:|---:|---:|---:|---:|
| **gptme** | **5** | **5** | 4 | **5** | 4 | **none** |
| simonw/llm | 5 | 2 | 5 | 3 | 5 | none |
| DocsGPT | 3 | 4 | 3 | 4 | 3 | none |
| home-assistant | 4 | 3 | 2 | 3 | 2 | none |
| browser-use | 2 | 5 | 2 | 5 | 2 | none |

gptme wins because it is the only candidate that is simultaneously (a) a real agentic
application rather than provider plumbing, (b) equipped with a *deterministic live eval
harness* (`gptme/eval/`, `expect: dict[str, Callable[[ResultContext], bool]]` in
`gptme/eval/types.py`, `gptme/eval/pass_rate_gate.py`) which gives both arms — and any
tool — a legitimate native execution path, and (c) carries dedicated upstream tests for
the exact contracts Astra changes (`tests/test_llm_openai_reasoning_effort.py`,
`tests/test_llm_openai_sampling.py`).

simonw/llm was the runner-up and was rejected as "provider plumbing, not application
behavior" — a migration there tests the OpenAI adapter layer, not an agent.
browser-use has the most naive wire surface but its agent tests mock at the
`BaseChatModel` boundary, so its suite could not adjudicate anything.

### Contamination check — PASSED
Searched the Upshift repo and the private rescue-ops repo. gptme was never a rescue
case, never investigated, never contacted, and no finding of ours exists for it. The
only string matches are an unrelated HuggingFace model-name list vendored inside a
litellm copy, and a raw GitHub discovery dump that scraped gptme's *own* upstream PRs.

### Pinned starting commit — BOTH ARMS START HERE, BYTE FOR BYTE
```
3411afec50c9e1124441bec1b00c0831400c8592
```
(master, committed 2026-09-14T11:55:21Z)

---

## 2. Current working model

`gpt-5.6-sol` — `gptme/llm/models/recommended.py:23`, key `"openai"`.

## 3. Target model

`gpt-6-astra`. Verified against current official documentation on 2026-09-14; the full
extract with sources is in `experiment/ASTRA_FACTS.md`. Summary of what actually changes:

| Change | Status for this app |
|---|---|
| Function calling requires `/v1/responses`; Chat Completions does not support it | gptme already gates on `supports_responses_api`; Astra's registry entry sets it True |
| `reasoning_effort: 'none'` returns HTTP 400 | **LATENT DEFECT** — see §4 |
| Remove `temperature`, `top_p`, `top_logprobs` | gptme guards these behind `if not is_reasoner`; Astra is a reasoner, so they are not sent |
| `prompt_cache_retention` -> `prompt_cache_options.ttl` | to be assessed by the arms |
| Behavioral: asks clarifying questions more often; more verbose/formatted output; verifies more thoroughly; delegates less | **PRIMARY RISK** — see §4 |

Note the documented trap: the 400 for tools-on-chat-completions advertises
`reasoning_effort: 'none'` as the fix, and that value is itself rejected on this model.

---

## 4. The migration risk in THIS application

This app is unusually well hardened, and that is deliberate on our part: an app that
fails loudly on the first call would make both arms trivially succeed and teach nothing.
The two real risks here are:

**R1 — latent, config-dependent wire break.** `_OPENAI_REASONING_EFFORTS` in
`gptme/llm/llm_openai.py:1279` is a flat, model-independent set containing `"none"` and
`"minimal"`. gptme validates the user's `GPTME_THINKING_EFFORT` against that set and
forwards the value. On Astra, `none` returns HTTP 400, and Astra's migration guide says
to move off `minimal` too. gptme's own validation reports these values as valid.
Upstream treats this set as a contract it must keep correct
(`test_openai_effort_set_matches_sdk_literal`, and
`test_openai_chat_sends_reasoning_effort` parametrized over
`["minimal","low","medium","high","xhigh"]`), which is what justifies §6's check M3
rather than it being invented for this experiment.

**R2 — behavioral drift in the agent loop (PRIMARY).** For a non-interactive terminal
coding agent, "more likely to ask the user a question when additional input could
materially change the result" is a failure mode, not a feature: the agent stops and asks
instead of acting, and the task fails. Increased verbosity and heavier self-verification
also change turn counts and output shape. None of this raises an API error. This is
precisely the class of regression a mocked unit suite cannot see, and it is the honest
question this experiment exists to answer.

**Neither arm is told R1 or R2.** This section exists so the founder can check afterwards
whether either arm found them, and it is withheld from Arms A, B and C until scoring.

---

## 5. Frozen existing test suite

Run from the repo root after `uv sync`.

**Tier 1 — targeted contract tests (must pass, offline, deterministic):**
```
tests/test_llm_openai.py
tests/test_llm_openai_reasoning_effort.py
tests/test_llm_openai_sampling.py
tests/test_llm_models.py
tests/test_llm_models_resolution.py
tests/test_llm_validate.py
tests/test_tool_use.py
tests/test_tools_choice.py
tests/test_prompt_tools.py
tests/test_util_cost.py
```

**Tier 2 — broad offline regression net:**
```
uv run pytest tests/ -m "not slow and not requires_api and not integration" -q
```
Upstream forces offline safety in `tests/conftest.py` (`OPENAI_BASE_URL=http://localhost:666`).

Tier 1 and Tier 2 are frozen as of the pinned SHA. Their content may not be modified,
weakened, skipped, xfailed, or deleted by either arm. A pre-existing failure at the
pinned SHA is recorded as such and is NOT counted as a migration regression.

---

## 6. Migration-specific acceptance checks (frozen)

Each is justified by an upstream contract, cited. These are ADDITIONS, not replacements.

- **M1 — endpoint.** With the target model selected and tools present, the request must
  reach `/v1/responses`, not `/v1/chat/completions`.
  *Justified by:* `_should_use_responses_api` (`gptme/llm/llm_openai.py`) and the
  `supports_responses_api` registry flag — upstream already treats endpoint choice as
  per-model correctness. *And by* the OpenAI reasoning guide: "Chat Completions does not
  support function calling with GPT-6 Astra."

- **M2 — sampling parameters.** No `temperature`, `top_p`, `top_logprobs` (nor `logprobs`
  on chat completions) may appear in a request to the target model.
  *Justified by:* `tests/test_llm_openai_sampling.py` and the migration guide's removal list.

- **M3 — reasoning effort validity.** Every effort level the application accepts for the
  target model must be one the target model accepts. Concretely: a level the app reports
  as valid must not produce HTTP 400 from the API.
  *Justified by:* `test_openai_effort_set_matches_sdk_literal` and
  `test_openai_chat_sends_reasoning_effort` — upstream already asserts that the accepted
  effort set matches what the provider will take.

- **M4 — tool loop completes.** On the target model, a tool-calling conversation must
  reach a terminal state: the model calls the tool, receives the result, and produces a
  final answer. It must not stall, loop, or terminate by asking the user a question in a
  non-interactive run.
  *Justified by:* `tests/test_tool_use.py` and the eval harness's own pass/fail contract
  (`gptme/eval/types.py`, `pass_rate_gate.py`), which require task completion.

- **M5 — structured output.** Where the app requests a strict schema
  (`"strict": True`, `gptme/llm/llm_openai.py`), the target model's response must conform.
  *Justified by:* the `supports_strict_tools` registry flag and the strict-schema call sites.

**Repetitions.** Every live check runs **N=5** repetitions per case, on both the current
model and the target model. Pass/fail is by threshold across repetitions, never a single
run. N is frozen at 5 and may not be lowered — lowering it would destroy the only
instrument capable of detecting R2.

---

## 7. Allowed documentation
developers.openai.com and platform.openai.com; gptme's own README, docs, tests and
GitHub issues; general language/library documentation.

**Forbidden to both arms:** the other arm's worktree; any Upshift run record, report,
`reports/`, `runs/`, or CLAUDE.md session log predating this experiment; any prior
Mechanism rescue-campaign finding; this file's §4.

## 8. Allowed capabilities
Both arms: read code, search docs, reason, edit source and configuration, run the
existing tests, run the application, add migration-specific tests where an existing
contract justifies them, and make live API calls within budget.
Arm B additionally: **Upshift v0.5.0 at commit `002dd600a53b0789654d2ca4c9a59b47d8437c61`**.
Both arms use the same Claude Code model and configuration.

## 9. Time limit
3 hours wall-clock per migration arm, from first action to final artifact.
**Setup time is included.** Installation, dependency resolution, and — for Arm B —
Upshift installation and adapter/runner construction all count. No exclusions.
The evaluator is not time-limited; it is not part of the efficiency comparison.

## 10. Budget
**No paid call may be made until the founder authorizes a cap for this experiment.**
Prior authorizations ($10 of 2026-09-08, re-scoped 2026-09-11) are treated as CLOSED;
see `experiment/BUDGET_REQUEST.md`. Until then both arms perform offline work only and
stop at the boundary. Sub-caps on authorization: Arm A $1.75, Arm B $3.50, evaluator
$2.00, reserve $0.75.

## 11. Verdicts
**SUCCESS** (per arm): the evaluator, applying that arm's patch alone to the pinned SHA
in a clean checkout, observes (a) Tier 1 and Tier 2 at no worse pass rate than the
pinned-SHA baseline, AND (b) M1-M5 passing at N=5 on live `gpt-6-astra`, AND (c) no test
weakened, deleted, skipped or xfailed.

**FAILURE**: any frozen acceptance check fails, or the existing suite regresses, or the
result was obtained by weakening the suite.

**INCONCLUSIVE**: the evaluator cannot distinguish the arms within budget, the
environment fails, or both patches are equivalent in effect.

A failure present at the pinned SHA on the CURRENT model is not a migration regression.
The baseline is measured first and subtracted.

## 12. Contamination controls
Arms A and B never read each other's directory, patch, notes, metrics or logs. Arm B may
read Upshift's own source and documentation — that is the product under test. The
evaluator writes `EVALUATION_PLAN.md` before seeing any patch, receives the two patches
anonymized as Candidate X / Candidate Y in randomized order, and learns which is which
only after its objective scoring is written to disk.

*Honest limitation:* the arms are subagents on one filesystem in separate directories.
Isolation is enforced by instruction and by not disclosing the other arm's path, not by
an OS boundary. Any breach would be visible in an arm's tool log and is reported if found.

## 13. Product-fix freeze
Upshift is NOT modified during this experiment. A product bug Arm B hits is recorded as
a result, not repaired. Any post-hoc fix and rerun is labelled
"POST-EXPERIMENT PRODUCT-FIXED RERUN" and reported separately from the primary result.
