"""Dashboard standalone entry point."""

from __future__ import annotations

from trading_strands.dashboard.api import app

__all__ = ["app"]


def main() -> None:
    """Run the dashboard service with uvicorn."""
    import uvicorn

    uvicorn.run(
        "trading_strands.dashboard.serve:app",
        host="0.0.0.0",  # noqa: S104
        port=8080,
        log_level="info",
    )


if __name__ == "__main__":
    main()
