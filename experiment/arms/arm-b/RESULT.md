# Migration: gptme → GPT-6 Astra (Arm B — with Upshift)

- Repo: `gptme/gptme` @ `3411afec50c9e1124441bec1b00c0831400c8592`
- Started **2026-09-14T18:53:13Z**, finished **2026-09-14T20:45Z** (1h52m; the migration
  itself was complete at 19:33Z / 40 min, the rest is full-suite verification)
- Spend: **$1.63 of the $3.50 cap** — $0.21 my own calls, $1.42 Upshift's (see §7)
- Attribution analysis: `UPSHIFT_CONTRIBUTION.md`. Minute-by-minute log: `CHRONO_LOG.md`.

---

## 1. Summary

gptme is genuinely well hardened against this migration. Three of the five documented
breaking changes do not apply to it at all, and a fourth is already handled. The one
real blocking defect is in reasoning-effort validation, and there are three smaller
correctness/consistency defects caused by the same root cause: **model capability is
inferred from the string `"gpt-5"`**, which GPT-6 does not match.

| # | Documented Astra change | Applies to gptme? | Evidence |
|---|---|---|---|
| Endpoint: tools require `/v1/responses` | **Already correct by default**, but defeatable | `supports_responses_api: True` in the registry; two escape hatches walk into the 400 | source + live |
| Sampling: remove `temperature`/`top_p`/`top_logprobs` | **Does not apply** | both sites are behind `if not is_reasoner:`; Astra is a reasoner | source + live N=5 |
| `reasoning_effort: 'none'` → 400 | **APPLIES — blocking** | gptme accepts `none` and `minimal` for every OpenAI model | source + live 400 |
| `prompt_cache_retention` → `prompt_cache_options.ttl` | **Does not apply** | zero occurrences of `prompt_cache` anywhere in `gptme/` | source |
| Behavioural (initiative, verbosity of style, over-verification) | **Applies, non-blocking** | measured A/B at N=5 | Upshift run |

**Verdict: the migration is safe once P1 is fixed.** With the patch applied, Astra passes
M1/M2/M4 at 5/5 through gptme's own request-building code path, matching gpt-5.6-sol 5/5.

---

## 2. Problems found

### P1 — `reasoning_effort` validation is provider-wide, not per-model (BLOCKING)
`gptme/llm/llm_openai.py` validates `GPTME_THINKING_EFFORT` against
`_OPENAI_REASONING_EFFORTS = {none, minimal, low, medium, high, xhigh, max}` for *every*
OpenAI model. `gpt-6-astra` rejects `none` and `minimal`:

```
/v1/responses         400 Unsupported value: 'none' is not supported with the 'gpt-6-astra'
                          model. Supported values are: 'low','medium','high','xhigh','max'.
/v1/chat/completions  400 Unsupported value: 'reasoning_effort' does not support 'none'
                          with this model. Supported values are: 'low','medium','high','xhigh'.
```

`GPTME_THINKING_EFFORT=none` is a **working configuration on gpt-5.6-sol today**, so this
is a true migration regression: a user's existing config starts returning HTTP 400
mid-conversation after switching model. *How found: source reading, then confirmed live.*

**Sub-finding not in the supplied fact sheet:** the accepted set is **endpoint-dependent**.
`max` is accepted on `/v1/responses` (HTTP 200, verified) and **rejected** on
`/v1/chat/completions` ("Supported values are: 'low','medium','high','xhigh'"). Both
endpoints are reachable in gptme, and the effort is spelled differently on each
(`reasoning.effort` vs `reasoning_effort` in `extra_body`), so one flat list is wrong.

### P2 — the Responses opt-out and the proxy path walk straight into the documented 400
`_should_use_responses_api()` returns `False` when `GPTME_OPENAI_RESPONSES_API` is
`0/false/no/off`, or when an `LLM_PROXY_URL` client is in use. On Astra *with tools* there
is no working Chat Completions request to fall back to — it is a hard 400, and the 400's
own suggested escape hatch (`reasoning_effort: 'none'`) is itself rejected by this model
(verified: both 400s, in that order). *How found: source reading + live probe.*

### P3 — `verbosity` is silently dropped on GPT-6
`_maybe_apply_verbosity()` and `_make_responses_text_config()` gate on
`model.startswith("gpt-5")`. I verified live that `gpt-6-astra` **accepts** `verbosity`
(chat completions) and `text.verbosity` (responses), HTTP 200 on both. So a user with
`OPENAI_VERBOSITY` set loses a working feature on migrating, with no warning.

