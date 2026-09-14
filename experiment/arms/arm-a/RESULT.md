# Arm A — migrating gptme to GPT-6 Astra

- **Repo:** `gptme/gptme` @ `3411afec50c9e1124441bec1b00c0831400c8592`
- **Working dir:** `/Users/atilavahedian/Desktop/exp914/w1/gptme`
- **Start (UTC):** 2026-09-14T18:52:49Z
- **Finish (UTC):** 2026-09-14T20:16:18Z
- **Workflow:** Claude Code + official OpenAI docs (via the verified extract in
  `experiment/ASTRA_FACTS.md`) + the application's own test suite + live API probes.
  No migration tooling of any kind was used.
- **Live spend:** ≈ **$0.80** of the authorized $1.75 (basis in §7).

Throughout, **[RAN]** marks something I observed by executing it and **[DOCS]** marks
something I took from documentation and did not confirm myself.

---

## 1. Summary

gptme is genuinely well hardened. `gpt-6-astra` is **already in the model registry**
(`gptme/llm/llm_openai_models.py`) with `supports_responses_api=True`, so in its
*default* configuration the app already routes Astra's tool calls to `/v1/responses` and
already omits `temperature`/`top_p` (both are gated behind `not is_reasoner`). A naive
"flip the model and see" would have found nothing.

The real breakage is in the **configurations either side of the default**, and in a set
of model-family checks that were spelled `"gpt-5" in model` and therefore stop matching
at GPT-6. I found and fixed four defects, two of which I reproduced as hard HTTP 400s
against the live API on the unmodified pinned checkout:

| | defect | severity | how found | reproduced live |
|---|---|---|---|---|
| **P1** | `GPTME_THINKING_EFFORT=none` (and `minimal`) is accepted by the app and rejected by Astra | **blocking** | docs → source reading → live | **yes** — unpatched run dies with 400 |
| **P2** | `GPTME_OPENAI_RESPONSES_API=0` (a documented debug switch) routes tool calls to `/v1/chat/completions`, which Astra does not support | **blocking** | source reading | **yes** — unpatched run dies with 400 |
| **P3** | `"gpt-5"`-spelled family checks (`_get_temperature`, `_get_top_p`, verbosity, o1-style message prep) silently exclude GPT-6 | **blocking** (latent) / behavioral | source reading | partially — the parameter values the fallback would have sent (`temperature=0`, `top_p=0.1`) both 400 on Astra |
| **P4** | Astra is documented as "more likely to ask the user a question"; gptme's non-interactive prompt forbids asking *permission* but never forbids asking a *question* | behavioral | docs → source reading | not observed in 15 live runs (see §6) |

All five frozen acceptance checks (M1–M5) pass at **N=5 on live `gpt-6-astra`** with the
patch applied. The frozen test suite does not regress.

**One thing the patch deliberately does NOT do:** it does not change
`RECOMMENDED_MODELS["openai"]`. Two frozen tests hardcode `gpt-5.6-sol` as that value, so
flipping the default *is not possible* without editing a frozen test. See §8.

---

## 2. What I found, in order

### P1 — reasoning effort: the app accepts two levels Astra rejects

`_OPENAI_REASONING_EFFORTS` in `gptme/llm/llm_openai.py` is the full openai-SDK literal
(`none, minimal, low, medium, high, xhigh, max`) and the code comment says, explicitly,
that "which subset a given model accepts is enforced server-side". That is a reasonable
design *until* you reach a model where the server's own error message sends you in a
circle. Astra is that model.

[RAN] Against the live API, on the unmodified checkout's exact request shapes:

```
cc + tools + reasoning_effort=low  → 400 "Function tools with reasoning_effort are not
    supported for gpt-6-astra in /v1/chat/completions. To use function tools, use
    /v1/responses or set reasoning_effort to 'none'."
responses + reasoning.effort=none     → 400 "Unsupported value: 'none' is not supported
    with the 'gpt-6-astra' model. Supported values are: 'low','medium','high','xhigh','max'."
responses + reasoning.effort=minimal  → 400 (same shape)
cc + tools + reasoning_effort=none    → 400 "'reasoning_effort' does not support 'none'
    with this model."
```

So the trap in the docs is real and I walked into the confirmation of it deliberately:
the fix the error message advertises produces a second 400. I had inferred `minimal` was
also gone from the published valid-values list [DOCS]; the probe **confirmed** it [RAN].

