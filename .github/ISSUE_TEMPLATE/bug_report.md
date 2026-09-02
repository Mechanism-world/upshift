---
name: Bug report
about: upshift did something wrong, crashed, or reported something you believe is untrue
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- What you expected, and what you got instead. -->

## Command

```
<!-- The exact upshift command, with any secrets removed. -->
```

## Output

```
<!-- The error or the wrong output. Full traceback if it crashed. -->
```

## Environment

- upshift version (`upshift --version`):
- Python version (`python3 --version`):
- OS:
- Provider used: <!-- sim / openai / anthropic -->
- Models (baseline → candidate):

## Run record

Runs are recorded to disk, and the record is usually the fastest way to see what happened —
`runs/<tag>/` holds the per-rep transcripts and the summary.

If you can attach the relevant `rep_*.json`, please **read it first**: records contain your
full system prompt, every message, and every tool result. Redact anything you would not post
publicly. (They never contain API keys — see SECURITY.md.)

## Anything else

<!-- Does it reproduce with `--provider sim`? Did it work in an earlier version? -->
