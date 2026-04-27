"""DDB store for rendered prompt snapshots (one row per bot).

Schema:
    pk = PROMPTSNAPSHOT#{bot_id}

Fields:
    bot_id, org_id, system_prompt, user_prompt, tick, rendered_at

Single row per bot; overwritten each tick. Keeping just the latest
snapshot is deliberate:
  - The memory file is the historical record.
  - Operators open the Prompt tab to see what the LLM saw *now*, not
    what it saw an hour ago.
  - Bounded storage.

Reads return None when no snapshot has been written yet (bot just
started, or snapshot persistence isn't wired). The endpoint surfaces
that as 404 so the UI can render a "no snapshot yet" placeholder.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict


class PromptSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bot_id: str
    org_id: str
    system_prompt: str
    user_prompt: str
    tick: int
    rendered_at: int


def _pk(bot_id: str) -> str:
    return f"PROMPTSNAPSHOT#{bot_id}"


class PromptSnapshotStore:
    """One-row-per-bot overwriter for last-rendered prompts."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def write(
        self,
        *,
        bot_id: str,
        org_id: str,
        system_prompt: str,
        user_prompt: str,
        tick: int,
    ) -> PromptSnapshot:
        snapshot = PromptSnapshot(
            bot_id=bot_id,
            org_id=org_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tick=tick,
            rendered_at=int(time.time()),
        )
        self._table.put_item(Item={
            "pk": _pk(bot_id),
            **snapshot.model_dump(mode="json"),
        })
        return snapshot

    def get(self, bot_id: str) -> PromptSnapshot | None:
        resp = self._table.get_item(Key={"pk": _pk(bot_id)})
        item = resp.get("Item")
        if item is None:
            return None
        return PromptSnapshot.model_validate(
            {k: v for k, v in item.items() if k != "pk"},
        )
