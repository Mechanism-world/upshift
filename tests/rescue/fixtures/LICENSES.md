# Rescue fixture licenses

`incidents.json` encodes, per incident, only **API request parameters**: the endpoint, the
baseline/candidate model pair, and the params dict the application sent. No system prompt,
tool schema, source excerpt, or eval datum from any target repository is reproduced in this
directory. Even so, only incidents whose upstream repository carries a permissive OSI
license are included, so the fixture set stays redistributable as a whole.

| fixture | upstream | license (SPDX) | where the license was read |
| --- | --- | --- | --- |
| `prisma` | Rynaro/prisma | `MIT` | rescue ledger `ops/queue.csv` row `ghi56-001` and `ops/cases/ghi56-001/CASE.md` |
| `crispen` | Voidious/crispen | `MIT` | `ops/cases/ghc-062/CASE.md` ("License: MIT", `LICENSE` at the pinned sha) |
| `synapse` | ardhaecosystem/synapse | `MIT` | `ops/queue.csv` row `ghc-223` (`license=MIT`) |
| `waku` | ShenSeanChen/waku-agent | `MIT` | `ops/cases/ghisdk-127/CASE.md` ("License: MIT") |
| `omnideck` | omnideck-dev/omnideck | `Apache-2.0` | `ops/queue.csv` row `ghisdk-052` |
| `policybench` | PolicyEngine/policybench | `MIT` | `agents/policybench/ATTRIBUTION.md` in this repository |
| `toponymy` | TutteInstitute/toponymy | `MIT` | `ops/anthropic/cases/opus47-001/DELIVERY_CHECKS.md` (GitHub license API + `LICENSE` line 3) |
| `litellm_capture` | none — a capture *shape* | n/a | Not derived from BerriAI/litellm, whose repository carries `NOASSERTION`. The fixture is a two-turn `tool_choice` sequence (`any` then `auto`), i.e. an API parameter shape, and contains no upstream material. |

## Excluded on purpose

| campaign case | upstream | why excluded |
| --- | --- | --- |
| autonima | autonima | no LICENSE at the pinned sha |
| `ghi56-006` | sandialabs/atlas-ui-3 | no LICENSE at the pinned sha (the ops record notes "license NONE so prompt/tools stay private") |
| plastiq | LayerDynamics/plastiq | PolyForm Noncommercial — not redistributable here |

`agents/plastiq/` already in this repository is unaffected by this exclusion: it is a
hand-written adapter that predates these fixtures and carries its own `ADAPT_EDITS.md`.
Nothing from it is copied into `tests/rescue/fixtures/`.