**Fix.** A per-model exception table, `_OPENAI_MODEL_UNSUPPORTED_EFFORTS`, plus:

- `valid_reasoning_efforts(provider, model_meta)` — a new helper that reports what a
  given model *actually* accepts, so anything that enumerates valid levels gets the
  truth instead of the raw SDK literal. This is the literal reading of M3 ("a level the
  app reports as valid must not produce HTTP 400").
- `_resolve_reasoning_effort` now substitutes `low` for `none`/`minimal` on an affected
  model, once, with a warning naming the accepted levels. I chose **substitute, not
  raise**, because (a) it is OpenAI's own published migration guidance ("if previously
  using `none` or `minimal`, start with `low`"), and (b) an operator who upgrades the
  model with a global `GPTME_THINKING_EFFORT=none` in their environment should get a
  working agent and a warning, not a hard stop on every request. The metadata stamped on
  the message records `low` — what was actually sent — so observability stays honest.

The substitution is strictly per-model. [RAN] In the M3 run's SDK debug log the same
process sends `reasoning.effort=low` to `gpt-6-astra` and `reasoning.effort=none` to
`gpt-5-mini` (gptme's summary model, used for conversation titles) in the same session.

### P2 — endpoint: a debug switch can route tool calls to an endpoint that cannot serve them

`_should_use_responses_api()` returned `False` whenever `GPTME_OPENAI_RESPONSES_API` is
disabled or an OpenAI-compatible proxy/base-URL is configured. On gpt-5.x that is a
harmless debugging fallback. On Astra, with tools in the request, it is a guaranteed 400
with no fallback to degrade to (because the 400's suggested escape hatch is P1's
rejected value).

**Fix.** `_should_use_responses_api()` now takes the request's `tools` (as an optional
4th argument, so the existing 3-positional-arg call sites and the tests that monkeypatch
it with `lambda *a:` are unaffected). For a model in
`_MODELS_REQUIRING_RESPONSES_FOR_TOOLS`, a tool-carrying request ignores the env
override and stays on `/v1/responses`, warning once. A *tool-less* request still honours
the override, because chat completions without tools is legal on Astra [RAN: confirmed,
returns 200]. Under a proxy we cannot force the Responses format — the proxy may not
implement it — so the code emits a specific diagnostic naming the cause instead of
letting an opaque 400 surface.

[RAN] End to end with `GPTME_OPENAI_RESPONSES_API=0`, one tool, same prompt:
- pinned checkout → `ERROR 400 Function tools with reasoning_effort are not supported…`, exit 1
- patched → warns "only supports function calling on /v1/responses; ignoring the
  override", completes the task, `answer.txt` correct, exit 0

### P3 — the `"gpt-5"` family checks

Four call sites keyed the GPT-5-era wire quirks on a literal `gpt-5` substring/prefix:

| site | what it gates | effect on `gpt-6-astra` before the fix |
|---|---|---|
| `_get_temperature` | pin temperature to 1.0 | falls through to gptme's default `TEMPERATURE=0` |
| `_get_top_p` | omit top_p | falls through to gptme's default `TOP_P=0.1` |
| `_maybe_apply_verbosity`, `_make_responses_text_config` | the `verbosity` field | `OPENAI_VERBOSITY` silently ignored on Astra |
| `_prepare_messages_for_api` | o1-style message prep | Astra silently takes the *deepseek-reasoner* branch instead, which keeps message 0 as `system` and **merges consecutive same-role messages** — a different conversation shape than the model being replaced |

The temperature/top_p rows are latent rather than live, because Astra is registered with
`supports_reasoning=True` and all four call sites sit behind `if not is_reasoner`. They
become live the moment Astra is reached through a path where that flag is not set — an
unknown/custom OpenAI-compatible provider, or the dynamic-registry fallback. [RAN] I
confirmed the values that fallback would have sent are hard errors, not no-ops:
`temperature=0.5 → 400 "Unsupported parameter: 'temperature' is not supported with this
model"`, `top_p=1.0 → 400`. (Curiosity worth recording: `temperature=1.0` *is* accepted —
so the GPT-5 pin of 1.0 would coincidentally have survived. `0` and `0.1`, gptme's
actual defaults, would not.)

**Fix.** One family predicate, `_is_gpt5_or_later()` (`^gpt-(?:[5-9]|\d\d)`), reusing the
regex the file *already* used correctly for `max_completion_tokens` — that site was
written family-wise and so had no GPT-6 bug, which is what convinced me this was the
intended spelling rather than my invention. Applied to all four sites. The o-series half
of the regex is kept separate (`_is_openai_reasoning_era_model`) so the o-series keeps
exactly the behaviour it had. Additionally `_get_temperature` may now return `None`
(callers omit the key) for models that *removed* the parameter rather than pinning it —
`_get_top_p` already had this shape, so the four call sites now treat both symmetrically.

[RAN] I verified the `verbosity` widening before shipping it, because extending a
parameter to a new model is exactly how you introduce a break: `text={"verbosity":"low"}`
on `/v1/responses` and `verbosity` in the chat-completions body both return **200** on
`gpt-6-astra`. If it had 400'd I would have left that gate at `gpt-5`.

### P4 — the documented initiative change

> "more likely to ask the user a question when additional input could materially change
> the result" [DOCS]

M4 forbids exactly this outcome ("must not … terminate by asking the user a question in
a non-interactive run"). gptme's non-interactive system prompt said "the user is not
available to provide feedback … Do not provide examples or ask for permission before
running commands" — it forbids asking *permission*, and never says what to do when
something is genuinely ambiguous.

**Fix.** One sentence added to the non-interactive branch only
(`gptme/prompts/templates.py`): do not ask a clarifying question, choose the most
reasonable interpretation, state the assumption in one line, continue. This is a
strengthening of the contract already stated two lines above it, not a new policy, and
interactive mode is untouched (it still says "if clarification is needed, ask the user").

**Honesty:** I did **not** observe this failure mode in any of the 15 live Astra
sessions. The task I could afford to run at N=5 is unambiguous, so it does not exercise
the trigger. This fix is *prophylactic and docs-driven*, and its effectiveness is
unproven. See §6.

---

## 3. What I changed

```
 gptme/llm/llm_openai.py      | 233 +++++++++++++++++++++++++++++++++-------
 gptme/prompts/templates.py   |   9 ++
 tests/test_llm_openai_gpt6.py| 268 ++++++++++++++++++++++++++++++ (new)
```

No existing test was modified, deleted, skipped or xfailed. No production code outside
those two files was touched. The new test file adds 19 tests, every one of which mirrors
an assertion that already exists for gpt-5.x in `test_llm_openai.py`,
`test_llm_openai_sampling.py` or `test_llm_openai_reasoning_effort.py` — which is the
justification required by the rules: the migration bug class here is precisely that the
gpt-5 assertions kept passing while the same code took a different branch for GPT-6.

---

## 4. Verification — what I ran

### Frozen suite (offline)

Both measured on clean checkouts of the same SHA, same interpreter, same flags
(`-q -n 8 --timeout 120`), run concurrently. The pinned baseline was measured in a
separate `git worktree` at `3411afec5` rather than by reverting in place.

| | pinned SHA (baseline) | with patch |
|---|---|---|
| Tier 2 (`tests/ -m "not slow and not requires_api and not integration"`) | 55 failed, 10,799 passed, 200 skipped | 55 failed, 10,819 passed, 199 skipped |
| Tier 1 (the 10 named files) + the new file | — | **669 passed, 1 skipped, 0 failed** |

*Two Tier-2 attempts were thrown away before I got a usable pair, and both are counted
in the elapsed time. (1) The first run was started before I began editing and finished
after, so it measured neither tree — discarded as contaminated. (2) The second attempt
ran the pristine and patched suites **concurrently** at `-n 8` each; both died around
28–31% with xdist worker loss (`OSError: cannot send (already closed?)` from
`pytest_sessionfinish` on many workers). gptme's suite spawns and kills real processes
(shell-background, tmux, subagent), so two full suites sharing a machine is not a safe
configuration. The third attempt ran them one at a time at `-n 16`, which is the
project's own Makefile setting. Total cost of these two mistakes: ~35 minutes.*

### 4.1 Tier 2 numbers

| | pinned SHA (baseline) | with patch | delta |
|---|---|---|---|
| **failed** | **55** | **55** | **0** |
| passed | 10,799 | 10,819 | +20 |
| skipped | 200 | 199 | −1 |
| collected | 11,054 | 11,073 | +19 (exactly the new test file) |
| wall clock | 22:41 | 25:30 | |

**The set of failing tests is byte-identical.** I wrote both `FAILED` lists to disk,
sorted them and diffed them: no output. Not "the same count" — the same tests.

```
$ diff fail_pristine.txt fail_patched.txt && echo "IDENTICAL FAILURE SET"
IDENTICAL FAILURE SET
```

The 55 pre-existing failures are environmental, not gptme defects, and they are present
at the pinned SHA on the current model:

| file | n | why |
|---|---:|---|
| `test_cmd_service.py` | 20 | generates and parses **systemd** units; this is macOS |
| `test_computer_transport.py` | 10 | X11 / `xdotool` paths |
| `test_sandbox.py` | 6 | Docker daemon absent (`.colima/default/docker.sock` missing) |
| `test_eval_behavioral_solutions.py` | 5 | depend on the same missing container runtime |
| 10 others | 14 | same families (shell memory limits, browser, local discovery, shell completions) |
| `test_prompts.py::test_get_prompt_full` | 1 | asserts the full prompt is under 10,250 tokens; on this machine gptme reads the host's real skills directory and the prompt is **33,634** tokens |

That last one is the only baseline failure that touches a file I modified, so I checked
it specifically: `get_prompt()` takes `interactive: bool = True` and both prompt-size
tests use that default, while my prompt change is inside the **non-interactive** branch
only. It contributes exactly zero tokens to those two assertions. [RAN] It fails
identically before and after.

**One unexplained difference, recorded rather than smoothed over:** +20 passed but only
+19 collected, and −1 skipped. Nineteen of those are my new tests; the twentieth is a
test that was *skipped* in the baseline run and *ran and passed* in the patched run —
a conditional skip flipping on something environmental between two 25-minute runs.
`pytest -q` does not print skipped test names, so identifying it would cost another full
suite run with `-rs`, and I chose not to spend the remaining wall clock on it. It is not
a regression in either direction (a skip becoming a pass cannot be caused by the patch
harming anything), but I cannot name the test.

### Live acceptance checks (N=5, `gpt-6-astra`, patched tree)

Every run is a real non-interactive `gptme` session (`-n -t shell --tool-format tool
--system full-noexamples`) in an isolated `HOME`/workspace, with a deterministic oracle:
the file the agent was told to write must contain the true line count of `/etc/hosts`.
This is the same shape of contract gptme's own eval harness uses.

| check | model | N | result | evidence |
|---|---|---|---|---|
| **M1** endpoint | gpt-6-astra, default config | 5 | **PASS 5/5** | SDK debug log across all 5 sessions: **15 requests, 15 of them `{'method':'post','url':'/responses'}`, zero to `/chat/completions`** |
| **M2** sampling params | gpt-6-astra, default config | 5 | **PASS 5/5** | the strings `temperature` and `top_p` occur **0 times** across the 5 complete SDK debug logs (`grep -c` = 0 for both) |
| **M3** effort validity | gpt-6-astra, `GPTME_THINKING_EFFORT=none` | 5 | **PASS 5/5** | body carries `'effort': 'low'`; warning emitted; task completes; same session sends `'effort': 'none'` to `gpt-5-mini` unchanged |
| **M4** tool loop completes | gpt-6-astra | 5 | **PASS 5/5** | exit 0, tool called, `answer.txt == 9`, terminal state reached, no question asked |
| **M4** tool loop (baseline) | gpt-5.6-sol | 5 | **PASS 5/5** | same oracle — so M4 is *not* a pre-existing failure being credited to the migration |
| **M5** strict structured output | gpt-6-astra | 5 | **PASS 5/5** | well-formed strict schema; response parsed by `Answer.model_validate_json` every time |

M1 and M2 were each run twice at N=5: once in the `effort=none` configuration and once
in the **default** configuration (no `GPTME_THINKING_EFFORT` set), because evidence
gathered only under a non-default env var is weaker evidence about the shipped default.
The figures above are from the default-config run.

Unpatched-vs-patched, same prompt, same model (the 400 legs bill $0):

| scenario | pinned SHA | patched |
|---|---|---|
| `GPTME_THINKING_EFFORT=none` | **400**, exit 1 | **5/5 pass** |
| `GPTME_OPENAI_RESPONSES_API=0` + tools | **400**, exit 1 | pass, warns, exit 0 |

---

## 5. Verified by running vs inferred from docs

**[RAN] — I executed this and observed the result**
- `gpt-6-astra` is present and accessible on the key (`GET /v1/models`, free).
- Tools on `/v1/chat/completions` → 400, and the 400 recommends `reasoning_effort:'none'`.
- `reasoning.effort` `none` → 400; `minimal` → 400; accepted set is exactly
  `low, medium, high, xhigh, max` (the API states this in the error body).
- Following the 400's own advice (`tools` + `reasoning_effort='none'` on chat
  completions) → a second 400. The documented trap, confirmed.
- `top_p` → 400. `temperature=0.5` → 400. `temperature=1.0` → **200** (accepted).
- `top_logprobs` → 400 ("logprobs are not supported with reasoning models").
- `verbosity` → 200, on both endpoints.
- Chat completions *without* tools → 200.
- All of M1–M5 at N=5 on the patched tree; M4 baseline at N=5 on gpt-5.6-sol.
- Both unpatched failure modes, end to end, on a pristine worktree of the pinned SHA.
- Tier 1 (all 10 frozen files) green on the patched tree.

**[DOCS] — taken from documentation, not confirmed by me**
- `prompt_cache_retention` → `prompt_cache_options.ttl`. **Not applicable**: I grepped
  the whole package and gptme never sends either field, nor `logprobs`/`top_logprobs`.
  Nothing to migrate.
- Astra's behavioral changes (initiative, verbosity of prose, instruction-file
  sensitivity, delegation, over-testing). Only the first one is addressed by the patch,
  and its effectiveness is unverified (§6).