### P4 — the migration silently changes how the conversation is formatted
`_prepare_messages_for_api()` sends `gpt-5*` down `_prep_o1` (every system message becomes
a `<system>` user message, no merging) and every other reasoner down
`_prep_deepseek_reasoner` (first message kept as a system message, rest merged). Astra is
a reasoner that does not match `gpt-5`, so **switching model changes the prompt
construction** on the Chat Completions path. Not a wire error — a silent behavioural
delta on top of the model change, which is exactly what a migration should not introduce.

### P7 — strict structured output is broken on BOTH models (PRE-EXISTING, not a regression)
`gptme --output-schema mymodule:MyModel` loads a **user-supplied pydantic class**
(`cli/main.py:1209-1222`) and `_make_responses_text_config()` sends
`{"type": "json_schema", "schema": Model.model_json_schema(), "strict": True}`. Pydantic
does not emit `additionalProperties: false` unless the model sets `extra="forbid"`, and
no schema in gptme does. Result, live, **5/5 on gpt-6-astra AND 5/5 on gpt-5.6-sol**:

```
400 Invalid schema for response_format 'FileReport': In context=(),
    'additionalProperties' is required to be supplied and to be false.
```

Because it fails **identically on the current model at the pinned SHA**, protocol §11
makes this explicitly *not* a migration regression, and I did not bundle a fix for it into
a migration patch (same reasoning as P6). But it means **M5 cannot be satisfied by either
model**, which I raise as a disputed criterion in §6. Records: `live-m5-*.json`. Cost $0 —
every rep was a schema-validation 400, which bills nothing.

### P5 (behavioural, not fixed in code) — tool preference and turn count shift
Measured A/B at N=5 (Upshift; see `UPSHIFT_CONTRIBUTION.md` §U4/§U5):
- Given "create `/srv/app/VERSION` containing exactly 1.2.0", gpt-5.6-sol called the
  dedicated `save` tool (4/5); **Astra called `shell` with `printf '1.2.0' > …` (4/5)**.
  Both complete the task. For gptme this is not cosmetic: `save`/`patch` carry gptme's
  own diff/confirmation semantics and `shell` is a different execution and approval path.
- On an open-ended edit task Astra used **7 assistant turns vs 6**, spending the extra
  turns on self-verification, consistent with the documented "verifies more thoroughly
  than necessary". gptme has no default turn cap (`GPTME_MAX_STEPS` is opt-in), so this
  costs tokens rather than breaking, but it is a real cost delta on a $10/$50 model.

I deliberately did **not** patch prompts for P5. Writing prompt text to steer tool choice
against a 4-case hand-written suite is eval overfitting, and the observation is not strong
enough (n=1 case, my own adapter) to justify changing gptme's system prompt for everyone.
It is reported, not "fixed".

---

## 3. Changes made

Two files changed, one test file added (`final.patch`, +396/−17). Verified to apply
cleanly to the pinned SHA in a clean clone; application files only.

**`gptme/llm/llm_openai.py`**
1. `_base_model_name()` / `_is_gpt5_plus()` — one helper replacing three ad-hoc
   `startswith("gpt-5")` string tests (fixes P3 and P4, and stops the next GPT-6 model
   silently re-introducing them).
2. `_MODEL_REASONING_EFFORTS` + `openai_efforts_for_model()` — a per-model, **per-endpoint**
   narrowing of the SDK-wide set, with each entry's supported list quoted from the API's
   own 400 body. `_resolve_reasoning_effort()` gained an `endpoint` argument (defaulted, so
   every existing caller and every existing test is unaffected) and now raises a local,
   actionable `ValueError` naming the model, the supported levels, and the migration
   guide's replacement (`Use 'low' instead.`). Fixes P1.
3. `_MODELS_REQUIRING_RESPONSES_FOR_TOOLS` + `_should_use_responses_api(..., tools)` —
   when tools are present on a model whose Chat Completions endpoint refuses them, the
   `GPTME_OPENAI_RESPONSES_API` opt-out is overridden (with a warning) rather than
   obeyed into a guaranteed 400; the proxy case cannot be overridden, so it warns clearly
   instead of failing cryptically. Fixes P2.

