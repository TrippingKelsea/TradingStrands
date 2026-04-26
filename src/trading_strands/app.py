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
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import structlog

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

logger = structlog.get_logger()

# Sentinel org_id used by local-dev (single-strategy-from-file) mode.
# The coordinator treats it like any other org_id, but the broker factory
# fans it to the legacy global credentials. Not meaningful in AWS mode.
LOCAL_DEV_ORG_ID = "local-dev"


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

    bot = StrategyBot(
        bot_id=bot_id,
        org_id=org_id,
        strategy_prompt=strategy_prompt,
        symbols=symbols,
        token_store=token_store,
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
    table_name = os.environ.get("DYNAMODB_TABLE")
    if table_name:
        publisher = StatePublisher(table_name)
        import boto3 as _boto3

        ddb = _boto3.resource("dynamodb")
        tbl = ddb.Table(table_name)
        marketdata_store = MarketDataStore(tbl)
        token_store = TokenUsageStore(tbl)
        ledger_store = LedgerStore(tbl)
        await logger.ainfo("publisher.enabled", table=table_name)
        await logger.ainfo("marketdata_store.enabled", table=table_name)
        await logger.ainfo("token_store.enabled", table=table_name)
        await logger.ainfo("ledger_store.enabled", table=table_name)

    risk_manager = RiskManager(RiskConfig())
    coordinator = TradeCoordinator(
        broker_factory=broker_factory,
        risk_manager=risk_manager,
        ledgers={},
        default_broker=market_broker,
        ledger_store=ledger_store,
    )

    market_data = MarketDataProvider(market_broker)

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

    if strategy_path:
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
                """

                if publisher is None:
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
