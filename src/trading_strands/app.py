"""TradingStrands application — wires the full system together.

Usage:
    # Local dev with a strategy file:
    uv run python -m trading_strands.app --strategy examples/strategies/turtle-trading.md

    # AWS mode (reads strategies from DynamoDB):
    DYNAMODB_TABLE=trading-strands-state uv run python -m trading_strands.app

Broker credentials:
- In AWS mode, each strategy's trades execute through its owning org's
  Alpaca credentials (read from `trading-strands/org/{org_id}/alpaca`).
- If a per-org secret is missing or malformed, we fall back to the legacy
  global secret named by SECRETS_MANAGER_SECRET_NAME and log a warning.
- In local mode, the global secret/env vars apply to the single test org.
"""

from __future__ import annotations

import json
import os
import signal
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import structlog

from trading_strands.agent_memory.store import AgentMemoryStore
from trading_strands.alpaca_secrets.store import secret_name_for
from trading_strands.auditor.reconciler import AuditConfig, Reconciler
from trading_strands.broker.alpaca import AlpacaAdapter
from trading_strands.coordinator.coordinator import TradeCoordinator
from trading_strands.dashboard.publisher import StatePublisher
from trading_strands.ledger.models import Ledger
from trading_strands.ledger_store.store import LedgerStore
from trading_strands.marketdata.provider import MarketDataProvider
from trading_strands.marketdata_store.store import MarketDataStore
from trading_strands.orchestrator.engine import Orchestrator
from trading_strands.risk.manager import RiskConfig, RiskManager
from trading_strands.strategies.bot import StrategyBot
from trading_strands.token_telemetry.store import TokenUsageStore
from trading_strands.whatif.tracker import WhatIfTracker

# S3 bucket name for the shared agent-memory store (v0). In v1 each Agent
# gets a dedicated bucket managed by BotProvisioner.
AGENT_MEMORY_BUCKET_ENV = "AGENT_MEMORY_BUCKET"

logger = structlog.get_logger()

# Sentinel org_id used by local-dev (single-strategy-from-file) mode.
# The coordinator treats it like any other org_id, but the broker factory
# fans it to the legacy global credentials. Not meaningful in AWS mode.
LOCAL_DEV_ORG_ID = "local-dev"


# ── Single-bot mode ──────────────────────────────────────────────────
#
# Per-bot Fargate: the StrategySupervisor Lambda creates one ECS service
# per active strategy, passing STRATEGY_ID + ORG_ID as task env. This
# process then registers exactly one bot and never polls for others.
# The StrategySupervisor handles status changes by changing desiredCount
# or deleting the service — the process exits cleanly on SIGTERM.


@dataclass(frozen=True)
class SingleBotConfig:
    """Frozen view of the one strategy this process should run."""

    strategy_id: str
    org_id: str
    bot_id: str
    strategy_prompt: str
    symbols: list[str]
    capital: Decimal
    name: str
    # Optional with empty defaults so existing tests that construct
    # SingleBotConfig without these fields don't break.
    tools: dict[str, Any] = None  # type: ignore[assignment]
    skills: list[str] = None      # type: ignore[assignment]
    model_id: str = ""

    def __post_init__(self) -> None:
        # Frozen dataclass can't assign normally; object.__setattr__
        # is the standard workaround for defaulting mutables.
        if self.tools is None:
            object.__setattr__(self, "tools", {})
        if self.skills is None:
            object.__setattr__(self, "skills", [])


def single_bot_mode_enabled(env: dict[str, str]) -> bool:
    """STRATEGY_ID in env → single-bot mode."""

    return bool(env.get("STRATEGY_ID"))