- Context window 1,050,000 / **max input 922,000** (see §6).

---

## 6. Unresolved concerns

1. **`max input` (922,000) is lower than `context` (1,050,000), and gptme uses `context`
   as an input budget.** `gptme/tools/autocompact/decision.py` compacts at
   `0.9 * model.context` = 945,000 — above Astra's published input ceiling. A
   conversation in that band would 400 before gptme decided to compact. I did **not**
   change it: `context` is documented and used as the *context window* (the token
   display counts input+output against it), and silently redefining it to mean "max
   input" is the kind of conflation that bites later; the correct fix is a separate
   `max_input` field on `ModelMeta` plumbed through the autocompact thresholds, which is
   more surface area than a migration should take on unannounced. Verifying the failure
   would need a ~1M-token request — far outside the budget. **Flagged, not fixed.**

2. **P4 is unproven.** I have no live evidence that Astra asks clarifying questions in
   this application, and no live evidence that my prompt sentence prevents it. Detecting
   it needs deliberately ambiguous eval tasks at N≥5 on both models; I chose to spend
   the budget on the five frozen checks instead. If the sentence turns out to be
   unnecessary it is a harmless restatement of the existing contract; I would not claim
   it as a "found bug".

3. **Strict tool schemas are never sent on the Responses API path.** `_spec2tool` sets
   `"strict": True` (driven by `supports_strict_tools`) but that function only serves
   chat completions; the Responses path builds tools via `_tool_spec_to_responses_tool`,
   which has no `strict` key at all. So on Astra — which *must* use Responses — the
   `supports_strict_tools=True` in the registry is inert. This is **pre-existing and
   affects gpt-5.6-sol identically** (it also uses Responses), so it is not a migration
   regression and I left it alone.

