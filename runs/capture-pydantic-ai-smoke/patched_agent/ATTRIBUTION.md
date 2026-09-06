# ATTRIBUTION — an agent directory built from captured wire traffic

## Where this came from

- Capture directory: `agents/capture-pydantic-ai-smoke/capture`
- Recorded: 2026-09-06T02:21:40.142716+00:00 → 2026-09-06T02:22:04.685104+00:00
- Mode: `forward`, upstream `https://api.anthropic.com`
- Requests: 6 in 3 conversation(s)
- Model(s) seen: claude-fable-5
- Framework: pydantic-ai

Detection evidence, verbatim from the request headers:

- `pydantic-ai` — user-agent: 'pydantic-ai/2.40.0' contains 'pydantic-ai/'

User agents observed:

- `pydantic-ai/2.40.0`

## What was taken, and from where

| Artifact | Source |
| --- | --- |
| `system_prompt.txt` | the `system` field of the recorded requests, verbatim |
| `tools.json` | the `tools` array of the recorded requests, verbatim |
| `agent.json` `model` | `claude-fable-5`, as sent |
| `agent.json` `params` | `max_tokens` / `tool_choice` / `thinking` / sampling params / effort, as sent |
| `cases/cases.json` | one case per captured conversation; user turns verbatim |
| `recorded_tools.json` | the `tool_result` payloads the framework fed back |

## Conversations

| id | requests | volatile suffix |
| --- | ---: | --- |
| `conv_01` | 2 | none |
| `conv_02` | 2 | none |
| `conv_03` | 2 | none |

No source file of the framework was read to produce any of this. upshift saw the bytes on the wire and nothing else — which is the whole point of capture mode: it works the same for a framework whose request path nobody can lift into five files.
