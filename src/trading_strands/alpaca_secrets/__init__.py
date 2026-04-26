"""Per-org Alpaca credential storage in AWS Secrets Manager.

Each org has its own secret at `trading-strands/org/{org_id}/alpaca`.
The JSON payload matches the existing single-org shape so the trading
service can continue consuming it without structural changes:

    {
        "ALPACA_API_KEY":    "<key>",
        "ALPACA_SECRET_KEY": "<secret>",
        "ALPACA_PAPER":      "true" | "false"
    }

Only the presence of credentials is ever exposed outside this module;
the actual key/secret values are read by the trading service directly
and never surfaced through the dashboard API. Reading at the dashboard
would be a privacy bug — sysadmin is hard-blocked from Alpaca secrets
by the authz policy.
"""

from trading_strands.alpaca_secrets.store import (
    AlpacaCredsStatus,
    AlpacaSecretsStore,
    secret_name_for,
)

__all__ = [
    "AlpacaCredsStatus",
    "AlpacaSecretsStore",
    "secret_name_for",
]
