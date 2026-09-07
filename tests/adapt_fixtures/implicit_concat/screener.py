"""A fixture agent whose system prompt is written as Python implicit string concatenation.

Fixture only: never imported or executed. Adjacent literals inside the parentheses are
joined by the compiler with nothing at all, so the prompt this module sends has no newline
between "screener." and "Return" — that is the case `upshift adapt` must reproduce.
"""

from openai import OpenAI

SYSTEM_PROMPT = (
    "You are a screener. "
    "Return JSON with keys a, b."
)

FOOTER = "Never answer in prose."

client = OpenAI()


def run(question: str) -> str:
    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT + "\n" + FOOTER},
            {"role": "user", "content": question},
        ],
    )
    return reply.choices[0].message.content
