---
name: "`upshift adapt` got my agent wrong"
about: You pointed `upshift adapt` at a real agent and the result was wrong, incomplete, or misleading
title: "adapt: "
labels: adapt, agent-report
assignees: ""
---

**This is the report we most want.** `upshift adapt` reads a repository and drafts a
five-file agent from it. It is honest about what it could not verify, but it is definitely
not right about every codebase — and the only way it gets better is people telling us which
shapes of agent it mangles. Wrong-but-confident is the bug class we care about most.

## The agent

- Repository or framework (a link is ideal; "internal, but it looks like X" is fine too):
- What it does, in a sentence:
- Provider / endpoint: <!-- OpenAI chat completions, OpenAI responses, Anthropic messages, other -->
- Roughly how it is built: <!-- hand-rolled SDK calls, LangChain, notebook, CLI tool, ... -->

## What you ran

```
upshift adapt <repo-or-url> --out <dir>
```

## What it got wrong

<!--
Be as specific as you can. Which of the five files, and what about it?
  - system_prompt.txt   — wrong text, missing text, invented text
  - tools.json          — missing tool, wrong schema, wrong required fields
  - agent.json          — wrong endpoint, wrong model, wrong or missing params
  - backend.py          — wrong tool behaviour, nondeterminism, wouldn't run
  - cases/cases.json    — cases that don't test anything real
-->

## What the right answer was

<!-- The actual prompt / schema / parameter, and where it lives in the repo (path:line). -->

## Confidence and TODOs

`adapt` reports a confidence level per file and lists lines a human must review. Did it flag
the thing it got wrong, or did it claim high confidence and hand you something false?

- [ ] It flagged this (low/medium confidence, or a TODO pointing at it)
- [ ] It claimed it was verified and it was not — **this is the serious case**

## Extraction record

`runs/<tag>/` holds the extraction record: what it read, what it cited, what it cost.
Attaching it helps a lot. It contains **verbatim source excerpts from the repository you
pointed it at** — if that repo is private, please redact or summarise instead.

## Environment

- upshift version:
- Python version:
- OS:
