# ADAPT_EDITS — hand-adapted from scratch

**`upshift adapt` produced nothing usable for this target, because notebooks are not readable.**
Every file in this directory was written by hand from `tool_use/tool_choice.ipynb`. There is no
generated baseline to diff against, so there is no per-file edit count; the honest number is
"five files, hand-written".

## Counts

| file | origin |
| --- | --- |
| `agent.json` | hand-written |
| `system_prompt.txt` | extracted verbatim from notebook cell 33 by script |
| `tools.json` | extracted verbatim from notebook cell 33 by script (`ast.literal_eval`), re-wrapped chat-style |
| `backend.py` | hand-written from cell 33's two mock functions |
| `cases/cases.json` | hand-written; 4 of 6 user messages copied verbatim from cells 35/37/39/41 |
| `ATTRIBUTION.md`, `ADAPT_EDITS.md` | hand-written |
| **edits relative to an adapt output** | **n/a — adapt output discarded in full** |

## What adapt actually produced, and why it is unusable

Run: `upshift adapt <cookbooks>/tool_use` (ADAPT_REPORT.md in
`scratchpad/adapt-claude/cookbooks-tool-use/`; extraction model `gpt-5.5` via `openai-flex`).

- **It never saw the notebook.** Its inventory reports "18 file(s) scanned · 7 with a signal";
  the seven ranked files are all `.py`. `tool_choice.ipynb` — and every other `.ipynb` in the
  directory — is invisible to `inventory.py`, which is why this target was hand-adapted. A
  `.ipynb` is JSON with source split across string lists, so neither the AST path nor the
  regex path fires on it.
- **It locked onto a different agent.** With the notebooks invisible, the highest-scoring file
  was `memory_demo/code_review_demo.py`, so the generated `agent.json` is a
  `code-review-assistant` with the memory tool — a different agent in a different subdirectory.
- **It produced no tools and no cases.** `tools: 0`; `tools.json` is a placeholder;
  `cases/cases.json` is empty (its own report says "no case survived: the suite is empty and
  upshift will refuse to run"); `backend.py` is a single `TODO_adapt_undetermined_tool` stub.
- **Model was invented.** `model` was undetermined, so it wrote upshift's default `gpt-5.5`
  into an Anthropic `messages` agent — flagged in its own must-review list.
- **It said so.** The report's Undetermined section contains, verbatim: *"Tool-choice SMS bot
  notebook hinted by the operator — pointer: `memory_demo/code_review_demo.py` (No SMS bot
  notebook or related tool_choice call site is present in the provided evidence.)"* The
  extractor was told what to look for, could not see it, and reported the hole instead of
  fabricating one. That is the failure-mode contract working (DESIGN.md, "wrong-but-confident
  is the bug class that kills the feature") — it just leaves the whole job to a human.

## The concrete v0.3 ask this target produces

Teach `inventory.py` to read `.ipynb`: concatenate each code cell's `source` list into a
synthetic Python module, keep a cell-index → line map so citations can say "cell 33" instead of
a line number in a file that does not exist on disk, and run the existing AST analysis over it.
Everything downstream — ranking, extraction, the verbatim gate — then works unchanged. Cell 33
here is a single self-contained agent definition (system prompt, tools, mocks, call site,
`tool_choice`), so it is close to a best case for extraction once it is readable at all.
Cookbook and tutorial repos are a large share of the "copied this pattern into production"
surface upshift is aimed at, and today adapt is blind to all of them.

## What was hand-written, and how it was kept honest

- Prompt and tool literals were **extracted by script**, not retyped: the notebook JSON is
  parsed, cell 33's source is reassembled, and `ast.literal_eval` evaluates the `tools = [...]`
  assignment. Double spaces and the trailing space inside `"The username of the user in
  question. "` survive because nothing retyped them.
- Every delta from the notebook runtime is listed in ATTRIBUTION.md — including the one
  invented piece of state (`delivered`) and why the check vocabulary made it necessary.
- Four of six cases use the notebook's own example inputs verbatim; the two written here are
  marked as such in the table.

## Verification

`upshift`'s own machinery, no API calls: `validate_agent_dir` passes; `run_suite` on
`sim-fable-5` passes 6/6 cases; `sim-fable-5-1` fails 6/6 with the documented
`tool_choice: type "tool" and "any" are not supported for this model.` 400. Covered by
`tests/test_agents_claude_a.py`.

A full `upshift upgrade --provider sim` (N=5, run records written outside the repo) ends
SAFE WITH PATCH on a single accepted candidate — `remove-forced-tool-choice` restores 6/6 with
0 broken. Neither tool name matches the sim's retrieval heuristic, so no second repair step is
needed. Sim results validate the machinery, never the thesis.
