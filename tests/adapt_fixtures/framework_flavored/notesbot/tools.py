"""Tool schemas built from a dict, the way most small frameworks do it.

Fixture only: never imported by the test suite.
"""

TOOL_SPECS = {
    "save_note": {
        "description": "Save one note for the user.",
        "properties": {
            "title": {"type": "string", "description": "Short title for the note."},
            "body": {"type": "string", "description": "The note text."},
        },
        "required": ["title"],
    },
    "list_notes": {
        "description": "List every note the user has saved.",
        "properties": {},
        "required": [],
    },
}


def build_tools():
    """TOOL_SPECS -> OpenAI function tool schemas."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": {
                    "type": "object",
                    "properties": spec["properties"],
                    "required": spec["required"],
                },
            },
        }
        for name, spec in TOOL_SPECS.items()
    ]
