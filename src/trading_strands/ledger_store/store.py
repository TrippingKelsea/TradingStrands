"""DynamoDB persistence for per-bot Ledger state."""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Attr

from trading_strands.ddb import scan_all
from trading_strands.ledger.models import Fill, Ledger

DEFAULT_EVENT_RETENTION_DAYS = 90


def _decimal_to_json(obj: Any) -> Any:
    """Custom encoder for Decimal; pydantic model_dump returns Decimals
    in place, and DynamoDB can handle Decimal directly — but we serialize
    the snapshot as a JSON string to keep the DDB item flat and avoid
    nested-attribute size limits as positions accumulate."""

    if isinstance(obj, Decimal):
        return str(obj)
    msg = f"Object of type {type(obj)} is not JSON-serializable"
    raise TypeError(msg)


class LedgerStore:
    """Persist + reload per-bot ledgers.

    Stateless. Inject a boto3 Table handle. Every write is a single
    DDB call; reads are one GetItem.
    """

    def __init__(
        self, table: Any,
        event_retention_days: int = DEFAULT_EVENT_RETENTION_DAYS,
    ) -> None:
        self._table = table
        self._event_retention_days = event_retention_days

    # ── Snapshot ──────────────────────────────────────────────────────

    def save_snapshot(self, bot_id: str, ledger: Ledger) -> None:
        """Overwrite the current snapshot for this bot.

        Pydantic's model_dump_json handles Decimal + nested models cleanly;
        we store the JSON string as one attribute rather than unpacking
        the structure into DDB's type system. Keeps schema evolution cheap
        (ledger model changes don't require DDB migrations) and avoids
        DynamoDB's 400KB item limit for bots with long order history —
        at 400KB of JSON you'd have to worry about compaction, not DDB.
        """

        payload = ledger.model_dump_json()
        self._table.put_item(Item={
            "pk": f"LEDGER#{bot_id}",
            "bot_id": bot_id,
            "updated_at": int(time.time()),
            "ledger_json": payload,
        })

    def load_snapshot(self, bot_id: str) -> Ledger | None:
        """Return the most recent snapshot for this bot, or None if no
        ledger has ever been persisted (first-ever run)."""

        resp = self._table.get_item(Key={"pk": f"LEDGER#{bot_id}"})
        item = resp.get("Item")
        if item is None:
            return None
        payload = item.get("ledger_json")
        if not payload:
            return None
        return Ledger.model_validate(json.loads(payload))

    # ── Event log (append-only) ──────────────────────────────────────

    def append_event(
        self, bot_id: str, fill: Fill, event_type: str = "fill",
    ) -> None:
        """Append a single fill event to the audit log.

        Each event is a standalone DDB item with a 90-day TTL. The snapshot
        is the fast read; this log is the authoritative audit trail. A full
        reconcile could replay this log from zero, but in practice we trust
        the snapshot and use events for forensics + the Auditor Agent.
        """

        ts = int(time.time())
        event_id = f"{ts}-{uuid.uuid4().hex[:6]}"
        ttl = ts + self._event_retention_days * 86400

        self._table.put_item(Item={
            "pk": f"LEDGER_EVENT#{bot_id}#{event_id}",
            "bot_id": bot_id,
            "event_type": event_type,
            "ts": ts,
            "ttl": ttl,
            "fill_json": fill.model_dump_json(),
        })

    def events_for_bot(
        self, bot_id: str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return the most recent events for a bot (newest first).

        Uses scan with a prefix filter. At event-log sizes we expect
        (100s per bot per day, 90d TTL → 10Ks per bot), scan is
        acceptable. If this becomes a hotspot we'd add a GSI on bot_id,
        but premature for current scale."""

        raw = scan_all(
            self._table,
            Attr("pk").begins_with(f"LEDGER_EVENT#{bot_id}#"),
        )
        items = [dict(i) for i in raw]
        # Sort by (ts, pk) so ties within the same second are deterministic.
        items.sort(
            key=lambda x: (int(x.get("ts", 0)), str(x.get("pk", ""))),
            reverse=True,
        )
        return items[:limit]

    # ── Combined convenience ─────────────────────────────────────────

    def record_and_persist(
        self, bot_id: str, ledger: Ledger, fill: Fill,
    ) -> None:
        """Apply a fill to the in-memory ledger AND persist both the event
        and the updated snapshot.

        Order matters for recovery semantics:
          1. ledger.record_fill(fill) — updates in-memory state
          2. append_event(fill) — if a crash happens here, the event is
             logged; next boot reads snapshot (missing this fill) AND
             sees the event (auditor can reconcile).
          3. save_snapshot() — finalizes the happy path.

        A crash between step 2 and 3 is recoverable from the event log.
        A crash between step 1 and 2 is the worst case: the fill happened
        at the broker but we have no durable record. The broker itself
        still has the fill; the Auditor Agent's cross-check catches this
        and re-ingests the missing fill into the snapshot.
        """

        ledger.record_fill(fill)
        try:
            self.append_event(bot_id, fill)
        finally:
            self.save_snapshot(bot_id, ledger)
