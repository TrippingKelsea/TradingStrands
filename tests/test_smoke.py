"""Smoke test — verifies the package is importable."""


def test_import() -> None:
    import trading_strands

    assert trading_strands is not None
