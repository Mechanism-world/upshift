# notesbot

A note-taking assistant wired through litellm.

## Usage

```python
from notesbot.app import run

run("Remember that the office wifi password is hunter2.")
# -> "Saved that as a note."

run("What notes do I have?")
# -> "You have one note: office wifi."
```
