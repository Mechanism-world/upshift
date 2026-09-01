"""Escalation helper, deliberately invisible to the static ranking.

Fixture only. This module carries no OpenAI/LiteLLM signal, no prompt-shaped constant and no
signal-bearing filename, so `inventory.take_inventory` scores it zero and it never reaches the
round-1 evidence bundle. The tool it exposes is described in a docstring, exactly like the
real repos that motivated extraction round 2: the only way in here is a pointer.
"""


def escalate_ticket(ticket_id, severity="normal"):
    """Escalate one support ticket to a human queue.

    Argument schema:
        {
          "type": "object",
          "properties": {
            "ticket_id": {"type": "string", "description": "The ticket id."},
            "severity": {"type": "string", "description": "normal or urgent."}
          },
          "required": ["ticket_id"]
        }
    """
    return {"ticket_id": ticket_id, "severity": severity, "queue": "human"}
