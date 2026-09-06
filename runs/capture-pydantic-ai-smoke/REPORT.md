# upshift diff

`claude-fable-5 @ messages`  ->  `claude-fable-5-1 @ messages`

- provider: anthropic
- n_reps: 3
- baseline run: `capture-pydantic-ai-smoke-baseline`
- candidate run: `capture-pydantic-ai-smoke-candidate`

## Verdict: SAFE WITH PATCH

restored 3/3 regressed, 0 previously-passing broken

patch: runs/capture-pydantic-ai-smoke/upgrade.patch

repair log:

- repair start: 3 regressed case(s), 0 protected passing case(s), budget 6 candidates
- candidate 1/6: [model_params] remove-forced-tool-choice — Drop the forced tool_choice param (rejected by this model) and state the same requirement as an instruction in the system prompt instead.
-   screen: 3/3 broken cases restored — running full verification
-   ACCEPTED: restored ['and-in-cairo_02', 'how-about-oslo_03', 'what-is-the-weather-in-paris_01']; 0 previously-passing cases broken; 0 regressed case(s) remain
- repair end: all 3 regressed cases restored

## Framework mapping

This agent directory was built from captured `pydantic-ai` requests. Each accepted repair, against the knob that expresses it in `pydantic-ai`:

| repair | knob | change | verified at |
| --- | --- | --- | --- |
| `remove-forced-tool-choice` | forced tool_choice | a structured `output_type` forces it; switch to `NativeOutput(...)` or `PromptedOutput(...)`, or union the output type with `str`. `ModelSettings(tool_choice=...)` does NOT reach output tools | `pydantic_ai/models/_tool_choice.py:99-101 + models/anthropic.py:1914-1923; output.py:21-22 @2.40.0` |
| `remove-forced-tool-choice` | system prompt | `Agent(system_prompt=...)` or `Agent(instructions=...)` | `ai.pydantic.dev agents docs @2.40.0` |

The full table, with the version every cell was verified against, is in `docs/framework-mapping.md`. "not mapped" means the knob does not exist at that version or could not be verified — the repair is still real, and still has to be applied some other way.

## Summary

- baseline: 3/3 cases pass (100.0%, CI 43.9-100.0%)
- candidate: 0/3 cases pass (0.0%, CI 0.0-56.1%)

3 regressed

## Cases that changed

0 stable-pass cases not listed.

| case | label | base | cand | p | signatures | first failing detail |
| --- | --- | ---: | ---: | ---: | --- | --- |
| and-in-cairo_02 | regressed | 3/3 | 0/3 | p=0.05 * | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| how-about-oslo_03 | regressed | 3/3 | 0/3 | p=0.05 * | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |
| what-is-the-weather-in-paris_01 | regressed | 3/3 | 0/3 | p=0.05 * | api_error_forced_tool_choice | API call failed: tool_choice: type "tool" and "any" are n... |

pass = rate >= 0.8 of N; fail <= 0.4; else flaky. p: one-sided Fisher exact, baseline vs candidate passes. CI: Wilson 95%.

full transcripts: runs/capture-pydantic-ai-smoke-candidate/cases/<case>/rep_k.json
