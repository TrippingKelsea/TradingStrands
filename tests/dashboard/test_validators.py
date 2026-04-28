"""Tests for dashboard.validators — the input-boundary defense-in-depth
layer for the XSS hardening work.

Each test is a concrete payload a real attacker might try, paired
with the expected accept/reject. Documents the exact boundary so a
future relaxation has to explicitly delete the case rather than
quietly loosening.
"""

from __future__ import annotations

import pytest

from trading_strands.dashboard import validators as dv


class TestValidateName:
    def test_ordinary_name_accepted(self) -> None:
        assert dv.validate_name("Momentum AAPL") == "Momentum AAPL"

    def test_strips_surrounding_whitespace(self) -> None:
        assert dv.validate_name("  momentum  ") == "momentum"

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            dv.validate_name("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            dv.validate_name("   ")

    def test_html_tags_rejected(self) -> None:
        with pytest.raises(ValueError, match="disallowed"):
            dv.validate_name("<script>alert(1)</script>")

    def test_angle_brackets_rejected(self) -> None:
        with pytest.raises(ValueError, match="disallowed"):
            dv.validate_name("name with < tag")

    def test_control_chars_rejected(self) -> None:
        with pytest.raises(ValueError, match="control"):
            dv.validate_name("name\x00with null")

    def test_overlong_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            dv.validate_name("x" * 100)

    def test_unicode_allowed(self) -> None:
        # Real operators may name strategies in non-ASCII languages.
        assert dv.validate_name("Stratégie Alpha") == "Stratégie Alpha"


class TestValidateMarkdown:
    def test_ordinary_markdown_accepted(self) -> None:
        md = "# Heading\n\n- point 1\n- point 2\n"
        assert dv.validate_markdown(md) == md

    def test_html_allowed(self) -> None:
        """Markdown legitimately embeds HTML (details/summary, sub,
        etc.). The render layer never interprets this as HTML, so the
        schema permits it — the escape boundary lives at render, not
        at input."""

        assert dv.validate_markdown("<details>foo</details>") == "<details>foo</details>"

    def test_overlong_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            dv.validate_markdown("x" * (dv.MARKDOWN_MAX_CHARS + 1))

    def test_newlines_and_tabs_allowed(self) -> None:
        md = "line1\nline2\tindented"
        assert dv.validate_markdown(md) == md

    def test_control_chars_rejected(self) -> None:
        with pytest.raises(ValueError, match="control"):
            dv.validate_markdown("raw\x07bell")


class TestValidateSymbol:
    @pytest.mark.parametrize("sym", [
        "AAPL", "SPY", "BRK.A", "BF-B", "BTC", "ES", "A",
    ])
    def test_legit_symbols_accepted(self, sym: str) -> None:
        assert dv.validate_symbol(sym) == sym

    @pytest.mark.parametrize("sym", [
        "aapl",           # lowercase
        "AAPL!",          # punctuation
        "",               # empty
        "TOOLONGSYMBOL",  # > 10 chars
        "<script>",       # html
        "AAPL AAPL",      # space
        "AAPL\n",         # newline
    ])
    def test_bad_symbols_rejected(self, sym: str) -> None:
        with pytest.raises(ValueError):
            dv.validate_symbol(sym)


class TestValidateSymbolsList:
    def test_empty_list_accepted(self) -> None:
        assert dv.validate_symbols_list([]) == []

    def test_all_valid_returned_unchanged(self) -> None:
        assert dv.validate_symbols_list(["AAPL", "SPY"]) == ["AAPL", "SPY"]

    def test_one_bad_fails_whole_list(self) -> None:
        with pytest.raises(ValueError):
            dv.validate_symbols_list(["AAPL", "<img src=x>"])

    def test_overlong_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="100"):
            dv.validate_symbols_list(["AAPL"] * 101)


class TestValidateIdent:
    @pytest.mark.parametrize("name", [
        "news", "morning_prep", "greeks_cheatsheet", "a", "x1", "abc_123_def",
    ])
    def test_legit_idents_accepted(self, name: str) -> None:
        assert dv.validate_ident(name) == name

    @pytest.mark.parametrize("name", [
        "News",            # uppercase
        "1leading",        # leading digit
        "",                # empty
        "has space",       # space
        "has-dash",        # dash
        "<script>",        # html
        "x" * 41,          # too long
    ])
    def test_bad_idents_rejected(self, name: str) -> None:
        with pytest.raises(ValueError):
            dv.validate_ident(name)


class TestValidateCapital:
    @pytest.mark.parametrize("v", ["0", "1000", "1000.50", "1e3"])
    def test_legit_capital_accepted(self, v: str) -> None:
        assert dv.validate_capital(v) == v

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            dv.validate_capital("-100")

    def test_over_cap_rejected(self) -> None:
        with pytest.raises(ValueError, match="1B"):
            dv.validate_capital("999999999999")

    def test_non_numeric_rejected(self) -> None:
        with pytest.raises(ValueError, match="numeric"):
            dv.validate_capital("one thousand")