**`docs/providers.rst`** — the reasoning-effort section said "gptme only rejects levels the
provider never accepts", which is no longer true; updated with the per-model table and the
endpoint asymmetry.

**`tests/test_llm_openai_gpt6_astra.py`** (new, 21 tests, all offline) — M1/M2/M3 as
executable contracts, plus parity tests for P3/P4 and, importantly, negative tests that
pin that **other models are unaffected** (`test_unlisted_model_keeps_the_sdk_wide_effort_set`,
`test_other_openai_models_are_unaffected`).

### What I deliberately did NOT change
- **`RECOMMENDED_MODELS["openai"]` still points at `gpt-5.6-sol`.** See §6.
- The strict-schema gap on the Responses path (§5).
- gptme's system prompt (see P5).

---

## 4. Verification

**Live, through gptme's own request-building code path** (`llm_openai.chat()` with a real
`ToolSpec`, requests instrumented at the SDK boundary), N=5 per model, identical
configuration on both:

| | gpt-5.6-sol (baseline) | gpt-6-astra (target) |
|---|---|---|
| M1 endpoint = `/v1/responses` with tools | 5/5 | **5/5** |
| M2 no `temperature`/`top_p`/`top_logprobs`/`logprobs` | 5/5 | **5/5** |
| M4 tool loop reaches a terminal answer | 5/5 | **5/5** |

Records: `live-verification-openai_gpt-6-astra.json`, `live-verification-openai_gpt-5.6-sol.json`.
This is the strongest scope available — it is the application's own code path, not a
reconstruction of it.

**M3** verified by the seven free 400-probes (`CHRONO_LOG.md` 19:18 and 20:05) plus 21
offline tests that pin the table those probes produced.

**Existing suite.** Tier 1 (the 10 frozen files) + the new file: see `metrics.json`.
No existing test was modified, weakened, skipped or xfailed; the only test-file change in
the patch is a new file.

### What I could NOT verify
- **Tier 2 (the full broad offline net) did not finish.** On this machine it runs ~10,000
  tests; at 8 workers it was ~5% complete after 11 minutes, i.e. ~3h, which does not fit
  the 3-hour wall clock that also had to contain setup, Upshift integration and the live
  runs. I killed it at 5%. It had emitted **11 failures within the first 5%**, and I could
  not determine whether they are pre-existing at the pinned SHA — I did not have a
  pre-change Tier 2 baseline to subtract, and obtaining one would have cost another 3
  hours. **This is a real gap in my verification and I am not going to paper over it.**
  My changed code is covered by Tier 1, which passed. Estimated cost to close: ~6h
  wall-clock, $0.
- **M5 was exercised and is blocked by a pre-existing defect on both models** (P7). At the
  wire, Astra accepts a strict function tool on `/v1/responses` (HTTP 200); through gptme's
  own `output_schema` path it returns 400 on *both* models, 5/5 each. So M5 is neither
  passed nor regressed — it is unreachable at the pinned SHA. See §6.
- **The behavioural A/B (P5) is on my hand-written 4-case adapter, not on gptme itself.**
  Running gptme's own `gptme/eval/` harness at N=5 on both models was not affordable:
  a single gptme eval episode carries the full tool-documentation system prompt, and at
  Astra's $10/$50 the 2-model × N=5 matrix over even a handful of eval cases is well
  above the remaining budget. Estimated cost: $15–40. **Not run.**

---

## 5. Unresolved concerns

1. **`supports_strict_tools` is a no-op on the Responses API.** `_spec2tool()` emits
   `"strict": true` when the model supports it and all parameters are required;
   `_tool_spec_to_responses_tool()` (`gptme/llm/openai_responses.py:174`) never emits
   `strict`. Since the Responses API is the default path for every GPT-5+ OpenAI model,
   the registry flag is currently decorative for tools. This is **pre-existing and
   identical on both models**, so it is not a migration regression, and making it live
   would change behaviour for gpt-5, 5.5 and 5.6 as well as Astra — out of scope for a
   migration patch. Worth an upstream issue.
2. **`_get_temperature`/`_get_top_p`'s `"gpt-5" in model` test is dead code** for any
   reasoner (both call sites are behind `if not is_reasoner:`). I left it alone: it is
   unreachable, and "fix" here means deleting logic whose original intent I can't confirm.
3. **P5 is under-powered.** 4 cases, one adapter, one observer. It is a pointer to run
   gptme's real eval harness on Astra before flipping any default, not a conclusion.
