# `upshift adapt` evaluation 2/3: HolmesGPT (framework-style harness)

Target: [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) @ `ad965c6` — CNCF-sandbox SRE
agent, ~3.2k stars. Chosen for harness-style diversity: litellm transport, system prompt
assembled from jinja2 templates across several modules, tools defined in YAML toolsets and
Python plugins. Command: `upshift adapt <clone> --out <dir> --flex --agent-hint "the CLI
ask/investigate agent; prefer the sqlite/database toolset for tools"`.

## Result

Wall clock **128s**, extraction cost **$0.26** (its evidence bundle is 3.7× shell_gpt's).
13 must-review lines. The full sim upgrade pipeline runs on the generated directory with
**zero edits**: 5/5 generated cases pass the sim-5.5 baseline, 5/5 regress on sim-5.6-sol,
repair restores 5/5 → SAFE WITH PATCH.

Per artifact:

- **`agent.json` — high.** Endpoint `chat_completions` (correct: litellm's default
  transport for this config) with litellm's `drop_params` surfaced into params. The model
  id was flagged by the gate — "'gpt-5.5' was not found as a literal in the cited file" —
  which is right: HolmesGPT takes the model from config/CLI, so the human must set the one
  their deployment uses. Uncertain-and-explicit, as designed.
- **`system_prompt.txt` — low, and the report says "write it by hand".** The jinja2
  assembly (base template + toolset instructions + runtime context) was too entangled to
  extract with verifiable citations, so adapt refused to fake one. This is the predicted
  framework-style failure mode, surfaced honestly instead of hallucinated.
- **`tools.json` — medium.** Three schemas (`kubectl_get`, `kubectl_delete`, `bash`)
  assembled from the repo's own tests with per-schema citations. Note: the agent hint
  asked for the sqlite toolset and extraction picked the kubernetes tools that dominate
  the test evidence instead — hints steer, they don't bind.
- **`cases/cases.json` — medium.** 5 drafts with citations, including an
  approval-workflow case and a parallel-calls case derived from the upstream tests.
- **`backend.py` — low.** kubectl and bash touch a live cluster and a real shell: all
  three tools are TODO stubs, correctly.

## Bottom line

For a framework-assembled monorepo, adapt delivered runnable scaffolding (config, tool
schemas, cited cases, sim-green pipeline) in two minutes for a quarter, and correctly
refused to invent the two artifacts it couldn't verify — the prompt and the tool
backends. Those remain the human's morning of work, with the report pointing at exactly
which files to read.
