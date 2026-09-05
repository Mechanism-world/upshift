"""Regenerate `tests/capture_fixtures/cookbook_sms/` — the committed capture fixture.

Run it from the repo root::

    uv run python tests/capture_fixtures/make_cookbook_sms_capture.py

What it does, and why it is shaped this way: it starts `upshift capture --sim` and then drives
`agents/cookbook-sms` through it **with the real `anthropic` SDK**, pointed at the recorder with
`base_url`. So the fixture is a genuine wire capture — real SDK headers, a real tool loop,
`tool_result` blocks fed back the way a framework feeds them back — and it costs nothing and
touches no network, because the recorder answers from the bundled simulator.

The SDK here stands in for "some framework upshift cannot read". It is the honest minimum:
whatever builds the request, capture mode only ever sees the bytes.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from upshift.agent_loop import convert_tools_messages
from upshift.capture.server import run_capture
from upshift.runner import load_backend_factory
from upshift.schemas import AgentConfig, Case

AGENT_DIR = ROOT / "agents" / "cookbook-sms"
OUT_DIR = Path(__file__).resolve().parent / "cookbook_sms"
PORT = 8799
SIM_MODEL = "sim-fable-5"
#: The recorder never forwards in --sim mode, so no credential is ever used or transmitted.
PLACEHOLDER_KEY = "not-a-real-key"
#: Three conversations is enough to exercise grouping, the tool loop and the replay backend.
CASE_IDS = ("greeting_texts_back", "order_help_with_username", "email_on_file_requested")
MAX_TURNS = 4


def drive(client, config: AgentConfig, case: Case, backend) -> None:
    """One conversation, the way a framework runs one: call, execute tools, call again."""
    messages: list[dict] = [{"role": "user", "content": case.user_messages[0]}]
    tools = convert_tools_messages(config.tools)
    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=SIM_MODEL,
            max_tokens=config.params.get("max_tokens", 1024),
            system=config.system_prompt,
            messages=messages,
            tools=tools,
            tool_choice=config.params.get("tool_choice", {"type": "auto"}),
        )
        blocks = [b.model_dump(mode="json") for b in response.content]
        messages.append({"role": "assistant", "content": blocks})
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        if not calls:
            return
        results = []
        for call in calls:
            result = backend.execute(call["name"], call.get("input") or {})
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": results})


def main() -> int:
    from anthropic import Anthropic

    shutil.rmtree(OUT_DIR, ignore_errors=True)
    config = AgentConfig.load(AGENT_DIR)
    cases = {c.id: c for c in Case.load_all(AGENT_DIR / "cases" / "cases.json")}
    backend_factory = load_backend_factory(AGENT_DIR)

    stop = threading.Event()
    ready = threading.Event()
    written: dict[str, Path] = {}

    def serve() -> None:
        written["index"] = run_capture(
            OUT_DIR,
            listen=f"127.0.0.1:{PORT}",
            sim=True,
            sim_agent=AGENT_DIR,
            framework="anthropic-sdk-python",
            stop=stop,
            on_ready=lambda host, port: ready.set(),
            on_record=lambda c, i, s, e: print(f"  {c} req {i}: {s} {e}"),
        )

    thread = threading.Thread(target=serve)
    thread.start()
    if not ready.wait(10):
        raise SystemExit("capture server did not start")
    try:
        client = Anthropic(api_key=PLACEHOLDER_KEY, base_url=f"http://127.0.0.1:{PORT}",
                           max_retries=0)
        for case_id in CASE_IDS:
            case = cases[case_id]
            print(f"driving {case_id}")
            drive(client, config, case, backend_factory(case.initial_state))
    finally:
        stop.set()
        thread.join(15)

    index = json.loads(written["index"].read_text())
    print(f"\nwrote {OUT_DIR} — {index['requests']} request(s), "
          f"{index['conversations']} conversation(s), tools {index['tools']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