4. **`strict: true` + a pydantic schema without `extra="forbid"` is a guaranteed 400 on
   every model.** Found while building the M5 check. `_make_response_format` and
   `_make_responses_text_config` set `"strict": True` and pass
   `output_schema.model_json_schema()` through verbatim, but pydantic does not emit
   `additionalProperties: false` unless the model sets `extra="forbid"`, and strict mode
   requires it. [RAN] Identical 400 on **gpt-4o, gpt-5.6-sol and gpt-6-astra** —
   model-independent, pre-existing, not a migration regression. gptme's own
   `_dict_to_jsonschema` (`gptme/tools/subagent/hooks.py`) builds object schemas without
   it, so gptme's own subagent structured output would hit this. I did not fix it; see §8.

5. **`openai-subscription/gpt-6-astra`** (the Codex/ChatGPT-subscription path) is a
   separate provider and a separate module. Its registry entry lacks
   `supports_responses_api`, but `_should_use_responses_api` requires `provider ==
   "openai"` anyway, so that path never consults it. I scoped this migration to the
   `openai` API provider — the one §2 of the protocol names — and did not audit or test
   the subscription path. If it is in scope, it is unmigrated.

6. **Stochastic behavior at N=5 on one task.** M4 passing 5/5 on one unambiguous
   file-writing task is real evidence that the tool loop closes, and no evidence at all
   about the harder documented behavioral drifts (output formatting, over-verification,
   delegation). A real production migration would run gptme's full `gptme-eval` suite on
   both models. That is far beyond $1.75.

