"""upshift backend for the policybench no-tools answer tool.

policybench's `submit_outputs` is a terminal answer contract, not an action: the harness
reads the tool call's arguments and stops (`policybench/eval_no_tools.py`, one request per
output for the grandfathered Claude roster). This backend therefore only records what the
model submitted, deterministically and in memory, so `cases/cases.json` can assert on it.

State:
  calls   -- number of submit_outputs calls in the episode
  outputs -- {variable: value exactly as submitted}
  dollars -- {variable: value rounded to the nearest whole unit}

`dollars` exists because upshift v0.3.1 has no numeric-tolerance check type. policybench
scores an answer correct when it is within $1 of the reference
(`paper/snapshot/20260501/us_reference_outputs.csv`); rounding to the nearest whole unit and
comparing for equality is the closest deterministic equivalent available here. For the
1/0 eligibility outputs used by this suite the two rules coincide exactly.
"""


class Backend:
    def __init__(self, initial_state):
        state = dict(initial_state or {})
        self.calls = int(state.get("calls", 0))
        self.outputs = dict(state.get("outputs", {}))
        self.dollars = dict(state.get("dollars", {}))

    def execute(self, name, arguments):
        if name != "submit_outputs":
            return {"error": f"unknown tool {name!r}"}
        if not isinstance(arguments, dict):
            return {"error": "arguments must be an object"}
        outputs = arguments.get("outputs")
        if not isinstance(outputs, dict):
            return {"error": "missing required object argument 'outputs'"}

        self.calls += 1
        recorded = []
        for variable, entry in sorted(outputs.items()):
            if isinstance(entry, dict):
                value = entry.get("value")
            else:
                value = entry
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return {"error": f"'{variable}' must carry a numeric 'value'"}
            self.outputs[variable] = value
            self.dollars[variable] = int(round(value))
            recorded.append(variable)
        if not recorded:
            return {"error": "'outputs' contained no variables"}
        return {"ok": True, "recorded": recorded}

    def state(self):
        return {
            "calls": self.calls,
            "outputs": dict(self.outputs),
            "dollars": dict(self.dollars),
        }


def create_backend(initial_state):
    return Backend(initial_state)
