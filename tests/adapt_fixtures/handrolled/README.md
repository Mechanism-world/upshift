# orderly

A tiny support agent over the store's order table.

## Usage

```python
from support_agent import run

run("Where is order A-1001?")
# -> "Order A-1001 has shipped."
```

Refunds work the same way:

```python
run("Refund order A-1001, it arrived damaged.")
# -> "I refunded order A-1001."
```

The agent always calls `lookup_order` before answering a status question.
