"""Mask personal contact details in page text before it is sent to the model or stored.

The documents are public, but the fields extracted never need an individual's email address or
phone number, so they are masked (``[EMAIL]``, ``[PHONE]``) in the text the model sees and in
the stored page text. Evidence checks run against the same masked text, so quotes still match.
Names of signing officials are not masked: they are part of the public record and are not
extracted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# North American numbers: (650) 604-1234, 650-604-1234, 650.604.1234, +1 650 604 1234.
# Separators are required, so contract numbers, amounts and DUNS numbers do not match.
PHONE = re.compile(r"(?<![\w$.,])(?:\+?1[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w])")


@dataclass(frozen=True)
class Redaction:
    text: str
    emails: int
    phones: int


def redact(text: str) -> Redaction:
    text, emails = EMAIL.subn("[EMAIL]", text)
    text, phones = PHONE.subn("[PHONE]", text)
    return Redaction(text=text, emails=emails, phones=phones)
