"""Reference data contracts: public datasets kept apart from the synthetic tenants (ADR 0044).

A reference contract maps a public source's records (for example USAspending API fields) to
Silver columns. It differs from a source contract (``contracts/source``), which describes an
operational PostgreSQL table and is checked against the live database schema:

* each column names its ``source_field`` in the source record (``NAICS.code`` for a nested field);
* change capture is by a modification timestamp in the record (``change_column``), not CDC;
* there are no deletes: a public API that publishes only current records cannot report them;
* every monetary column is in the contract's ``currency_code`` (AGENTS.md section 2.4).

The compatibility rule is the source contracts' rule: within a major ``contract_version``, the
only change allowed is adding a nullable column.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

TYPE_PATTERN = re.compile(r"^(text|date|timestamp_ntz|numeric\(\d+,\d+\))$")
_NUMERIC = re.compile(r"^numeric\((\d+),(\d+)\)$")
_SOURCE_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(\.[A-Za-z][A-Za-z0-9_]*)?$")


class ReferenceColumn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_field: str
    type: str
    nullable: bool
    description: str = Field(min_length=1)
    monetary: bool = False

    @model_validator(mode="after")
    def _known(self) -> ReferenceColumn:
        if not TYPE_PATTERN.match(self.type):
            raise ValueError(f"column {self.name}: unsupported type {self.type!r}")
        if not _SOURCE_FIELD.match(self.source_field):
            raise ValueError(f"column {self.name}: invalid source_field {self.source_field!r}")
        if self.monetary and not _NUMERIC.match(self.type):
            raise ValueError(f"column {self.name}: a monetary column must be numeric(p,s)")
        return self

    @property
    def spark_type(self) -> str:
        if self.type == "text":
            return "string"
        if match := _NUMERIC.match(self.type):
            return f"decimal({match.group(1)},{match.group(2)})"
        return self.type  # date, timestamp_ntz

    @property
    def scale(self) -> int | None:
        match = _NUMERIC.match(self.type)
        return int(match.group(2)) if match else None


class ReferenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: int = Field(ge=1)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_system: str
    dataset: str
    bronze_schema_version: str = Field(min_length=1)
    silver_table: str = Field(pattern=r"^silver_[a-z][a-z0-9_]*$")
    owner: str
    description: str = Field(min_length=1)
    grain: str = Field(min_length=1)
    data_classification: Literal["public"]
    provenance: str = Field(min_length=1)
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
    primary_key: list[str] = Field(min_length=1)
    change_column: str
    deletes: Literal["none"]
    max_reject_ratio: float = Field(ge=0, le=1)
    columns: list[ReferenceColumn] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> ReferenceContract:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name}: duplicate column names")
        if "currency_code" in names:
            raise ValueError(f"{self.name}: currency_code is added from the contract; do not map it")
        by_name = {c.name: c for c in self.columns}
        for key in self.primary_key:
            if key not in by_name or by_name[key].nullable:
                raise ValueError(f"{self.name}: primary key column {key!r} must exist and be non-nullable")
        change = by_name.get(self.change_column)
        if change is None or change.nullable or change.type != "timestamp_ntz":
            raise ValueError(f"{self.name}: change_column must be a non-nullable timestamp_ntz column")
        return self

    @property
    def tenant_scoped(self) -> bool:
        return False


def load_reference_contract(path: Path) -> ReferenceContract:
    with path.open(encoding="utf-8") as handle:
        return ReferenceContract.model_validate(yaml.safe_load(handle))


def load_reference_contracts(directory: Path) -> dict[str, ReferenceContract]:
    contracts = [load_reference_contract(p) for p in sorted(directory.glob("*.yaml"))]
    return {c.name: c for c in contracts}
