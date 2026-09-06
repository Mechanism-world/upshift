# Capture mode, live: a pydantic-ai agent from `claude-fable-5` to `claude-fable-5-1`

**This is a smoke test, not evidence.** It exists to prove that `upshift capture` →
`upshift adapt --from-capture` → `upshift upgrade` works end to end against the real Anthropic
API, on a framework upshift has never been able to read. It is 3 cases at **N=3**, below the
default N=5, on one agent of one shape. Nothing here is a claim about how `claude-fable-5-1`
behaves. The two runs that would be evidence — the real 5.5 → 5.6-sol experiment
(`CLAUDE.md`) and the per-case Anthropic rescue reports in this directory — are elsewhere.

**Result: `SAFE WITH PATCH`.** Baseline **3/3 cases (9/9 reps)** on `claude-fable-5`;
candidate **0/3 (0/9)** on `claude-fable-5-1`, every rep a 400; one repair candidate restores
**3/3 at full-suite verification** with 0 previously-passing cases broken. **$0.99** of a
$3.00 cap.

| | |
|---|---|
| framework | `pydantic-ai` 2.40.0 (`pydantic-ai-slim[anthropic]`), over `anthropic` 1.4.0 |
| the agent | `agents/capture-pydantic-ai-smoke/pai_smoke.py` — `AnthropicModel` + a structured `output_type` + one trivial tool, 3 one-shot conversations |
| pair | `claude-fable-5` → `claude-fable-5-1`, endpoint `messages`, provider `anthropic` (real, not simulated) |
| N | 3 reps per case per model, thresholds pass ≥ 0.8 / fail ≤ 0.4 (never lowered) |
| capture | `agents/capture-pydantic-ai-smoke/capture/` — 6 requests, 3 conversations |
| agent dir | `agents/capture-pydantic-ai-smoke/agent/` — the five files, generated |
| runs | `runs/capture-pydantic-ai-smoke{,-baseline,-candidate,-c01-18217d26-screen,-c01-18217d26-verify}` |
| spend | **$0.9917** total, priced from the API's own usage fields |
| upshift | 0.4.0-dev (`feat/capture-adapter`) |

## What was run

```bash
upshift capture --out agents/capture-pydantic-ai-smoke/capture       # terminal 1
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 python pai_smoke.py         # terminal 2, then Ctrl-C
upshift adapt --from-capture agents/capture-pydantic-ai-smoke/capture \
  --out agents/capture-pydantic-ai-smoke/agent
upshift upgrade --agent agents/capture-pydantic-ai-smoke/agent --provider anthropic \
  --baseline-model claude-fable-5 --candidate-model claude-fable-5-1 \
  --n 3 --tag capture-pydantic-ai-smoke
```

The agent script knows nothing about upshift. One environment variable — the one the Anthropic
SDK already reads — put the recorder in the path.

## 1. Capture

6 requests, 3 conversations, `claude-fable-5`, tools `temperature_c` and `final_result`.
Framework detected as `pydantic-ai` from `user-agent: pydantic-ai/2.40.0`; the path was
`POST /v1/messages?beta=true`, as `docs/framework-mapping.md` §1 says it is.

The break was on the wire before anything was adapted, in `index.json`'s `params_seen`:

```
tool_choice: [{"type": "any"}]
```

That is pydantic-ai turning a structured `output_type` into a forced tool choice
(`docs/framework-mapping.md` §2: `pydantic_ai/models/_tool_choice.py:99-101` →
`models/anthropic.py:1914-1923`), and it is the exact param `claude-fable-5-1` rejects.

## 2. Adapt

3 cases (one per conversation), 2 tools, `max_turns` 3, model `claude-fable-5`,
`tool_choice: {"type": "any"}` carried through as sent. No model call, no source file of
pydantic-ai read. Every deviation from the recorded bytes is in the generated
`ADAPT_EDITS.md`; the two specific to this capture are the `terminal_tools` detection below
and the wrapping of an `int` tool result into `{"content": 14}`, because `ADAPTER.md` requires
`execute()` to return a dict.

## 3. Upgrade

| | baseline `claude-fable-5` | candidate `claude-fable-5-1` | patched candidate |
|---|---|---|---|
| cases | **3/3** | **0/3** | **3/3** |
| reps | 9/9 | 0/9 | 9/9 (full-suite verification) |

