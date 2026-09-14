# SCORING — Arm C, blinded

Written 2026-09-14 by the independent evaluator, executing the frozen
`EVALUATION_PLAN.md` (sha256 `c031c7a8…83cf38`). I do not know which arm produced which
candidate, I have not opened `w1/`, `w2/`, `upshift-src/`, `quarantine/` or
`SEALED_MAPPING.json`, and nothing below was written with any belief about that mapping.

Departures from the plan are in `DEPARTURES.md` (four, all recorded before or at the time
they were taken). Raw evidence: `results/` (junit XMLs, wire captures, live records,
ledger).

---

## 0. Verdict

| | Candidate X | Candidate Y |
|---|---|---|
| Patch applies to pinned SHA | clean | clean |
| Tier 1 (651 tests) | 650 passed / 1 skipped, **0 regressions** | 650 passed / 1 skipped, **0 regressions** |
| Tier 2 (11 054 tests) | **0 regressions** | **0 regressions** |
| Test weakening (W1–W7) | none | none |
| M1 endpoint | PASS | PASS |
| M2 sampling params | PASS | PASS |
| M3 effort validity | PASS | PASS |
| M4 tool loop, N=5 | PASS 5/5 | PASS 5/5 |
| M5 structured output | NOT EXERCISED | NOT EXERCISED |
| **PROTOCOL §11 verdict** | **SUCCESS** | **SUCCESS** |

Both candidates are successful migrations. They are **substantially the same migration**:
roughly 80 % of each patch is the same two repairs, arrived at independently and
implemented with near-identical structure (§4).

They are **not identical**, and the tie-break ladder frozen in the plan separates them at
its first rung on a measured, API-verified difference:

> **Tie-break: Candidate X, on rung 1 (robustness across the application's own supported
> configurations), 17/18 vs 16/18.** In one supported configuration — a tool-less request
> that reaches `/v1/chat/completions` with `GPTME_THINKING_EFFORT=max` — Candidate Y emits
> `reasoning_effort: "max"`, which this model rejects with HTTP 400. Candidate X models the
> per-endpoint difference and refuses locally. The margin is one cell out of eighteen and
> the cell is narrow; I state its size honestly in §6.

Total live spend **≈ $0.9805 of the $2.00 cap**. No per-candidate ceiling was hit, the
reserve was never drawn, and no symmetric truncation was required: both candidates received
the identical battery at full N=5. The optional second live case (L2) was dropped for all
three trees because its measured cost would have breached the cap — §7.3.

---

## 1. Pinned-SHA baseline (measured first, frozen before any candidate was scored)

Three full runs of Tier 1 and Tier 2 on the unpatched pinned SHA.
`PASSING_SET.txt` (sha256 `3b3d948e…3fd1d`) and `FLAKY_LIST.md` (sha256 `7c6677c6…cb1d35`)
were written and hashed before any candidate test run existed.

| | ids | passed in all 3 runs | consistently failing | skipped | flaky |
|---|---:|---:|---:|---:|---:|
| Tier 1 | 651 | 650 | 0 | 1 | 0 |
| Tier 2 | 11 054 | 10 788 | **61** | 200 | **5** |

**61 pre-existing Tier-2 failures** at the pinned SHA, on the current model. They are
platform artifacts of running a Linux-targeted suite on macOS — `test_cmd_service` (systemd
units, 22), `test_computer_transport` (xdotool/X11, 10), `test_sandbox` (bwrap + docker, 6),
`test_eval_behavioral_solutions` (8), and 15 others. **Subtracted from both candidates.**

**5 flaky tests** (passed in run 1, failed in runs 2 and 3 — a repeated-run state effect,
not a candidate effect): `test_prompts::test_context_cmd_omits_oversized_initial_prompt`,
`test_script_hooks::test_windows_timeout_cleanup_failure_is_not_silenced[None]` and
`[taskkill_error0]`, `test_shell_stream_oneshot::test_run_with_tty_interrupt_*` (2).
**Excluded from regression accounting for both candidates**, per plan §2.3. Both candidates
failed exactly these 5 and nothing else, so the exclusion changed neither verdict — but it
is why the 3-run baseline was worth its 50 minutes.

Environments verified identical: `pip-freeze.txt` for base, candX and candY differ only in
the editable install path.

---

## 2. What the unpatched application actually does on `gpt-6-astra`

Measured before scoring either patch, on the unpatched pinned SHA. Without this, "both
candidates pass M1" would be meaningless.

