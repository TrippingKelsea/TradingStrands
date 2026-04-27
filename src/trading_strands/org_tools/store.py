"""Per-org tool availability storage.

Schema:
    pk = ORG_TOOL#{org_id}#{tool_name}
    enabled: bool
    updated_by: user_id of the orgadmin who last touched this
    updated_at: epoch seconds

Absence of the row is the default-deny state — strategies cannot
opt into a tool whose row is missing. Flipping to enabled=True
makes the tool selectable for strategies in the org.
"""

from __future__ import annotations

import time
from typing import Any

from boto3.dynamodb.conditions import Attr
from pydantic import BaseModel, ConfigDict

PK_PREFIX = "ORG_TOOL#"


class OrgToolConfig(BaseModel):
    """One (org, tool) availability row."""

    model_config = ConfigDict(extra="ignore")

    org_id: str
    tool_name: str
    enabled: bool
    updated_by: str
    updated_at: int


class OrgToolsStore:
    def __init__(self, table: Any) -> None:
        self._table = table

    @staticmethod
    def _pk(org_id: str, tool_name: str) -> str:
        return f"{PK_PREFIX}{org_id}#{tool_name}"

    def set_enabled(
        self,
        org_id: str,
        tool_name: str,
        enabled: bool,
        updated_by: str,
    ) -> OrgToolConfig:
        now = int(time.time())
        self._table.put_item(Item={
            "pk": self._pk(org_id, tool_name),
            "org_id": org_id,
            "tool_name": tool_name,
            "enabled": enabled,
            "updated_by": updated_by,
            "updated_at": now,
        })
        return OrgToolConfig(
            org_id=org_id, tool_name=tool_name, enabled=enabled,
            updated_by=updated_by, updated_at=now,
        )

    def is_enabled(self, org_id: str, tool_name: str) -> bool:
        """True only when an explicit row exists with enabled=True.
        Absence = not enabled."""

        resp = self._table.get_item(
            Key={"pk": self._pk(org_id, tool_name)},
        )
        item = resp.get("Item")
        if item is None:
            return False
        return bool(item.get("enabled", False))

    def list_for_org(self, org_id: str) -> list[OrgToolConfig]:
        prefix = f"{PK_PREFIX}{org_id}#"
        resp = self._table.scan(
            FilterExpression=Attr("pk").begins_with(prefix),
        )
        items = resp.get("Items", [])
        items.sort(key=lambda x: str(x.get("tool_name", "")))
        return [
            OrgToolConfig(
                org_id=str(i["org_id"]),
                tool_name=str(i["tool_name"]),
                enabled=bool(i["enabled"]),
                updated_by=str(i["updated_by"]),
                updated_at=int(i["updated_at"]),
            )
            for i in items
        ]
