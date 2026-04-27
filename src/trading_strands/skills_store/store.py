"""Skills DDB storage + prompt composition.

Schema:
    pk = SKILL#{org_id}#{skill_name}
    skill_name, org_id, markdown, author_user_id, created_at, updated_at

One row per skill. Skills are small (bounded to 32 KB), small in
count (orgadmins write them by hand), and read-heavy at bot-start
only. Plain put/get with a prefix scan for list — no GSI needed at
this scale.
"""

from __future__ import annotations

import time
from typing import Any

from boto3.dynamodb.conditions import Attr
from pydantic import BaseModel, ConfigDict

from trading_strands.ddb import scan_all

# SPEC §8.2 — cap skill bodies so a rogue edit doesn't blow up the
# system prompt. 32 KB is generous for a prompt fragment; a skill
# longer than that should probably be two skills.
_MAX_SKILL_BYTES = 32 * 1024

PK_PREFIX = "SKILL#"


class SkillNotFoundError(KeyError):
    """Raised by get() when the (org, name) pair doesn't exist."""


class SkillTooLargeError(ValueError):
    """Raised by put() when the markdown body exceeds the size cap."""


class Skill(BaseModel):
    """One skill as stored."""

    model_config = ConfigDict(extra="ignore")

    org_id: str
    skill_name: str
    markdown: str
    author_user_id: str
    created_at: int
    updated_at: int


class SkillsStore:
    """DDB-backed read/write of per-org skills."""

    def __init__(self, table: Any) -> None:
        self._table = table

    @staticmethod
    def _pk(org_id: str, skill_name: str) -> str:
        return f"{PK_PREFIX}{org_id}#{skill_name}"

    def put(
        self,
        org_id: str,
        skill_name: str,
        markdown: str,
        author_user_id: str,
    ) -> Skill:
        """Create or update a skill. Preserves created_at on update —
        only updated_at advances when an existing skill is edited."""

        if len(markdown.encode("utf-8")) > _MAX_SKILL_BYTES:
            msg = (
                f"skill {skill_name!r} body exceeds "
                f"{_MAX_SKILL_BYTES}-byte cap"
            )
            raise SkillTooLargeError(msg)

        now = int(time.time())
        pk = self._pk(org_id, skill_name)
        # Preserve created_at on updates. get_item first; if absent,
        # created_at = now. Can't do this in one UpdateExpression
        # cleanly without a SET if_not_exists dance that complicates
        # the happy path.
        existing = self._table.get_item(Key={"pk": pk}).get("Item")
        created_at = int(existing["created_at"]) if existing else now

        self._table.put_item(Item={
            "pk": pk,
            "org_id": org_id,
            "skill_name": skill_name,
            "markdown": markdown,
            "author_user_id": author_user_id,
            "created_at": created_at,
            "updated_at": now,
        })
        return Skill(
            org_id=org_id, skill_name=skill_name, markdown=markdown,
            author_user_id=author_user_id,
            created_at=created_at, updated_at=now,
        )

    def get(self, org_id: str, skill_name: str) -> Skill:
        resp = self._table.get_item(
            Key={"pk": self._pk(org_id, skill_name)},
        )
        item = resp.get("Item")
        if item is None:
            raise SkillNotFoundError((org_id, skill_name))
        return Skill(
            org_id=str(item["org_id"]),
            skill_name=str(item["skill_name"]),
            markdown=str(item["markdown"]),
            author_user_id=str(item["author_user_id"]),
            created_at=int(item["created_at"]),
            updated_at=int(item["updated_at"]),
        )

    def delete(self, org_id: str, skill_name: str) -> None:
        """Idempotent — deleting a missing skill is a no-op."""

        self._table.delete_item(
            Key={"pk": self._pk(org_id, skill_name)},
        )

    def list_for_org(self, org_id: str) -> list[Skill]:
        """Every skill owned by the given org. Prefix scan. Fine for
        the expected scale (tens of skills per org)."""

        prefix = f"{PK_PREFIX}{org_id}#"
        items = scan_all(self._table, Attr("pk").begins_with(prefix))
        # Sort by name for stable output.
        items.sort(key=lambda x: str(x.get("skill_name", "")))
        return [
            Skill(
                org_id=str(i["org_id"]),
                skill_name=str(i["skill_name"]),
                markdown=str(i["markdown"]),
                author_user_id=str(i["author_user_id"]),
                created_at=int(i["created_at"]),
                updated_at=int(i["updated_at"]),
            )
            for i in items
        ]


# ── Prompt composition ─────────────────────────────────────────────


def compose_system_prompt(
    base_prompt: str,
    skills: list[Skill],
    strategy_name: str,
    strategy_markdown: str,
) -> str:
    """Assemble the strategy bot's system prompt per SPEC §8.4.

    Layout:
        <base_prompt>

        # Skill: <name_1>
        <skill_1 body>

        # Skill: <name_2>
        <skill_2 body>

        # Strategy: <strategy_name>
        <strategy body>

    Skills are rendered in the order given — callers (the bot
    construction path) pass them in the order the strategy's
    `skills` list specifies. Strategy markdown is always last,
    closest to the LLM's attention.
    """

    parts: list[str] = [base_prompt.strip()]
    for skill in skills:
        parts.append(f"# Skill: {skill.skill_name}")
        parts.append(skill.markdown.strip())
    parts.append(f"# Strategy: {strategy_name}")
    parts.append(strategy_markdown.strip())
    return "\n\n".join(parts)
