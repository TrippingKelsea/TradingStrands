"""Bootstrap — idempotent first-deploy (and re-deploy) initialization.

Runs on CI after CDK deploy, or manually. Responsibilities:

  1. Ensure the system org "Women with Super Powers" exists.
  2. Ensure the default user superwoman@tradingstrands.xyz exists and is
     a member of the system org. NOT auto-granted sysadmin — grant must
     be explicit so privilege isn't acquired by operating the bootstrap.
  3. Delete any STRATEGY# items lacking org_id/author_user_id (legacy
     rows from the pre-refactor schema) — these are the ones causing the
     cross-org leak.

Idempotency is the contract. Running `bootstrap` on a fresh empty account
and on an already-bootstrapped account must yield the same final state,
with no errors, no duplicate rows, and no unexpected mutations.
"""

from trading_strands.bootstrap.runner import (
    BootstrapReport,
    bootstrap,
    delete_legacy_strategies,
    ensure_superwoman_user,
    ensure_system_org,
)

__all__ = [
    "BootstrapReport",
    "bootstrap",
    "delete_legacy_strategies",
    "ensure_superwoman_user",
    "ensure_system_org",
]
