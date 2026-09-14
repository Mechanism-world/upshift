# Rules binding BOTH migration arms (frozen before either arm starts)

## Your objective
"Competently migrate this application to GPT-6 Astra while preserving the behaviors the
application actually depends on."

Not: make a tool look good. Not: find the flashiest bug. Produce the migration a strong
engineer would actually ship.

## You MAY
- read any file in YOUR OWN working directory
- search and read official OpenAI documentation (developers.openai.com, platform.openai.com)
- read the target application's own README, docs, tests, and GitHub issues
- reason, edit source, and change configuration
- run the application's existing tests
- add migration-specific tests ONLY where an existing contract justifies them
- make live OpenAI API calls, within the budget stated in your brief

## You MUST NOT
- change, weaken, delete, skip, or xfail any existing test to make things pass
- relax the frozen acceptance criteria after seeing a failure
- look at the other arm's directory, patch, notes, logs, or results
- read any Upshift run record, report, reports/, runs/, or CLAUDE.md session log that
  predates this experiment (these contain prior Astra findings and would contaminate you)
- use knowledge from any other worker

If you believe a frozen test is WRONG, do not change it. Record the argument in your
RESULT.md under "disputed criteria" and leave the test alone.

## Honesty requirements
- Report failures, dead ends, and wasted time. They are data, not embarrassment.
- Never claim a tool found something you found yourself, or vice versa.
- If you run out of time or budget mid-task, say exactly where you stopped.
- Distinguish "I verified this by running it" from "I inferred this from docs."

## Timekeeping (you are being measured)
Record a timestamp when you start and when you finish. Setup time counts. Installation
time counts. Reading time counts. Do not exclude anything because it feels unfair.

## Required artifacts (write these before you finish)
1. `RESULT.md` — narrative: what you found, what you changed, why, what you verified,
   what you could NOT verify, unresolved concerns, disputed criteria.
2. `final.patch` — `git diff` against the pinned starting SHA, applying cleanly to it.
3. `metrics.json` — the measurement schema given in your brief, filled honestly.
