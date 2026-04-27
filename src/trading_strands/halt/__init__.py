"""Halt state store (system + per-org).

v1 halt model:
  - System-wide halt (pk=CONTROL): sysadmin emergency stop. Stops every
    org regardless of per-org state.
  - Per-org halt (pk=CONTROL#{org_id}): scoped to one org; set by
    Auditor Agent on drift, or orgadmin via the dashboard.

Effective halt for an org = system_halted OR org_halted. The Coordinator
reads the effective state before routing trades; rejecting when halted
is unchanged from v0 behavior — only the source of the flag is broader.
"""

from trading_strands.halt.store import HaltEvent, HaltState, HaltStore

__all__ = ["HaltEvent", "HaltState", "HaltStore"]
