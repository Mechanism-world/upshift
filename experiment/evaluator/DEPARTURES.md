# Departures from EVALUATION_PLAN.md

None yet. Each entry: UTC timestamp, what changed, why, and whether it was applied to both candidates.

## 2026-09-14 ~14:45Z — candidate trees built before baseline artifacts were hashed
Plan §2.2 says `PASSING_SET.txt`/`FLAKY_LIST.md` are hashed "before any candidate tree is
built". In execution all three trees (base, candX, candY) were cloned, patched and
installed in one pass *first*, so that the three environments would be byte-identical
(`pip-freeze.txt` differs only in the editable path — verified), and only then were the
three baseline runs executed and the artifacts hashed.
Why this is harmless: building a candidate tree is a filesystem operation in a separate
directory that cannot influence the baseline's test outcomes, and I did not read either
diff before the baseline was frozen. The property the rule protects — that the definition
of "regression" is fixed before any candidate result exists — holds: no candidate test run
had been executed when PASSING_SET.txt was written.
Applied to: both candidates equally.

## 2026-09-14 ~22:25Z — M4 detector corrected before any candidate was scored
Plan §3.4 defined a rep's PASS by five conditions. Two were written in prose that my first
implementation got wrong, and the error showed up on the BASELINE (which is why the
baseline is run first):
1. `loop_roundtrip` originally looked for `function_call` in the truncated first 3 KB of
   the SSE response, which is protocol preamble — it returned False on every baseline rep
   even though the loop demonstrably round-tripped. Replaced with the plan's own written
   criterion, read off the recorded REQUEST bodies: a request whose conversation carries a
   `function_call` and a `function_call_output` with the SAME call_id (Responses shape), or
   an assistant `tool_calls` entry and a `role:"tool"` message with the same id (Chat
   Completions shape).
2. `literal_in_output` matched the literal anywhere in stdout, including the echoed prompt,
   so it could never fail. Replaced with the plan's actual wording ("the FINAL assistant
   message contains the required literal"), implemented as: the literal appears after the
   line `System: Ran allowlisted command: ` echo upshift-eval-ok ` `, plus a separate
   `tool_actually_ran` check for that line itself (plan §3.4 condition 2).
Both changes are pure re-derivations from data already on disk — no extra API spend — and
were made BEFORE either candidate's live battery was run. Applied identically to base,
candX and candY.

## 2026-09-14 ~22:40Z — ADDITION: the unpatched baseline was also run live on gpt-6-astra
Plan §4.6 ordered the baseline on the CURRENT model (gpt-5.6-sol) only. I additionally ran
the full L0+L1 battery on the UNPATCHED pinned SHA against gpt-6-astra ($0.2473).
Reason: without it I could not say which of M1-M5 the migration actually repaired, and
"both candidates pass M1" is meaningless if the unpatched tree also passes M1. A 400 bills
nothing, so the effort probe was nearly free.
Symmetry: applied to the BASELINE only. It adds no measurement to either candidate and
cannot advantage one over the other.

## 2026-09-14 ~23:05Z — ADDITION: rung-1 grid completed (responses-off x effort, tool-less)
Plan §6 rung 1 committed in advance to counting robustness across "`--stream`/`--no-stream`;
`GPTME_OPENAI_RESPONSES_API=0`; each accepted `GPTME_THINKING_EFFORT` level; `-t none`;
`LLM_PROXY_URL` set". I ran those cells and then, having seen that the two patches model the
effort/endpoint relationship differently, ran the CROSS of the last two families
(responses-off x each of the seven levels, tool-less) as `R1c-toolless-cc`.
Honesty note: this cell was run AFTER I read the diffs, so it was chosen knowing it could
discriminate. Mitigations: (a) it is the natural completion of a grid committed in advance,
not a new criterion; (b) I ran the FULL 7-level grid on all three trees, not the single
cell that separates them; (c) the pass/fail for each cell is decided by the live API's own
400, verified independently by raw calls (see LEDGER.md), not by my judgement; (d) it feeds
only the tie-break ladder, never the M1-M5 verdict.

## 2026-09-14 ~23:05Z — rung-1 wording disambiguated
Plan §6 rung 1 said a configuration counts when "the migration holds (correct endpoint, no
banned parameter, no pre-request exception)". Taken literally, "no pre-request exception"
would score a candidate DOWN for refusing a level the API rejects — i.e. it would reward
sending a known-bad request. That cannot be the intent, and under it neither candidate's
policy is scoreable. Resolved as: a configuration HOLDS iff the application either issues a
request the API accepts, or refuses locally instead of issuing a request the API rejects;
it FAILS iff it issues a request the API demonstrably rejects. This reading is neutral
between "refuse loudly" and "substitute a valid value" - both count as holding.

## 2026-09-14 ~23:30Z — L2 (write-and-run) NOT run; its plan condition was met but its cost was not
Plan §4.5 made case L2 conditional on "remaining budget >= $0.80" after all mandatory work,
and required it to run "for baseline, X and Y, or for none of them". After the mandatory
battery, $1.0954 remained, so the stated condition WAS met.
I then measured the actual cost of one L2 rep rather than guessing: $0.0759
(5 requests, 5 523 in / 414 out) — recorded at results/costprobe/L2-costprobe.json.
The full symmetric battery is 3 trees x 5 reps = $1.139, which on top of the $0.9805 then
spent would total $2.12 — over the $2.00 hard cap. Running L2 for only X and Y would fit
($1.74) but would leave no baseline to subtract, which is the one thing PROTOCOL §11 and
plan §3.4 both forbid.
Resolution: L2 is NOT run, for any tree. Per PROTOCOL §6 the case set is narrowed, never
the repetitions. The $0.80 threshold in the plan was set before L2's cost had been
measured and was simply too low a bar; that is a defect in my own plan, recorded as such.
Effect on the verdict: M4 rests on case L1 alone at N=5. Stated as a limitation in
SCORING.md §7.3. Neither candidate is advantaged: neither was measured on L2.
