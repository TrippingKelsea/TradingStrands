"""Default ToolRegistry populated with the shipped tools.

app.py calls build_default_registry() at bot-start to get a registry
with every currently-available tool wired in. Tests construct their
own minimal registries.

As new tools land (filings, social, ...), add their factory to the
list below. The tool-name → factory mapping is the only per-tool
registration touch point.
"""

from __future__ import annotations

from typing import Any

from trading_strands.tools.base import ToolContext, ToolFactory, ToolRegistry


def _make_news_factory() -> ToolFactory:
    """Lazy wrapper — imports trading_strands.tools.news only when
    the registry is built, so tests that only care about the base
    types don't pull in strands/alpaca at import time."""

    def factory(ctx: ToolContext) -> Any:
        from trading_strands.tools.news import make_news_tool
        # Tool factory reads the strategy's per-tool daily_quota via
        # the caller (bind_tools_for_strategy wires per-config).
        # For now the registry factory uses a default quota of 50;
        # the bot-binding layer will override.
        return make_news_tool(ctx, daily_quota=50)

    return factory


def build_default_registry() -> ToolRegistry:
    """Return a ToolRegistry pre-populated with the ship-time tools."""

    reg = ToolRegistry()
    reg.register("news", _make_news_factory())
    return reg