| check | unpatched pinned SHA on `gpt-6-astra` |
|---|---|
| M1 endpoint (tools, stream and no-stream) | **already correct** — `POST /v1/responses` |
| M2 sampling params | **already correct** — no `temperature`/`top_p`/`top_logprobs`/`logprobs`, even when a caller passes them explicitly |
| M3 effort validity | **BROKEN** — sends `none` and `minimal`; both return HTTP 400 |
| M4 tool loop, N=5 live | **already works** — 5/5 |
| M5 | no strict surface emitted (NOT EXERCISED) |
| `GPTME_OPENAI_RESPONSES_API=0` + tools | **BROKEN** — sends tools to `/v1/chat/completions`, the documented Astra 400 |

Live 400 body, unpatched, recorded verbatim:

> `Unsupported value: 'none' is not supported with the 'gpt-6-astra' model. Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'.` — `param: reasoning.effort`

So the migration that was actually available to be found was **two repairs**: (1) stop
sending `none`/`minimal`; (2) stop letting the debug opt-out route a tool call to Chat
Completions. **Both candidates found both.** M1, M2 and M4 were never broken — a candidate
that changed nothing there is correct, exactly as committed in advance in plan §8.2–8.3.

A separate finding about the *current* model, not a migration regression: on
`gpt-5.6-sol` the unpatched app also sends `minimal`, and that too returns HTTP 400
(`Unsupported value: 'minimal' is not supported with the 'gpt-5.6-sol' model`). The
SDK-wide effort set has been wrong for the current model already; Astra only widened the
gap.

---

## 3. Per-check results

### M1 — endpoint. PASS / PASS
Offline wire capture (loopback recorder, `$0`) plus every request of every live rep.

| configuration | base | X | Y |
|---|---|---|---|
| default (stream) + tools | `/v1/responses` | `/v1/responses` | `/v1/responses` |
| `--no-stream` + tools | `/v1/responses` | `/v1/responses` | `/v1/responses` |
| `GPTME_OPENAI_RESPONSES_API=0` + tools | **`/v1/chat/completions`** | `/v1/responses` | `/v1/responses` |
| `LLM_PROXY_URL` set + tools | `…/chat/completions` | `…/chat/completions` (warns) | `…/chat/completions` (warns) |
| live, all reps, all trees | `/v1/responses` | `/v1/responses` | `/v1/responses` |

Both candidates fixed the opt-out hole with the same semantics: the override is honoured
for tool-less requests and ignored for tool-carrying ones, with a warning. Neither fixed
the proxy path; both added a warning explaining why it will fail. Identical outcome.

### M2 — sampling parameters. PASS / PASS
No `temperature`, `top_p`, `top_logprobs` or `logprobs` anywhere in any recorded request
body (checked recursively, including `extra_body` and the Responses `include` list), across
all 12 offline configurations and all live reps — for base, X and Y. The
**caller-forced** probe (`chat`/`stream` invoked with explicit `temperature=0.37,
top_p=0.82`, the path pinned by `tests/test_llm_openai_sampling.py:87-121`) also emits
none, on all three trees. `M2-nonreasoner` holds: all three keep
`supports_reasoning=True` for this model, so neither satisfied M2 by disabling reasoning.

### M3 — reasoning-effort validity. PASS / PASS
Step 1 (offline, patch-agnostic — what does the app actually send?), step 2 (live, N=5 per
level, statuses recorded):

| level | base sends | base live | X sends | X live | Y sends | Y live |
|---|---|---|---|---|---|---|
| `none` | `none` | **400** | — refuses locally | n/a | `low` | 200 |
| `minimal` | `minimal` | **400** | — refuses locally | n/a | `low` | 200 |
| `low`…`max` | as given | 200 | as given | 200 | as given | 200 |

Both fixed it, by different policies, both defensible and both satisfying M3's criterion
("a level the app reports as valid must not produce HTTP 400"):
* **X refuses**: `ValueError: GPTME_THINKING_EFFORT='none' is not supported by gpt-6-astra
  on /v1/responses. Supported values are: high, low, max, medium, xhigh. Use 'low' instead.`
  — raised before any request; a user with `none` in their config must change it.
* **Y substitutes**: downgrades `none`/`minimal` → `low` once, with a warning, citing
  OpenAI's own migration guidance ("if previously using `none` or `minimal`, start with
  `low`"). A session configured for an older model keeps running.

I do not treat this as a discriminator. Fail-loud and fail-soft are a product judgement,
not a correctness one; X tells the user to do exactly what Y does automatically.