---

## 7. Cost

**$0.795** (call it **$0.80**), against a $1.75 cap.

Basis: for the agent sessions I used **gptme's own end-of-session cost line**, which it
computes from the `usage` block the API returns (measured, not estimated), rounded by
gptme to the cent — so the true figure is within roughly ±$0.01 of this. For the raw
probes I computed it from the returned `usage` at the published $10/$50 per MTok.

| item | calls | cost |
|---|---|---|
| free `GET /v1/models` | 1 | $0.00 |
| wire probes (4 accepted, 7 rejected) | 11 | $0.002 |
| pilot Astra session (cost calibration) | 1 session | $0.04 |
| M4 baseline, gpt-5.6-sol, N=5 | 5 sessions | $0.13 |
| M4/M1/M2, gpt-6-astra, N=5 | 5 sessions | $0.17 |
| M3, gpt-6-astra `effort=none`, N=5 | 5 sessions | $0.20 |
| M1/M2 re-run, gpt-6-astra **default config**, N=5 | 5 sessions | $0.19 |
| M5 strict schema, N=5 (+5 rejected, +3 on other models) | 13 | $0.012 |
| patched `RESPONSES_API=0` demo | 1 session | $0.05 |
| unpatched 400 demos | 2 sessions | $0.00 |

Rejected requests (HTTP 400) bill nothing, which is why the two most valuable pieces of
evidence in this report — both unpatched failure modes — were free.

