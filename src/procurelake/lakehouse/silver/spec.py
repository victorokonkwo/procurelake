"""Silver table specifications: a source contract plus the pipeline's business rules.

The contract supplies columns, types, the primary key, nullability, and allowed values. The
pipeline config (``configs/pipelines/silver/<table>.yaml``) adds row rules as Spark SQL boolean
expressions, the quarantine threshold, and the partitioning. Each rule mirrors a CHECK
constraint in the operational database: the same rules apply whatever later feeds Bronze
(e.g. DMS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from procurelake.contracts.source import SourceContract

_CHAR = re.compile(r"^char\((\d+)\)$")
_NUMERIC = re.compile(r"^numeric\((\d+),(\d+)\)$")


def spark_type(contract_type: str) -> str:
    """Spark SQL type for a contract column type."""
    if contract_type in {"uuid", "text"} or _CHAR.match(contract_type):
        return "string"
    if match := _NUMERIC.match(contract_type):
        return f"decimal({match.group(1)},{match.group(2)})"
    mapping = {"boolean": "boolean", "date": "date", "timestamptz": "timestamp"}
    if contract_type not in mapping:
        raise ValueError(f"no Spark type for contract type {contract_type!r}")
    return mapping[contract_type]


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    sql: str = Field(min_length=1, description="Spark SQL boolean expression; true = row is valid")


class SilverPipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_table: str
    silver_table: str = Field(pattern=r"^silver_[a-z][a-z0-9_]*$")
    partition_by: list[str] = Field(default_factory=list)
    max_reject_ratio: float = Field(ge=0, le=1)
    rules: list[Rule] = Field(default_factory=list)


@dataclass(frozen=True)
class SilverTableSpec:
    contract: SourceContract
    config: SilverPipelineConfig

    @property
    def source_table(self) -> str:
        return self.contract.name

    @property
    def silver_table(self) -> str:
        return self.config.silver_table

    @property
    def primary_key(self) -> list[str]:
        return self.contract.primary_key

    @property
    def columns(self) -> list[tuple[str, str]]:
        """(name, Spark type) in contract order."""
        return [(c.name, spark_type(c.type)) for c in self.contract.columns]

    @property
    def char_lengths(self) -> dict[str, int]:
        return {c.name: int(m.group(1)) for c in self.contract.columns if (m := _CHAR.match(c.type))}

    @property
    def not_null(self) -> list[str]:
        return [c.name for c in self.contract.columns if not c.nullable]

    @property
    def allowed_values(self) -> dict[str, list[str]]:
        return {c.name: c.allowed_values for c in self.contract.columns if c.allowed_values}

    @property
    def change_column(self) -> str:
        return self.contract.change_capture.source_updated_at_column

    def ddl_schema(self) -> str:
        return ", ".join(f"`{name}` {stype}" for name, stype in self.columns)


def load_spec(contract: SourceContract, config_path: Path) -> SilverTableSpec:
    with config_path.open(encoding="utf-8") as handle:
        config = SilverPipelineConfig.model_validate(yaml.safe_load(handle))
    if config.source_table != contract.name:
        raise ValueError(
            f"{config_path.name}: source_table {config.source_table!r} != contract {contract.name!r}"
        )
    unknown = [c for c in config.partition_by if c not in {col.name for col in contract.columns}]
    if unknown:
        raise ValueError(f"{config_path.name}: partition columns not in contract: {unknown}")
    return SilverTableSpec(contract=contract, config=config)


def load_specs(contracts: list[SourceContract], config_dir: Path) -> dict[str, SilverTableSpec]:
    by_name = {c.name: c for c in contracts}
    specs = {}
    for path in sorted(config_dir.glob("*.yaml")):
        with path.open(encoding="utf-8") as handle:
            source = yaml.safe_load(handle)["source_table"]
        if source not in by_name:
            raise ValueError(f"{path.name}: no source contract named {source!r}")
        specs[source] = load_spec(by_name[source], path)
    return specs
