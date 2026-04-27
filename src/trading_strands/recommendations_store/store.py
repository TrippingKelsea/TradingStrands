"""DDB store for cross-agent recommendations (Org Advisories).

Schema:
    pk = RECOMMENDATION#{org_id}#{unix_ts}#{agent_type}

Fields:
    org_id, created_at, agent_type, agent_id,
    severity (info|warn|critical), summary, body, ttl

Reads are by org prefix — list_for_org filters on pk
begins_with("RECOMMENDATION#{org_id}#") so cross-org data is
physically unreachable from a single call.

TTL keeps the table bounded. Advisories older than 90 days self-
prune; the agent's own recommendations.md retains history for
review/audit beyond that window.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from boto3.dynamodb.conditions import Attr
from pydantic import BaseModel, ConfigDict

from trading_strands.ddb import scan_all

_TTL_SECONDS = 90 * 24 * 3600


class RecommendationSeverity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class RecommendationEntry(BaseModel):
    """A single cross-agent advisory for an org."""

    model_config = ConfigDict(extra="ignore")

    org_id: str
    created_at: int
    agent_type: str
    agent_id: str
    severity: RecommendationSeverity
    summary: str
    body: str = ""


def _pk(org_id: str, created_at: int, agent_type: str) -> str:
    return f"RECOMMENDATION#{org_id}#{created_at}#{agent_type}"


def _prefix(org_id: str) -> str:
    return f"RECOMMENDATION#{org_id}#"


class RecommendationsStore:
    """Writer + reader for RECOMMENDATION#* rows."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def append(
        self,
        *,
        org_id: str,
        agent_type: str,
        agent_id: str,
        severity: RecommendationSeverity | str,
        summary: str,
        body: str = "",
        created_at: int | None = None,
    ) -> RecommendationEntry:
        """Persist one advisory. `created_at` defaults to now; callers
        can pin it in tests to avoid sleeping between writes."""

        ts = int(created_at) if created_at is not None else int(time.time())
        sev = RecommendationSeverity(severity)
        entry = RecommendationEntry(
            org_id=org_id,
            created_at=ts,
            agent_type=agent_type,
            agent_id=agent_id,
            severity=sev,
            summary=summary,
            body=body,
        )
        self._table.put_item(Item={
            "pk": _pk(org_id, ts, agent_type),
            **entry.model_dump(mode="json"),
            "ttl": ts + _TTL_SECONDS,
        })
        return entry

    def list_for_org(
        self, org_id: str, *, limit: int = 50,
    ) -> list[RecommendationEntry]:
        """Return the org's advisories newest-first.

        Scan-and-filter — the table's shared single-PK design doesn't
        support begins_with queries without a GSI. Advisory volume is
        small (a few per day per review-agent pass) so a filter scan
        is cheap. If that changes, switch to a GSI on (org_id,
        created_at)."""

        items = scan_all(self._table, Attr("pk").begins_with(_prefix(org_id)))
        entries = [
            RecommendationEntry.model_validate(
                {k: v for k, v in item.items() if k != "pk" and k != "ttl"},
            )
            for item in items
        ]
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries[:limit]