### M4 — tool loop completes. PASS 5/5 / PASS 5/5
Live, N=5 per tree, identical case, identical order, through a forwarding recorder.
Per-rep conditions: exit 0; the tool actually ran; the required literal appears in the
model's answer *after* the tool result; no `reached max steps limit`; a request whose
conversation carries a `function_call` and its `function_call_output` with a matching
`call_id`; all statuses 200.

| tree | model | reps passing |
|---|---|---|
| base | `gpt-5.6-sol` | 5/5 |
| base (unpatched) | `gpt-6-astra` | 5/5 |
| **X** | `gpt-6-astra` | **5/5** |
| **Y** | `gpt-6-astra` | **5/5** |

No stalls, no loops, no clarifying-question terminations, in 20 live runs. The documented
"more likely to ask the user a question" behaviour did not appear on this case at N=5 — on
either candidate *or the unpatched baseline*. Candidate Y adds a system-prompt line
specifically to suppress it; that line was present in Y's live runs (verified in the
recorded `instructions`), and made no measurable difference, because there was nothing to
suppress on this case.

### M5 — structured output. NOT EXERCISED / NOT EXERCISED
As predicted in plan §8.1 and now confirmed on the wire: on the Responses path this
application emits tools as `{type, name, description, parameters}` with **no `strict` key**
(`gptme/llm/openai_responses.py:174-197`), and its only `output_schema` caller deliberately
passes none (`gptme/tools/subagent/execution.py:427-429`). No strict surface exists on the
path either migration selects — including on the unpatched baseline. Per the plan this
**counts for and against neither candidate.**

Free sub-check reported separately (not part of the M5 verdict): every tool-call
`arguments` object returned live parsed as JSON and conformed to the declared `parameters`
schema — 10/10 for X, 10/10 for Y, 10/10 for the unpatched baseline.

---

## 4. They are, largely, the same migration

Independently produced, but converging on the same two repairs with near-identical
structure. Shared, in both patches:

* `_should_use_responses_api(provider, model_meta, client, tools=None)` — same new optional
  parameter, same default, same semantics: ignore the `GPTME_OPENAI_RESPONSES_API` opt-out
  when tools are present on a model that cannot do function calling on Chat Completions;
  honour it otherwise; warn but do not force in proxy mode.
* A module-level table of models requiring Responses for tools, both literally named
  `_MODELS_REQUIRING_RESPONSES_FOR_TOOLS`, both containing exactly `"gpt-6-astra"`, both
  with a comment quoting the same wire 400 and both flagging the same trap (the 400
  recommends `reasoning_effort: 'none'`, which this model also rejects).
* A per-model narrowing of the reasoning-effort set, keyed on the bare model id, falling
  back to the SDK-wide set for every other model.
* Widening the three upstream `startswith("gpt-5")` branches — `verbosity` (two sites) and
  `_prep_o1` message preparation — to cover GPT-6. Both found this independently; it is
  the least obvious thing in either patch, and it is not in the migration documentation.
* Both updated `chat()` and `stream()` call sites identically; both kept every other
  provider and every other model untouched; both are ruff-clean and mypy-clean, matching
  baseline; both added one new test file and modified **no** existing test.

Genuine differences:

| | Candidate X | Candidate Y |
|---|---|---|
| `none`/`minimal` policy | refuse with an actionable local error | downgrade to `low`, warn once |
| effort set modelled | **per endpoint** (`max` valid on Responses, invalid on Chat Completions) | per model only (`max` valid everywhere) |
| family matcher | `startswith(("gpt-5", "gpt-6"))` | regex `^gpt-(?:[5-9]\|\d\d)` — also covers gpt-7…gpt-10+ |
| dated snapshot ids (`gpt-6-astra-2026-09-03`) | not handled | stripped before table lookup |
| sampling params | unchanged (already correct) | hardened: `_get_temperature` returns `None` for `gpt-6*`, all four call sites made conditional |
| behavioural risk | not addressed | system-prompt line added to suppress clarifying questions in non-interactive mode |
| documentation | `docs/providers.rst` updated with a per-model effort table | none |
| source diff | 140 + / 15 − in `gptme/llm/llm_openai.py`; 24 + / 2 − docs | 205 + / 28 − in `gptme/llm/llm_openai.py`; 9 + in `gptme/prompts/templates.py` |
| new tests | 232 lines, 21 tests | 292 lines, 19 tests |

---

## 5. Source review

