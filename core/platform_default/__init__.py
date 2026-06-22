"""Shielva platform default skills.

Each sub-package is a named skill that can be loaded by any Shielva service.
Skills encapsulate external provider integrations with shared config, retry logic,
and conversation memory so callers never have to manage these concerns directly.

Available skills:
  - platform_default.claude  — Claude LLM with conversation memory + R2 plan cache guard
"""