def load_single_bot_config(table: Any, env: dict[str, str]) -> SingleBotConfig:
    """Read the single strategy this task should run from DDB.

    Fail-closed checks (all raise RuntimeError):
      - ORG_ID must be set alongside STRATEGY_ID
      - Strategy row must exist (StrategySupervisor only starts tasks for
        existing rows; missing = race with a delete)
      - The row's org_id MUST match ORG_ID (defense against a stale task
        starting up after the strategy was re-authored under a different
        org — never trade on a strategy you don't own)
      - Status must be 'active' (a pause/stop between start and boot
        means we shouldn't begin trading)
    """

    strategy_id = env.get("STRATEGY_ID", "")
    org_id = env.get("ORG_ID", "")
    if not strategy_id:
        msg = "STRATEGY_ID required for single-bot mode"
        raise RuntimeError(msg)
    if not org_id:
        msg = "ORG_ID required alongside STRATEGY_ID"
        raise RuntimeError(msg)

    resp = table.get_item(Key={"pk": f"STRATEGY#{strategy_id}"})
    item = resp.get("Item")
    if item is None:
        msg = f"strategy {strategy_id} not found"
        raise RuntimeError(msg)

    actual_org = str(item.get("org_id", ""))
    if actual_org != org_id:
        msg = (
            f"strategy {strategy_id} org_id mismatch: "
            f"task has ORG_ID={org_id!r}, strategy has {actual_org!r}"
        )
        raise RuntimeError(msg)

    status = str(item.get("status", ""))
    if status != "active":
        msg = f"strategy {strategy_id} not active (status={status!r})"
        raise RuntimeError(msg)

    return SingleBotConfig(
        strategy_id=strategy_id,
        org_id=org_id,
        bot_id=f"strategy-{strategy_id}",
        strategy_prompt=str(item.get("markdown", "")),
        symbols=list(item.get("symbols", []) or ["AAPL"]),
        capital=Decimal(str(item.get("capital", "1000"))),
        name=str(item.get("name", "")),
        tools=dict(item.get("tools", {}) or {}),
        skills=list(item.get("skills", []) or []),
        model_id=str(item.get("model_id", "") or ""),
    )


def _load_strategy(path: str) -> str:
    return Path(path).read_text()


def _load_global_env() -> dict[str, str]:
    """Load the legacy global Alpaca credentials.

    Kept as a fallback for (a) local dev with a strategy file, (b) orgs
    whose per-org secret is missing at runtime. In AWS multi-org mode
    this is not the primary source — per-org secrets are.
    """

    from dotenv import load_dotenv

    load_dotenv()

    secret_name = os.environ.get("SECRETS_MANAGER_SECRET_NAME")
    if secret_name:
        import boto3

        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=secret_name)
        secrets = json.loads(resp["SecretString"])
        return {
            "ALPACA_API_KEY": secrets.get("ALPACA_API_KEY", ""),
            "ALPACA_SECRET_KEY": secrets.get("ALPACA_SECRET_KEY", ""),
            "ALPACA_PAPER": secrets.get("ALPACA_PAPER", "true"),
        }

    return {
        "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "ALPACA_PAPER": os.environ.get("ALPACA_PAPER", "true"),
    }


def _make_broker_factory(
    global_env: dict[str, str],
    secrets_client: Any | None,
) -> Any:
    """Return a broker_factory(org_id) that reads per-org Alpaca creds.

    - In AWS mode (secrets_client provided), reads
      `trading-strands/org/{org_id}/alpaca`. On missing or malformed per-
      org secret, falls back to the global env creds and logs.
    - In local mode (secrets_client=None), every org gets the global creds.
    """

    sm: Any = secrets_client  # rebind to Any so narrowing doesn't fight mypy

    def _factory(org_id: str) -> AlpacaAdapter:
        if sm is not None:
            try:
                secret_name = secret_name_for(org_id)
                resp = sm.get_secret_value(SecretId=secret_name)
                payload = json.loads(resp.get("SecretString", "{}"))
                api_key = payload.get("ALPACA_API_KEY", "")
                secret_key = payload.get("ALPACA_SECRET_KEY", "")
                paper_str = str(payload.get("ALPACA_PAPER", "true")).lower()
                if api_key and secret_key:
                    logger.info(
                        "broker.per_org_creds", org_id=org_id, paper=paper_str,
                    )
                    return AlpacaAdapter(
                        api_key=api_key,
                        secret_key=secret_key,
                        paper=paper_str in ("true", "1", "yes"),
                    )
                logger.warning(
                    "broker.per_org_secret_incomplete org_id=%s falling_back_to_global",
                    org_id,
                )
            except sm.exceptions.ResourceNotFoundException:
                logger.warning(
                    "broker.per_org_secret_missing org_id=%s falling_back_to_global",
                    org_id,
                )
            except Exception:
                logger.exception(
                    "broker.per_org_secret_read_failed org_id=%s falling_back_to_global",
                    org_id,
                )

        # Fallback: global creds. Always used in local mode.
        if not global_env.get("ALPACA_API_KEY") or not global_env.get(
            "ALPACA_SECRET_KEY",
        ):
            msg = (
                f"no credentials available for org {org_id} "
                f"(per-org secret missing AND no global fallback configured)"
            )
            raise RuntimeError(msg)
        return AlpacaAdapter(
            api_key=global_env["ALPACA_API_KEY"],
            secret_key=global_env["ALPACA_SECRET_KEY"],
            paper=global_env["ALPACA_PAPER"].lower() == "true",
        )

    return _factory