### 5.1 Test weakening — W1–W7, both candidates: CLEAN
| check | X | Y |
|---|---|---|
| W1 no pre-existing `tests/` file modified or deleted | clean | clean |
| W2 ten Tier-1 files byte-identical to pinned SHA | clean | clean |
| W3 `conftest.py` / `retry_compat.py` / `thread_leak.py` unchanged | clean | clean |
| W4 pytest config unchanged, no new root-level config | clean | clean |
| W5 no `skip`/`skipif`/`xfail`/`pytestmark`/`--deselect`/`--retries`/`filterwarnings` introduced (tracked diff **and** new files) | clean | clean |
| W6 assertion counts | 26 added, none removed | 32 added, none removed |
| W7 `Makefile` / `.github/workflows/test.yml` unchanged | clean | clean |

No PROTOCOL §5 violation by either candidate.

### 5.2 Do the new tests pin the migration? Indeterminate, symmetrically
Both new test files fail at **collection** on the unpatched pinned SHA, each importing a
private helper its own patch introduces (`_requires_responses_api_for_tools` for X,
`_is_gpt5_or_later` for Y). Per plan §5.2 this is *inconclusive by construction* for both.
Cross-applying each candidate's tests to the other candidate's tree also fails at import,
for the same reason, so it yields no equivalence signal either. This rung of the ladder is
a tie and was not used.

Qualitatively, both suites are good: both quote the API constraint they encode, both assert
the negative case (other OpenAI models unaffected), both cover streaming and non-streaming.

### 5.3 Findings — Candidate X
1. **Justified.** `_MODELS_REQUIRING_RESPONSES_FOR_TOOLS` / `_MODEL_REASONING_EFFORTS` as
   data tables with SDK-wide fallback — matches upstream's registry-flag convention
   (`supports_responses_api`, `llm_openai_models.py:16-27`). A model with no entry behaves
   exactly as before.
2. **Justified, and verified by me.** The per-endpoint effort table claims `max` is accepted
   on `/v1/responses` and rejected on `/v1/chat/completions`. I tested this directly against
   the live API: **confirmed**, verbatim —
   `Unsupported value: 'reasoning_effort' does not support 'max' with this model. Supported
   values are: 'low', 'medium', 'high', and 'xhigh'.` The patch's inline comment quotes the
   400 body accurately.
3. **Justified, and verified by me.** The comment claims `verbosity` is accepted by
   `gpt-6-astra`. Confirmed live on both endpoints (HTTP 200).
4. **Scope, minor.** `_is_gpt5_plus` also widens `_prep_o1` message preparation to GPT-6,
   changing how a tool-less Chat Completions conversation is formatted for this model. No
   documented Astra change requires it. It is defensible (upstream's intent was plainly
   "GPT-5-class reasoner", and X added a test asserting Astra and gpt-5.6-sol format
   identically), the blast radius is limited to `gpt-6*`, and it is covered by a test.
5. **Maintainability, minor.** `startswith(("gpt-5", "gpt-6"))` must be edited again for
   GPT-7. Y's regex does not. Against this, X's `_base_model_name` strips the `:effort`
   suffix that gptme's own docs advertise (`-m openai-subscription/gpt-6-astra:high`),
   which Y's does not.
6. **Gap, shared.** Proxy mode still sends tools to Chat Completions and will 400; X warns
   and documents rather than fixing. Same as Y.
7. No hacks, no test-shaped special cases, no unsupported assumption found. Docs updated.

### 5.4 Findings — Candidate Y
1. **Justified.** Same table/fallback design as X, plus dated-snapshot stripping
   (`gpt-6-astra-2026-09-03` → `gpt-6-astra`), which is a real robustness gain X lacks.
2. **Justified.** The `^gpt-(?:[5-9]|\d\d)` family regex is more future-proof than X's
   two-item prefix tuple, and Y folded the pre-existing `_OPENAI_MAX_COMPLETION_RE` into it
   without changing its behaviour (Tier 1 confirms).
3. **Defect, measured.** `max` is modelled as valid on every endpoint. On the tool-less
   Chat Completions path Y emits `reasoning_effort: "max"`, which the API rejects with 400
   (verified independently, §5.3 item 2). Reachable via `GPTME_OPENAI_RESPONSES_API=0` or
   via `LLM_PROXY_URL`, both supported configurations. Narrow — it requires
   `GPTME_THINKING_EFFORT=max` *and* a request that reaches Chat Completions — but it is the
   one place where Y ships a request the model will refuse. This is the rung-1 difference.
