"""A hand-rolled OpenAI tool-calling agent, the shape `upshift adapt` is built to read.

Fixture only: this module is never imported or executed by the test suite. It exists so the
inventory/AST/extraction stages have a realistic single-file agent to chew on.
"""

from openai import OpenAI

SYSTEM_PROMPT = (
    "You are Orderly, a support assistant for an online store.\n"
    "Look orders up before you answer, and never invent an order id.\n"
    "Keep replies to two sentences."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": "Look up one order by its id.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "The order id."}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refund_order",
            "description": "Refund an order that has already shipped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "The order id."},
                    "reason": {"type": "string", "description": "Why the refund is issued."},
                },
                "required": ["order_id"],
            },
        },
    },
]

ORDERS = {
    "A-1001": {"order_id": "A-1001", "status": "shipped", "total": 42.0},
    "A-1002": {"order_id": "A-1002", "status": "processing", "total": 12.5},
}


def lookup_order(order_id):
    """Read one order out of the store."""
    order = ORDERS.get(order_id)
    return order or {"error": f"no order {order_id}"}


def refund_order(order_id, reason=""):
    """Mark an order refunded and record why."""
    order = ORDERS.get(order_id)
    if order is None:
        return {"error": f"no order {order_id}"}
    order["status"] = "refunded"
    order["refund_reason"] = reason
    return order


def run(user_message):
    client = OpenAI()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    while True:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            temperature=0.2,
        )
        message = response.choices[0].message
        if not message.tool_calls:
            return message.content
        messages.append(message)
        for call in message.tool_calls:
            handlers = {"lookup_order": lookup_order, "refund_order": refund_order}
            handler = handlers[call.function.name]
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": str(handler(**__import__("json").loads(call.function.arguments))),
                }
            )