def _register_strategy(
    orchestrator: Orchestrator,
    coordinator: TradeCoordinator,
    bot_id: str,
    org_id: str,
    strategy_prompt: str,
    symbols: list[str],
    capital: Decimal,
    token_store: TokenUsageStore | None = None,
    ledger_store: LedgerStore | None = None,
    s3_client: Any | None = None,
    memory_bucket: str | None = None,
    heartbeat_store: Any | None = None,
    calendar_store: Any | None = None,
    ta_store: Any | None = None,
    tools_config: dict[str, Any] | None = None,
    skills_config: list[str] | None = None,
    strategy_name: str = "",
    tools_table: Any | None = None,
    tools_secrets_client: Any | None = None,
    model_id: str = "",
    prompt_snapshot_store: Any | None = None,
) -> None:
    """Create a strategy bot and register it with the orchestrator.

    `org_id` is threaded through to the bot and into every TradeIntent
    it emits, so the coordinator can route trades to the right per-org
    broker. `token_store` records token usage per decision.

    If `ledger_store` is provided and a prior snapshot exists for this
    bot_id, the ledger resumes from that state — this is how the
    scale-down scheduler's nightly cycle preserves PnL and open
    positions across restarts. If no snapshot exists, a fresh ledger
    is created from `capital` (first-ever run for this strategy).
    """

    ledger: Ledger | None = None
    if ledger_store is not None:
        ledger = ledger_store.load_snapshot(bot_id)
        if ledger is not None:
            logger.info(
                "ledger.restored bot_id=%s realized_pnl=%s positions=%d",
                bot_id, ledger.realized_pnl, len(ledger.open_positions),
            )
    if ledger is None:
        ledger = Ledger(starting_capital=capital)
    coordinator.ledgers[bot_id] = ledger

    memory_store: AgentMemoryStore | None = None
    if s3_client is not None and memory_bucket:
        memory_store = AgentMemoryStore(
            s3_client=s3_client, bucket=memory_bucket,
            org_id=org_id, agent_type="strategy", agent_id=bot_id,
        )

    # Unified tool config surface: per-spec, all tools (injected or
    # call-based) are opt-in via the Strategy.tools dict. Derive the
    # per-kind booleans from that dict here so the bot's constructor
    # stays dumb. Absence → disabled.
    from trading_strands.tools.base import (
        StrategyToolConfig,
        ToolContext,
        bind_tools_for_strategy,
    )
    from trading_strands.tools.registry import build_default_registry

    tools_cfg_raw = tools_config or {}
    # Config entries might be dicts (from DDB) or StrategyToolConfig
    # (from tests). Normalize to the model.
    tool_config_models: dict[str, StrategyToolConfig] = {}
    for tname, raw in tools_cfg_raw.items():
        if isinstance(raw, StrategyToolConfig):
            tool_config_models[tname] = raw
        elif isinstance(raw, dict):
            tool_config_models[tname] = StrategyToolConfig(**raw)
    calendar_enabled = tool_config_models.get(
        "calendar", StrategyToolConfig(),
    ).enabled
    ta_enabled = tool_config_models.get(
        "ta", StrategyToolConfig(),
    ).enabled

    # Tool-call tools (news, filings, social) bound via registry.
    # Absence of tools_table or secrets means local-dev mode — skip
    # tool binding; bot gets no external-API tools.
    bound_tools: list[Any] = []
    if tools_table is not None and tools_secrets_client is not None:
        from trading_strands.tool_quota.store import ToolQuotaStore
        ctx = ToolContext(
            strategy_id=bot_id,
            org_id=org_id,
            quota_store=ToolQuotaStore(tools_table),
            secrets_client=tools_secrets_client,
            table=tools_table,
        )
        # Filter to tool-call kinds; the registry doesn't know about
        # calendar/ta (those are context-injected, not @tool).
        tool_call_cfg = {
            k: v for k, v in tool_config_models.items()
            if k in {"news", "filings", "social"}
        }
        registry = build_default_registry()
        raw_bindings = bind_tools_for_strategy(
            registry, tool_call_cfg, ctx,
        )
        # Some factories return a list of tools; flatten.
        for b in raw_bindings:
            if isinstance(b, list):
                bound_tools.extend(b)
            else:
                bound_tools.append(b)

    # Skills: resolve names to Skill objects. Missing ones skipped
    # with a warning per SPEC §4 — strategies aren't blocked from
    # running just because a skill was deleted.
    from trading_strands.skills_store.store import (
        SkillNotFoundError,
        SkillsStore,
    )

    loaded_skills: list[Any] = []
    if skills_config and tools_table is not None:
        skills_store = SkillsStore(tools_table)
        for name in skills_config:
            try:
                loaded_skills.append(skills_store.get(org_id, name))
            except SkillNotFoundError:
                logger.warning(
                    "strategy.skill_missing bot_id=%s skill=%s",
                    bot_id, name,
                )

    # Resolve model — empty string → platform default. Unknown id
    # that somehow survived save-time validation falls back to
    # default with a loud warning rather than crashing the bot.
    from trading_strands.models.registry import (
        UnknownModelError,
        resolve_model_id,
    )
    try:
        resolved_model = resolve_model_id(model_id)
    except UnknownModelError:
        from trading_strands.models.registry import DEFAULT_MODEL_ID
        logger.warning(
            "strategy.unknown_model_id_fallback bot_id=%s model_id=%s default=%s",
            bot_id, model_id, DEFAULT_MODEL_ID,
        )
        resolved_model = DEFAULT_MODEL_ID

    bot = StrategyBot(
        bot_id=bot_id,
        org_id=org_id,
        strategy_prompt=strategy_prompt,
        symbols=symbols,
        model=resolved_model,
        token_store=token_store,
        memory_store=memory_store,
        heartbeat_store=heartbeat_store,
        tools=bound_tools or None,
        calendar_store=calendar_store,
        calendar_enabled=calendar_enabled,
        ta_store=ta_store,
        ta_enabled=ta_enabled,
        skills=loaded_skills or None,
        strategy_name=strategy_name,
        prompt_snapshot_store=prompt_snapshot_store,
    )

    orchestrator.register_bot(
        bot_id=bot_id,
        symbols=symbols,
        callback=bot.decide,
        tta=bot.tta,
    )


