"""CLI entry for the bootstrap step.

Usage (in CI after CDK deploy):
    uv run python -m trading_strands.bootstrap

Reads DYNAMODB_TABLE from env (defaults to 'trading-strands-state').
Prints a structured summary and exits 0 on success, non-zero on failure.
"""

from __future__ import annotations

import os
import sys

import boto3

from trading_strands.bootstrap.runner import bootstrap


def _main() -> int:
    table_name = os.environ.get("DYNAMODB_TABLE", "trading-strands-state")
    ddb = boto3.resource("dynamodb")
    table = ddb.Table(table_name)
    sm = boto3.client("secretsmanager")

    print(f"bootstrap: table={table_name}")
    try:
        report = bootstrap(table, secretsmanager_client=sm)
    except Exception as exc:
        print(f"bootstrap: FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"  system_org: {report.system_org.org_id} ({report.system_org.name}) "
          f"created={report.system_org_created}")
    print(f"  superwoman: {report.superwoman.user_id} ({report.superwoman.email}) "
          f"created={report.superwoman_created} "
          f"membership_added={report.superwoman_membership_added} "
          f"sysadmin_granted={report.superwoman_sysadmin_granted}")
    print(f"  legacy_strategies_deleted: {report.legacy_strategies_deleted}")
    print(f"  system_org_alpaca_seeded: {report.system_org_alpaca_seeded}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
