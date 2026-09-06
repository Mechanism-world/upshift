# opus47-001 — TutteInstitute/toponymy, `claude-haiku-4-5` → `claude-sonnet-5`

**Terminal state: `REPAIRED_VERIFIED` — upshift's repair loop generated the repair itself, on
its first candidate, and accepted it on full-suite verification. Verdict `SAFE WITH PATCH`,
5/5 regressed cases restored, 0 previously-passing cases broken.**

Measured on the real Anthropic API: baseline **5/5 cases (25/25 reps)** on
`claude-haiku-4-5-20251001`; candidate **0/5 cases (0/25 reps)** on `claude-sonnet-5`, every
rep the API's own 400 on the request body; the loop's `drop-sampling-params` candidate
**restores 5/5 cases** — 25/25 reps on the screen, 25/25 again on the full-suite
verification. **$0.2005** of the $5.00 per-case cap across both attempts.

This case was run twice. **Attempt 1** (2026-09-05) reproduced the regression and proved the
repair by hand, but upshift's loop produced *zero* candidates and returned `STAY PINNED`; the
case closed `REGRESSION_REPRODUCED_REPAIR_FAILED` and its §4 was about nothing but the two
product gaps that caused it. Both gaps are fixed in the product at `a9676be` ("anthropic:
route sampling params through extra_body so the wire decides"). **Attempt 2** (2026-09-06) is
this report's headline: same repo, same commit, same pair, same suite, same N, same
thresholds, a new run tag, and a repair loop that now does the work. §4.1 is the before/after
of the machine itself.

| | |
|---|---|
| repo | https://github.com/TutteInstitute/toponymy (102 stars, MIT, active) |
| commit | `bcc46b86fc8ef1b791098497556f85678ec8cac0` — HEAD at discovery, 2026-09-04, *"Merge pull request #207 from TutteInstitute/version-0.5.4"* |
| source | https://github.com/TutteInstitute/toponymy/issues/185 — **open**, 8 comments, maintainer engaged; unfixed at the pin |
| failing call | `toponymy/llm_wrappers.py:1444-1468`, `LiteLLMNamer._provider_kwargs`, `"temperature": temperature` set unconditionally at **`:1455`** |
| pair | `claude-haiku-4-5-20251001` → `claude-sonnet-5`, endpoint `messages`, provider `anthropic` (real, not simulated) |
| N | 5 reps per case per model; thresholds pass >= 0.8 / fail <= 0.4, unchanged, never lowered |
| suite | 5 cases, `agent/cases/cases.json`, prompts rendered from the repo's own templates over the repo's own committed fixtures |
| spend | **$0.2005** total (`cost.json`, priced from the API's own usage fields); attempt 2 alone **$0.1269** |
| upshift | 0.3.1 at `a9676be` (attempt 2); pre-`a9676be` (attempt 1) |
| lab image | `upshift-lab:0.1`, rebuilt 2026-09-06; `anthropic` SDK 1.4.0 inside |
| run tag | `opus47-001-r2` (attempt 2); `opus47-001` (attempt 1, retained in full) |
| scope | `extension-opus47` — the from-model is on the *near* side of the sampling-param wall |

> **Paths in this published copy.** This report was written against the private lab case
> directory, so bare paths below are relative to it. In this repository they are:
> `agent/*` → `agents/toponymy/*`, `report/toponymy-drop-temperature.patch` →
> `reports/toponymy-drop-temperature.patch`, `state.json` → `runs/opus47-001-r2/state.json`;
> `runs/*` paths are unchanged, except the three `runs/opus47-001-sim*` directories, which are
> machinery smoke rather than evidence and are not published. `cost.json`, `logs/`,
> `workspace/`, `pr-workspace/`, `install_review.txt` and the delivery package
> (`PR_BODY.md`, `ISSUE_COMMENT.md`, `DELIVERY_CHECKS.md`, `DELIVERY_COMMANDS.sh`) are
> lab-internal and are not published here.

## 0. Why this pair

`LAB_BATCH_3` §3 closed this signature structurally: on the mission pairs both models reject a
non-default `temperature`, so an agent that sends one is `BASELINE_BROKEN` by construction.
The `opus47` extension asks the same question from the near side of the wall, and toponymy is
the cleanest instance of it: **the from-model is the project's own shipped default.**
`AnthropicNamer(model="claude-haiku-4-5-20251001")` (`llm_wrappers.py:1866`) is what a
toponymy user gets by typing `AnthropicNamer()`, and the reporter's control run on it succeeds
while `claude-sonnet-5` raises. This is not a hypothetical migration; it is what happens to
that default the day a user changes one string.

## 1. What broke, and why

`LiteLLMNamer._provider_kwargs` builds every litellm call and sets `temperature`
unconditionally:

```python
# toponymy/llm_wrappers.py:1450-1458  (at bcc46b86, unchanged)
kwargs = dict(self.provider_kwargs)
kwargs.update(
    {
        "model": self.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
)
```

The value is `0.4` — `LLMWrapper.generate_topic_name`'s default (`:500`), which nothing in the
default path overrides (`temperature_override` is `None`). There is no `drop_params`, no
model gate, and no capability check anywhere in the 4,713-line file.

`claude-sonnet-5` rejects it. Verbatim from
`runs/opus47-001-r2-candidate/cases/name_topic_technology/rep_01.json`:

```json
{"message": "`temperature` is deprecated for this model.",
 "status_code": 400, "type": "api_status_error"}
```

All 25 candidate reps failed identically, on the first API call, before any tokens were
generated (`input_tokens: 0`, `resolved_model: null`). Signature
`api_error_unsupported_sampling_params` on all five cases, p=0.00397 each. Identical in
attempt 1.

Two things this case checked rather than assumed:

- **`BadRequestError` is fail-fast in toponymy.** It is listed in
  `LiteLLMNamer.FAIL_FAST_EXCEPTIONS` (`:1341-1347`) and `_should_retry` skips it, so the run
  does not degrade — it aborts on the first cluster. The issue title says so and the code
  agrees.
- **The request carries no forced `tool_choice`.** `AnthropicNamer` sets
  `use_json_object=True`, which adds `response_format={"type": "json_object"}`, and litellm
  *can* turn a `response_format` into a forced `json_tool_call`. At toponymy's own locked
  litellm (1.85.0, `uv.lock:2398`) it does not: with no schema attached,
  `map_response_format_to_anthropic_tool` returns `None` and the caller `continue`s
  (`litellm/llms/anthropic/chat/transformation.py:1458-1497`, `:1281-1300`). Verified by
  reading the wheel; never installed. So `temperature` is the only offending field, and the
  case is not confounded with the forced-tool-choice signature that `A-085` measured.

## 2. Before and after

```
claude-haiku-4-5-20251001 @ messages  ->  claude-sonnet-5 @ messages
provider: anthropic    n_reps: 5
baseline: opus47-001-r2-baseline    candidate: opus47-001-r2-candidate

baseline   5/5 cases pass (100.0%, Wilson 95% CI 56.6-100.0%)
candidate  0/5 cases pass (  0.0%, Wilson 95% CI  0.0- 43.4%)
5 regressed, 0 flaky, 0 improved

verdict: SAFE WITH PATCH — restored 5/5 regressed, 0 previously-passing broken
```

| case | baseline `claude-haiku-4-5` | candidate `claude-sonnet-5` | after the loop's patch | label | p |
|---|---|---|---|---|---|
| `name_topic_technology` | 5/5 | 0/5 | 5/5 | regressed -> restored | 0.00397 |
| `name_topic_health` | 5/5 | 0/5 | 5/5 | regressed -> restored | 0.00397 |
| `name_topic_environment` | 5/5 | 0/5 | 5/5 | regressed -> restored | 0.00397 |
| `name_topic_education` | 5/5 | 0/5 | 5/5 | regressed -> restored | 0.00397 |
| `name_topic_economics` | 5/5 | 0/5 | 5/5 | regressed -> restored | 0.00397 |
| **total** | **5/5 cases, 25/25 reps** | **0/5 cases, 0/25 reps** | **5/5 cases, 25/25 reps** | | |

No case sits between the thresholds on any arm, in either attempt. Every count is at an
extreme.

### 2.1 Every run, both attempts

**Attempt 2 — tag `opus47-001-r2`, upshift at `a9676be`.** `agent/agent.json` declares
`params: {"max_tokens": 128, "temperature": 0.4}` — a plain params key.

| run | model | what | cases / reps | cost |
|---|---|---|---|---|
| `opus47-001-r2-baseline` | `claude-haiku-4-5-20251001` | baseline | 5/5 · 25/25 | $0.0202 |
| `opus47-001-r2-candidate` | `claude-sonnet-5` | candidate, unpatched | 0/5 · 0/25 | $0.0000 |
| `opus47-001-r2-c01-447f0fbd-screen` | `claude-sonnet-5` | **repair candidate 1/6, screened on the 5 broken cases** | 5/5 · 25/25 | $0.0534 |
| `opus47-001-r2-c01-447f0fbd-verify` | `claude-sonnet-5` | **full-suite verification, accepted** | 5/5 · 25/25 | $0.0533 |
| | | | **attempt 2 subtotal** | **$0.1269** |

**Attempt 1 — tag `opus47-001`, upshift pre-`a9676be`.** Retained on disk in full. The
declaration was `params: {"extra_body": {"temperature": 0.4}}` (§4.1); the repair was applied
by hand because the loop could not produce it.

| run | model | what | cases / reps | cost |
|---|---|---|---|---|
| `opus47-001-baseline` | `claude-haiku-4-5-20251001` | baseline | 5/5 · 25/25 | $0.0202 |
| `opus47-001-candidate` | `claude-sonnet-5` | candidate, unpatched | 0/5 · 0/25 | $0.0000 |
| `opus47-001-patched-candidate` | `claude-sonnet-5` | hand-patched, full-suite verification | 5/5 · 25/25 | $0.0533 |
| `opus47-001-naive-params-baseline` | `claude-haiku-4-5-20251001` | diagnostic of the old SDK gap, §4.1 | 0/5 · 0/25 | $0.0000 |
| `opus47-001-sim-*` | `sim-fable-5` / `-5-1` | machinery smoke before any money (not published, see below) | 5/5 · 5/5 | $0.0000 |
| | | | **attempt 1 subtotal** | **$0.0735** |

**Case total: $0.2005.** Both unpatched candidate runs cost **$0.0000**: a request-body 400
bills no tokens. The sim records are retained in the lab and are **not** evidence, so they are
not published in this repository — no claim in this report rests on them; the live report
carries no simulated-provider stamp because `anthropic` is a real provider.

What the models actually answer, rep 01 of each arm in attempt 2 (both parse cleanly under
toponymy's own regex; `claude-haiku-4-5` wraps its JSON in a markdown fence, `claude-sonnet-5`
does not):

| case | `claude-haiku-4-5` | `claude-sonnet-5`, patched |
|---|---|---|
| `name_topic_technology` | `Emerging Technologies` (0.75) | `Emerging Technology` (0.6) |
| `name_topic_health` | `Wellness Health` (0.85) | `Health & Wellness` (0.6) |
| `name_topic_environment` | `Environmental Sustainability` (0.92) | `Environment` (0.6) |
| `name_topic_education` | `Educational Systems` (0.75) | `Education` (0.5) |
| `name_topic_economics` | `Economic Principles` (0.75) | `Economics` (0.6) |

Both models name the fixture's five ground-truth topics correctly. **No check asserts that** —
see §7.2.

All three run-phase network probes matched the specification on every container run of both
attempts: `pypi.org` blocked through the proxy, `1.1.1.1` unreachable directly (no route),
`api.anthropic.com` reachable. `state.json`
`network.{run,run-r2,verify,naive-params}.probes`; attempt 2's are also in
`logs/run-r2-probes.json`.

## 3. The suite

Five cases, N=5, deterministic checks only, no LLM judge. Built by hand; `upshift adapt` was
not run and **$0.00 went to adaptation**. Full ledger in `agent/ADAPT_EDITS.md`. **The suite
is byte-identical across the two attempts** — `cases/cases.json`, `system_prompt.txt`,
`tools.json` and `backend.py` were not touched. The only adapter change between attempts is
where `agent.json` *declares* `temperature` (§4.1); the request that leaves for
`api.anthropic.com` is identical either way.

The agent is a **single-call, tool-less** namer, so `tools.json` is `[]` and `max_turns` is 1.
The system prompt is `PROMPT_TEMPLATES["layer"]["system"]` rendered with
`topic_name_prompt`'s own params; the five user messages are the matching `["user"]` rendering
over the five clusters of the repo's committed fixture `toponymy/tests/subtopic_objects.json`,
grouped exactly as `conftest.py`'s `cluster_label_vector` and `cluster_tree` group them. One
system prompt serves all five because toponymy uses one system prompt per *layer* and varies
only the user message per cluster.

Two checks per case:

1. `no_api_error`.
2. `response_matches` with toponymy's own `GET_TOPIC_NAME_REGEX`, verbatim from
   `templates.py:14`. This is the repo's own definition of a usable answer:
   `llm_output_to_result` (`llm_wrappers.py:284-293`) does
   `re.findall(regex, output, re.DOTALL)[0]`, so a response the regex misses raises
   `IndexError`, burns all three `tenacity` attempts and leaves the topic unnamed.

Both models resolved to the requested ids (`resolved_model` in every rep record that reached
the wire).

## 4. The patch

### 4.1 upshift produced it — and why it could not before

**What the loop did in attempt 2.** Verbatim from `report/verdict.json`:

```
repair start: 5 regressed case(s), 0 protected passing case(s), budget 6 candidates
candidate 1/6: [model_params] drop-sampling-params — Remove temperature / top_p / top_k:
  this model rejects non-default sampling params (typically left over from an
  OpenAI-style config).
  screen: 5/5 broken cases restored — running full verification
  ACCEPTED: restored ['name_topic_economics', 'name_topic_education',
  'name_topic_environment', 'name_topic_health', 'name_topic_technology'];
  0 previously-passing cases broken; 0 regressed case(s) remain
repair end: all 5 regressed cases restored
```

One candidate, generated from the observed signature, screened, verified on the full suite and
accepted. Nothing was hand-edited: `report/upgrade.patch` is upshift's own output.

**What stopped it in attempt 1 — two product gaps, both measured then, both fixed now.**

*Gap 1: upshift could not put a sampling parameter on the Anthropic wire at all.* The provider
called `client.messages.create(**request)`, and `anthropic` >= 1.1.0 has no `temperature`
keyword on that signature, so a top-level `params.temperature` raised `TypeError` in-process.
The provider recorded that as a synthetic 400 so the differ's signature stayed reachable — but
the rejection was **model-independent**: it fired on the baseline exactly as on the candidate,
so the naive five-file form could only ever produce `BASELINE_BROKEN` and could never carry a
diff. Measured, at $0: `runs/opus47-001-naive-params-baseline/` is `params: {"temperature":
0.4}` run against the **baseline** model at N=5 — 0/5 cases, 0/25 reps, every one

```
the installed anthropic SDK rejected a request parameter before it reached the wire:
Messages.create() got an unexpected keyword argument 'temperature'
```

with `input_tokens: 0`. The adapter therefore had to spell the field
`params.extra_body.temperature` to reach the wire at all.

*Gap 2: the repair candidate could not reach it there.* `drop-sampling-params` called
`_agent_json_remove(agent_dir, ["temperature", "top_p", "top_k"])`, which tested
`k in (raw.get("params") or {})` — **top-level keys only**. Against
`params.extra_body.temperature` it returned `None`, `add()` dropped the candidate for having
no edits, and the loop had nothing left: *"no further candidates for the observed failure
signatures; giving up"* — 0 candidates generated, $0.00 spent on the repair pass.

*The fix, at `a9676be`.* `map_params` asks the installed SDK's own signature once per name
(`anthropic_provider.messages_create_accepts`, cached) and routes anything it will not take
into `extra_body` — while the request is **built**, so the recorded request shows each param
where it was really sent. And `_agent_json_remove(..., also_extra_body=True)` removes the
params from `params` **and** from `params.extra_body`, dropping an `extra_body` emptied by
that. The halves are complementary: the first makes the pair decidable, the second makes the
decision repairable.

*Verified inside the rebuilt lab image before any paid call:*

```
messages_create_accepts: True          # the function exists at all
anthropic SDK: 1.4.0
  temperature -> False   top_p -> False   top_k -> False   max_tokens -> True
also_extra_body in _agent_json_remove: True
```

*And verified on the wire.* `agent/agent.json` declares `"temperature": 0.4` as a plain
`params` key, per `ADAPTER.md`'s updated contract, and upshift moves it. Every rep record of
`runs/opus47-001-r2-baseline/` shows the request going out as

```json
"request": {"extra_body": {"temperature": 0.4}, "max_tokens": 128,
            "model": "claude-haiku-4-5-20251001", "messages": [...], "system": [...]}
```

— the same HTTP body litellm sends from toponymy, now produced by upshift's transport rather
than by an adapter workaround. The candidate arm sends the identical body to `claude-sonnet-5`
and gets the API's own 400 back. **That is the point of the fix: the same request now gets two
different answers from the two models, instead of one client-side crash from both.**

The concurrently-run `a-101` (`antoncl/local-writing-app`, `opus47` row 3) hit the same wall
independently, on a different repo with a different SDK version pinned. Two repos, two agents,
one product defect — now closed.

### 4.2 What the repair does, and how it was verified

One field. `report/upgrade.patch`, upshift's own output, against `agent.json`:

```diff
   "params": {
-    "max_tokens": 128,
-    "temperature": 0.4
-  },
+    "max_tokens": 128},
   "system_prompt_file": "system_prompt.txt",
```

(The brace placement is the minimal-textual-deletion path. The file is valid JSON and the
resulting request is `{"max_tokens": 128, "model": ..., "messages": [...], "system": [...]}` —
no `temperature`, no `extra_body`. **Verified for the request shape**: that is the recorded
`request` object of every rep in both the screen and the verify runs, not an inference from
the diff.)

Run against the **candidate** model, the whole suite, N=5, unchanged thresholds, in the same
container with the same run-phase network restriction and the same three probes
(`logs/run-r2.log`, `logs/run-r2-probes.json`, `logs/proxy-run-r2.log`):

- screen (`opus47-001-r2-c01-447f0fbd-screen`), the 5 broken cases: **5/5 cases, 25/25 reps**
- full-suite verification (`opus47-001-r2-c01-447f0fbd-verify`): **5/5 cases, 25/25 reps**

Every regressed case restored, zero previously-passing cases broken, verified on the full
suite — by the tool. Attempt 1's hand-applied version of the same one-field change
(`opus47-001-patched-candidate`, 5/5 · 25/25) is retained and agrees exactly: 25 further paid
reps of the same conclusion.

**Honest limit on "0 collateral damage":** all five cases regressed, so the set of
previously-passing cases outside the regression was empty — the loop's own log says
`0 protected passing case(s)`. The verification re-ran the same five cases and found them all
passing; it had no untouched case to protect. The claim is true and is the strongest this
suite supports — it is not evidence that nothing else in toponymy is affected.

### 4.3 The same repair in toponymy's own code

`report/toponymy-drop-temperature.patch` — `git apply --check`-clean against the pinned
commit, +89/-14 in `toponymy/llm_wrappers.py`, in that file's own style.

It does not hardcode a model list. It mirrors the capability-detection pattern the file
already uses for system prompts (`_system_prompt_capability`,
`_looks_like_unsupported_system_prompt_error`, `_flatten_system_into_user`): send
`temperature` until the API says it is deprecated, then drop it and remember for the rest of
the run.

```python
if self._sampling_params_capability is not False:
    kwargs["temperature"] = temperature
```

plus a `_looks_like_unsupported_sampling_param_error` sniffer and a single retry in
`_completion_with_messages`. The same change is applied to the async twin
(`AsyncLiteLLMNamer._acompletion_with_messages`) because the defect is identical there.

**Unverified about this file-level patch, stated plainly:** it was never compiled, imported or
run — no target code was installed for this case (`--no-install`). What is verified is the
**request shape it produces**: 50 paid reps on `claude-sonnet-5` (25 screen + 25 full-suite
verify in attempt 2, plus 25 more in attempt 1) show that a Messages request with
`max_tokens` and no `temperature` succeeds for this suite, and 25 reps show that the same
request *with* `temperature` is refused with a 400. This patch's whole purpose is to move
toponymy from the second request to the first. The retry path itself, the async twin, and the error-string
sniffer's wording were not exercised against a live 400 (the sniffer's target string,
"`temperature` is deprecated for this model.", is the API's own, recorded 25 times in
`runs/opus47-001-r2-candidate/`). A maintainer should also decide
whether a silent first-call retry suits their fail-fast design, or whether they prefer the
check up front.

### 4.4 Comparison with the maintainers' preferred fix

The thread's agreed direction is: bump `litellm>=1.89.0` and set `drop_params=True`. Read at
the pinned wheels (downloaded, never installed):

| litellm | what happens to `temperature=0.4` on `claude-sonnet-5` |
|---|---|
| **1.85.0** (toponymy's `uv.lock`) | forwarded unconditionally — `map_openai_params` has a bare `elif param == "temperature": optional_params["temperature"] = value` (`transformation.py:1454-1455`). `drop_params` is irrelevant: `temperature` is in `get_supported_openai_params` (`:406`), so nothing drops it. **400.** |
| **1.89.0** (the floor the thread names) | the gate exists — `_apply_sampling_param` (`llms/anthropic/common_utils.py`) drops or raises via `AnthropicModelInfo._supports_sampling_params(model)` — but that resolves from the model map, and 1.89.0's bundled map **has no `claude-sonnet-5` entry at all**, while the hardcoded name fallback covers only `fable`, `opus-4-7`, `opus-4-8`. `claude-sonnet-5` is reported as *supporting* sampling params. **Still 400.** |
| **1.100.0** (current) | `claude-sonnet-5` and `claude-opus-5` carry `supports_sampling_params: false` in the map. With `drop_params=True` the field is dropped; without it, a clean client-side `UnsupportedParamsError`. **Works.** |

So the maintainers' fix is right in kind and **short by eleven minor versions in degree**: on
the pair this case measures, `>=1.89.0` is not a high enough floor for the bundled model map.
Two caveats that keep this from being a flat contradiction: litellm fetches the model map from
the network at import unless `LITELLM_LOCAL_MODEL_COST_MAP` is set, so a 1.89.0 install with
egress may already pick up a newer map carrying the flag; and the flag is data, so the exact
version at which `claude-sonnet-5` appeared was not bisected — only 1.89.0 and 1.100.0 were
read.

Two further differences worth putting in a PR body:

1. `drop_params=True` drops **silently** and globally. Naming then runs at the provider's
   default sampling instead of toponymy's chosen 0.4, with no log line. The capability patch
   in §4.3 drops the same field but only after the API has said so, and only for the model
   that said it.
2. `drop_params` is a litellm-wide setting; toponymy supports 100+ providers through the same
   wrapper. `_provider_kwargs` already merges user `provider_kwargs`, so a user can set
   `drop_params` themselves today — which makes it a workaround the library can document
   immediately, independent of whichever fix lands.

**What has been sent, and what has not.** As of this publication, **nothing has been sent to
`TutteInstitute/toponymy`**: no fork, no push, no pull request, no issue comment, no outreach
of any kind. The one thing that *has* been published is this report and the run records beside
it, in this repository, so that a pull request — if the founder chooses to open one — has a
citable evidence link instead of a placeholder. Publishing the evidence is not contact with the
upstream project, and this report should not be read as announcing one. If a PR is ever opened
it will appear on `TutteInstitute/toponymy` and be linked from issue #185; check there, not
here, for the current state.

Because the case closed `REPAIRED_VERIFIED`, a delivery package was prepared locally and left
unsent, on the `a-085` template: `PR_BODY.md` (title *"Drop `temperature` when the provider
says the model rejects it (#185)"*, every number traced to a run record, the litellm-version
correction, the limitations), `DELIVERY_CHECKS.md` (exclusion assessment and the file list to
publish), `ISSUE_COMMENT.md` (a comment for the open issue #185, linking the PR), and
`DELIVERY_COMMANDS.sh` (idempotent; fork → push → PR → issue comment → four `funnel.csv`
lines). Those four files are lab-internal and are not published here. The branch
`fix/anthropic-sampling-params-4-7` exists only in the gitignored `pr-workspace/toponymy`
clone, at `47d88a0`: this patch plus four unit tests in the repo's own
`patch("litellm.completion", ...)` style. `origin/main` had not moved from the pinned commit
when the branch was cut, and was re-checked at publication — still `bcc46b86`, with issue #185
still open and the fix still absent from `toponymy/llm_wrappers.py` upstream — so nothing
needed rebasing. `black` 26.5.1 is `--check` clean on all three changed files before and after;
**nothing was installed, no target code was run, and the four tests have never been executed.**
`DELIVERY_COMMANDS.sh` refuses to run unless `gh` is authenticated as the founder, the branch
is at the prepared commit, upstream has not moved, and `PR_BODY.md` no longer carries its
evidence-URL placeholder.

## 5. Adjudication

**No 2N adjudication was required, in either attempt.** The runbook mandates 10 reps for any
contested status — a case near a threshold, or a restoration or veto resting on one run. There
is none: every case is 5/5 baseline, 0/5 unpatched, 5/5 on the screen and 5/5 again on the
full-suite verification; the differ reported zero flaky cases on either comparison. Nothing
here is decided by a margin of one rep, and the restoration does not rest on one run — it was
measured three times (screen, verify, and attempt 1's hand-patched run), 75 reps in total, all
at the extreme. Budget would have allowed 10 reps (the whole case cost $0.2005 of $5.00);
there was nothing contested to spend it on.

## 6. Ops notes

Two `tools/budget.py` snags, both cosmetic but worth fixing:

- `refresh_case()` given a **relative** case dir silently records `unknown_rate: true` with
  `note: "upshift cost failed (exit 2)"` — `upshift cost` is invoked from the product repo, so
  the relative path does not resolve. Because `check` refuses to authorise spend while any
  record has an unknown rate, a relative path in a helper script can freeze the track for a
  reason that has nothing to do with money. It needs `Path(...).resolve()`.
- `budget.py status` / `refresh` calls `refresh_all()`, which rewrites `refreshed_at` in
  **every** case's `cost.json` — 53 files of timestamp-only churn per invocation. Reverted
  again after attempt 2 (`git checkout` on the untouched cases) so this case's diff is its own
  directory alone.

A third, smaller one found in attempt 2: **`lab_case.py` derives the upshift `--tag` from
`--case-id`**, and the case directory from it too, so there is no way to re-run a case under a
new tag without creating a second case directory. Re-running with the same tag would have
resumed attempt 1's records over the top of it and destroyed the evidence. Attempt 2 therefore
ran through `logs/rerun_attempt2.py`, which calls `lab_case`'s own helpers — same internal
network, same allowlist proxy, same three probes, same container flags, same two passes with a
budget check between them — and passes `--tag opus47-001-r2`. A `--tag` option on the driver
would remove the need for that wrapper.

## 7. Limitations

1. **`temperature` is declared as a plain param and travels in `extra_body`.** That is
   upshift's transport decision, made from the installed SDK's signature (§4.1), not a
   property of toponymy — toponymy reaches the same HTTP body through litellm. Everything
   measured here is about the wire request, and the recorded `request` object of every rep is
   what backs that claim. An upshift running against an older `anthropic` SDK that still takes
   the keyword would send the same body with the field at the top level.
2. **The checks are structural only.** `no_api_error` plus the repo's own JSON regex. Nothing
   asserts what the topic name *says*, because the output is free-text naming and a content
   match would be invented. This suite can detect a 400, an empty answer or a malformed one —
   and cannot detect a *worse but well-formed* name. `claude-sonnet-5`'s answers are visibly
   blander than `claude-haiku-4-5`'s (`Environment` vs `Environmental Sustainability`, and lower
   self-reported specificity on all five), and **this suite has no opinion about that**; it is
   not measured and is not claimed either way.
3. **No keyphrases in the prompts.** `cluster_keywords` is empty because keyphrases come from
   `KeyphraseBuilder` over a corpus, which needs the install this case declined. Both models
   see the identical shorter prompt, so it cannot create a difference between them, but real
   toponymy prompts are longer and richer (the maintainers' own recorded run in
   `doc/debugging_llm_runs.ipynb` shows ~9,261 prompt tokens per call against ~430 here).
4. **One layer, one routine.** Only `generate_topic_name` at layer 1 is exercised.
   `generate_topic_cluster_names`, the disambiguation prompt, topic summaries and the batch
   namers all send `temperature` the same way through the same `_provider_kwargs` and are
   presumably affected identically — presumably, not measured.
5. **`claude-opus-5` was not run.** The issue names both `claude-sonnet-5` and
   `claude-opus-5`; only sonnet-5 was measured, on cost grounds.
6. **Retries are toponymy's, not upshift's.** A rep here is one API call. toponymy wraps its
   calls in `tenacity` with three attempts, but `BadRequestError` is fail-fast so the retry
   never engages for this defect — that is read from the code, not measured.
7. **N=5, thresholds never moved.** 25/25 bounds that arm's true rep pass rate at 86.7-100%
   (Wilson 95%). It is not a claim of perfection at scale.
8. **Prompt caching did nothing here.** The system block is ~220 tokens, under Anthropic's
   1024-token minimum; every rep records `cached_input_tokens: 0`. Cost is the plain billable
   total.
