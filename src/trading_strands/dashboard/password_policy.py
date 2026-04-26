"""Password strength policy — enforced on change-password and reset flows.

Cognito already requires 12+ chars, upper+lower+digit+symbol via the user
pool's PasswordPolicy. That protects against trivially-short passwords but
accepts lots of garbage ('Password123!' meets those rules).

On TOP of Cognito, we run zxcvbn entropy scoring plus a deliberate
user-context blocklist:
  - Reject anything that scores below 3 (strength 0-4 scale, per zxcvbn).
    Score 3 means "safely unguessable; moderate protection from offline
    slow-hash scenario."
  - Reject if the password contains the user's email local-part
  - Reject known-starter tokens used by bootstrap/ops
  - Reject presence of 'tradingstrands'

Pure function: callers pass the password + optional context (email,
forbidden tokens) and get back a PasswordCheck with `ok` and a
human-readable `reason`. The reason goes into the change-password error
page so the user knows what to fix.
"""

from __future__ import annotations

from typing import NamedTuple

from zxcvbn import zxcvbn

# Below this zxcvbn score, reject. 3 = moderate/good; 4 = strong.
MIN_STRENGTH_SCORE = 3

# Tokens we NEVER want in a password, independent of user context.
# Keep the list small; zxcvbn's built-in dictionary handles common words.
_HARD_BLOCKLIST = frozenset({
    "tradingstrands",
    "changemeonfirstlogin",  # our default starter prefix
    "superwoman",
})


class PasswordCheck(NamedTuple):
    ok: bool
    reason: str
    score: int  # zxcvbn 0-4


def check_password(
    password: str,
    email: str | None = None,
    forbidden: tuple[str, ...] = (),
) -> PasswordCheck:
    """Return a PasswordCheck for the given candidate password.

    `email` and `forbidden` are context hints — we pass them to zxcvbn as
    `user_inputs` so scoring accounts for them, and we also apply a direct
    substring check because zxcvbn's dictionary isn't perfect about things
    like case-insensitive email-local-part containment.
    """

    if len(password) < 12:
        return PasswordCheck(False, "Password must be at least 12 characters.", 0)

    lowered = password.lower()
    for token in _HARD_BLOCKLIST:
        if token in lowered:
            return PasswordCheck(
                False,
                "Password contains a blocked word — pick something unrelated "
                "to TradingStrands or the default account.",
                0,
            )
    for token in forbidden:
        if token and token.lower() in lowered:
            return PasswordCheck(
                False,
                "Password is too similar to your previous / starter password.",
                0,
            )
    if email:
        local = email.split("@", 1)[0].lower().strip()
        if local and len(local) >= 3 and local in lowered:
            return PasswordCheck(
                False,
                "Password cannot contain your email address.",
                0,
            )

    user_inputs: list[str] = []
    if email:
        user_inputs.append(email)
        user_inputs.append(email.split("@", 1)[0])
    user_inputs.extend(forbidden)
    user_inputs.extend(_HARD_BLOCKLIST)

    result = zxcvbn(password, user_inputs=user_inputs)
    score = int(result.get("score", 0))
    if score < MIN_STRENGTH_SCORE:
        suggestions = result.get("feedback", {}).get("suggestions", [])
        warning = result.get("feedback", {}).get("warning", "") or (
            "Password is too weak."
        )
        hint = suggestions[0] if suggestions else (
            "Try a longer passphrase of unrelated words or random characters."
        )
        return PasswordCheck(False, f"{warning} {hint}", score)
    return PasswordCheck(True, "ok", score)
