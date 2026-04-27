"""Shared DynamoDB helpers.

One place for utilities that every store needs so the bug class
"one-shot scan drops rows" can't sneak back in via a new store
copying the wrong pattern. See docs/SPEC/operational_notes.md §"TODO:
DDB scan pagination sweep" for the context on why this exists.
"""

from __future__ import annotations

from trading_strands.ddb.scan_all import scan_all

__all__ = ["scan_all"]
