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
import uuid
from dataclasses import dataclass
from typing import Any

from trading_strands.emf.emitter import emit_metric

SYSTEM_HALT_PK = "CONTROL"
ORG_HALT_PK_PREFIX = "CONTROL#"
HALT_EVENT_PK_PREFIX = "HALT_EVENT#"

# Audit rows self-expire after 90 days — same retention as other
# event logs in this codebase (LEDGER_EVENT#, TOKENEVENT#, etc.).
# TTL attribute on the table does the cleanup; operators pulling
# longer history point at S3-exported logs instead.
_HALT_EVENT_TTL_SECONDS = 90 * 24 * 3600


@dataclass
class HaltState:
    halted: bool
    reason: str | None
    updated_at: int


@dataclass
class HaltEvent:
    """One row from the halt audit log — what changed, when, why."""

    ts: int
    scope: str  # "system" | "org"
    halted: bool
    reason: str | None
    org_id: str | None = None


def _empty() -> HaltState:
    return HaltState(halted=False, reason=None, updated_at=0)


def _emit_transition(
    *, scope: str, halted: bool, org_id: str | None = None,
    reason: str = "",
) -> None:
    """Emit the EMF halt-transition metric. Kept as a module-level
    helper so tests can read from stdout via capsys and any new halt
    writers in the future get the emission for free by using HaltStore.

    `halted` rides as a dimension (so CW alarms can key off it); the
    scope dimension lets a single alarm fire on (scope=system, halted=true)
    specifically, which is the "sysadmin emergency" case we most want
    to alarm on immediately. Reason and org_id ride as extra fields
    (searchable in Logs Insights, not chargeable dimensions).
    """

    extra: dict[str, Any] = {}
    if org_id:
        extra["halt_org_id"] = org_id
    if reason:
        extra["halt_reason"] = reason
    emit_metric(
        "halt.transition.count",
        value=1,
        unit="Count",
        dimensions={
            "scope": scope,
            "halted": "true" if halted else "false",
        },
        extra=extra or None,
    )


class HaltStore:
    """Read/write halt flags. Single table, two pk shapes."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def _record_transition(
        self, *, scope: str, halted: bool,
        org_id: str | None, reason: str,
    ) -> None:
        """Fan out a real state change to: the EMF metric (drives CW
        alarms) and the audit log (drives dashboard history). Same
        guard upstream means the two stay consistent."""

        _emit_transition(
            scope=scope, halted=halted, org_id=org_id, reason=reason,
        )
        now = int(time.time())
        # Rand suffix breaks ties when two transitions land in the
        # same second — matches LEDGER_EVENT# conventions.
        pk = f"{HALT_EVENT_PK_PREFIX}{now}-{uuid.uuid4().hex[:8]}"
        item: dict[str, Any] = {
            "pk": pk,
            "ts": now,
            "scope": scope,
            "halted": halted,
            "reason": reason or "",
            "ttl": now + _HALT_EVENT_TTL_SECONDS,
        }
        if org_id:
            item["org_id"] = org_id
        self._table.put_item(Item=item)

    # ── Per-org ─────────────────────────────────────────────────────

    def set_org_halt(
        self, org_id: str, halted: bool, reason: str = "",
    ) -> None:
        """Set the org's halt flag. Records a transition (EMF metric +
        audit row) ONLY on a state change — re-writing the same state
        (defensive unhalts on startup, re-triggered auditor halts) is
        a no-op.
        """

        prior = self.get_org_state(org_id)
        self._table.put_item(Item={
            "pk": f"{ORG_HALT_PK_PREFIX}{org_id}",
            "org_id": org_id,
            "desk_halted": halted,
            "halt_reason": reason,
            "updated_at": int(time.time()),
        })
        if prior.halted != halted:
            self._record_transition(
                scope="org", halted=halted,
                org_id=org_id, reason=reason,
            )

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
        prior = self.get_system_state()
        self._table.put_item(Item={
            "pk": SYSTEM_HALT_PK,
            "desk_halted": halted,
            "halt_reason": reason,
            "updated_at": int(time.time()),
        })
        if prior.halted != halted:
            self._record_transition(
                scope="system", halted=halted,
                org_id=None, reason=reason,
            )

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

    # ── Audit log ───────────────────────────────────────────────────

    def list_events(self, limit: int = 50) -> list[HaltEvent]:
        """Return the most-recent halt/unhalt events, newest first.

        Uses a scan on the pk prefix. Expected sizes (a handful of
        halts per day over 90d retention) make this cheap; if halt
        frequency ever justifies it, a GSI on ts solves the cost.
        """

        from boto3.dynamodb.conditions import Attr

        from trading_strands.ddb import scan_all

        items = scan_all(
            self._table,
            Attr("pk").begins_with(HALT_EVENT_PK_PREFIX),
        )
        items.sort(key=lambda x: int(x.get("ts", 0)), reverse=True)
        return [
            HaltEvent(
                ts=int(i.get("ts", 0)),
                scope=str(i.get("scope", "")),
                halted=bool(i.get("halted", False)),
                reason=str(i.get("reason", "")) or None,
                org_id=(str(i["org_id"]) if i.get("org_id") else None),
            )
            for i in items[:limit]
        ]
