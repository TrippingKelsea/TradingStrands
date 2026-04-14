"""TradingStrands application — wires the full system together.

Usage:
    uv run python -m trading_strands.app --strategy examples/strategies/turtle-trading.md
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import anyio
import structlog

from trading_strands.broker.alpaca import AlpacaAdapter
from trading_strands.coordinator.coordinator import TradeCoordinator
from trading_strands.ledger.models import Ledger
from trading_strands.marketdata.provider import MarketDataProvider
from trading_strands.orchestrator.engine import Orchestrator
from trading_strands.risk.manager import RiskConfig, RiskManager
from trading_strands.strategies.bot import StrategyBot

logger = structlog.get_logger()


def _load_strategy(path: str) -> str:
    return Path(path).read_text()


def _load_env() -> dict[str, str]:
    """Load environment variables, with .env file support."""
    import os

    from dotenv import load_dotenv

    load_dotenv()
    return {
        "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "ALPACA_PAPER": os.environ.get("ALPACA_PAPER", "true"),
    }


async def run(
    strategy_path: str,
    capital: Decimal = Decimal("1000"),
    symbols: list[str] | None = None,
    tick_interval: float = 5.0,
) -> None:
    """Boot the full TradingStrands system and run."""
    env = _load_env()

    if not env["ALPACA_API_KEY"] or not env["ALPACA_SECRET_KEY"]:
        await logger.aerror("missing ALPACA_API_KEY or ALPACA_SECRET_KEY")
        return

    strategy_prompt = _load_strategy(strategy_path)
    if symbols is None:
        symbols = ["AAPL"]  # default, should be extracted from strategy

    # Wire up the system
    broker = AlpacaAdapter(
        api_key=env["ALPACA_API_KEY"],
        secret_key=env["ALPACA_SECRET_KEY"],
        paper=env["ALPACA_PAPER"].lower() == "true",
    )

    risk_manager = RiskManager(RiskConfig())
    bot_id = "strategy-0"
    ledger = Ledger(starting_capital=capital)

    coordinator = TradeCoordinator(
        broker=broker,
        risk_manager=risk_manager,
        ledgers={bot_id: ledger},
    )

    market_data = MarketDataProvider(broker)

    orchestrator = Orchestrator(
        coordinator=coordinator,
        market_data=market_data,
        tick_interval=tick_interval,
    )

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

    await logger.ainfo(
        "system.start",
        strategy=strategy_path,
        capital=str(capital),
        symbols=symbols,
        tick_interval=tick_interval,
    )

    await orchestrator.run()


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
        "--strategy", required=True, help="Path to strategy markdown file",
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
