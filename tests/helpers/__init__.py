"""Test-only helpers for process-level fault-injection validation.

Nothing in this package is imported by production code. These modules exist so
a test can launch a real operating-system process, stop it at an exact point
relative to a committed SQLite transaction, and then inspect the durable state
that survived.
"""
