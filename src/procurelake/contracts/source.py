"""Source data contracts: the agreed shape and semantics of a source table.

A contract is the reference that ingestion, CDC, and Silver use for keys, types, nullability,
tenant scoping, and how changes are captured. Contracts for the operational PostgreSQL
database are checked against the live catalog in ``database/postgres/tests``, so a contract
cannot drift from the schema without a test failing.

Compatibility: within a major ``contract_version``, the only change allowed is adding a
nullable column. Removing or renaming a column, changing a type, or changing nullability or
keys requires a new major version.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Column types as written in contracts (PostgreSQL spellings used by the migrations).
TYPE_PATTERN = re.compile(r"^(uuid|text|boolean|date|timestamptz|char\(\d+\)|numeric\(\d+,\d+\))$")


class Column(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: str
    nullable: bool
    description: str = Field(min_length=1)
    allowed_values: list[str] | None = None

    @model_validator(mode="after")
    def _type_known(self) -> Column:
        if not TYPE_PATTERN.match(self.type):
            raise ValueError(f"column {self.name}: unsupported type {self.type!r}")
        return self


class ForeignKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    columns: list[str] = Field(min_length=1)
    references_table: str
    references_columns: list[str] = Field(min_length=1)


class ChangeCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["cdc"]
    source_updated_at_column: str
    deletes: Literal["hard", "soft", "none"]


class PhysicalLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system: Literal["postgresql"]
    schema_name: str = Field(alias="schema")
    table: str


class SourceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    contract_version: int = Field(ge=1)
    name: str
    source_system: str
    physical: PhysicalLocation
    owner: str
    description: str = Field(min_length=1)
    grain: str = Field(min_length=1)
    data_classification: Literal["synthetic", "public", "internal", "confidential"]
    tenant_scoped: bool
    primary_key: list[str] = Field(min_length=1)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    change_capture: ChangeCapture
    columns: list[Column] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> SourceContract:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name}: duplicate column names")
        by_name = {c.name: c for c in self.columns}
        for key in self.primary_key:
            if key not in by_name or by_name[key].nullable:
                raise ValueError(f"{self.name}: primary key column {key!r} must exist and be non-nullable")
        for fk in self.foreign_keys:
            missing = [c for c in fk.columns if c not in by_name]
            if missing:
                raise ValueError(f"{self.name}: foreign key columns {missing} are not declared")
            if len(fk.columns) != len(fk.references_columns):
                raise ValueError(f"{self.name}: foreign key column counts differ")
        if self.tenant_scoped and ("tenant_id" not in by_name or by_name["tenant_id"].nullable):
            raise ValueError(f"{self.name}: tenant-scoped contracts need a non-nullable tenant_id")
        updated = self.change_capture.source_updated_at_column
        if updated not in by_name or by_name[updated].type != "timestamptz":
            raise ValueError(f"{self.name}: change_capture column {updated!r} must be a declared timestamptz")
        return self


def load_contract(path: Path) -> SourceContract:
    with path.open(encoding="utf-8") as handle:
        return SourceContract.model_validate(yaml.safe_load(handle))


def load_contracts(directory: Path) -> list[SourceContract]:
    return [load_contract(p) for p in sorted(directory.glob("*.yaml"))]