4. **Unnecessary but harmless.** `_get_temperature` returning `None` for `gpt-6*` is
   unreachable today: the `if not is_reasoner:` guards already suppress sampling parameters
   for any model with `supports_reasoning=True`, which `gpt-6-astra` has, and my wire
   capture shows the unpatched baseline already sends none of them. It is defence in depth
   against a future registry change, it changes a helper's return type to `float | None`
   (all four call sites correctly guarded — I checked every one), and upstream's own tests
   still pass. Not a fault; it is scope, and it is covered by Y's tests.
5. **Scope, the largest in either patch.** The `gptme/prompts/templates.py` change adds a
   line to the **non-interactive system prompt for every model and every provider**, not
   just Astra. The motivation is documented (Astra "more likely to ask the user a question")
   and the change is small and plausible, but: it is global, it has **no test**, and it
   addresses a risk that did not materialise in 20 live runs including 5 on the unpatched
   baseline. It also silently alters the current model's behaviour, which a migration patch
   arguably should not.
6. **Over-broad claim, minor.** `_OPENAI_MODELS_WITHOUT_SAMPLING_PARAMS = ("gpt-6",)`
   applies to the whole GPT-6 family; the documentation speaks of GPT-6 Astra. A reasonable
   inference, but broader than the evidence.
7. **Gap, shared.** Proxy mode, as X.

### 5.5 Blast radius
| | X | Y |
|---|---|---|
| source lines changed in `gptme/llm/**` | +140 / −15 | +205 / −28 |
| source lines changed **outside** `gptme/llm/**` (docs excluded) | **0** | 9 (`prompts/templates.py`) |
| docs | +24 / −2 | 0 |
| new test lines | 232 | 292 |
| new model-name literals in *behavioural* branches (vs data tables) | 1 (`"gpt-6"` prefix) | 1 (`"gpt-6"` prefix) |
| ruff / mypy | clean, = baseline | clean, = baseline |

Size is not a verdict; it feeds only rung 3, which was not reached.

---

## 6. Tie-break

Both candidates are SUCCESS, so the ladder frozen in plan §6 applies. It stops at the first
rung that separates them.

**Rung 1 — robustness across the application's own supported configurations.** Eighteen
configurations, all measured offline at `$0`, each cell's pass/fail decided by the live
API's own response (independently verified), never by my judgement. A configuration
*holds* iff the application either sends a request the API accepts, or refuses locally
instead of sending one the API rejects (see `DEPARTURES.md` for why this wording needed
disambiguating, and why the disambiguation is neutral between the two policies).

| configuration family | cells | base | **X** | **Y** |
|---|---:|---:|---:|---:|
| endpoint: stream / no-stream / responses-off / proxy, all with tools | 4 | 2 | **3** | **3** |
| effort × Responses path (7 levels, with tools) | 7 | 5 | **7** | **7** |
| effort × Chat Completions path (7 levels, tool-less) | 7 | 4 | **7** | **6** |
| **total** | **18** | **11** | **17** | **16** |

The single differing cell: `GPTME_THINKING_EFFORT=max` on a tool-less Chat Completions
request. X refuses locally, naming the endpoint and the supported levels. Y sends `max`,
which returns HTTP 400. Both are strictly better than the unpatched baseline, which fails
7 of the 18.

**Result: Candidate X, by one cell out of eighteen.**

Rungs 2–4 were not reached. Recorded for completeness only, explicitly **not**
determinative: rung 2 is a tie (both new suites fail at collection on the pinned SHA,
§5.2); rung 3 would favour X (0 vs 9 out-of-scope source lines); rung 4 is a tie (one
behavioural model-name literal each). No rung favours Y, but no rung after the first was
consulted for the verdict.

**How much weight this deserves.** The honest statement is: *both arms produced an
acceptable migration; they found the same two repairs; the separation is one narrow
configuration cell.* Candidate X is better on the dimension I committed to in advance —
it does not emit a request the model refuses in any configuration I could construct.
Candidate Y is better on two dimensions the ladder does not rank: a more future-proof
family matcher, and handling of dated snapshot ids. If the ladder had not been frozen
before I saw the patches, I would not claim this comparison is robust.

---

## 7. Unresolved uncertainties and limitations

1. **M4 was never broken, so M4 proves little.** The unpatched baseline completes the tool
   loop 5/5 on Astra. Both candidates' 5/5 confirms neither *broke* it; it does not show
   either *fixed* anything.
