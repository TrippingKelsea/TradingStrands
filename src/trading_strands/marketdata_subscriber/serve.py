"""Market Data Subscriber entry point.

Run as:
    uv run python -m trading_strands.marketdata_subscriber.serve

Env:
    DYNAMODB_TABLE        — state table (required)
    ALPACA_API_KEY        — subscriber broker creds (required)
    ALPACA_SECRET_KEY     — subscriber broker creds (required)
    ALPACA_PAPER          — "true" (default) or "false"
    SUBSCRIBER_POLL_INTERVAL_SECONDS  — default 5
    SUBSCRIBER_SYMBOL_REFRESH_INTERVAL_SECONDS — default 60

Deliberately minimal: one broker, one loop, no signal-plumbing beyond
the default anyio cancellation on SIGTERM that Fargate sends.
"""

from __future__ import annotations

import json
import os
import signal
from typing import Any

import anyio
import structlog

from trading_strands.broker.alpaca import AlpacaAdapter
from trading_strands.marketdata_store.store import MarketDataStore
from trading_strands.marketdata_subscriber.loop import run_forever
from trading_strands.strategies_store.store import StrategyStore

logger = structlog.get_logger()


def _load_creds() -> dict[str, str]:
    """Resolve Alpaca credentials for the subscriber.

    In AWS mode, prefer a Secrets Manager secret (SECRETS_MANAGER_SECRET_NAME
    — the legacy global secret, written by the CDK stack with the
    superwoman org's paper creds). Falls back to env vars for local dev.
    """

    secret_name = os.environ.get("SECRETS_MANAGER_SECRET_NAME")
    if secret_name:
        import boto3

        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=secret_name)
        payload = json.loads(resp["SecretString"])
        return {
            "ALPACA_API_KEY": payload.get("ALPACA_API_KEY", ""),
            "ALPACA_SECRET_KEY": payload.get("ALPACA_SECRET_KEY", ""),
            "ALPACA_PAPER": str(payload.get("ALPACA_PAPER", "true")),
        }

    return {
        "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "ALPACA_PAPER": os.environ.get("ALPACA_PAPER", "true"),
    }


async def _main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    table_name = os.environ.get("DYNAMODB_TABLE")
    if not table_name:
        msg = "DYNAMODB_TABLE is required"
        raise RuntimeError(msg)

    creds = _load_creds()
    if not creds["ALPACA_API_KEY"] or not creds["ALPACA_SECRET_KEY"]:
        msg = "Subscriber needs Alpaca creds — set ALPACA_API_KEY/SECRET_KEY"
        raise RuntimeError(msg)

    import boto3

    ddb = boto3.resource("dynamodb")
    table: Any = ddb.Table(table_name)
    md_store = MarketDataStore(table)
    strategy_store = StrategyStore(table)
    broker = AlpacaAdapter(
        api_key=creds["ALPACA_API_KEY"],
        secret_key=creds["ALPACA_SECRET_KEY"],
        paper=creds["ALPACA_PAPER"].lower() == "true",
    )

    poll_interval = float(
        os.environ.get("SUBSCRIBER_POLL_INTERVAL_SECONDS", "5"),
    )
    refresh_interval = float(
        os.environ.get("SUBSCRIBER_SYMBOL_REFRESH_INTERVAL_SECONDS", "60"),
    )

    await logger.ainfo(
        "subscriber.start table=%s paper=%s poll=%.1fs refresh=%.1fs",
        table_name, creds["ALPACA_PAPER"], poll_interval, refresh_interval,
    )

    # Signal handling: SIGTERM from Fargate → flush then exit. The
    # run_forever loop is an infinite sleep/poll cycle; we cancel it
    # via the task group.
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async with anyio.create_task_group() as tg:

            async def _watch_signals() -> None:
                async for _sig in signals:
                    await logger.ainfo("subscriber.shutdown_signal")
                    tg.cancel_scope.cancel()
                    break

            async def _run_loop() -> None:
                await run_forever(
                    broker=broker,
                    store=md_store,
                    strategy_store=strategy_store,
                    poll_interval=poll_interval,
                    symbol_refresh_interval=refresh_interval,
                )

            tg.start_soon(_watch_signals)
            tg.start_soon(_run_loop)

    # One last flush before exit so the final minute's buffered ticks
    # hit DDB instead of vanishing with the container.
    try:
        md_store.flush()
    except Exception:
        await logger.aexception("subscriber.final_flush_failed")
    await logger.ainfo("subscriber.stopped")


def main() -> None:
    anyio.run(_main)


if __name__ == "__main__":
    main()