4. **P7 deserves an upstream issue of its own.** `--output-schema` is a documented,
   user-facing flag that cannot work against any current OpenAI model. It is unrelated to
   this migration and I left it alone, but it is the most user-visible defect I found.
5. **Astra costs 2× the baseline per token** ($10/$50 vs $5/$30) *and* uses more turns.
   The combined cost delta for gptme users is larger than the price-list delta suggests.

---

## 6. Disputed criteria

**The migration's most obvious change — flipping the default — is blocked by a frozen test,
and I did not make it.**

`tests/test_llm_models.py::test_get_recommended_model` hard-codes
`("openai", "gpt-5.6-sol")`. Changing `RECOMMENDED_MODELS["openai"]` to `"gpt-6-astra"`
fails that test, and both `COMMON_RULES.md` and my brief forbid modifying an existing test
to make a change pass.

I read the frozen acceptance checks M1–M5 as supporting this reading: every one of them is
phrased as *"with the target model selected"* / *"a level the application accepts **for the
target model**"*. None requires the target to be the default. So I made
`gptme -m openai/gpt-6-astra` fully correct and left the default alone.

**My argument that the test is not wrong, but the coupling is:** pinning a product
*recommendation* by equality in a unit test means every model-default change is a
test change. A drift guard here should assert the invariant the docstring actually cares
about ("the recommended model exists in `MODELS` for its provider" — which the same test
already asserts on the next line), not the specific identity. I am recording this rather
than acting on it.

**Second disputed criterion — M5 is unsatisfiable at the pinned SHA.** M5 requires that
"where the app requests a strict schema, the target model's response must conform". gptme
requests one only via `--output-schema`, and that request is rejected with HTTP 400 by
gpt-6-astra *and* by gpt-5.6-sol, 5/5 each (P7). A criterion that the baseline also fails
cannot discriminate a migration, and protocol §11 says a failure present at the pinned SHA
on the current model is not a migration regression. I therefore read M5 as **vacuous for
this application** and did not patch an unrelated pre-existing defect to make a frozen
acceptance check reachable — doing so would be exactly the "relax/adjust after seeing a
failure" move the rules forbid, in reverse. The evidence is in `live-m5-*.json` either way.

If the intended deliverable *was* the default flip, it is a one-line change to
`gptme/llm/models/recommended.py:23` plus the corresponding one-line expectation update in
that test, and everything else in this patch is the work that makes it safe.

---

## 7. Cost accounting

| | in tokens | out tokens | cost |
|---|---|---|---|
| My own probes (7×400, 5×200) | 66 | 51 | $0.003 |
| My live N=5 verification, gpt-6-astra (+1 smoke) | 7,602 | 1,136 | $0.133 |
| My live N=5 verification, gpt-5.6-sol | 7,219 | 1,130 | $0.070 |
| **My subtotal** | | | **$0.206** |
| Upshift (all legs: baseline, candidate, 3 repair screens, 1 verification) | 119,291 | 11,439 | **$1.425** |
| **Total** | | | **$1.631 of $3.50** |

All parameter-rejection 400s billed zero tokens, which is why M1/M2/M3 discovery cost $0.

**Cost-accounting discrepancy (recorded, not reconciled in the tool's favour):**
`upshift cost` reports **$1.3344**. It prices `gpt-5.6-sol` at **$4.00/$20.00 per MTok**
(`src/upshift/pricing.py:56`). gptme's own registry and the OpenAI page both carry
**$5.00/$30.00** (`gptme/llm/llm_openai_models.py`). Recomputing the baseline leg at
$5/$30 with cached input at 10% gives **$0.3472** instead of Upshift's $0.2568 — Upshift
**under-reports that leg by $0.090, about 7% of the whole run**. The Astra legs agree
exactly ($10/$50). I use the higher figure throughout. The practical risk is that
`--max-cost-usd` is enforced against the under-reported number, so the ceiling is looser
than it says on any model whose rate is stale.

**Cost control applied:** I measured one real request (407 in / 56 out) before committing
to any N=5 run, and sized from the measurement. I did not use OpenAI's flex tier: it
returned HTTP 200 on `gpt-6-astra` but **429 "processing too many requests"** on
`gpt-5.6-sol`, so it could not have been applied identically to both arms of the
comparison. Recorded rather than used. N was never lowered below 5.