async def run(
    strategy_path: str | None = None,
    capital: Decimal = Decimal("1000"),
    symbols: list[str] | None = None,
    tick_interval: float = 5.0,
) -> None:
    """Boot the full TradingStrands system and run."""

    global_env = _load_global_env()

    # Build the broker factory. In AWS mode, prefer per-org secrets;
    # the global secret is a fallback. In local mode, every org uses
    # the global env creds.
    secrets_client: Any | None = None
    if os.environ.get("DYNAMODB_TABLE"):
        import boto3

        secrets_client = boto3.client("secretsmanager")

    broker_factory = _make_broker_factory(global_env, secrets_client)

    # Market data uses a platform-level broker. We build it eagerly from
    # the global env because market data reads need to work even before
    # any per-org broker is instantiated (e.g., during pre-market warmup).
    # When the v1 market-data subscriber ships, this responsibility moves
    # to that service and the coordinator no longer needs a default broker.
    if not global_env["ALPACA_API_KEY"] or not global_env["ALPACA_SECRET_KEY"]:
        await logger.aerror(
            "missing global Alpaca creds; cannot fetch market data",
        )
        return
    market_broker = AlpacaAdapter(
        api_key=global_env["ALPACA_API_KEY"],
        secret_key=global_env["ALPACA_SECRET_KEY"],
        paper=global_env["ALPACA_PAPER"].lower() == "true",
    )

    # Build DDB-backed stores BEFORE the coordinator so the coordinator
    # can take the ledger_store as a constructor arg (fills are persisted
    # inside execute()). Order matters — changing it reintroduces the
    # "Tuesday's bot starts with empty ledger" bug.
    publisher: StatePublisher | None = None
    marketdata_store: MarketDataStore | None = None
    token_store: TokenUsageStore | None = None
    ledger_store: LedgerStore | None = None
    heartbeat_store: Any | None = None
    calendar_store: Any | None = None
    ta_store: Any | None = None
    tools_table: Any | None = None
    tools_secrets_client: Any | None = None
    s3_client: Any | None = None
    memory_bucket: str | None = None
    prompt_snapshot_store: Any | None = None
    table_name = os.environ.get("DYNAMODB_TABLE")
    if table_name:
        publisher = StatePublisher(table_name)
        import boto3 as _boto3

        ddb = _boto3.resource("dynamodb")
        tbl = ddb.Table(table_name)
        tools_table = tbl
        marketdata_store = MarketDataStore(tbl)
        token_store = TokenUsageStore(tbl)
        ledger_store = LedgerStore(tbl)
        from trading_strands.calendar_store.store import CalendarStore as _CS
        from trading_strands.heartbeat.store import HeartbeatStore as _HB
        from trading_strands.prompt_snapshots.store import (
            PromptSnapshotStore as _PS,
        )
        from trading_strands.ta_snapshot.store import TASnapshotStore as _TS
        heartbeat_store = _HB(tbl)
        calendar_store = _CS(tbl)
        ta_store = _TS(tbl)
        prompt_snapshot_store = _PS(tbl)
        tools_secrets_client = _boto3.client("secretsmanager")
        await logger.ainfo("publisher.enabled", table=table_name)
        await logger.ainfo("marketdata_store.enabled", table=table_name)
        await logger.ainfo("token_store.enabled", table=table_name)
        await logger.ainfo("ledger_store.enabled", table=table_name)
        await logger.ainfo("heartbeat_store.enabled", table=table_name)
        await logger.ainfo("calendar_store.enabled", table=table_name)
        await logger.ainfo("ta_store.enabled", table=table_name)

        memory_bucket = os.environ.get(AGENT_MEMORY_BUCKET_ENV)
        if memory_bucket:
            s3_client = _boto3.client("s3")
            await logger.ainfo(
                "agent_memory.enabled", bucket=memory_bucket,
            )
        else:
            await logger.ainfo(
                "agent_memory.disabled",
                reason=f"{AGENT_MEMORY_BUCKET_ENV} not set",
            )

    # HaltStore: per-org + system-wide halt state. Built from the same
    # DDB table; when absent (no table_name, i.e. local dev), the
    # coordinator falls back to its v0 in-memory RiskManager flag.
    halt_store: Any | None = None
    if table_name:
        import boto3 as _boto3_halt

        from trading_strands.halt.store import HaltStore as _HaltStore
        halt_store = _HaltStore(
            _boto3_halt.resource("dynamodb").Table(table_name),
        )

    risk_manager = RiskManager(RiskConfig())
    coordinator = TradeCoordinator(
        broker_factory=broker_factory,
        risk_manager=risk_manager,
        ledgers={},
        default_broker=market_broker,
        ledger_store=ledger_store,
        halt_store=halt_store,
    )

    # Market-data source: broker by default (v0 behavior). Set
    # MARKET_DATA_SOURCE=store to read from MarketDataStore instead,
    # with the broker as fallback on miss/staleness. Opt-in because
    # the swap is reversible only by restart; default-off lets us
    # ship the subscriber side without affecting the hot path.
    market_data_source = os.environ.get(
        "MARKET_DATA_SOURCE", "broker",
    ).lower()
    market_data: Any
    if (
        market_data_source == "store"
        and marketdata_store is not None
    ):
        from trading_strands.marketdata.store_provider import (
            StoreBackedMarketDataProvider,
        )
        market_data = StoreBackedMarketDataProvider(
            store=marketdata_store, fallback_broker=market_broker,
        )
        await logger.ainfo(
            "market_data.source store staleness_seconds=120",
        )
    else:
        market_data = MarketDataProvider(market_broker)
        if market_data_source == "store":
            # Caller asked for store-backed but no store wired (local
            # dev without DYNAMODB_TABLE). Silently falling back to
            # broker-only would be surprising; log so it's obvious.
            await logger.awarn(
                "market_data.source_requested_store_but_no_store "
                "falling_back_to_broker",
            )
        else:
            await logger.ainfo("market_data.source broker")

    # Auditor reconciler — checks ledger-broker consistency
    reconciler = Reconciler(AuditConfig())

    # What-if counterfactual tracker — records missed opportunities
    whatif_tracker = WhatIfTracker()

    orchestrator = Orchestrator(
        coordinator=coordinator,
        market_data=market_data,
        tick_interval=tick_interval,
        publisher=publisher,
        whatif_tracker=whatif_tracker,
        reconciler=reconciler,
        marketdata_store=marketdata_store,
    )

    single_bot = single_bot_mode_enabled(dict(os.environ))
    if single_bot:
        # Per-bot Fargate: one ECS task = one strategy. The multi-strategy
        # poll loop below is skipped; the StrategySupervisor handles
        # lifecycle (PAUSE/STOP → it sets desiredCount=0 and we get
        # SIGTERM).
        if table_name is None:
            msg = "single-bot mode requires DYNAMODB_TABLE"
            raise RuntimeError(msg)
        import boto3 as _boto3_single

        _table = _boto3_single.resource("dynamodb").Table(table_name)
        cfg = load_single_bot_config(_table, dict(os.environ))
        _register_strategy(
            orchestrator, coordinator,
            bot_id=cfg.bot_id,
            org_id=cfg.org_id,
            strategy_prompt=cfg.strategy_prompt,
            symbols=cfg.symbols,
            capital=cfg.capital,
            token_store=token_store,
            ledger_store=ledger_store,
            s3_client=s3_client,
            memory_bucket=memory_bucket,
            heartbeat_store=heartbeat_store,
            calendar_store=calendar_store,
            ta_store=ta_store,
            tools_table=tools_table,
            tools_secrets_client=tools_secrets_client,
            tools_config=cfg.tools,
            skills_config=cfg.skills,
            strategy_name=cfg.name,
            model_id=cfg.model_id,
            prompt_snapshot_store=prompt_snapshot_store,
        )
        await logger.ainfo(
            "system.start.single_bot",
            strategy_id=cfg.strategy_id,
            org_id=cfg.org_id,
            bot_id=cfg.bot_id,
            name=cfg.name,
            symbols=cfg.symbols,
            capital=str(cfg.capital),
        )
    elif strategy_path:
        # Local mode: single strategy from file
        strategy_prompt = _load_strategy(strategy_path)
        if symbols is None:
            symbols = ["AAPL"]
        _register_strategy(
            orchestrator, coordinator,
            bot_id="strategy-0",
            org_id=LOCAL_DEV_ORG_ID,
            strategy_prompt=strategy_prompt,
            symbols=symbols,
            capital=capital,
            token_store=token_store,
            ledger_store=ledger_store,
            s3_client=s3_client,
            memory_bucket=memory_bucket,
            heartbeat_store=heartbeat_store,
            calendar_store=calendar_store,
            ta_store=ta_store,
            tools_table=tools_table,
            tools_secrets_client=tools_secrets_client,
            tools_config={},
            skills_config=[],
            strategy_name=Path(strategy_path).stem,
            model_id="",
            prompt_snapshot_store=prompt_snapshot_store,
        )
        await logger.ainfo(
            "system.start.local",
            strategy=strategy_path,
            capital=str(capital),
            symbols=symbols,
        )
    elif publisher:
        # AWS mode: load strategies from DynamoDB
        strategies = publisher.get_strategies()
        active = [s for s in strategies if s.get("status") == "active"]
        if not active:
            await logger.awarn("system.no_strategies",
                               msg="No active strategies in DynamoDB. "
                               "Submit strategies via the dashboard.")
        for strat in active:
            sid = strat["strategy_id"]
            strat_org_id = strat.get("org_id")
            if not strat_org_id:
                await logger.awarn(
                    "strategy.skipped.no_org_id strategy_id=%s", sid,
                )
                continue
            bot_id = f"strategy-{sid}"
            strat_symbols = strat.get("symbols", ["AAPL"])
            strat_capital = Decimal(strat.get("capital", "1000"))
            _register_strategy(
                orchestrator, coordinator,
                bot_id=bot_id,
                org_id=strat_org_id,
                strategy_prompt=strat["markdown"],
                symbols=strat_symbols,
                capital=strat_capital,
                token_store=token_store,
                ledger_store=ledger_store,
                s3_client=s3_client,
                memory_bucket=memory_bucket,
                heartbeat_store=heartbeat_store,
                calendar_store=calendar_store,
                ta_store=ta_store,
                tools_table=tools_table,
                tools_secrets_client=tools_secrets_client,
                tools_config=strat.get("tools", {}),
                skills_config=strat.get("skills", []),
                strategy_name=str(strat.get("name", "")),
                model_id=str(strat.get("model_id", "") or ""),
                prompt_snapshot_store=prompt_snapshot_store,
            )
            await logger.ainfo(
                "system.strategy.loaded",
                strategy_id=sid,
                name=strat.get("name"),
                org_id=strat_org_id,
                symbols=strat_symbols,
                capital=str(strat_capital),
            )
        await logger.ainfo("system.start.aws", strategy_count=len(active))
    else:
        await logger.aerror(
            "no strategy specified. Use --strategy for local dev or "
            "set DYNAMODB_TABLE for AWS mode.",
        )
        return

    # Graceful shutdown on SIGTERM/SIGINT
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async with anyio.create_task_group() as tg:

            async def _watch_signals() -> None:
                async for _sig in signals:
                    await logger.ainfo("system.shutdown_signal")
                    orchestrator.stop()
                    tg.cancel_scope.cancel()
                    break

            async def _poll_strategies() -> None:
                """Periodically check DynamoDB for new/changed strategies.

                Loop is unconditional (no `while orchestrator._running` guard)
                so it starts polling whether or not orchestrator.run has been
                scheduled yet. The task group cancels us on shutdown; the
                body's try/except keeps transient DDB failures from killing
                the loop.

                Skipped entirely in single-bot mode — the StrategySupervisor
                owns lifecycle there, and polling for "other" strategies
                would be a policy violation in a per-bot task.
                """

                if publisher is None or single_bot:
                    return
                await anyio.sleep(1)  # small delay so orchestrator can log first
                while True:
                    try:
                        strategies = publisher.get_strategies()
                        active_ids = set()
                        for strat in strategies:
                            if strat.get("status") != "active":
                                continue
                            sid = strat["strategy_id"]
                            strat_org_id = strat.get("org_id")
                            if not strat_org_id:
                                # Pre-refactor strategies without org_id are skipped;
                                # bootstrap prunes them on next deploy.
                                continue
                            bot_id = f"strategy-{sid}"
                            active_ids.add(bot_id)
                            if bot_id not in orchestrator._bots:
                                strat_symbols = strat.get("symbols", ["AAPL"])
                                strat_capital = Decimal(
                                    strat.get("capital", "1000"),
                                )
                                _register_strategy(
                                    orchestrator, coordinator,
                                    bot_id=bot_id,
                                    org_id=strat_org_id,
                                    strategy_prompt=strat["markdown"],
                                    symbols=strat_symbols,
                                    capital=strat_capital,
                                    token_store=token_store,
                                    ledger_store=ledger_store,
                                    s3_client=s3_client,
                                    memory_bucket=memory_bucket,
                                    heartbeat_store=heartbeat_store,
                                    calendar_store=calendar_store,
                                    ta_store=ta_store,
                                    tools_table=tools_table,
                                    tools_secrets_client=tools_secrets_client,
                                    tools_config=strat.get("tools", {}),
                                    skills_config=strat.get("skills", []),
                                    strategy_name=str(strat.get("name", "")),
                                    model_id=str(strat.get("model_id", "") or ""),
                                    prompt_snapshot_store=prompt_snapshot_store,
                                )
                                await logger.ainfo(
                                    "system.strategy.hot_loaded",
                                    strategy_id=sid,
                                    name=strat.get("name"),
                                    org_id=strat_org_id,
                                )
                        # Unregister bots for strategies that are no longer active
                        for bot_id in list(orchestrator._bots):
                            if bot_id.startswith("strategy-") and bot_id not in active_ids:
                                orchestrator.unregister_bot(bot_id)
                                coordinator.ledgers.pop(bot_id, None)
                                await logger.ainfo(
                                    "system.strategy.unloaded",
                                    bot_id=bot_id,
                                )
                    except Exception:
                        await logger.aexception("strategy_poll.error")
                    await anyio.sleep(30)

            tg.start_soon(_watch_signals)
            tg.start_soon(_poll_strategies)
            tg.start_soon(orchestrator.run)


def main() -> None:
    """CLI entry point."""
    import argparse

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    parser = argparse.ArgumentParser(description="TradingStrands")
    parser.add_argument(
        "--strategy", required=False, default=None,
        help="Path to strategy markdown file (optional in AWS mode)",
    )
    parser.add_argument(
        "--capital", type=Decimal, default=Decimal("1000"),
        help="Starting capital (default: 1000)",
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Symbols to trade (default: extracted from strategy)",
    )
    parser.add_argument(
        "--tick-interval", type=float, default=5.0,
        help="Tick interval in seconds (default: 5.0)",
    )
    args = parser.parse_args()

    anyio.run(
        run,
        args.strategy,
        args.capital,
        args.symbols,
        args.tick_interval,
    )


if __name__ == "__main__":
    main()
