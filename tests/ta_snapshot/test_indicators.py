"""Tests for the pure-function TA indicators.

Indicator math should match reference implementations. Using
hand-computed values for small inputs so edge cases (not enough data,
flat series, monotonic series) are obvious.
"""

from __future__ import annotations

from decimal import Decimal

from trading_strands.ta_snapshot.indicators import (
    compute_bollinger,
    compute_ema,
    compute_macd,
    compute_rsi_wilder,
    compute_sma,
)

# ── SMA ──────────────────────────────────────────────────────────────


def test_sma_matches_mean_for_sufficient_data() -> None:
    closes = [Decimal(str(x)) for x in (10, 12, 14, 16, 18)]
    assert compute_sma(closes, 5) == Decimal("14")


def test_sma_uses_only_last_N() -> None:
    """Early closes beyond the window are ignored."""

    closes = [Decimal(str(x)) for x in (1000, 2000, 10, 12, 14, 16, 18)]
    assert compute_sma(closes, 5) == Decimal("14")


def test_sma_insufficient_data_returns_none() -> None:
    closes = [Decimal("10"), Decimal("12")]
    assert compute_sma(closes, 5) is None


def test_sma_flat_series() -> None:
    closes = [Decimal("100")] * 20
    assert compute_sma(closes, 20) == Decimal("100")


# ── EMA ──────────────────────────────────────────────────────────────


def test_ema_flat_series_equals_value() -> None:
    closes = [Decimal("100")] * 50
    result = compute_ema(closes, 12)
    assert result is not None
    assert abs(result - Decimal("100")) < Decimal("0.0001")


def test_ema_insufficient_data_returns_none() -> None:
    assert compute_ema([Decimal("10")] * 3, 12) is None


def test_ema_responds_to_recent_values_more_than_sma() -> None:
    """Step up at the end: EMA should be higher than SMA-equivalent
    because EMA weights recent values heavier."""

    closes = [Decimal("100")] * 25 + [Decimal("150")] * 5
    ema = compute_ema(closes, 12)
    sma = compute_sma(closes, 12)
    assert ema is not None and sma is not None
    assert ema > sma


# ── RSI (Wilder) ────────────────────────────────────────────────────


def test_rsi_flat_series_is_fifty() -> None:
    """Zero gain + zero loss → RSI convention divides zero over zero.
    Common handling: return 50 (neutral) to avoid div-by-zero noise."""

    closes = [Decimal("100")] * 20
    result = compute_rsi_wilder(closes, 14)
    assert result is not None
    # Implementation-specific: some return 50, some 100 (no losses).
    # We codify: flat = 50 (neutral). Asserted tightly.
    assert result == Decimal("50")


def test_rsi_monotonic_rising_approaches_one_hundred() -> None:
    closes = [Decimal(str(i)) for i in range(1, 30)]
    result = compute_rsi_wilder(closes, 14)
    assert result is not None
    # All gains, no losses → RSI 100.
    assert result == Decimal("100")


def test_rsi_monotonic_falling_approaches_zero() -> None:
    closes = [Decimal(str(i)) for i in range(30, 1, -1)]
    result = compute_rsi_wilder(closes, 14)
    assert result is not None
    assert result == Decimal("0")


def test_rsi_insufficient_data_returns_none() -> None:
    """RSI-14 needs 15 closes minimum (14 diffs). Fewer → None."""

    closes = [Decimal(str(i)) for i in range(1, 14)]
    assert compute_rsi_wilder(closes, 14) is None


# ── MACD ─────────────────────────────────────────────────────────────


def test_macd_flat_series_is_zero() -> None:
    """Flat closes → EMA-12 == EMA-26 → MACD == 0."""

    closes = [Decimal("100")] * 50
    result = compute_macd(closes)
    assert result is not None
    macd, _signal, hist = result
    # Not exactly zero due to recursive EMA init, but vanishing.
    assert abs(macd) < Decimal("0.01")
    assert abs(hist) < Decimal("0.01")


def test_macd_returns_triple_when_enough_data() -> None:
    """With 60+ closes MACD, signal, and hist are all resolvable."""

    import random
    random.seed(42)
    closes = [
        Decimal(str(100 + random.uniform(-5, 5))) for _ in range(60)
    ]
    result = compute_macd(closes)
    assert result is not None
    macd, signal, hist = result
    # Triple must be self-consistent: hist = macd - signal.
    assert abs((macd - signal) - hist) < Decimal("0.0001")


def test_macd_insufficient_data_returns_none() -> None:
    """MACD needs 26 closes for the slow EMA plus 9 more for signal."""

    closes = [Decimal("100")] * 20
    assert compute_macd(closes) is None


# ── Bollinger ────────────────────────────────────────────────────────


def test_bollinger_flat_series_collapses_bands() -> None:
    """Zero std → upper == middle == lower == price. Bands collapse."""

    closes = [Decimal("100")] * 25
    result = compute_bollinger(closes, 20, Decimal("2"))
    assert result is not None
    upper, middle, lower = result
    assert upper == middle == lower == Decimal("100")


def test_bollinger_produces_symmetric_bands() -> None:
    closes = [Decimal(str(x)) for x in (
        100, 101, 99, 102, 98, 103, 97, 104, 96, 105,
        100, 101, 99, 102, 98, 103, 97, 104, 96, 105,
    )]
    result = compute_bollinger(closes, 20, Decimal("2"))
    assert result is not None
    upper, middle, lower = result
    # Mid = mean; upper = mid + 2*stddev; lower = mid - 2*stddev; symmetric.
    assert abs((upper - middle) - (middle - lower)) < Decimal("0.0001")


def test_bollinger_insufficient_data_returns_none() -> None:
    closes = [Decimal("100")] * 5
    assert compute_bollinger(closes, 20, Decimal("2")) is None
