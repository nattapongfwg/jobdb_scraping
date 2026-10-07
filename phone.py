"""Thai phone numbers, one rule for the whole board (the browser side is static/phone.js).

- mobile: 10 digits → 'xxx-xxx-xxxx'   e.g. 081-234-5678
- tel:     9 digits → 'x-xxxx-xxxx'    e.g. 0-2034-4147 (landline)

Only the digits count; a +66 country code becomes a leading 0. normalize() returns ''
for a blank value, the formatted number when it has the right number of digits, and
None otherwise (the caller rejects it).
"""
from __future__ import annotations

import re

DIGITS = {"mobile": 10, "tel": 9}
EXAMPLE = {"mobile": "xxx-xxx-xxxx", "tel": "x-xxxx-xxxx"}


def normalize(value: str | None, kind: str = "mobile") -> str | None:
    if not (value or "").strip():
        return ""
    want = DIGITS[kind]
    digits = re.sub(r"\D", "", value)
    if len(digits) == want + 1 and digits.startswith("66"):
        digits = "0" + digits[2:]
    if len(digits) != want:
        return None
    if kind == "tel":
        return f"{digits[:1]}-{digits[1:5]}-{digits[5:]}"
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
