"""Tests for the tools framework base types + registry.

No actual tool implementations yet — this tests the plumbing that
commits 2+ plug into. Registry, context, config, and the
bind_tools_for_strategy helper that wires selected tools into a
Strands Agent.
"""

from __future__ import annotations

from typing import Any

from trading_strands.tools.base import (
    StrategyToolConfig,
    ToolContext,
    ToolRegistry,
    bind_tools_for_strategy,
)

# ── StrategyToolConfig ──────────────────────────────────────────────


def test_tool_config_defaults_are_off() -> None:
    """A tool with no config is off by default — matches the 'strategy
    must explicitly opt in' invariant. enabled=False, daily_quota=0."""

    c = StrategyToolConfig()
    assert c.enabled is False
    assert c.daily_quota == 0


def test_tool_config_round_trip() -> None:
    c = StrategyToolConfig(enabled=True, daily_quota=25)
    assert c.model_dump() == {"enabled": True, "daily_quota": 25}


# ── ToolRegistry ────────────────────────────────────────────────────


def _stub_factory(name: str):
    """A factory matching the framework's expected signature. Returns
    a callable whose identity we can assert on."""

    def factory(ctx: ToolContext) -> Any:
        def bound(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"tool": name, "args": args, "kwargs": kwargs}
        bound.__name__ = name
        return bound

    return factory


def test_registry_register_and_lookup() -> None:
    reg = ToolRegistry()
    reg.register("news", _stub_factory("news"))
    reg.register("filings", _stub_factory("filings"))
    assert "news" in reg.names()
    assert "filings" in reg.names()


def test_registry_duplicate_registration_raises() -> None:
    """Registering the same tool name twice is a programming error —
    better to raise loudly than silently overwrite."""

    import pytest

    reg = ToolRegistry()
    reg.register("news", _stub_factory("news"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register("news", _stub_factory("news2"))


def test_registry_lookup_unknown_name_raises() -> None:
    import pytest

    reg = ToolRegistry()
    with pytest.raises(KeyError):
        reg.factory_for("news")


# ── bind_tools_for_strategy ─────────────────────────────────────────


def _ctx(strategy_id: str = "strat-1", org_id: str = "org-a") -> ToolContext:
    """Test-only ToolContext: no real store/secrets — just fields a
    tool might read."""

    return ToolContext(
        strategy_id=strategy_id, org_id=org_id,
        quota_store=None, secrets_client=None, table=None,
    )


def test_bind_returns_only_enabled_tools() -> None:
    """Strategy config with enabled=False → tool is not bound, even
    if registered."""

    reg = ToolRegistry()
    reg.register("news", _stub_factory("news"))
    reg.register("filings", _stub_factory("filings"))

    tools_config = {
        "news": StrategyToolConfig(enabled=True, daily_quota=5),
        "filings": StrategyToolConfig(enabled=False, daily_quota=10),
    }
    bound = bind_tools_for_strategy(reg, tools_config, _ctx())
    names = [t.__name__ for t in bound]
    assert names == ["news"]


def test_bind_skips_tools_not_in_registry() -> None:
    """Strategy references a tool the registry doesn't know about
    (e.g., a feature flag off, or the tool was removed). Skip with
    a warning — don't block the bot from starting."""

    reg = ToolRegistry()
    reg.register("news", _stub_factory("news"))

    tools_config = {
        "news": StrategyToolConfig(enabled=True, daily_quota=5),
        "phantom": StrategyToolConfig(enabled=True, daily_quota=5),
    }
    bound = bind_tools_for_strategy(reg, tools_config, _ctx())
    assert [t.__name__ for t in bound] == ["news"]


def test_bind_handles_empty_config() -> None:
    """No tools enabled — bind returns an empty list, not None, so
    callers can pass it directly to Agent(tools=...)."""

    reg = ToolRegistry()
    reg.register("news", _stub_factory("news"))
    bound = bind_tools_for_strategy(reg, {}, _ctx())
    assert bound == []


def test_bind_passes_context_to_factory() -> None:
    """Each factory receives the ToolContext — that's how it gets
    access to the strategy/org/secrets/quota store it needs at
    call-time."""

    captured: list[ToolContext] = []

    def factory(ctx: ToolContext) -> Any:
        captured.append(ctx)

        def bound() -> None: ...
        bound.__name__ = "news"
        return bound

    reg = ToolRegistry()
    reg.register("news", factory)

    ctx = _ctx(strategy_id="abc", org_id="o1")
    bind_tools_for_strategy(
        reg,
        {"news": StrategyToolConfig(enabled=True, daily_quota=5)},
        ctx,
    )
    assert len(captured) == 1
    assert captured[0].strategy_id == "abc"
    assert captured[0].org_id == "o1"
