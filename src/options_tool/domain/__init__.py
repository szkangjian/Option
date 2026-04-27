"""Pure domain logic: ROC, intents, cost basis, advisors.

Modules in this package must NOT import from ``options_tool.ibkr``,
``options_tool.db``, or any other I/O layer. They take primitives or
dataclasses in, return primitives or dataclasses out. This keeps them
trivially unit-testable and lets us iterate on scoring rules without
booting the database.
"""
