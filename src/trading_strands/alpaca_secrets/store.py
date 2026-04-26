"""Secrets Manager wrapper for per-org Alpaca credentials.

Stateless. Injecting the boto3 secretsmanager client makes testing with
moto trivial and means the production caller can reuse a module-level
client without this class caring about lifecycle.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, NamedTuple

SECRET_PATH_PREFIX = "trading-strands/org"  # noqa: S105 -- path prefix, not a credential


def secret_name_for(org_id: str) -> str:
    """Return the Secrets Manager secret name for this org.

    Centralized so the dashboard, trading service, and tests all agree
    on one path format. Changing the scheme here is the single point of
    change needed for a future rename.
    """

    return f"{SECRET_PATH_PREFIX}/{org_id}/alpaca"


class AlpacaCredsStatus(NamedTuple):
    """What the dashboard UI needs to decide how to render.

    Intentionally does NOT include the key/secret values. The dashboard
    never surfaces credentials — only whether they're configured, and
    whether they target paper or live.
    """

    configured: bool
    paper: bool | None  # True=paper, False=live, None=unknown/not-configured


class AlpacaSecretsStore:
    """Write + check status of org-scoped Alpaca credentials."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def upsert(
        self, org_id: str, api_key: str, secret_key: str, paper: bool = True,
    ) -> None:
        """Create or overwrite the secret for this org. Called from the
        admin UI when an orgadmin submits credentials."""

        payload = json.dumps({
            "ALPACA_API_KEY": api_key,
            "ALPACA_SECRET_KEY": secret_key,
            "ALPACA_PAPER": "true" if paper else "false",
        })
        name = secret_name_for(org_id)
        try:
            self._client.create_secret(
                Name=name,
                Description=f"Alpaca credentials for org {org_id}",
                SecretString=payload,
            )
        except self._client.exceptions.ResourceExistsException:
            self._client.put_secret_value(SecretId=name, SecretString=payload)

    def delete(self, org_id: str) -> None:
        """Remove credentials for an org (used when an org is deleted).

        Uses force-delete-without-recovery to avoid the 7-30 day retention
        window which would block re-creating the secret if an org is
        re-added with the same id.
        """

        name = secret_name_for(org_id)
        with contextlib.suppress(self._client.exceptions.ResourceNotFoundException):
            self._client.delete_secret(
                SecretId=name,
                ForceDeleteWithoutRecovery=True,
            )

    def status(self, org_id: str) -> AlpacaCredsStatus:
        """Return whether this org has credentials and their paper/live mode.

        Does NOT return the actual key/secret values. If you need those,
        read directly via the trading service's task role — the dashboard
        has no legitimate reason to see them.
        """

        name = secret_name_for(org_id)
        try:
            resp = self._client.get_secret_value(SecretId=name)
        except self._client.exceptions.ResourceNotFoundException:
            return AlpacaCredsStatus(configured=False, paper=None)

        raw = resp.get("SecretString", "")
        if not raw:
            return AlpacaCredsStatus(configured=False, paper=None)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return AlpacaCredsStatus(configured=False, paper=None)

        has_key = bool(data.get("ALPACA_API_KEY"))
        has_secret = bool(data.get("ALPACA_SECRET_KEY"))
        if not (has_key and has_secret):
            return AlpacaCredsStatus(configured=False, paper=None)

        paper_str = str(data.get("ALPACA_PAPER", "true")).lower()
        return AlpacaCredsStatus(
            configured=True,
            paper=paper_str in ("true", "1", "yes"),
        )
