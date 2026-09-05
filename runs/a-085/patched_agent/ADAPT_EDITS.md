# ADAPT_EDITS — hand-written adapter, no `upshift adapt` run

The target is a TypeScript pnpm monorepo (`LayerDynamics/plastiq` @ `7f84779f`). `upshift
adapt`'s inventory and extractor are Python-oriented, and the case brief directs the A-015
route: **transcribe the request by hand from the pinned source**. `adapt` was never invoked, so
**$0.00 of the case budget went to adaptation**, and no target code was installed or executed —
the install phase was declined (`--no-install`), see §"Install" below.

| file | origin |
| --- | --- |
| `system_prompt.txt` | `ai/prompt.ts:12-88` (`parametricSystemPrompt()`), extracted mechanically from the template literal; the one interpolation resolved from the repo's own data |
| `tools.json` | `ai/tools/toolDefs.ts:43-78` (`toolDefs({creative:false})`), with the two zod-derived schemas transcribed from `ai/tools/schema.ts:37-132` and `ai/planning.ts:20-37` |
| `agent.json` | `ai/providers/anthropic.ts:181-217` + `ai/agentRunner.ts:72-73` |
| `backend.py` | `ai/tools/toolDefs.ts:157-200`, `ai/tools/buildPart.ts:52-94`, `ai/planning.ts:47-92`, `ai/tools/inspectGeometry.ts:150-153` |
| `cases/cases.json` | five user turns copied verbatim from the repo's own tests (§"Cases") |

## The request this adapter reproduces

plastiq's forced-tool turn, exactly as `AnthropicAdapter.stream` builds it when
`req.toolChoice = {tool: "build_part"}` — the `firstTool` seam (`agentRunner.ts:72-73`,
`runGeneration.ts:36-37,52`, `headless/generate.ts:34-35,123`, `plastiq-gen --first-tool`):

| wire field | value here | source |
| --- | --- | --- |
| `model` | `claude-opus-4-8` | `providers/models.ts:44` — the repo curates exactly `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5` and **no Fable**, so the migration pair is Opus 4.8 → Fable 5.1 |
| `max_tokens` | `16000` | `anthropic.ts:183`, `cfg.maxTokens ?? 16000`; no in-repo caller overrides it |
| `system` | `parametricSystemPrompt()` | `runGeneration.ts:15-21` with no open document (`editContext` returns null on an empty doc, `editContext.ts:64-65`) and `creative:false` |
| `tools` | the four `toolDefs({creative:false})` entries | `toolDefs.ts:43-78`; `create_mesh` is only added when `createMesh` deps are wired (`toolDefs.ts:199`) |
| `tool_choice` | `{"type":"tool","name":"build_part"}` | `toAnthropicToolChoice({tool:"build_part"})`, `anthropic.ts:36-41` |
| `thinking` | **absent** | `anthropic.ts:201-202`: `forcesTool` is true, so `useThinking` is false and the block at `:213` is omitted. This is the repo's own mitigation, and it is the thing this case is about |
| no `temperature` / `top_p` / `top_k` | — | the adapter sends none (`anthropic.ts:205-217`) |

## The five deliberate deviations

Each is a place where plastiq cannot be expressed in the five files. Listed so a reader can
disagree.

### 1. Non-streaming

`AnthropicAdapter.stream` is an `async *` generator sending `stream: true` (`anthropic.ts:214`)
with an SSE reducer at `:117-159`; upshift's Anthropic provider is non-streaming. The failure
under test is a **400 on the request body**, where transport is irrelevant: the body is built in
one place by three pure functions (`toAnthropicTools`, `toAnthropicToolChoice`,
`toAnthropicMessages`) and those are what is reproduced here. This adapter is a faithful port of
plastiq's **request construction**, not of its transport, and it says nothing about the SSE path.

### 2. `max_turns: 1` — the forced turn, and only it

plastiq forces the tool on **turn 1 only** and lets later turns run `auto`
(`agentRunner.ts:70-73`). upshift sends the same params on every turn, so a multi-turn episode
here would force `build_part` forever and could never reach `answer_user`. The episode is
therefore capped at the one turn the forcing applies to. Everything after turn 1 — the
self-correction loop, the `answer_user` finalizer, `turns_at_most` behaviour — is out of view.

### 3. `tools.json` is the **dereferenced** schema, and drops `additionalProperties`

`toolDefs.ts:25-31` derives `build_part`'s document schema from zod at runtime
(`z.toJSONSchema(authoringDocumentSchema)`), which emits `$defs`/`$ref` and one recursive
reference (`boolean.data.toolFeatures` → the feature union). The repo's own headless path — the
path that is actually driven with `--first-tool build_part` — sends the **dereferenced** form:
`grammarSafeToolDefs`/`dereferenceSchema` (`headless/nodeBuild.ts:31-96`) inlines every `$ref`
and represents the recursion point as a bare `{"type":"object"}`. This file is transcribed in
that form, for that reason.

