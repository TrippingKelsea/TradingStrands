"""Tests for strategy compilation (§4).

Tests the deterministic parts of compilation: risk config conversion,
TTA construction, compiled strategy model validation. LLM compilation
is tested against live APIs.
"""

from decimal import Decimal

from trading_strands.ir.compiler import (
    CompiledStrategy,
    IRField,
    compiled_to_risk_config,
    compiled_to_tta,
)


def _make_compiled() -> CompiledStrategy:
    return CompiledStrategy(
        symbols=["AAPL", "MSFT"],
        starting_capital=Decimal("1000"),
        tta_entry={"field": "price.AAPL", "op": "gt", "value": 155},
        tta_exit={"field": "price.AAPL", "op": "lt", "value": 148},
        ir_fields=[
            IRField(
                name="price.AAPL",
                description="Current AAPL price",
                source="market_data",
            ),
            IRField(
                name="indicator.atr_20",
                description="20-day ATR",
                source="indicator",
            ),
        ],
        risk_config={
            "max_position_pct": "0.02",
            "max_drawdown_pct": "0.15",
            "daily_loss_cap_pct": "0.05",
        },
        strategy_summary="Turtle trading: buy on 20-day breakout, sell on 10-day breakdown",
    )


class TestCompiledStrategy:
    def test_model_validates(self) -> None:
        compiled = _make_compiled()
        assert compiled.symbols == ["AAPL", "MSFT"]
        assert compiled.starting_capital == Decimal("1000")
        assert len(compiled.ir_fields) == 2

    def test_risk_config_conversion(self) -> None:
        compiled = _make_compiled()
        config = compiled_to_risk_config(compiled)
        assert config.max_position_pct == Decimal("0.02")
        assert config.max_drawdown_pct == Decimal("0.15")
        assert config.daily_loss_cap_pct == Decimal("0.05")

    def test_risk_config_defaults(self) -> None:
        """Missing risk params should use defaults."""
        compiled = _make_compiled()
        compiled.risk_config = {}
        config = compiled_to_risk_config(compiled)
        assert config.max_position_pct == Decimal("0.20")
        assert config.max_drawdown_pct == Decimal("0.15")

    def test_tta_construction(self) -> None:
        compiled = _make_compiled()
        tta = compiled_to_tta(compiled)
        assert "or" in tta
        assert len(tta["or"]) == 2
        assert tta["or"][0] == {"field": "price.AAPL", "op": "gt", "value": 155}
        assert tta["or"][1] == {"field": "price.AAPL", "op": "lt", "value": 148}

    def test_ir_fields(self) -> None:
        compiled = _make_compiled()
        assert compiled.ir_fields[0].name == "price.AAPL"
        assert compiled.ir_fields[0].source == "market_data"
        assert compiled.ir_fields[1].name == "indicator.atr_20"
        assert compiled.ir_fields[1].source == "indicator"
