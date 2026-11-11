"""The human review queue (DynamoDB ``procurelake-<env>-ai-review``).

One item per extraction: ``pk`` = document id, ``sk`` = extraction id (time-ordered). An item
is created ``pending_review`` and decided exactly once (a conditional update), by a named
reviewer, as ``approved`` or ``rejected``. Only approved items reach Silver and Gold.

Field decisions on an approved document:

* ``accept``: keep the model's value (allowed only when the field's checks passed, so a value
  whose evidence could not be traced is never accepted as is);
* ``correct``: the reviewer's value replaces it (it must name the page it was read from, and
  passes the same type rules as model output: ISO dates, decimal amounts, ISO currency);
* ``reject``: the field is left empty in Gold.

The decision records the reviewer's name and the AWS identity (STS caller ARN) that made it.

Fields the model left empty stay empty unless the reviewer corrects them. Every field the
model filled must be decided; ``accept_passed`` accepts all fields whose checks passed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from boto3.dynamodb.conditions import Attr
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from procurelake.ai.schema import ALL_FIELDS, AmountField, DateField, TextField

PENDING = "pending_review"


class ReviewError(ValueError):
    pass


class FieldDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["accept", "correct", "reject"]
    value: str | None = None
    page: int | None = Field(default=None, ge=1)
    quote: str | None = None
    currency: str | None = None
    amount_type: str | None = None

    @model_validator(mode="after")
    def _shape(self):
        if self.action == "correct":
            if self.value is None or self.page is None:
                raise ValueError("a correction needs a value and the page it was read from")
        elif any(v is not None for v in (self.value, self.page, self.quote, self.currency, self.amount_type)):
            raise ValueError(f"{self.action} takes no value")
        return self


@dataclass(frozen=True)
class ReviewItem:
    document_id: str
    extraction_id: str
    status: str
    record: dict  # everything stored on the item, JSON-decoded

    @property
    def extraction(self) -> dict:
        return self.record["extraction"]

    @property
    def checks(self) -> dict:
        return self.record["checks"]


_JSON_ATTRS = ("extraction", "checks", "decisions", "provenance")


def _to_item(record: dict) -> dict:
    return {k: json.dumps(v, sort_keys=True) if k in _JSON_ATTRS else v for k, v in record.items()}


def _from_item(item: dict) -> ReviewItem:
    record = {k: json.loads(v) if k in _JSON_ATTRS and isinstance(v, str) else v for k, v in item.items()}
    return ReviewItem(item["pk"], item["sk"], item["status"], record)


def enqueue(table, record: dict) -> None:
    """Create a pending item. ``record`` holds pk, sk and the extraction, checks, provenance."""
    item = _to_item({**record, "status": PENDING})
    table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists() & Attr("sk").not_exists())


def get(table, document_id: str, extraction_id: str) -> ReviewItem:
    item = table.get_item(Key={"pk": document_id, "sk": extraction_id}, ConsistentRead=True).get("Item")
    if item is None:
        raise ReviewError(f"no review item {document_id}/{extraction_id}")
    return _from_item(item)


def list_items(table, status: str | None = None) -> list[ReviewItem]:
    """All items (the queue holds one item per extraction, so a scan stays small)."""
    kwargs = {"FilterExpression": Attr("status").eq(status)} if status else {}
    items: list[dict] = []
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted((_from_item(i) for i in items), key=lambda r: (r.document_id, r.extraction_id))


def _validate_correction(field_name: str, decision: FieldDecision) -> None:
    evidence = {"value": decision.value, "page": decision.page, "quote": decision.quote or "(reviewer)"}
    try:
        if field_name == "contract_amount":
            AmountField(**evidence, currency=decision.currency, amount_type=decision.amount_type)
        elif field_name in ("effective_date", "performance_start_date", "performance_end_date"):
            DateField(**evidence)
        else:
            TextField(**evidence)
    except ValidationError as exc:
        raise ReviewError(f"correction for {field_name} is invalid: {exc}") from exc


def resolve_decisions(
    item: ReviewItem, decisions: dict[str, FieldDecision], accept_passed: bool
) -> dict[str, FieldDecision]:
    """Complete and validate the field decisions for an approval."""
    unknown = set(decisions) - set(ALL_FIELDS)
    if unknown:
        raise ReviewError(f"unknown fields: {sorted(unknown)}")
    resolved: dict[str, FieldDecision] = {}
    for name in ALL_FIELDS:
        status = item.checks[name]["status"]
        decision = decisions.get(name)
        if decision is None and accept_passed and status == "passed":
            decision = FieldDecision(action="accept")
        if decision is None:
            if status == "missing":
                continue
            raise ReviewError(f"{name} ({status}) needs a decision: accept, correct or reject")
        if decision.action == "accept" and status != "passed":
            raise ReviewError(f"{name} cannot be accepted as is: its checks are {status}")
        if decision.action == "correct":
            _validate_correction(name, decision)
        resolved[name] = decision
    return resolved


def decide(
    table,
    item: ReviewItem,
    reviewer: str,
    reviewer_arn: str,
    outcome: Literal["approved", "rejected"],
    decisions: dict[str, FieldDecision] | None = None,
    accept_passed: bool = False,
    comment: str | None = None,
    now=lambda: datetime.now(UTC),
) -> ReviewItem:
    if not reviewer.strip():
        raise ReviewError("a reviewer name is required")
    if item.status != PENDING:
        raise ReviewError(f"{item.document_id}/{item.extraction_id} is already {item.status}")
    resolved: dict[str, FieldDecision] = {}
    if outcome == "approved":
        resolved = resolve_decisions(item, decisions or {}, accept_passed)
    elif decisions:
        raise ReviewError("a rejected document takes no field decisions")
    decided_at = now().isoformat()
    payload = {k: v.model_dump(exclude_none=True) for k, v in resolved.items()}
    table.update_item(
        Key={"pk": item.document_id, "sk": item.extraction_id},
        UpdateExpression=(
            "SET #s = :s, reviewer = :r, reviewer_arn = :a, decided_at = :t,"
            " decisions = :d, review_comment = :c"
        ),
        ConditionExpression=Attr("status").eq(PENDING),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": outcome,
            ":r": reviewer.strip(),
            ":a": reviewer_arn,
            ":t": decided_at,
            ":d": json.dumps(payload, sort_keys=True),
            ":c": comment or "",
        },
    )
    return get(table, item.document_id, item.extraction_id)
