"""Input validators for dashboard API request bodies.

Defense-in-depth layer paired with the DOM render helpers — even if a
render site regresses, the validator ensures user-authored strings
that enter persistence cannot carry HTML or control characters in the
first place.

Each validator is a standalone function so the schemas can call them
via `@field_validator` without coupling to a particular request body.

Validation rules (chosen to match observed legitimate input +
documented broker conventions):
  - Strategy / skill / tool / symbol names use restricted character
    classes that reject `<`, `>`, quotes, control chars, and other
    HTML-dangerous glyphs by construction.
  - Length caps are generous enough for real content but small enough
    that a pasted attacker payload is rejected before reaching DDB.

docs/SPEC/operational_notes.md §"Dashboard XSS hardening" has the
context.
"""

from __future__ import annotations

import re
import unicodedata

# Broker symbols: US equity + futures tickers. Uppercase alphanumerics,
# with '.' and '-' for BRK.A / BF-B style names. Length capped at 10 —
# the longest real ticker I know of is ~6 chars; 10 leaves headroom for
# options-style OSI symbols without inviting pasted HTML.
SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")

# Skill / tool names are lowercase snake_case identifiers.
IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

# Reasonable caps for human-authored free-text fields. Chosen empirically:
# real strategy markdown in the repo is typically 1-4 KB; 50 KB is the
# hard cap so a pasted payload can't cost DDB more than a trivial
# amount either.
NAME_MAX_CHARS = 80
MARKDOWN_MAX_CHARS = 50_000
RATIONALE_MAX_CHARS = 4_000


def _has_control_chars(s: str) -> bool:
    """True iff s contains characters in Unicode category 'C' other
    than newline + tab. Control chars have no place in a user-typed
    name — they're the usual vector for obfuscating attack payloads
    and confusing downstream terminal renderers."""

    for ch in s:
        if ch in ("\n", "\t"):
            continue
        if unicodedata.category(ch).startswith("C"):
            return True
    return False


def validate_name(value: str, *, field: str = "name") -> str:
    """Strategy / org / display name. Short, human-typed, no HTML."""

    stripped = value.strip()
    if not stripped:
        msg = f"{field} cannot be empty"
        raise ValueError(msg)
    if len(stripped) > NAME_MAX_CHARS:
        msg = f"{field} exceeds {NAME_MAX_CHARS} characters"
        raise ValueError(msg)
    if _has_control_chars(stripped):
        msg = f"{field} contains control characters"
        raise ValueError(msg)
    # Hard no on HTML-significant glyphs. The render layer also escapes
    # these; rejecting at the schema stops them from reaching DDB.
    for bad in ("<", ">"):
        if bad in stripped:
            msg = f"{field} contains disallowed character {bad!r}"
            raise ValueError(msg)
    return stripped


def validate_markdown(value: str, *, field: str = "markdown") -> str:
    """Strategy prompt / skill body. Larger cap, allow newlines + tabs.

    Does NOT reject HTML in the content — markdown legitimately uses
    `<details>`, `<sub>`, etc. The render layer never interprets this
    content as HTML (it's always rendered as text via textContent or
    `<pre>` with value-assignment), so the escape boundary lives at
    the render layer, not here. The schema only enforces the size
    cap and rejects raw control chars that have no legitimate use.
    """

    if len(value) > MARKDOWN_MAX_CHARS:
        msg = f"{field} exceeds {MARKDOWN_MAX_CHARS} characters"
        raise ValueError(msg)
    if _has_control_chars(value):
        msg = f"{field} contains control characters"
        raise ValueError(msg)
    return value


def validate_symbol(value: str) -> str:
    """Single ticker symbol. Uppercase, alphanumeric with . and -.

    Uses `fullmatch` so the regex's `$` can't match pre-newline (a
    Python default that lets `FOO\\n` slip past a `^FOO$` check).
    """

    if not SYMBOL_RE.fullmatch(value):
        msg = (
            f"invalid symbol {value!r}: must match ^[A-Z0-9.\\-]{{1,10}}$"
        )
        raise ValueError(msg)
    return value


def validate_symbols_list(values: list[str]) -> list[str]:
    """List of tickers. Empty list is legal (strategy with dynamic
    symbol selection). Validates each entry via validate_symbol."""

    if len(values) > 100:
        msg = "symbols list exceeds 100 entries"
        raise ValueError(msg)
    return [validate_symbol(v) for v in values]


def validate_ident(value: str, *, field: str = "name") -> str:
    """Lowercase snake_case identifier (skill names, tool names).

    Matches `^[a-z][a-z0-9_]{0,39}$`: 1-40 chars, leading letter.
    Shares the pattern with `_TOOL_INVENTORY` registrations and the
    skills frontend's client-side check.
    """

    if not IDENT_RE.fullmatch(value):
        msg = (
            f"invalid {field} {value!r}: must be lowercase snake_case "
            f"(a-z, 0-9, _; start with letter; ≤40 chars)"
        )
        raise ValueError(msg)
    return value


def validate_capital(value: str) -> str:
    """Capital is typed as a string (Decimal-compatible) so the JSON
    shape doesn't coerce to float. Must parse cleanly and be
    non-negative."""

    try:
        f = float(value)
    except ValueError as exc:
        msg = f"capital must be a numeric string: {value!r}"
        raise ValueError(msg) from exc
    if f < 0:
        msg = f"capital must be non-negative, got {value!r}"
        raise ValueError(msg)
    if f > 1_000_000_000:
        # Sanity cap; no single strategy should be running with >$1B of
        # paper capital either.
        msg = f"capital exceeds $1B cap: {value!r}"
        raise ValueError(msg)
    return value
