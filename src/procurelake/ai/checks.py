"""Deterministic checks on a model extraction: no model is involved in deciding pass or fail.

For each field:

* ``missing``: the model returned no value (allowed: an empty field is never an error).
* ``failed``: a reason is recorded. Reasons:
  - ``page_out_of_range``: the cited page does not exist;
  - ``quote_not_on_page``: the quote is not on the cited page (after collapsing whitespace and
    ignoring case), so the evidence cannot be traced;
  - ``value_not_in_quote``: the quote does not show the value (contract number characters,
    the same amount, the same calendar date, or every word of a name);
  - ``contract_id_not_in_usaspending``: the contract number matches neither the USAspending
    award's PIID nor its parent award's PIID.
* ``passed``: none of the above.

``notes`` carry information for the reviewer that is not a failure, for example a supplier
name that differs from the USAspending recipient name (common: "SPACEX" and "SPACE
EXPLORATION TECHNOLOGIES CORP.").
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from procurelake.ai.schema import ALL_FIELDS, DATE_FIELDS, ContractExtraction
from procurelake.ai.text import PageText

MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ]
    )
    for m in names
}  # fmt: skip
NAME_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "the",
}


@dataclass(frozen=True)
class FieldCheck:
    field: str
    status: Literal["passed", "missing", "failed"]
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckReport:
    fields: dict[str, FieldCheck] = field(default_factory=dict)

    @property
    def failed(self) -> list[str]:
        return [f for f, c in self.fields.items() if c.status == "failed"]

    @property
    def all_passed(self) -> bool:
        return not self.failed


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def alnum(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def words(text: str) -> list[str]:
    return re.findall(r"[0-9a-z]+", text.casefold())


def _year(text: str) -> int:
    year = int(text)
    return year + 2000 if year < 100 else year


def dates_in(text: str) -> set[date]:
    """Every calendar date written in ``text`` in the formats US contract forms use."""
    found: set[date] = set()
    low = text.casefold()

    def add(y: int, m: int, d: int) -> None:
        with suppress(ValueError):  # not a real date (e.g. 13/45/2020): ignore
            found.add(date(y, m, d))

    for m, d, y in re.findall(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})(?!\d)", low):
        add(_year(y), int(m), int(d))
    for y, m, d in re.findall(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", low):
        add(int(y), int(m), int(d))
    for y, m, d in re.findall(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", low):
        add(int(y), int(m), int(d))
    for mon, d, y in re.findall(r"\b([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", low):
        if mon in MONTHS:
            add(int(y), MONTHS[mon], int(d))
    for d, mon, y in re.findall(r"\b(\d{1,2})[\s-]([a-z]{3,9})[\s-](\d{4}|\d{2})\b", low):
        if mon in MONTHS:
            add(_year(y), MONTHS[mon], int(d))
    return found


def amounts_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for raw in re.findall(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.,])\d+(?:\.\d+)?", text):
        try:
            found.add(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def _significant(name: str) -> list[str]:
    return [w for w in words(name) if w not in NAME_SUFFIXES]


def value_in_quote(field_name: str, value: str, quote: str) -> bool:
    if field_name == "contract_id":
        return alnum(value) != "" and alnum(value) in alnum(quote)
    if field_name in DATE_FIELDS:
        return date.fromisoformat(value) in dates_in(quote)
    if field_name == "contract_amount":
        return Decimal(value) in amounts_in(quote)
    quote_words = set(words(quote))
    tokens = _significant(value) or words(value)
    return bool(tokens) and all(t in quote_words for t in tokens)


def check_extraction(
    extraction: ContractExtraction,
    pages: list[PageText],
    usaspending_award: dict | None = None,
) -> CheckReport:
    by_page = {p.page: squash(p.text) for p in pages}
    results: dict[str, FieldCheck] = {}
    for name in ALL_FIELDS:
        ev = extraction.evidence(name)
        if ev.value is None:
            results[name] = FieldCheck(name, "missing")
            continue
        reasons: list[str] = []
        notes: list[str] = []
        page_text = by_page.get(ev.page)
        if page_text is None:
            reasons.append("page_out_of_range")
        elif squash(ev.quote) not in page_text:
            reasons.append("quote_not_on_page")
        if not value_in_quote(name, ev.value, ev.quote):
            reasons.append("value_not_in_quote")
        if usaspending_award is not None:
            reference = reference_check(name, ev.value, usaspending_award)
            if reference is not None:
                (reasons if name == "contract_id" else notes).append(reference)
        results[name] = FieldCheck(name, "failed" if reasons else "passed", tuple(reasons), tuple(notes))
    return CheckReport(results)


def reference_check(field_name: str, value: str, award: dict) -> str | None:
    """Compare a value with the USAspending award record; returns a reason, or None if it agrees
    (or the field has no USAspending counterpart)."""
    if field_name == "contract_id":
        piids = {alnum(award.get("piid") or "")}
        parent = award.get("parent_award") or {}
        piids.add(alnum(parent.get("piid") or ""))
        piids.discard("")
        return None if alnum(value) in piids else "contract_id_not_in_usaspending"
    if field_name == "supplier":
        recipient = (award.get("recipient") or {}).get("recipient_name") or ""
        same = set(_significant(value)) == set(_significant(recipient))
        return None if same else f"supplier_differs_from_usaspending:{recipient}"
    return None