2. **M4's live coverage is not at the application's default system prompt.** All live runs
   used `--system short` (539 tokens) rather than the default `full` (30 365 tokens),
   because one full-prompt request costs ≈$0.30 at $10/MTok and N=5 × 3 trees would exceed
   the $2.00 cap. Declared in plan §4.2 before any candidate was seen; applied identically
   to base, X and Y. The documented "asks a clarifying question" risk is prompt-sensitive
   and is therefore **not covered live** for either candidate. This is the largest gap in
   this evaluation.
3. **One case, one task.** M4 rests on a single two-turn shell round-trip at N=5. The
   second case, L2 (write a file, run it, report its output), was defined in the plan as
   conditional on ≥$0.80 remaining and on running for baseline, X and Y or none of them.
   $1.0954 remained, so the stated condition was met — but I then *measured* one L2 rep
   rather than guessing: **$0.0759**, so the symmetric battery (3 trees × 5 reps) costs
   **$1.139** and would have taken the total to **$2.12**, over the hard cap. Running it
   for X and Y alone would have fit, and would have left no baseline to subtract. So L2 was
   **not run for any tree** — the case set is narrowed, never the repetitions. The $0.80
   threshold in my own plan was set before L2's cost was known and was too low a bar; that
   is a defect in the plan, recorded in `DEPARTURES.md`. Neither candidate is advantaged:
   neither was measured on L2. It is the first thing I would add with more budget.
4. **M5 is vacuous on this application**, as predicted. Neither candidate is credited or
   debited. If a future change starts emitting strict tool schemas on the Responses path,
   M5 becomes live and untested by this experiment.
5. **The proxy path is broken for both.** With `LLM_PROXY_URL` set, a tool-carrying Astra
   request still goes to Chat Completions and will 400. Both candidates detected it and
   warned; neither fixed it. Arguably correct (a proxy may not speak the Responses format),
   but it is an open hole in both migrations.
6. **Rung 1's decisive cell was run after I read the diffs.** It completes a grid committed
   in advance, I ran the full 7-level grid on all three trees rather than the one cell, and
   each cell's outcome is decided by the API's own 400 — but the ordering is a real
   methodological wart and is recorded as such in `DEPARTURES.md`.
7. **Cost accounting is approximate and conservative.** Token counts come from the
   recorder's view of each response's `usage`; a handful of L0 reps returned no parseable
   usage block and are counted as zero. Cache reads are billed at the full input rate.
   Both biases are small and the ledger over-states rather than under-states.
8. **Contamination incident, self-reported (plan §0).** Before Phase 2 I ran a bare `ps`
   while diagnosing a slow test run and saw both arms' process command lines: their
   directories and that both were running pytest. No patch content, no source, no arm
   identity. Nothing in this document derives from it.
9. **Two protocol defects found, reported as findings about the protocol, not about either
   arm.** (a) PROTOCOL §5's claim that `tests/conftest.py` forces offline safety is
   conditional — it holds only when no API key is visible, and `pytest_sessionstart` makes a
   real billable Anthropic call when one is (`conftest.py:104-133, 254-262`); I enforced
   offline safety myself. (b) PROTOCOL §6 cites `gptme/eval/pass_rate_gate.py` as the eval
   harness's pass/fail contract; it is a lesson-injection gate. The real contract is
   `EvalSpec.expect` (`gptme/eval/types.py:182-191`), which is what I used.
10. **An upstream bug neither arm was asked about, and neither fixed.** On the *current*
    model `gpt-5.6-sol`, the unpatched application already sends `minimal`, which the API
    rejects with HTTP 400. X's per-model table and Y's per-model exception set both have
    the right shape to fix it; neither added an entry for `gpt-5.6-sol`. Out of scope for
    the migration, worth an upstream issue.

---

## 8. Spend

| line | ceiling | spent |
|---|---:|---:|
| baseline, `gpt-5.6-sol` | $0.35 | $0.1301 |
| baseline unpatched on `gpt-6-astra` (plan addition, baseline only) | — | $0.2473 |
| Candidate Y (drawn first by recorded coin flip) | $0.55 | $0.2667 |
| Candidate X | $0.55 | $0.2595 |
| claim-verification raw calls (7) | — | <$0.0010 |
| L2 cost probe, 1 rep, baseline tree (not evidence — see §7.3) | — | $0.0759 |
| **total** | **$2.00** | **≈$0.9805** |

Reserve never drawn. No ceiling reached. No symmetric truncation. Both candidates received
the identical battery — same cases, same seven effort levels, same N=5, same order, same
scripts — and both completed it in full.
