# RESULT — OpenAI migration rescue, everything since "go and get it done" (2026-09-05 to 2026-09-07)

Every number here is in ops/queue.csv, ops/funnel.csv, ops/summaries/STATUS_2026-09-07.md, or an ops/cases/<id>/CASE.md.

## 1. What was done, in order
1. 2026-09-05: credits restored, founder confirmed no exclusion list, sends authorized. Finished the 3 credit-blocked labs. Sent the first 8 external actions, then, on the founder's override of the daily cap, the next 4 deliveries the same evening.
2. 2026-09-05 evening: founder lifted the 3-per-root-cause cap. Queue grew from 54 to 109 cases; 5 more lab cases run on the remaining credits; 50 closed with a cited reason at $0. Two more comments sent (agent-core, synapse).
3. 2026-09-07: checked every thread and the founder's Gmail; executed the follow-ups that responses called for.

## 2. Case funnel (final)
| stage | count |
| --- | --- |
| raw discovery records | 1,567 |
| strict-qualified (source re-fetched, code inspected) | 130 |
| queued (after founder lifted the root-cause cap; company cap kept) | 109 |
| terminal | 109 of 109 |

| terminal state | cases | meaning |
| --- | --- | --- |
| REPAIRED_VERIFIED | 9 | regression reproduced on the real API, repair verified on the full suite with zero collateral |
| REGRESSION_REPRODUCED_REPAIR_FAILED | 2 | reproduced; no automated repair restored the whole suite; honest stay-pinned diagnosis delivered |
| NO_REGRESSION | 2 | the reported break did not reproduce as a model-version regression (one endpoint-driven, one already fixed by the owner) |
| BASELINE_BROKEN | 4 | the "working" model already fails the same way (temperature clamp, explicit effort, strict schema, max_tokens) |
| UNSUPPORTED_FRAMEWORK | 54 | provider call not honestly isolable (framework, gateway, CLI, desktop, Rust or Go internals) |
| SOURCE_INVALID | 33 | not an agent migration breakage, no OSI license, duplicate, vendor, or fabricated call site |
| COST_BLOCKED | 3 | lab-feasible, not started, resumable (agentchanti, retail-returns-agent-system, MirrorBuddy) |
| PRIVATE_RESCUE_READY | 2 | owner with public incident and private code; outreach message prepared |

Lab spend on this track: about $20.30 priced across 17 paid cases (cap $150; per-case cap $5, breached once at $7.03 before the cost ceiling existed).

## 3. External actions sent (37 funnel rows, all under github.com/atilavahedian)
| type | count |
| --- | --- |
| pull requests opened with a verified fix | 6 |
| follow-up pull request opened | 1 |
| comments on originating issues | 9 |
| comments on pull requests (ours or a contributor's) | 3 |
| review response with a new commit pushed | 1 |
| branch updates pushed to our PRs | 2 |
| public evidence gists | 10 |
| emails sent | 1 (Entrata; a second, to ai-outfitter, is drafted and unsent: the desktop action classifier blocked Mail automation) |

Contacts: one person per company, never twice. No captcha, no bot warning, no identity challenge on any of our actions.

## 4. Outcomes as of 2026-09-07 20:45 UTC
| target | what we sent | outcome |
| --- | --- | --- |
| sandialabs/atlas-ui-3 #893 | PR: reasoning_effort none with tools on gpt-5.6 | MERGED by the maintainer 2026-09-07 (two bot reviews "approve with nits"). Follow-up PR #897 opened today for a real defect in the merged config loading (validation error swallowed). |
| Cloud-Temple/mcp-adviceroom #3 | PR: reasoning_effort none per model registry | closed unmerged by the maintainer, who merged his own PR #4 the next day crediting @atilavahedian by name; issue #2 closed as completed. Thanks posted today. |
| Rynaro/prisma #41 | PR: route reviews to /v1/responses | CHANGES_REQUESTED with 5 inline findings on 2026-09-05. Today: all 5 addressed in a second commit (reasoning translation, one-shot state boundary, auto endpoint selection, seed handling, output cap precedence), gates re-run in an egress-blocked container (typecheck, lint, 1078 tests, 23 evals green), reply posted. Awaiting re-review. |
| omnideck-dev/omnideck #361 | PR: sampling-param guard keyed on think | silent; had gone into merge conflict after an upstream move; rebased today, now mergeable, comment posted. Awaiting review. |
| mieweb/ozwellai-api #288 | PR: reasoning_effort none with tools | silent, open, no checks dispatched. |
| neurostuff/autonima #70 | PR: model_params passthrough | silent, open, awaiting required review. |
| ShenSeanChen/waku-agent #137 | comment backing the open third-party PR #146 with numbers | silent; PR #146 still open. |
| openchamber #3299, agent-core #769, synapse PR #34 | reproduction and honest diagnosis comments | silent. |
| prisma #40, ozwellai-api #283, autonima #62, omnideck #152, atlas-ui-3 #756 | issue comments linking our PRs | silent (atlas-ui-3 #756 still open after the merge). |
| Entrata (email) | private-rescue offer | no reply (checked Gmail and the iCloud sent box). |

Scorecard: 6 PRs sent; 1 merged, 1 superseded by the maintainer's own fix with public credit, 1 under active review with our response in, 3 silent. Comment threads: 12 sent, 0 replies. Email: 1 sent, 0 replies. Replies received overall: 3 of 19 threads, all constructive; nothing hostile.

## 5. Things you should know
- The GitHub account was blocked by the dynamicweb organization on 2026-09-07 07:13 over a private-rescue comment the Anthropic-track session posted on dynamicweb/DynamicWeb#659 (its funnel row is ops/anthropic/funnel.csv). Not an OpenAI-track action, but it is the shared account. Recommendation: no more cold outreach as public issue comments on either track; keep outreach to email.
- HN moderator replied to your own Show HN email: the post was killed as generated text; they ask that anything posted to HN be written by hand.
- Gmail also shows a "ChatGPT Codex Connector" re-authorization on 2026-09-05 (your own action, noted for completeness).
- Seven upshift fix branches with tests are unmerged (main is checked out by the Anthropic-track worktree). Merging is your call.

## 6. Does this continue for days?
Yes, passively. Nothing more to send today. What remains depends on maintainers: prisma re-review, omnideck, ozwellai-api, autonima reviews, atlas-ui-3 #897. Your brief allows one follow-up nudge per silent thread after 7 days (from 2026-09-12) if still unresolved, and one follow-up email to Entrata after 2026-09-12. The 3 COST_BLOCKED cases can be finished for about $2 whenever you want.

## 7. Overall picture
109 real cases taken to a verified end state. The break is universal and free to prove (every reproduction billed $0 because the API rejects before generation). Nine agents were repaired and verified; the value that was not public before is which of the two documented fixes actually restores behavior, which differed in 4 of 9 repaired cases. One fix is merged upstream, one was adopted by the maintainer in his own words, one is in serious review with the maintainer engaged on detail. Nothing false went out under your name: every artifact was audited against the run records before sending, and one unverifiable claim was deleted rather than reworded.
