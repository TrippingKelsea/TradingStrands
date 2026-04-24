"""TradingStrands application — wires the full system together.

Usage:
    # Local dev with a strategy file:
    uv run python -m trading_strands.app --strategy examples/strategies/turtle-trading.md

    # AWS mode (reads strategies from DynamoDB):
    DYNAMODB_TABLE=trading-strands-state uv run python -m trading_strands.app
"""

from __future__ import annotations

import json
import os
import signal
from decimal import Decimal
from pathlib import Path

import anyio
import structlog

from trading_strands.auditor.reconciler import AuditConfig, Reconciler
from trading_strands.broker.alpaca import AlpacaAdapter
from trading_strands.coordinator.coordinator import TradeCoordinator
from trading_strands.dashboard.publisher import StatePublisher
from trading_strands.ledger.models import Ledger
from trading_strands.marketdata.provider import MarketDataProvider
from trading_strands.orchestrator.engine import Orchestrator
from trading_strands.risk.manager import RiskConfig, RiskManager
from trading_strands.strategies.bot import StrategyBot
from trading_strands.whatif.tracker import WhatIfTracker

logger = structlog.get_logger()


def _load_strategy(path: str) -> str:
    return Path(path).read_text()


def _load_env() -> dict[str, str]:
    """Load environment variables, with .env file and Secrets Manager support."""
    from dotenv import load_dotenv

    load_dotenv()

    # If running in AWS with Secrets Manager, fetch creds from there
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

    # Fall back to env vars / .env for local dev
    return {
        "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "ALPACA_PAPER": os.environ.get("ALPACA_PAPER", "true"),
    }


def _register_strategy(
    orchestrator: Orchestrator,
    coordinator: TradeCoordinator,
    bot_id: str,
    strategy_prompt: str,
    symbols: list[str],
    capital: Decimal,
) -> None:
    """Create a strategy bot and register it with the orchestrator."""
    ledger = Ledger(starting_capital=capital)
    coordinator.ledgers[bot_id] = ledger

    bot = StrategyBot(
        bot_id=bot_id,
        strategy_prompt=strategy_prompt,
        symbols=symbols,
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
    env = _load_env()

    if not env["ALPACA_API_KEY"] or not env["ALPACA_SECRET_KEY"]:
        await logger.aerror("missing ALPACA_API_KEY or ALPACA_SECRET_KEY")
        return

    # Wire up the system
    broker = AlpacaAdapter(
        api_key=env["ALPACA_API_KEY"],
        secret_key=env["ALPACA_SECRET_KEY"],
        paper=env["ALPACA_PAPER"].lower() == "true",
    )

    risk_manager = RiskManager(RiskConfig())
    coordinator = TradeCoordinator(
        broker=broker,
        risk_manager=risk_manager,
        ledgers={},
    )

    market_data = MarketDataProvider(broker)

    # Optional DynamoDB publisher for dashboard
    publisher: StatePublisher | None = None
    table_name = os.environ.get("DYNAMODB_TABLE")
    if table_name:
        publisher = StatePublisher(table_name)
        await logger.ainfo("publisher.enabled", table=table_name)

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
    )

    if strategy_path:
        # Local mode: single strategy from file
        strategy_prompt = _load_strategy(strategy_path)
        if symbols is None:
            symbols = ["AAPL"]
        _register_strategy(
            orchestrator, coordinator,
            bot_id="strategy-0",
            strategy_prompt=strategy_prompt,
            symbols=symbols,
            capital=capital,
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
            bot_id = f"strategy-{sid}"
            strat_symbols = strat.get("symbols", ["AAPL"])
            strat_capital = Decimal(strat.get("capital", "1000"))
            _register_strategy(
                orchestrator, coordinator,
                bot_id=bot_id,
                strategy_prompt=strat["markdown"],
                symbols=strat_symbols,
                capital=strat_capital,
            )
            await logger.ainfo(
                "system.strategy.loaded",
                strategy_id=sid,
                name=strat.get("name"),
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
                """Periodically check DynamoDB for new/changed strategies."""
                if publisher is None:
                    return
                while orchestrator._running:
                    await anyio.sleep(30)
                    try:
                        strategies = publisher.get_strategies()
                        active_ids = set()
                        for strat in strategies:
                            if strat.get("status") != "active":
                                continue
                            sid = strat["strategy_id"]
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
                                    strategy_prompt=strat["markdown"],
                                    symbols=strat_symbols,
                                    capital=strat_capital,
                                )
                                await logger.ainfo(
                                    "system.strategy.hot_loaded",
                                    strategy_id=sid,
                                    name=strat.get("name"),
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
