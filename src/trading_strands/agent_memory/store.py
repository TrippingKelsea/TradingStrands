"""S3-backed per-Agent memory store.

Single-bucket + prefix-isolation model (v0). The store takes a bucket
name and a scope (`org_id`, `agent_type`, `agent_id`) at construct time;
all paths are derived inside the store so callers can't accidentally
read across Agents.

Path layout within a bucket:
    <org_id>/<agent_type>/<agent_id>/YYYY-MM-DD.md
    <org_id>/<agent_type>/<agent_id>/YYYY-MM-DD.compressed.md
    <org_id>/<agent_type>/<agent_id>/lessons.md

Writes:
    - append_to_today(text): appends to today's raw daily file. Read-
      modify-write; not lock-free, but v0 Agents are single-tasked per
      bot so there's no concurrent writer. v1 per-bot-Fargate keeps
      that property (each bot has its own task).
    - append_lesson(text): appends to lessons.md.

Reads:
    - read_day(date): returns the full raw markdown for a given day,
      or empty string if no file exists.
    - read_compressed(date): returns the compressed version, falling
      back to raw if no compressed file exists (end-of-day compactor
      hasn't run yet).
    - read_lessons(): returns the full lessons.md.

All reads/writes are synchronous S3 calls. v0 Agents are slow enough
(LLM invocation dominates) that S3 latency is noise. v1 per-Agent
long-running loops will want async at some point; deferred.
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple


def today_utc() -> str:
    """YYYY-MM-DD in UTC. Rollover at midnight UTC regardless of the
    operator's timezone; display is a UI concern."""

    lt = time.gmtime()
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


class DailyFileKey(NamedTuple):
    org_id: str
    agent_type: str
    agent_id: str
    date: str  # YYYY-MM-DD
    kind: str  # 'raw' | 'compressed'

    @property
    def s3_key(self) -> str:
        suffix = ".compressed.md" if self.kind == "compressed" else ".md"
        return (
            f"{self.org_id}/{self.agent_type}/{self.agent_id}/"
            f"{self.date}{suffix}"
        )


class AgentMemoryStore:
    """Durable memory for one Agent, backed by S3.

    Construct with a specific (org_id, agent_type, agent_id) scope. The
    store holds no other Agents' data — reads/writes are rooted at the
    agent's prefix. In v0 this is enforced by the store's own path
    derivation (the caller doesn't construct keys). In v1 per-Agent
    IAM will enforce it at AWS's layer.
    """

    def __init__(
        self,
        s3_client: Any,
        bucket: str,
        org_id: str,
        agent_type: str,
        agent_id: str,
    ) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._org_id = org_id
        self._agent_type = agent_type
        self._agent_id = agent_id

    @property
    def _prefix(self) -> str:
        return f"{self._org_id}/{self._agent_type}/{self._agent_id}"

    def _daily_key(self, date: str, kind: str = "raw") -> str:
        return DailyFileKey(
            self._org_id, self._agent_type, self._agent_id, date, kind,
        ).s3_key

    def _lessons_key(self) -> str:
        return f"{self._prefix}/lessons.md"

    def _recommendations_key(self) -> str:
        """Recommendations file — mirror of lessons.md for review
        agents (Risk, Compliance, Auditor). Kept as a separate file so
        consumers can filter UI or notifications by type."""

        return f"{self._prefix}/recommendations.md"

    # ── Reads ─────────────────────────────────────────────────────────

    def _read(self, key: str) -> str:
        """Return the contents at `key` or '' if the object doesn't exist.

        Missing-key is expected (new day before first write, or a day
        we never wrote anything for). ClientError for other reasons
        (permissions, throttling) is allowed to propagate so the caller
        sees the failure rather than silently interpreting it as empty.
        """

        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            body: bytes = resp["Body"].read()
            return body.decode("utf-8")
        except self._s3.exceptions.NoSuchKey:
            return ""
        except self._s3.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return ""
            raise

    def read_day(self, date: str) -> str:
        """Return the raw markdown for a given day, empty if none."""

        return self._read(self._daily_key(date, "raw"))

    def read_compressed(self, date: str) -> str:
        """Return the compressed version. Falls back to raw when no
        compressed file exists yet (compactor hasn't run for this day)."""

        content = self._read(self._daily_key(date, "compressed"))
        if content:
            return content
        return self.read_day(date)

    def read_lessons(self) -> str:
        return self._read(self._lessons_key())

    # ── Writes ────────────────────────────────────────────────────────

    def _write(self, key: str, content: str) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )

    def append_to_today(self, text: str) -> None:
        """Append a block to today's raw file.

        Read-modify-write. Single-writer assumption (one Agent → one
        task → one appender). Not atomic; if two callers raced, the
        later one would win. v0 is safe because the Agent is a single
        process per bot.
        """

        date = today_utc()
        key = self._daily_key(date, "raw")
        current = self._read(key)
        if current and not current.endswith("\n"):
            current += "\n"
        if not text.endswith("\n"):
            text = text + "\n"
        self._write(key, current + text)

    def write_compressed(self, date: str, content: str) -> None:
        """Write / overwrite the compressed file for a date.

        Called by the end-of-day Compactor Lambda. Overwrites any
        previous compression for that day (agent run it twice, the
        more-recent summary wins).
        """

        self._write(self._daily_key(date, "compressed"), content)

    def append_lesson(self, text: str) -> None:
        """Append a block to lessons.md.

        Lessons are preserved across the agent's lifetime; the system
        prompt is responsible for ensuring lessons are worth keeping.
        """

        current = self._read(self._lessons_key())
        if current and not current.endswith("\n"):
            current += "\n"
        if not text.endswith("\n"):
            text = text + "\n"
        self._write(self._lessons_key(), current + text)

    def read_recommendations(self) -> str:
        return self._read(self._recommendations_key())

    def append_recommendation(self, text: str) -> None:
        """Append a block to recommendations.md.

        Review agents (Risk/Compliance/Auditor) write here. The file
        is append-only so orgadmins see the full history of what was
        flagged over time — they reference past recommendations when
        deciding whether the same issue is recurring.
        """

        current = self._read(self._recommendations_key())
        if current and not current.endswith("\n"):
            current += "\n"
        if not text.endswith("\n"):
            text = text + "\n"
        self._write(self._recommendations_key(), current + text)

    # ── Range / recent days helper ────────────────────────────────────

    def load_recent_days(
        self, count: int, end_date: str | None = None,
    ) -> list[tuple[str, str]]:
        """Return [(date, content), ...] for the last `count` calendar
        days, newest first. Missing days yield empty strings — the
        caller decides whether to skip or include.

        Callers use this to assemble the default working context (last
        5 trading days' compressed memory) per docs/SPEC/agent_memory.md.
        """

        import datetime

        if end_date:
            end = datetime.date.fromisoformat(end_date)
        else:
            end = datetime.date.fromisoformat(today_utc())
        out: list[tuple[str, str]] = []
        for i in range(count):
            d = end - datetime.timedelta(days=i)
            out.append((d.isoformat(), self.read_compressed(d.isoformat())))
        return out