`additionalProperties` is **omitted** on the transcribed feature/document objects because the
exact emission of zod v4's `toJSONSchema` for a non-strict object was not verified at this
commit (no install, no TypeScript run). Omitting it is the permissive choice: it cannot forbid
anything the target allows, and the handler's own zod parse is the real gate — which is what
`toolDefs.ts:23-24` says it is. The literal `additionalProperties: false` that appears in the
source (the `build_part` wrapper, `inspect_geometry`, `answer_user`) is preserved verbatim.

### 4. `backend.py` has no CAD kernel

plastiq's build probe is OCCT in a worker (`toolDefs.ts:102`, `agentTurn.ts:65-74`); upshift
requires determinism (ADAPTER.md rule 3), and OCCT is neither installable here nor deterministic
to port. The backend therefore enforces the **structural** part of what the real probe enforces,
and nothing else:

- the document schema (`schema.ts:37-132`): `features` array + `params` record of numbers,
  per-feature required `params`/`data` keys, known feature `type`;
- the `"assembly"` guard, with the repo's own message (`buildPart.ts:58-69`);
- the documented build rule that `extrude`/`revolve`/`cut` consume the sketch **immediately
  before** them (`prompt.ts:45-47`), reported with the repo's own error wording
  (`agentRunner.unit.test.ts:47`: `feature 'f1' (extrude): no sketch profile upstream`).

No geometry is evaluated, so a document that is structurally valid but geometrically empty
counts as built here and would not upstream. That direction is conservative for a *regression*
question — it can only make a case easier to pass, on both models equally.

`inspect_geometry` always returns upstream's empty-geometry answer verbatim ("There is no built
geometry to inspect yet.", `inspectGeometry.ts:150-153`), which is what upstream returns with
nothing built. Under `max_turns: 1` with `build_part` forced it is never reached.

### 5. Prompt-cache breakpoints

upshift always marks the system block and the last tool definition with
`cache_control: {"type":"ephemeral"}` (LAB_RUNBOOK §2.1). plastiq does not. This changes price,
not the model's inputs.

## Cases

Five, the brief's cap. Every user turn is copied verbatim from the pinned repo; none was
written for this lab.

| id | user message | source |
| --- | --- | --- |
| `cube_40mm` | `Make a 40 mm cube.` | `ai/providers/anthropic.integration.test.ts:35` — the repo's own live Anthropic round-trip, which asserts a `build_part` tool call is streamed |
| `cube_20mm` | `make a 20mm cube` | `ai/runGeneration.unit.test.ts:70`, `ai/aiStore.unit.test.ts:66` |
| `cube_10mm` | `make a 10mm cube` | `persistence/projectsStore.test.ts:221` |
| `box_10x20x30` | `Make a 10×20×30 mm box.` | `headless/generate.test.ts:48` — the headless path the CADGenBench harness drives with `--first-tool build_part` |
| `box_with_hole` | `make a box with a hole` | `ai/tools/toolDefs.unit.test.ts:218` |

Checks are deterministic only (no LLM judge), and each is a rule the repo states:

- `no_api_error`.
- `tool_called build_part`, `min_times = max_times = 1` — CB6.2's whole purpose is that the
  model calls `build_part` rather than answering in prose
  (`docs/plans/2026-06-22-cadgenbench-integration.md`, "CB6.2 — Generation: get a real model to
  call `build_part` (not `answer_user`)"), and the forced turn is how it is made to.
- `final_state first_feature_type == "box"` — `prompt.ts:30`, *a rectangular block is a `box`*.
  All five prompts ask for a rectangular body (the hole case for a box with a hole in it).
- `final_state box_dims_mm == "<sorted dx,dy,dz>"` on the four cases whose prompt states its
  dimensions — `prompt.ts:15`: "every length you write is in MILLIMETRES". Sorted, so the
  orientation the model picks is not asserted. `box_with_hole` names no dimensions and carries
  no dimension check.
- `state_count violations == 0` — the ported structural rules above found nothing wrong with
  the submitted document.

The `sim.oracle_plan` blocks exist only so the pipeline could be smoke-tested for $0 before any
paid pass. Sim results validate machinery, never the thesis.

## Install

Declined (`--no-install`). `install_review.txt` (10 files, sha256 each) shows the only install
command the driver would run is `npm install --ignore-scripts` over a pnpm workspace whose
packages resolve through `workspace:*`. Nothing in any `package.json` declares a `postinstall`
or `prepare` hook; `benchmark/harness/serve-model.sh` execs local model servers and is not in
the driver's command list. The install is not *unsafe* — it is *unnecessary*: the request under
test is fully determined by the files transcribed above, and running it would install
`opencascade.js` and a browser toolchain to produce nothing this adapter uses.

## Free machinery smoke, before any money

```
upshift upgrade --agent <this dir> --provider sim \
  --baseline-model sim-fable-5 --candidate-model sim-fable-5-1 --no-repair --tag a085-sim
```

baseline 5/5, candidate 0/5, signature `api_error_forced_tool_choice` on all five, p=0.00397.
$0.00 (`runs/a085-sim/`). It proves the five files load, the tool schema converts to the
Messages wire shape, the checks evaluate and the differ classifies. It proves nothing about
Opus 4.8 or Fable 5.1.
