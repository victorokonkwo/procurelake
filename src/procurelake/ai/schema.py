"""The contract extraction schema: what the model must return, and how it is validated.

Every field carries its evidence: the 1-based ``page`` it was read from and a ``quote`` copied
from that page. A field the document does not support is returned with ``value``, ``page``
and ``quote`` all null; it is never filled in. Values are strings exactly as normalised by
the prompt rules (ISO dates, plain decimal amounts), so the deterministic checks can compare
them with the quote.

``json_schema()`` is the schema sent to Bedrock as the structured-output format; it is also
committed as ``configs/ai/extraction_schema.json`` (a unit test keeps the two identical).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "contract_extraction.v1"

AMOUNT_TYPES = (
    "total_contract_value",  # value of the whole contract or order, including options if stated
    "obligated_amount",  # total funds obligated to date
    "modification_change",  # the increase or decrease made by this modification
    "other",
)
DOCUMENT_TYPES = ("award", "modification", "order", "other")
TEXT_FIELDS = (
    "contract_id",
    "supplier",
    "issuing_agency",
    "effective_date",
    "performance_start_date",
    "performance_end_date",
    "contract_category",
)
DATE_FIELDS = ("effective_date", "performance_start_date", "performance_end_date")
ALL_FIELDS = (*TEXT_FIELDS, "contract_amount")
FIELD_DESCRIPTIONS = {
    "contract_id": "The contract, order or agreement number (PIID) the document belongs to, "
    "as printed. For an order, the order number, not the parent contract's.",
    "supplier": "The contractor's name as printed.",
    "issuing_agency": "The government agency or office that issued the document.",
    "effective_date": "The date the document takes effect, as YYYY-MM-DD.",
    "performance_start_date": "Start of the period of performance, as YYYY-MM-DD.",
    "performance_end_date": "End of the period of performance, as YYYY-MM-DD.",
    "contract_category": "What is being bought: the product or service code (PSC), NAICS code, "
    "or the short description of the supplies or services, as printed.",
    "contract_amount": "The most specific total amount the document states, as a plain decimal "
    "number (no symbols or commas), with its ISO 4217 currency and which kind of amount it is.",
}


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str | None
    page: int | None = Field(default=None, ge=1)
    quote: str | None

    @model_validator(mode="after")
    def _evidence_matches_value(self):
        has = [self.value is not None, self.page is not None, self.quote is not None]
        if any(has) and not all(has):
            raise ValueError("value, page and quote must be all present or all null")
        if self.quote is not None and not self.quote.strip():
            raise ValueError("quote must not be blank")
        if self.value is not None and not self.value.strip():
            raise ValueError("value must not be blank; use null when the document does not say")
        return self


class TextField(_Evidence):
    pass


class DateField(_Evidence):
    @field_validator("value")
    @classmethod
    def _iso(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)  # raises ValueError if not YYYY-MM-DD
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"date must be YYYY-MM-DD: {value!r}")
        return value


class AmountField(_Evidence):
    currency: str | None
    amount_type: Literal[AMOUNT_TYPES] | None  # type: ignore[valid-type]

    @field_validator("value")
    @classmethod
    def _decimal(cls, value: str | None) -> str | None:
        if value is not None:
            if not re.fullmatch(r"-?\d+(\.\d+)?", value):
                raise ValueError(f"amount must be a plain decimal number: {value!r}")
            try:
                Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"not a decimal: {value!r}") from exc
        return value

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError(f"currency must be an ISO 4217 code: {value!r}")
        return value

    @model_validator(mode="after")
    def _amount_complete(self):
        if self.value is None and (self.currency is not None or self.amount_type is not None):
            raise ValueError("currency and amount_type must be null when there is no amount")
        if self.value is not None and (self.currency is None or self.amount_type is None):
            raise ValueError("an amount needs its currency and amount_type")
        return self


class ContractExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_type: Literal[DOCUMENT_TYPES]  # type: ignore[valid-type]
    contract_id: TextField
    supplier: TextField
    issuing_agency: TextField
    effective_date: DateField
    performance_start_date: DateField
    performance_end_date: DateField
    contract_category: TextField
    contract_amount: AmountField

    def evidence(self, name: str) -> _Evidence:
        return getattr(self, name)


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _evidence_schema(description: str, value_schema: dict, extra: dict | None = None) -> dict:
    properties = {
        "value": _nullable(value_schema),
        "page": _nullable({"type": "integer", "description": "1-based page number of the quote."}),
        "quote": _nullable(
            {"type": "string", "description": "Text copied exactly from that page that shows the value."}
        ),
        **(extra or {}),
    }
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def json_schema() -> dict:
    """The structured-output schema (JSON Schema subset accepted by Claude structured outputs)."""
    string = {"type": "string"}
    properties: dict[str, dict] = {
        "document_type": {
            "type": "string",
            "enum": list(DOCUMENT_TYPES),
            "description": "award, modification, order, or other.",
        }
    }
    for name in TEXT_FIELDS:
        properties[name] = _evidence_schema(FIELD_DESCRIPTIONS[name], string)
    properties["contract_amount"] = _evidence_schema(
        FIELD_DESCRIPTIONS["contract_amount"],
        string,
        extra={
            "currency": _nullable({"type": "string", "description": "ISO 4217 code, e.g. USD."}),
            "amount_type": _nullable({"type": "string", "enum": list(AMOUNT_TYPES)}),
        },
    )
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
