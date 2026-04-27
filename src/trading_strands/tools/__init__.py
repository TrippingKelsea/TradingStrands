"""Strategy-agent tools framework.

A tool is a Strands `@tool`-compatible callable that fetches data or
computes something the LLM asks for. See docs/SPEC/tools.md for the
policy model (per-org credentials, per-strategy opt-in + quota, org
gate, cache-first for rate-limited sources).

This package holds:
  - base types (StrategyToolConfig, ToolContext, ToolRegistry)
  - binding helper (bind_tools_for_strategy)
  - individual tools under subpackages, e.g. trading_strands.tools.news

The base types intentionally don't import the Strands Agent — unit
tests exercise tool wiring without pulling Bedrock into the test path.
"""

from trading_strands.tools.base import (
    StrategyToolConfig,
    ToolContext,
    ToolRegistry,
    bind_tools_for_strategy,
)

__all__ = [
    "StrategyToolConfig",
    "ToolContext",
    "ToolRegistry",
    "bind_tools_for_strategy",
]
