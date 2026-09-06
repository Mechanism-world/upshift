"""A plain pydantic-ai agent: AnthropicModel + a structured output_type + one trivial tool.

Nothing here knows about upshift. It reads ANTHROPIC_BASE_URL through the Anthropic SDK, so
pointing it at `upshift capture` is one environment variable.
"""
import os

from anthropic import AsyncAnthropic
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider


class Weather(BaseModel):
    city: str
    summary: str
    celsius: int


# default_headers only because this key is identity-linked; base_url comes from the env.
client = AsyncAnthropic(
    default_headers={"anthropic-workspace-id": os.environ["ANTHROPIC_WORKSPACE_ID"]}
)
agent = Agent(
    AnthropicModel("claude-fable-5", provider=AnthropicProvider(anthropic_client=client)),
    output_type=Weather,
    instructions="Report the weather. Use the temperature_c tool for the temperature.",
)


@agent.tool_plain
def temperature_c(city: str) -> int:
    """Current temperature in Celsius for a city."""
    return {"Paris": 14, "Cairo": 33, "Oslo": 3}.get(city, 20)


for question in ("What is the weather in Paris?", "And in Cairo?", "How about Oslo?"):
    print(agent.run_sync(question).output)
