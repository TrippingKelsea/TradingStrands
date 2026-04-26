"""Tests for the server-side password policy."""

from __future__ import annotations

from trading_strands.dashboard.password_policy import check_password


def test_rejects_short_password() -> None:
    result = check_password("Short1!")
    assert result.ok is False
    assert "12 characters" in result.reason


def test_rejects_blocklist_token_in_password() -> None:
    # Contains 'tradingstrands' substring — should fail.
    result = check_password("TradingStrands1!a9z")
    assert result.ok is False
    assert "blocked" in result.reason.lower()


def test_rejects_starter_prefix() -> None:
    # The default bootstrap password — should never be accepted as new.
    result = check_password("ChangeMeOnFirstLogin1!")
    assert result.ok is False


def test_rejects_email_local_part_in_password() -> None:
    result = check_password("supercoolabc123!", email="supercool@example.com")
    assert result.ok is False
    assert "email" in result.reason.lower()


def test_rejects_forbidden_token_similarity() -> None:
    """Passing the starter password as `forbidden` prevents reuse."""

    result = check_password(
        "ChangeMeOnFirstLogin1!", forbidden=("ChangeMeOnFirstLogin1!",),
    )
    assert result.ok is False


def test_rejects_common_weak_password() -> None:
    """Hits zxcvbn's dictionary even though it meets length + char policy."""

    result = check_password("Password123!")
    assert result.ok is False


def test_accepts_strong_passphrase() -> None:
    # Long unrelated words — should score 3+.
    result = check_password("correct horse battery staple 9!")
    assert result.ok is True
    assert result.score >= 3


def test_accepts_random_high_entropy() -> None:
    result = check_password("xQ4!mnT9_rvB@zpLkf2J")
    assert result.ok is True


def test_blocklist_is_case_insensitive() -> None:
    result = check_password("TRADINGSTRANDS-Is!-Cool-9")
    assert result.ok is False