### 7.1 How I kept N=5 affordable, and what I changed to do it

I measured before I committed. `gptme --show-prompt-stats` on my intended validation
configuration reported **28,769 tokens per request** (≈$0.29/call at $10/MTok), because
it was reading the machine's real gptme skills directory. N=5 across three checks at
that rate is ≈$10 — 6× the cap. Only after fixing that did I spend anything on a
multi-rep run, and I ran **one** pilot Astra session first to confirm the measured rate
end to end ($0.04) before committing to 15 more.

Four configuration choices made validation cheaper. **Every one is an option the
application already supports, and every one was applied identically to the baseline
model and the target model:**

| choice | effect | why it does not change what is tested |
|---|---|---|
| isolated `HOME`/`XDG_*` | 28,769 → 860 prompt tokens | removes third-party skill files that are not part of gptme; the agent, tools and provider code path are unchanged |
| `--system full-noexamples` | ~40% fewer prompt tokens (the app's own documented option) | drops tool *examples* from the prompt, not tools or tool definitions |
| `-t shell` (one tool) | fewer tool-schema tokens | M1/M2/M4 need tools *present*, not many tools |
| `--no-stream` | none on cost | makes the response inspectable; both endpoints are exercised the same way |

**Ordering.** I ran one Astra pilot session first purely to calibrate cost. The scored
runs then went: baseline `gpt-5.6-sol` N=5, then target `gpt-6-astra` N=5 (these two
overlapped in wall-clock — I launched the target run while the baseline was still
finishing), then the M3 `effort=none` run. The baseline result therefore exists
independently and was not derived from the target run; M4 passing 5/5 on `gpt-5.6-sol`
is what lets me say the Astra 5/5 is not a pre-existing failure being credited to the
migration. I did not, however, run the baseline strictly *to completion* before starting
the target, and I am recording that rather than implying otherwise.

**Not verified live, and what it would cost:**

- P4 (does Astra actually end non-interactive turns with a question, and does the prompt
  sentence stop it): needs a set of deliberately ambiguous tasks at N=5 on both models.
  At the measured ≈$0.035/Astra-session and ≈$0.026/sol-session, a 4-task set is
  4 × 5 × ($0.035 + $0.026) ≈ **$1.22**. Affordable in isolation, but only by displacing
  the frozen M-checks, and designing the ambiguous tasks is where the real cost is.
- P6 (the 922K max-input ceiling): needs a ~1M-token request. ≈**$10 per repetition**,
  ≈$50 at N=5. Out of reach by more than an order of magnitude.
- The behavioural drifts other than P4 (output formatting, over-verification, reduced
  delegation): need gptme's own `gptme-eval` suite on both models — tens of dollars at
  minimum.
- The `openai-subscription` provider path: not audited, not run; it needs a ChatGPT
  Plus/Pro OAuth token which I do not have, so cost is not the blocker there.

---

## 8. Disputed criteria

I am leaving all three of these alone, as instructed, and recording the argument.

### 8.1 Two frozen tests make the actual default-model flip impossible

- `tests/test_llm_models.py:35` — `assert model.model == "gpt-5.6-sol"  # current recommended model`
- `tests/test_llm_models.py:243` — `("openai", "gpt-5.6-sol")` in `test_get_recommended_model`

A shipped migration would set `RECOMMENDED_MODELS["openai"] = "gpt-6-astra"` in
`gptme/llm/models/recommended.py:23` — that is a one-line change and it is what
"migrate the application to GPT-6 Astra" means in the ordinary sense. Both tests would
then fail, which under §11 of the protocol is a FAILURE for this arm.

I read the freeze as binding and the acceptance checks as the operative definition of
"migrated": M1–M5 are all phrased as "*with the target model selected*", not "as the
default". So this patch makes `gptme -m openai/gpt-6-astra` correct and safe, and leaves
the recommendation where the frozen suite pins it.

**If the freeze is lifted, the complete flip is:** `recommended.py:23`
`"openai": "gpt-6-astra"`, plus those two literals in `tests/test_llm_models.py`. Nothing
else in the suite depends on that value (`test_recommended_models_have_metadata` and
`test_recommended_models_resolve_via_get_model` both read the table dynamically, and
`gpt-6-astra` already satisfies both). I verified that by reading every reference to
`RECOMMENDED_MODELS` and `gpt-5.6-sol` in `tests/`.

### 8.2 M5 is unsatisfiable for a plain pydantic schema, and the frozen tests are why

`tests/test_llm_openai.py:1101` and `:1190` assert that the schema reaches the API as
`OutputSchema.model_json_schema()` **verbatim**. Combined with the unconditional
`"strict": True` in the same dict, that makes every plain pydantic schema a 400 (§6.4),
on every model, forever. The one-line fix — recursively inject
`additionalProperties: false` into object schemas when `strict` is set — breaks both
frozen tests, so I did not make it.

There is a defensible reading in which the frozen tests are *right*: gptme passes the
caller's schema through untouched, and strict mode is then the schema author's
responsibility (`model_config = ConfigDict(extra="forbid")`). Under that reading the bug
is not in `llm_openai.py` at all but in gptme's own `_dict_to_jsonschema`, which builds
schemas that omit it. **I scored M5 with a well-formed strict schema** (`extra="forbid"`)
and it passes 5/5 on Astra — so the migration is not what breaks it either way. I flag
it only so nobody reads a green M5 as "strict structured output is healthy in gptme".

### 8.3 Nothing, on the tests I was told not to weaken

For completeness: I found no frozen test I believe is *wrong on the merits*. 8.1 and 8.2
are both cases of a frozen test pinning a value that a change has to move, not of a test
asserting something false.

---

## 9. Dead ends and wasted time (recorded as data)

- **~20 min**: my first Tier-2 baseline run overlapped my first edits, so it measured
  neither tree. Discarded; redone properly with a `git worktree` at the pinned SHA. My
  mistake — I should have snapshotted the baseline before touching anything.
- **~4 min**: the first `--show-prompt-stats` measurement read the user's real gptme
  skills directory and reported a 28,769-token system prompt (≈$0.29/call). At N=5 across
  three checks that would have been ≈$10 and blown the budget by 6x. Isolating `HOME`
  brought it to 860 tokens. Measuring the prompt size *before* spending was the single
  highest-value thing I did with the budget.
- **~5 min**: I initially widened the sampling helpers to the whole reasoning era
  (o-series included), which would have silently changed `_get_temperature` for o1/o3/o4
  from the caller's value to `1.0`. Caught before running anything; split into two
  predicates so the o-series is untouched.
- **~6 min**: my first M5 check failed 5/5 with a 400 and I briefly believed I had found
  a migration regression. Testing the same schema on gpt-5.6-sol and gpt-4o showed an
  identical failure — pre-existing and model-independent. Recorded as §6.4 rather than
  claimed as a find.
- Chasing `prompt_cache_retention` and `logprobs` through the codebase found nothing:
  gptme sends neither. Two of the five documented breaking changes simply do not apply
  to this application. Cheap to check, and worth checking.
