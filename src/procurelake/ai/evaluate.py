"""Score extractions against ground-truth labels (``configs/ai/evaluation/ground_truth.yaml``).

Each (document, field) gets one outcome:

* ``correct``: the label has a value and the model's value matches it;
* ``correct_empty``: the label is empty (the document does not state it, or it is redacted)
  and the model left it empty;
* ``wrong``: both have a value and they differ;
* ``missed``: the label has a value and the model left it empty;
* ``fabricated``: the label is empty and the model filled it in. This is the outcome the AI
  governance rules care most about.

Matching: contract numbers compare letters and digits only; dates and amounts compare as
values; names and categories compare their significant words (corporate suffixes ignored)
against the label value or any of its listed ``accept`` variants.

The labels' own provenance (who labelled, who checked) is reported with every result, so a
score is never presented without saying what it was measured against.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from procurelake.ai.checks import _significant, alnum
from procurelake.ai.schema import ALL_FIELDS, DATE_FIELDS, DOCUMENT_TYPES

Outcome = Literal["correct", "correct_empty", "wrong", "missed", "fabricated"]


class LabelField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str | None
    page: int | None = None
    accept: list[str] = []
    note: str | None = None


class DocumentLabels(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_type: str
    fields: dict[str, LabelField]

    def model_post_init(self, _context) -> None:
        if self.document_type not in DOCUMENT_TYPES:
            raise ValueError(f"document_type {self.document_type!r} not in {DOCUMENT_TYPES}")
        missing = set(ALL_FIELDS) - set(self.fields)
        extra = set(self.fields) - set(ALL_FIELDS)
        if missing or extra:
            raise ValueError(f"labels must cover exactly the schema fields; missing {missing}, extra {extra}")


class GroundTruth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    labels_version: int
    labelled_by: str
    labelled_on: date
    checked_by: str | None
    checked_on: date | None
    method: str
    documents: dict[str, DocumentLabels]


def load_ground_truth(path: Path) -> GroundTruth:
    return GroundTruth.model_validate(yaml.safe_load(path.read_text()))


def matches(field_name: str, predicted: str, label: LabelField) -> bool:
    candidates = [v for v in [label.value, *label.accept] if v is not None]
    for candidate in candidates:
        if field_name == "contract_id" and alnum(predicted) == alnum(candidate):
            return True
        if field_name in DATE_FIELDS and date.fromisoformat(predicted) == date.fromisoformat(candidate):
            return True
        if field_name == "contract_amount" and Decimal(predicted) == Decimal(candidate):
            return True
        is_name = field_name not in ("contract_id", "contract_amount", *DATE_FIELDS)
        if is_name and set(_significant(predicted)) == set(_significant(candidate)):
            return True
    return False


def outcome(field_name: str, predicted: str | None, label: LabelField) -> Outcome:
    if label.value is None:
        return "correct_empty" if predicted is None else "fabricated"
    if predicted is None:
        return "missed"
    return "correct" if matches(field_name, predicted, label) else "wrong"


@dataclass
class DocumentScore:
    document_id: str
    extraction_id: str
    document_type_correct: bool
    outcomes: dict[str, Outcome]
    checks: dict[str, str]
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass
class EvaluationReport:
    ground_truth: GroundTruth
    documents: list[DocumentScore] = field(default_factory=list)
    unlabelled: list[str] = field(default_factory=list)
    not_extracted: list[str] = field(default_factory=list)

    def by_field(self) -> dict[str, Counter]:
        counts = {name: Counter() for name in ALL_FIELDS}
        for doc in self.documents:
            for name, result in doc.outcomes.items():
                counts[name][result] += 1
        return counts

    def summary(self) -> dict:
        totals: Counter = Counter()
        for counter in self.by_field().values():
            totals.update(counter)
        n = sum(totals.values())
        filled = sum(1 for d in self.documents for s in d.checks.values() if s != "missing")
        traced = sum(1 for d in self.documents for s in d.checks.values() if s == "passed")
        latencies = [d.latency_ms for d in self.documents]
        return {
            "documents_scored": len(self.documents),
            "field_judgements": n,
            "accuracy": round((totals["correct"] + totals["correct_empty"]) / n, 4) if n else None,
            "outcomes": dict(totals),
            "fabricated": totals["fabricated"],
            "document_type_accuracy": (
                round(sum(d.document_type_correct for d in self.documents) / len(self.documents), 4)
                if self.documents
                else None
            ),
            "filled_fields": filled,
            "filled_fields_passing_checks": traced,
            "latency_ms_median": median(latencies) if latencies else None,
            "latency_ms_max": max(latencies) if latencies else None,
            "input_tokens": sum(d.input_tokens for d in self.documents),
            "output_tokens": sum(d.output_tokens for d in self.documents),
            "cost_usd_estimate": str(sum((d.cost_usd for d in self.documents), Decimal(0))),
            "labels": {
                "labelled_by": self.ground_truth.labelled_by,
                "checked_by": self.ground_truth.checked_by,
                "method": self.ground_truth.method,
            },
            "unlabelled_documents": self.unlabelled,
            "labelled_documents_not_extracted": self.not_extracted,
        }


def evaluate(ground_truth: GroundTruth, items) -> EvaluationReport:
    """Score the latest extraction of every labelled document. ``items`` are review items
    (``procurelake.ai.review.ReviewItem``) in any status: scoring is of the model's output,
    before any reviewer correction."""
    latest = {}
    for item in sorted(items, key=lambda i: i.extraction_id):
        latest[item.document_id] = item
    report = EvaluationReport(ground_truth)
    report.unlabelled = sorted(set(latest) - set(ground_truth.documents))
    report.not_extracted = sorted(set(ground_truth.documents) - set(latest))
    for document_id in sorted(set(latest) & set(ground_truth.documents)):
        item, labels = latest[document_id], ground_truth.documents[document_id]
        extraction = item.extraction
        report.documents.append(
            DocumentScore(
                document_id=document_id,
                extraction_id=item.extraction_id,
                document_type_correct=extraction["document_type"] == labels.document_type,
                outcomes={
                    name: outcome(name, extraction[name]["value"], labels.fields[name]) for name in ALL_FIELDS
                },
                checks={name: item.checks[name]["status"] for name in ALL_FIELDS},
                latency_ms=int(item.record["latency_ms"]),
                input_tokens=int(item.record["input_tokens"]),
                output_tokens=int(item.record["output_tokens"]),
                cost_usd=Decimal(item.record["cost_usd_estimate"]),
            )
        )
    return report
