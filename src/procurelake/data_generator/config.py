"""Validated configuration for the synthetic data generator."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

TENANT_ID_PATTERN = re.compile(r"^tenant_[a-z][a-z0-9_]*$")  # mirrors tenants_tenant_id_format_chk
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")  # mirrors *_currency_code_chk


class TenantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    tenant_name: str = Field(min_length=1)
    agency_names: list[str] = Field(min_length=1)
    supplier_count: int = Field(gt=0)
    solicitation_count: int = Field(gt=0)
    currencies: dict[str, float] = Field(min_length=1)
    near_duplicate_supplier_rate: float = Field(ge=0, lt=1)
    open_market_po_ratio: float = Field(ge=0, lt=1)

    @field_validator("tenant_id")
    @classmethod
    def _tenant_id_format(cls, value: str) -> str:
        if not TENANT_ID_PATTERN.match(value):
            raise ValueError(f"tenant_id {value!r} must match {TENANT_ID_PATTERN.pattern}")
        return value

    @field_validator("agency_names")
    @classmethod
    def _agency_names_unique(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("agency names must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("agency names must be unique within a tenant")
        return value

    @field_validator("currencies")
    @classmethod
    def _currencies_valid(cls, value: dict[str, float]) -> dict[str, float]:
        for code, weight in value.items():
            if not CURRENCY_PATTERN.match(code):
                raise ValueError(f"currency code {code!r} must be three uppercase letters")
            if weight <= 0:
                raise ValueError(f"currency weight for {code} must be positive")
        return value


class GeneratorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: AwareDatetime
    history_start: AwareDatetime
    tenants: list[TenantConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> GeneratorConfig:
        if self.history_start >= self.as_of:
            raise ValueError("history_start must be before as_of")
        # The generator reserves the first 60 days for supplier onboarding.
        if (self.as_of - self.history_start).days < 120:
            raise ValueError("history window must be at least 120 days")
        ids = [t.tenant_id for t in self.tenants]
        if len(set(ids)) != len(ids):
            raise ValueError("tenant_id values must be unique")
        names = [t.tenant_name for t in self.tenants]
        if len(set(names)) != len(names):
            raise ValueError("tenant_name values must be unique")
        return self


def load_config(path: Path) -> GeneratorConfig:
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return GeneratorConfig.model_validate(raw)


def scaled(config: GeneratorConfig, scale: float) -> GeneratorConfig:
    """Return a copy with supplier and solicitation counts multiplied by ``scale``."""
    if scale <= 0:
        raise ValueError("scale must be positive")
    tenants = [
        t.model_copy(
            update={
                "supplier_count": max(1, round(t.supplier_count * scale)),
                "solicitation_count": max(1, round(t.solicitation_count * scale)),
            }
        )
        for t in config.tenants
    ]
    return config.model_copy(update={"tenants": tenants})
