"""Alpaca broker adapter (§5.5, v0).

Implements the BrokerAdapter protocol against the Alpaca Trading API.
Primary development and testing target.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import anyio
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.enums import OrderStatus as AlpacaStatus
from alpaca.trading.enums import TimeInForce as AlpacaTIF
from alpaca.trading.models import Order, TradeAccount
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from trading_strands.broker.types import (
    AccountInfo,
    BrokerPosition,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from trading_strands.ledger.models import FeeBreakdown, Side

# Alpaca is commission-free for equities, but regulatory fees still apply.
# These are approximate rates — the auditor maintains its own copy.
_SEC_FEE_RATE = Decimal("0.00008")  # $8 per $1M (SEC Transaction Fee)
_TAF_RATE = Decimal("0.000166")  # $0.000166 per share (FINRA TAF), max $8.30
_FINRA_RATE = Decimal("0.0000278")  # $0.0000278 per share (FINRA fee)


def _estimate_regulatory_fees(
    side: Side, quantity: Decimal, price: Decimal,
) -> FeeBreakdown:
    """Estimate regulatory fees for an equity trade on Alpaca.

    Alpaca is commission-free. Regulatory fees (SEC, TAF, FINRA)
    apply to sells only per SEC/FINRA rules.
    """
    if side == Side.BUY:
        return FeeBreakdown()

    notional = quantity * price
    sec_fee = (notional * _SEC_FEE_RATE).quantize(Decimal("0.01"))
    taf_fee = min(
        (quantity * _TAF_RATE).quantize(Decimal("0.01")),
        Decimal("8.30"),
    )
    finra_fee = (quantity * _FINRA_RATE).quantize(Decimal("0.01"))
    return FeeBreakdown(
        sec_fee=sec_fee,
        taf_fee=taf_fee,
        finra_fee=finra_fee,
    )


def _map_tif(tif: TimeInForce) -> AlpacaTIF:
    return {
        TimeInForce.DAY: AlpacaTIF.DAY,
        TimeInForce.GTC: AlpacaTIF.GTC,
        TimeInForce.IOC: AlpacaTIF.IOC,
    }[tif]


def _map_status(status: AlpacaStatus) -> OrderStatus:
    mapping: dict[AlpacaStatus, OrderStatus] = {
        AlpacaStatus.NEW: OrderStatus.PENDING,
        AlpacaStatus.ACCEPTED: OrderStatus.PENDING,
        AlpacaStatus.PENDING_NEW: OrderStatus.PENDING,
        AlpacaStatus.FILLED: OrderStatus.FILLED,
        AlpacaStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
        AlpacaStatus.CANCELED: OrderStatus.CANCELLED,
        AlpacaStatus.EXPIRED: OrderStatus.CANCELLED,
        AlpacaStatus.REJECTED: OrderStatus.REJECTED,
        AlpacaStatus.SUSPENDED: OrderStatus.REJECTED,
    }
    return mapping.get(status, OrderStatus.PENDING)


class AlpacaAdapter:
    """Alpaca broker adapter implementing the BrokerAdapter protocol."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
    ) -> None:
        self._trading = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
        )
        self._data = StockHistoricalDataClient(
            api_key=api_key,
            secret_key=secret_key,
        )

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        """Submit an order to Alpaca and return the result."""
        alpaca_side = AlpacaSide.BUY if order.side == Side.BUY else AlpacaSide.SELL

        alpaca_request: MarketOrderRequest | LimitOrderRequest
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            alpaca_request = LimitOrderRequest(
                symbol=order.symbol,
                qty=float(order.quantity),
                side=alpaca_side,
                time_in_force=_map_tif(order.time_in_force),
                limit_price=float(order.limit_price),
            )
        else:
            alpaca_request = MarketOrderRequest(
                symbol=order.symbol,
                qty=float(order.quantity),
                side=alpaca_side,
                time_in_force=_map_tif(order.time_in_force),
            )

        raw_response = await anyio.to_thread.run_sync(
            lambda: self._trading.submit_order(alpaca_request),
        )
        response = cast(Order, raw_response)

        filled_qty = Decimal(str(response.filled_qty or 0))
        filled_price = Decimal(str(response.filled_avg_price or 0))
        fees = _estimate_regulatory_fees(order.side, filled_qty, filled_price)

        return OrderResult(
            order_id=str(response.id),
            status=_map_status(response.status),
            filled_quantity=filled_qty,
            filled_price=filled_price,
            fees=fees,
        )

    async def get_account(self) -> AccountInfo:
        """Get current Alpaca account state."""
        raw_acct = await anyio.to_thread.run_sync(self._trading.get_account)
        acct = cast(TradeAccount, raw_acct)
        return AccountInfo(
            cash=Decimal(str(acct.cash)),
            portfolio_value=Decimal(str(acct.portfolio_value)),
            buying_power=Decimal(str(acct.buying_power)),
        )

    async def get_positions(self) -> list[BrokerPosition]:
        """Get all open positions from Alpaca."""
        raw_positions = await anyio.to_thread.run_sync(
            self._trading.get_all_positions,
        )
        positions = cast(list[AlpacaPosition], raw_positions)
        return [
            BrokerPosition(
                symbol=str(pos.symbol),
                quantity=Decimal(str(pos.qty)),
                market_value=Decimal(str(pos.market_value)),
                current_price=Decimal(str(pos.current_price)),
            )
            for pos in positions
        ]

    async def get_quote(self, symbol: str) -> dict[str, object]:
        """Get a current quote from Alpaca market data."""
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = await anyio.to_thread.run_sync(
            lambda: self._data.get_stock_latest_quote(request),
        )
        quote = quotes[symbol]
        mid = (Decimal(str(quote.ask_price)) + Decimal(str(quote.bid_price))) / 2
        return {
            "price": mid,
            "bid": Decimal(str(quote.bid_price)),
            "ask": Decimal(str(quote.ask_price)),
            "bid_size": quote.bid_size,
            "ask_size": quote.ask_size,
        }

    def get_fee_schedule(self) -> FeeBreakdown:
        """Return Alpaca's fee schedule (commission-free, regulatory only)."""
        return FeeBreakdown(
            commission=Decimal("0"),
            sec_fee=_SEC_FEE_RATE,
            taf_fee=_TAF_RATE,
            finra_fee=_FINRA_RATE,
        )

    def estimate_fees(self, order: OrderRequest) -> FeeBreakdown:
        """Estimate fees for a hypothetical order."""
        price = order.limit_price or Decimal("0")
        return _estimate_regulatory_fees(order.side, order.quantity, price)