Every candidate rep failed with the documented 400:

```
tool_choice: type "tool" and "any" are not supported for this model.
```

The repair loop accepted candidate 1 of 6 —
`[model_params] remove-forced-tool-choice`: drop the param, and state the same requirement as
a sentence in the system prompt. 0 previously-passing cases broken (there were none to break;
all 3 had regressed). Verdict **`SAFE WITH PATCH`**, `runs/capture-pydantic-ai-smoke/`.

## 4. The framework mapping

The patch edits `agents/capture-pydantic-ai-smoke/agent/`, which nobody maintains. The report
and the patch header therefore both name the setting in pydantic-ai itself, cited
(`docs/framework-mapping.md`):

> **`remove-forced-tool-choice` / forced tool_choice** — a structured `output_type` forces it;
> switch to `NativeOutput(...)` or `PromptedOutput(...)`, or union the output type with `str`.
> `ModelSettings(tool_choice=...)` does NOT reach output tools.
> Verified at `pydantic_ai/models/_tool_choice.py:99-101` + `models/anthropic.py:1914-1923`;
> `output.py:21-22` @2.40.0.

Worth knowing, and also in the table: pydantic-ai ≥2.40 already degrades a forced tool choice
to `{'type':'auto'}` for model names it recognises (`profiles/anthropic.py:255`). This capture
was taken on `claude-fable-5`, where it does not apply — which is precisely why the recorded
bytes still carry `{"type":"any"}`, and why replaying them against `claude-fable-5-1` 400s.
A user on an unrecognised or custom-profiled model name is in the same position.

## 5. Two product bugs this run found, both fixed on the branch

1. **The recorder wrote the workspace id.** `anthropic-workspace-id` was on the header
   keep-list, so all six request records carried the real value — in an artifact upshift tells
   you to share. It now redacts like a credential: recorded as present, never as a value.

2. **The baseline failed, and the harness was why.** pydantic-ai's `final_result` *is* the
   answer: the framework stops on that call and never returns a tool_result. upshift's loop
   fed one back, and the model — still under the forced `tool_choice` — called `final_result`
   again: four assistant turns where the real agent could take two, failing the case's own
   `turns_at_most` **on the baseline model**. The capture already proves which tools those
   are (a `tool_use` id no later request ever answers), so `adapt --from-capture` now writes
   `terminal_tools` and the episode ends where the framework ended. The first upgrade attempt,
   before the fix, is the counterfactual: baseline 0/3, verdict a meaningless `SAFE`. Its
   $0.3179 is included in the total below.

## 6. Spend

| run | model | in | out | USD |
|---|---|---:|---:|---:|
| capture (2 runs of the script) | `claude-fable-5` | 8,050 | 912 | 0.1261 |
| discarded first upgrade, baseline only | `claude-fable-5` | 20,205 | 2,317 | 0.3179 |
| `-baseline` | `claude-fable-5` | 12,129 | 1,396 | 0.1911 |
| `-candidate` | `claude-fable-5-1` | 0 | 0 | 0.0000 |
| `-c01-18217d26-screen` | `claude-fable-5-1` | 10,365 | 1,495 | 0.1784 |
| `-c01-18217d26-verify` | `claude-fable-5-1` | 10,365 | 1,490 | 0.1782 |
| **total** | | | | **0.9917** |

The candidate run cost $0 because a 400 bills nothing. The capture legs are priced by hand
from the `usage` blocks in the recorded responses — a capture is not a run record, so
`upshift cost` does not see it. No prompt caching was in play at this size (the cacheable
prefix is well under the 512-token minimum), which is why every `cached` column is 0.

## 7. What this does not show

- **N=3 and 3 cases.** Enough to see a hard 400 appear and disappear; not enough to measure a
  behavioral difference. Any real use of capture mode should run the default N=5 or higher.
- **One conversation shape**: single user turn, one tool call, one structured answer. No
  multi-turn conversation, no streaming capture, no parallel tool calls.
- **One framework.** The other seven rows of `docs/framework-mapping.md` are verified from
  source, not from a live capture. Detection for `langchain-anthropic` in particular cannot be
  exercised at all — it sends no header of its own and needs `--framework`.
- **Nothing about model quality.** The candidate never produced a token; it 400'd.
