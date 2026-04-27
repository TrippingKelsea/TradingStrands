"""Technical indicators.

Pure functions over `list[Decimal]` close series. Dependency-free —
a few rolling means and one recursive EMA, nothing that warrants
numpy. The value add here is the LLM's interpretation of the numbers,
not indicator precision; stdlib math is plenty.

Return None for any indicator that doesn't have enough data. Callers
(the snapshot computer, the formatter) surface that gracefully.
"""

from __future__ import annotations

from decimal import Decimal

_ZERO = Decimal("0")
_TWO = Decimal("2")
_HUNDRED = Decimal("100")


def compute_sma(closes: list[Decimal], period: int) -> Decimal | None:
    """Simple moving average of the last `period` closes."""

    if len(closes) < period or period <= 0:
        return None
    window = closes[-period:]
    return sum(window, _ZERO) / Decimal(period)


def compute_ema(closes: list[Decimal], period: int) -> Decimal | None:
    """Exponential moving average using the standard 2/(period+1) smoothing.

    Seeded with the SMA of the first `period` closes — standard practice
    to avoid the EMA being pinned to the first close forever.
    """

    if len(closes) < period or period <= 0:
        return None
    alpha = _TWO / (Decimal(period) + Decimal("1"))
    seed_window = closes[:period]
    ema = sum(seed_window, _ZERO) / Decimal(period)
    for close in closes[period:]:
        ema = alpha * close + (Decimal("1") - alpha) * ema
    return ema


def compute_rsi_wilder(
    closes: list[Decimal], period: int = 14,
) -> Decimal | None:
    """Wilder's RSI over the given period. Needs period+1 closes.

    Flat series convention: all zero diffs → return 50 (neutral).
    Removes a class of div-by-zero traps and matches most reference
    implementations.
    """

    if len(closes) < period + 1 or period <= 0:
        return None

    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    initial_diffs = diffs[:period]
    gains = [d if d > _ZERO else _ZERO for d in initial_diffs]
    losses = [-d if d < _ZERO else _ZERO for d in initial_diffs]
    avg_gain = sum(gains, _ZERO) / Decimal(period)
    avg_loss = sum(losses, _ZERO) / Decimal(period)

    # Wilder's smoothing for subsequent diffs.
    for d in diffs[period:]:
        gain = d if d > _ZERO else _ZERO
        loss = -d if d < _ZERO else _ZERO
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)

    if avg_loss == _ZERO and avg_gain == _ZERO:
        return Decimal("50")  # flat series, neutral
    if avg_loss == _ZERO:
        return _HUNDRED  # all gains → RSI 100
    if avg_gain == _ZERO:
        return _ZERO      # all losses → RSI 0
    rs = avg_gain / avg_loss
    return _HUNDRED - (_HUNDRED / (Decimal("1") + rs))


def compute_macd(
    closes: list[Decimal],
    fast: int = 12, slow: int = 26, signal_period: int = 9,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return (macd, signal, histogram) using standard 12/26/9.

    Needs `slow + signal_period` closes — enough to seed both EMAs
    and then build the signal-line EMA over the MACD series.
    Returns None if any of those constraints isn't met.
    """

    if len(closes) < slow + signal_period:
        return None

    # Build the MACD series by walking the closes and keeping both
    # EMAs in sync at each step. Saves recomputing EMA-12 / EMA-26
    # redundantly from scratch.
    alpha_fast = _TWO / (Decimal(fast) + Decimal("1"))
    alpha_slow = _TWO / (Decimal(slow) + Decimal("1"))
    seed_fast = sum(closes[:fast], _ZERO) / Decimal(fast)
    seed_slow = sum(closes[:slow], _ZERO) / Decimal(slow)

    ema_fast = seed_fast
    ema_slow = seed_slow
    macd_series: list[Decimal] = []
    # Walk from the slow-seed index onward. ema_fast is already past
    # its seed (needs catching up through the intervening closes).
    for close in closes[fast:slow]:
        ema_fast = alpha_fast * close + (Decimal("1") - alpha_fast) * ema_fast
    for close in closes[slow:]:
        ema_fast = alpha_fast * close + (Decimal("1") - alpha_fast) * ema_fast
        ema_slow = alpha_slow * close + (Decimal("1") - alpha_slow) * ema_slow
        macd_series.append(ema_fast - ema_slow)

    if len(macd_series) < signal_period:
        return None
    # Signal line is EMA of the MACD series.
    alpha_sig = _TWO / (Decimal(signal_period) + Decimal("1"))
    sig = sum(
        macd_series[:signal_period], _ZERO,
    ) / Decimal(signal_period)
    for m in macd_series[signal_period:]:
        sig = alpha_sig * m + (Decimal("1") - alpha_sig) * sig
    macd = macd_series[-1]
    return macd, sig, macd - sig


def compute_bollinger(
    closes: list[Decimal],
    period: int = 20,
    std_mult: Decimal = Decimal("2"),
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return (upper, middle, lower) Bollinger bands.

    middle = SMA of last `period` closes
    upper / lower = middle +/- std_mult * sample stddev of the window
    """

    if len(closes) < period or period <= 0:
        return None
    window = closes[-period:]
    n = Decimal(period)
    mean = sum(window, _ZERO) / n
    # Sample variance (N-1 denominator); matches most charting libs.
    if period == 1:
        var = _ZERO
    else:
        sq_diffs = sum(((c - mean) ** 2 for c in window), _ZERO)
        var = sq_diffs / Decimal(period - 1)
    # Decimal sqrt via float round-trip — precision is more than
    # sufficient for display indicators. Avoids pulling in mpmath or
    # writing a Newton iteration for what's a UI-facing number.
    stddev = Decimal(str(float(var) ** 0.5))
    return mean + std_mult * stddev, mean, mean - std_mult * stddev


def compute_snapshot(closes: list[Decimal]) -> dict[str, Decimal | None]:
    """Bundle all indicators into a single dict. Used by the scheduled
    computer Lambda; returns Nones for anything that doesn't have
    enough data yet."""

    macd_triple = compute_macd(closes)
    bb = compute_bollinger(closes)
    return {
        "rsi_14": compute_rsi_wilder(closes, 14),
        "macd": macd_triple[0] if macd_triple else None,
        "macd_signal": macd_triple[1] if macd_triple else None,
        "macd_hist": macd_triple[2] if macd_triple else None,
        "sma_20": compute_sma(closes, 20),
        "sma_50": compute_sma(closes, 50),
        "sma_200": compute_sma(closes, 200),
        "bb_upper": bb[0] if bb else None,
        "bb_middle": bb[1] if bb else None,
        "bb_lower": bb[2] if bb else None,
        "last_close": closes[-1] if closes else None,
    }
