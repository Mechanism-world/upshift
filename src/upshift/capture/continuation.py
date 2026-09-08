"""What a capture-derived episode does when it runs off the end of the recording.

DESIGN.md §F. A capture is a record of a finite conversation: N assistant turns, each with the
params the framework sent on that turn. The replayed episode is not finite in the same way — a
different model can take more turns to reach the same place, and a repaired one usually does.
Turn N+1 has no recorded params, no recorded tool results, and no recorded anything. The
question is what upshift does there, and the wrong answer is the one that used to be implicit:
keep going with the last recorded turn's params and score the outcome as behaviour.

Two policies, `agent.json` `continuation`:

* **`fail`** (the default) — the episode ends with a `continuation_exhausted` api_error. That
  is a `runner_error`-class, NON-behavioural failure (`native.protocol`): the evidence ran out,
  the model did not regress. The verdict layer treats the case as INCONCLUSIVE rather than
  regressed, which is the whole point — a candidate model that needs one more turn than the
  recording had is a fact about the recording's depth, not a regression to repair.
* **`repeat_last`** — the last recorded turn's params are reused (which `params_for_turn`
  already does by construction) and the record says so: `continuation_used: true`. Never
  silently: a run that leaned on repetition must be readable as one.

Inert for every agent that was not built from a recording. The policy applies only when
`agent.json` carries `recorded_turns`, which only `adapt --from-capture` writes — a hand-written
agent has no "recording" to run off the end of, and its `max_turns` is already the budget.

Rescue evidence for why this is a real class and not a hypothetical: the Anthropic track closed
five cases (`FRAMEWORK_REASSESSMENT.md` §3 Group C — `A-007`, `A-011`, `A-023`, `A-061`,
`A-062`) whose defects live in exactly the region a capture cannot supply: `tool_use` ids with
no recorded `tool_result` (A-007), a trailing assistant prefill turn upshift's loop cannot end
on (A-011), a `tool_result` separated from its `tool_use` by parallel-batch ordering (A-023).
Every one of them is a conversation that continues past what the capture holds.
"""

from __future__ import annotations

from typing import Any

from upshift.native.protocol import CONTINUATION_EXHAUSTED, error_payload
from upshift.schemas import CONTINUATION_REPEAT_LAST, AgentConfig


def recorded_turns(config: AgentConfig) -> int | None:
    """How many assistant turns the recording behind this agent had, or None."""
    turns = getattr(config, "recorded_turns", None)
    return turns if isinstance(turns, int) and not isinstance(turns, bool) else None


def exhausted(config: AgentConfig, turn_index: int) -> bool:
    """Is the episode about to build a turn the recording never covered?"""
    limit = recorded_turns(config)
    return limit is not None and turn_index >= limit


def before_turn(config: AgentConfig, result: Any, turn_index: int) -> dict[str, Any] | None:
    """Called by the agent loop before it builds assistant turn `turn_index`.

    Returns the api_error the episode should end with, or None to continue. Marks
    `result.continuation_used` when the run is leaning on the repeat policy.

    All of the policy lives here so the loop's call site is three lines and no capture concept
    leaks into the endpoint translation code.
    """
    if not exhausted(config, turn_index):
        return None
    limit = recorded_turns(config)
    if getattr(config, "continuation", None) == CONTINUATION_REPEAT_LAST:
        result.continuation_used = True
        return None
    return error_payload(
        f"the episode needed assistant turn {turn_index + 1}, and the capture this agent was "
        f"built from recorded {limit}. `continuation` is \"fail\" (the default), so the "
        f"episode stops here rather than replaying the last recorded turn's parameters as if "
        f"they were evidence about turn {turn_index + 1}. Set "
        f'`"continuation": "repeat_last"` in agent.json to reuse them, or record a deeper '
        f"conversation.",
        error_type=CONTINUATION_EXHAUSTED,
    )
