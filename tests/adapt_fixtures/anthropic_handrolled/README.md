# docly

A tiny handbook assistant built directly on the Anthropic Messages API.

## Usage

```python
from docs_agent import run

run("What is the office wifi password?")
# -> "Section S-3 says the password is hunter2."
```

The agent always calls `search_docs` before answering, because `tool_choice` forces it to.
