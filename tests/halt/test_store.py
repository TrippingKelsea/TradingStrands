"""Tests for HaltStore.

v1 halt model: system-wide halt (CONTROL) AND per-org halt
(CONTROL#{org_id}) both checked. One org halting must not stop
another org's trades; sysadmin halting via CONTROL stops everything.
"""

from __future__ import annotations

import boto3
from moto import mock_aws

from trading_strands.halt.store import HaltStore


def _table():
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


# ── set_org_halt / is_org_halted ────────────────────────────────────


def test_is_org_halted_false_when_nothing_set() -> None:
    with mock_aws():
        store = HaltStore(_table())
        assert store.is_org_halted("org-a") is False


def test_set_org_halt_persists_per_org() -> None:
    with mock_aws():
        store = HaltStore(_table())
        store.set_org_halt("org-a", True, reason="auditor: drift")
        assert store.is_org_halted("org-a") is True
        # Another org is NOT halted just because org-a is.
        assert store.is_org_halted("org-b") is False


def test_unhalt_org_flips_back() -> None:
    with mock_aws():
        store = HaltStore(_table())
        store.set_org_halt("org-a", True, reason="x")
        store.set_org_halt("org-a", False, reason="cleared by orgadmin")
        assert store.is_org_halted("org-a") is False


def test_org_halt_includes_reason_metadata() -> None:
    with mock_aws():
        store = HaltStore(_table())
        store.set_org_halt("org-a", True, reason="auditor: AAPL drift")
        state = store.get_org_state("org-a")
        assert state.halted is True
        assert "auditor" in (state.reason or "").lower()
        assert state.updated_at > 0


# ── system halt (back-compat with CONTROL row) ──────────────────────


def test_is_system_halted_false_when_nothing_set() -> None:
    with mock_aws():
        store = HaltStore(_table())
        assert store.is_system_halted() is False


def test_set_system_halt_persists() -> None:
    with mock_aws():
        store = HaltStore(_table())
        store.set_system_halt(True, reason="sysadmin emergency")
        assert store.is_system_halted() is True


# ── combined enforcement view ───────────────────────────────────────


def test_is_effective_halted_true_when_system_halted() -> None:
    """System halt stops every org regardless of per-org state."""

    with mock_aws():
        store = HaltStore(_table())
        store.set_system_halt(True, reason="sysadmin")
        assert store.is_effective_halted("org-a") is True
        assert store.is_effective_halted("org-b") is True


def test_is_effective_halted_true_when_org_halted_only() -> None:
    with mock_aws():
        store = HaltStore(_table())
        store.set_org_halt("org-a", True, reason="auditor")
        assert store.is_effective_halted("org-a") is True
        # Sibling org unaffected.
        assert store.is_effective_halted("org-b") is False


def test_is_effective_halted_false_when_neither_halted() -> None:
    with mock_aws():
        store = HaltStore(_table())
        assert store.is_effective_halted("org-a") is False


def test_get_effective_reason_prefers_system_over_org() -> None:
    """If both are halted, the system reason is the one that matters —
    it's the broader scope and the operator needs to clear it first."""

    with mock_aws():
        store = HaltStore(_table())
        store.set_org_halt("org-a", True, reason="auditor: drift")
        store.set_system_halt(True, reason="sysadmin emergency")
        reason = store.get_effective_reason("org-a")
        assert "sysadmin" in (reason or "").lower()


# ── EMF emission ────────────────────────────────────────────────────


def test_emit_on_org_halt_transition(capsys) -> None:
    """A first-time halt writes the row AND emits an EMF metric —
    lets a CW alarm fire on unexpected halts. Subsequent writes that
    don't change state (already halted -> halted) do NOT re-emit."""

    with mock_aws():
        store = HaltStore(_table())
        store.set_org_halt("org-a", True, reason="auditor: drift")
        first = capsys.readouterr().out
        assert '"halt.transition.count"' in first
        assert '"scope"' in first and '"org"' in first
        assert '"halted"' in first and '"true"' in first

        # Write again with same state — should NOT emit.
        store.set_org_halt("org-a", True, reason="auditor: drift")
        second = capsys.readouterr().out
        assert '"halt.transition.count"' not in second


def test_emit_on_unhalt_transition(capsys) -> None:
    with mock_aws():
        store = HaltStore(_table())
        # Halt first (produces a transition) then clear and read again.
        store.set_org_halt("org-a", True, reason="x")
        capsys.readouterr()  # drop the halt-transition emission

        store.set_org_halt("org-a", False, reason="orgadmin cleared")
        out = capsys.readouterr().out
        assert '"halt.transition.count"' in out
        assert '"halted"' in out and '"false"' in out


def test_emit_on_system_halt_uses_system_scope(capsys) -> None:
    with mock_aws():
        store = HaltStore(_table())
        store.set_system_halt(True, reason="sysadmin emergency")
        out = capsys.readouterr().out
        assert '"halt.transition.count"' in out
        assert '"scope"' in out and '"system"' in out


def test_no_emit_when_state_unchanged_false_to_false(capsys) -> None:
    """Default state is unhalted. Writing unhalt to an already-unhalted
    org must not emit — otherwise we'd spam the metric on every startup
    that runs a defensive unhalt."""

    with mock_aws():
        store = HaltStore(_table())
        store.set_org_halt("org-a", False, reason="defensive")
        out = capsys.readouterr().out
        assert '"halt.transition.count"' not in out
