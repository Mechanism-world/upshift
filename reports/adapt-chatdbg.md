# `upshift adapt` evaluation 3/3: ChatDBG (hand-rolled harness, hardest extraction)

Target: [ChatDBG](https://github.com/plasma-umass/ChatDBG) @ `8ca3cd6` — AI debugger,
~1.1k stars, Apache-2.0. Chosen as the adversarial extraction case: a hand-rolled agent
loop where the OpenAI tool schemas live as raw JSON *inside function docstrings*, loaded
at runtime via `json.loads(function.__doc__)`, and the system prompt is chosen from
per-model template files. Command: `upshift adapt <clone> --out <dir> --flex`, no hint.

## Round 1 result

Wall clock **40s**, extraction cost **$0.09**. Honest partial:

- **`agent.json` — high, and correct**: endpoint `chat_completions` (litellm transport),
  model `gpt-4o` — ChatDBG's real default (`config.py`), verbatim-verified.
- **`system_prompt.txt` — medium**: the gpt-4o instruction template extracted, with the
  `{functions}` placeholder explained precisely (filled from function docstrings, cited to
  the exact `format_map` call).
- **`tools.json` / `cases` — failed, and reported as failed**: no tool schema was
  traceable, the suite is empty, and the report says the adapter will refuse to run. No
  invented schemas, no invented cases.

The diagnostic gold is in the report's *Undetermined* section: the extraction itself
pointed at exactly where the missing pieces live (`assistant.py:48` registers functions,
`:260` emits `f["schema"]` as tools) and stated the files with the concrete definitions
"are not included in the evidence" — a ranking gap in the tool, precisely localized by the
tool's own output.

## What we changed because of this run

The round-1 finding became a feature: extraction now runs a second round that follows the
model's own pointers — slicing the files (or the uncovered line ranges of already-seen
files) that round 1 cited as the location of what it couldn't determine — and re-extracts
with the new source. Verified against the recorded round-1 request that the planner pulls
exactly the `assistant.py:78–171` gap that fell between two round-1 slices. 51 offline
tests pin the behavior.

## Round 2, live

Re-run: **85s, $0.15** (two rounds, prompt cache absorbing most of the round-1 re-read).
Round 2 fired — "followed 4 of 6 pointers into unread source" — and the honest outcome is:
**it did not rescue ChatDBG.** This run's round-1 pointers (extraction is stochastic; they
differ run to run) named only the *registration* site in `assistant.py`, and ChatDBG's
concrete tool definitions live in files no citation ever names (`chatdbg_pdb.py`,
`dbg_dialog.py` hold the docstring schemas). A ±80-line window around a registration line
cannot reach definitions in other files; the report line says plainly "0 claims settled".
Reaching them would take cross-file identifier chasing — a real capability, out of scope
for this release and noted as such.

## Bottom line

ChatDBG is the harness style adapt handles worst, and the output says so instead of
pretending otherwise: correct `agent.json` (endpoint, real `gpt-4o` default), a usable
prompt template with its `{functions}` placeholder precisely documented, and an explicit
refusal on tools and cases — with pointers a human can follow to write `tools.json` from
the docstrings in well under an hour. Adapter not runnable without that human work. The
limits section of the README states this class of failure; better to be the tool that
says "I couldn't see it" than the one that makes it up.
