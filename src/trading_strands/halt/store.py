"""Halt state storage.

System halt lives at pk=CONTROL (same row as v0 for back-compat).
Per-org halt lives at pk=CONTROL#{org_id}. An org is effectively
halted if either its row or the system row is halted.

Keeping the two separate means:
  - Sysadmin's emergency stop can't be cleared by an orgadmin.
  - An Auditor Agent's org-scoped halt doesn't block sibling orgs.
  - The Coordinator's hot-path check reads both rows per intent
    (or once per tick, cached by the orchestrator's halt poller).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

SYSTEM_HALT_PK = "CONTROL"
ORG_HALT_PK_PREFIX = "CONTROL#"


@dataclass
class HaltState:
    halted: bool
    reason: str | None
    updated_at: int


def _empty() -> HaltState:
    return HaltState(halted=False, reason=None, updated_at=0)


class HaltStore:
    """Read/write halt flags. Single table, two pk shapes."""

    def __init__(self, table: Any) -> None:
        self._table = table

    # ── Per-org ─────────────────────────────────────────────────────

    def set_org_halt(
        self, org_id: str, halted: bool, reason: str = "",
    ) -> None:
        self._table.put_item(Item={
            "pk": f"{ORG_HALT_PK_PREFIX}{org_id}",
            "org_id": org_id,
            "desk_halted": halted,
            "halt_reason": reason,
            "updated_at": int(time.time()),
        })

    def get_org_state(self, org_id: str) -> HaltState:
        resp = self._table.get_item(
            Key={"pk": f"{ORG_HALT_PK_PREFIX}{org_id}"},
        )
        item = resp.get("Item")
        if item is None:
            return _empty()
        return HaltState(
            halted=bool(item.get("desk_halted", False)),
            reason=str(item.get("halt_reason", "")) or None,
            updated_at=int(item.get("updated_at", 0)),
        )

    def is_org_halted(self, org_id: str) -> bool:
        return self.get_org_state(org_id).halted

    # ── System-wide ─────────────────────────────────────────────────

    def set_system_halt(
        self, halted: bool, reason: str = "",
    ) -> None:
        self._table.put_item(Item={
            "pk": SYSTEM_HALT_PK,
            "desk_halted": halted,
            "halt_reason": reason,
            "updated_at": int(time.time()),
        })

    def get_system_state(self) -> HaltState:
        resp = self._table.get_item(Key={"pk": SYSTEM_HALT_PK})
        item = resp.get("Item")
        if item is None:
            return _empty()
        return HaltState(
            halted=bool(item.get("desk_halted", False)),
            reason=str(item.get("halt_reason", "")) or None,
            updated_at=int(item.get("updated_at", 0)),
        )

    def is_system_halted(self) -> bool:
        return self.get_system_state().halted

    # ── Effective view ──────────────────────────────────────────────

    def is_effective_halted(self, org_id: str) -> bool:
        """OR of system + per-org. This is what the Coordinator checks
        to decide whether to route a trade."""

        return self.is_system_halted() or self.is_org_halted(org_id)

    def get_effective_reason(self, org_id: str) -> str | None:
        """The reason most relevant to explaining the halt. System
        halts take precedence — an orgadmin who sees only their own
        reason when a sysadmin stop is in effect would be misled."""

        sys_state = self.get_system_state()
        if sys_state.halted:
            return sys_state.reason
        org_state = self.get_org_state(org_id)
        if org_state.halted:
            return org_state.reason
        return None
