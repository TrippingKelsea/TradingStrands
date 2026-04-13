# Turtle Trading — $1000 starter

Use the classic Turtle Trading methodology, starting with $1000 in managed capital.

## Rules

- **Entry (long):** Buy when the price breaks above the 20-day high.
- **Exit (long):** Sell when the price breaks below the 10-day low.
- **Position sizing:** Risk no more than 2% of current equity per position, sized by Average True Range (ATR) over the last 20 days.
- **Stop loss:** 2 × ATR below entry price.
- **Universe:** S&P 500 constituents.

## Risk tolerance

- Hard drawdown cap: 15% from high-water mark.
- Predictive halt: at 10% drawdown, stop opening new positions; existing positions ride to their stops.
- Daily loss cap: 5% of equity at session open.

## Notes

This is the canonical "simple prompt" example — every rule is mechanical. It should compile cleanly into TTA predicates with minimal ambiguity.
