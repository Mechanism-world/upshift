# notebook_agent

A fixture repo whose whole agent lives in a Jupyter notebook, `agent.ipynb`.

Modelled on the shape adapt met in the wild (anthropics/claude-cookbooks keeps its agents
in `.ipynb`): the prompt, the tool schemas and the `messages.create` call are all in code
cells, and the usage example a case can be built from is in a markdown cell.
