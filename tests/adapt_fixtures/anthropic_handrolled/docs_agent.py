"""A hand-rolled Anthropic Messages API agent — the shape `upshift adapt` must recognise.

Fixture only: this module is never imported or executed by the test suite. It exists so the
inventory/AST/extraction stages have a realistic single-file Anthropic agent to chew on.

The forced `tool_choice={"type": "tool", ...}` below is the exact pattern documented to 400
on claude-fable-5-1, which is why this agent is worth putting through upshift at all.
"""

import anthropic

SYSTEM_PROMPT = (
    "You are Docly, a documentation assistant for the Ledger handbook.\n"
    "Search the handbook before you answer, and never invent a section number.\n"
    "Answer in at most three sentences."
)

TOOLS = [
    {
        "name": "search_docs",
        "description": "Search the handbook and return matching sections.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for."}},
            "required": ["query"],
        },
    },
    {
        "name": "open_ticket",
        "description": "Open a support ticket when the handbook does not answer the question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One-line summary."},
                "detail": {"type": "string", "description": "Everything the user told us."},
            },
            "required": ["summary"],
        },
    },
]

SECTIONS = {
    "wifi": {"section_id": "S-3", "title": "Office wifi", "body": "The password is hunter2."},
    "expenses": {"section_id": "S-7", "title": "Expenses", "body": "File within 30 days."},
}


def search_docs(query):
    """Read matching handbook sections out of the in-memory index."""
    hits = [s for key, s in SECTIONS.items() if key in query.lower()]
    return {"results": hits}


def open_ticket(summary, detail=""):
    """Record a support ticket. Talks to the real ticketing system in production."""
    raise NotImplementedError("wired to the ticketing service at deploy time")


def run(user_message):
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-fable-5",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=TOOLS,
            tool_choice={"type": "tool", "name": "search_docs"},
            output_config={"effort": "low"},
        )
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            return "".join(block.text for block in response.content if block.type == "text")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in tool_uses:
            handler = {"search_docs": search_docs, "open_ticket": open_ticket}[block.name]
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(handler(**block.input)),
                }
            )
        messages.append({"role": "user", "content": results})
