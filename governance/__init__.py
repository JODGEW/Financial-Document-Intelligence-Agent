"""Governance layer for the document agent.

Wraps the existing agent with runtime grounding validation, risk scoring, and a
per-answer governance report. The modules here read the same signals the eval
runner and audit log already produce; they do not change how the agent answers.
"""
