"""Base types for the tools framework.

Deliberately small. A tool factory takes a ToolContext and returns a
Strands-compatible callable (typically a `@tool`-decorated function).
The registry maps tool names to factories; `bind_tools_for_strategy`
walks a strategy's `StrategyToolConfig` dict, asks the registry for
each enabled factory, and produces the list a Strands Agent expects.

Keeping the registry shape flat (name → factory) rather than a
class hierarchy means adding a new tool is: write the factory,
register it, done. Subpackages under `trading_strands.tools.<name>`
own their own factory and registration happens at import time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class StrategyToolConfig(BaseModel):
    """Per-strategy configuration for one tool.

    Defaults are off — a strategy without explicit config never gets
    the tool. Matches the "org gate, strategy opts in" invariant
    from SPEC/tools.md §5.4.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    # Hard stop at this many external calls per UTC day. 0 = the tool
    # is effectively disabled; ToolQuotaStore.reserve raises on 0.
    daily_quota: int = 0


@dataclass
class ToolContext:
    """Runtime context passed to every tool factory.

    Holds the handles a tool needs at call time: strategy/org identity
    (for quota + credential lookup), the quota store, a Secrets
    Manager client, and a DDB table handle (for cached reads).

    Kept loose — `Any` on client handles — so tests can pass None when
    the tool being tested doesn't touch that dependency.
    """

    strategy_id: str
    org_id: str
    quota_store: Any
    secrets_client: Any
    table: Any


# A tool factory is a Callable[[ToolContext], Callable]. The returned
# callable is what Strands calls; typically it's the output of @tool.
# We keep the return type as `Any` to avoid importing Strands here —
# the framework is unit-testable without Bedrock.
ToolFactory = Callable[[ToolContext], Any]


class ToolRegistry:
    """Maps tool name → factory. Tool subpackages register themselves
    at import time (or at app start via an explicit register_all())."""

    def __init__(self) -> None:
        self._factories: dict[str, ToolFactory] = {}

    def register(self, name: str, factory: ToolFactory) -> None:
        """Register a tool. Duplicate registration is a programming
        error — fail loudly so the second registrant sees the
        conflict immediately rather than silently winning or losing."""

        if name in self._factories:
            msg = f"tool {name!r} already registered"
            raise ValueError(msg)
        self._factories[name] = factory

    def factory_for(self, name: str) -> ToolFactory:
        return self._factories[name]

    def names(self) -> list[str]:
        """Names of registered tools, sorted for stable iteration."""

        return sorted(self._factories.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._factories


def bind_tools_for_strategy(
    registry: ToolRegistry,
    tools_config: dict[str, StrategyToolConfig],
    context: ToolContext,
) -> list[Any]:
    """Produce the list of bound tools a Strands Agent expects.

    Walks the strategy's tools_config. For each entry with
    enabled=True that the registry knows about, invokes the factory
    with the context and collects the result. Config entries for
    unknown tools are logged and skipped — a strategy that references
    a tool name we no longer ship shouldn't fail to start; it just
    runs without that tool until the strategy is updated.
    """

    bound: list[Any] = []
    for name, cfg in tools_config.items():
        if not cfg.enabled:
            continue
        if name not in registry:
            logger.warning(
                "tool %r enabled for strategy %s but not in registry; "
                "skipping",
                name, context.strategy_id,
            )
            continue
        factory = registry.factory_for(name)
        bound.append(factory(context))
    return bound
