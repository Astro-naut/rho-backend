"""Phone normalisation to E.164. Assume +91 for bare 10-digit Indian mobiles."""

from __future__ import annotations

import re


_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: str | None) -> str | None:
    """Return E.164 phone or None if empty/invalid."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith("+"):
        digits = _DIGITS.sub("", s[1:])
        if len(digits) < 8 or len(digits) > 15:
            return None
        return "+" + digits
    digits = _DIGITS.sub("", s)
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if 8 <= len(digits) <= 15:
        return "+" + digits
    return None
