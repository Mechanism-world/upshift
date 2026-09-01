"""Notebook: prompt assembled from a template file, model called through litellm.

Fixture only: never imported by the test suite.
"""

from pathlib import Path

import litellm
from notesbot.tools import build_tools

PRODUCT_NAME = "Ledger"
MODEL = "gpt-4.1"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "notes_role.txt"

NOTES = []


def system_prompt():
    """Render the role template with the product name."""
    return PROMPT_PATH.read_text().format(product=PRODUCT_NAME)


def save_note(title, body=""):
    note = {"note_id": f"N-{1000 + len(NOTES) + 1}", "title": title, "body": body}
    NOTES.append(note)
    return note


def list_notes():
    return {"results": list(NOTES)}


def step(messages):
    return litellm.completion(
        model=MODEL,
        messages=messages,
        tools=build_tools(),
        tool_choice="auto",
        temperature=0.0,
    )


def run(user_message):
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": user_message},
    ]
    for _ in range(8):
        response = step(messages)
        message = response["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            return message["content"]
        messages.append(message)
        for call in calls:
            handler = {"save_note": save_note, "list_notes": list_notes}[call["function"]["name"]]
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(handler())})
    return None
