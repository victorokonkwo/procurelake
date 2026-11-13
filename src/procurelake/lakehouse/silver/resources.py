"""Where the Silver contracts and pipeline configs live, and loading the table specs.

Spark-free, so processes without Spark (the data-quality checks, ADR 0040) can read the same
specs the Silver Glue job applies.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from procurelake.contracts.reference import ReferenceContract, load_reference_contracts
from procurelake.contracts.source import load_contracts
from procurelake.lakehouse.silver.spec import SilverTableSpec, load_specs


def resource_root() -> Path:
    """Directory holding ``contracts/source`` and ``configs/pipelines/silver``.

    In a built wheel they are bundled under ``procurelake/_bundled``. In an editable checkout
    (local development, tests) they are read from the repository root.
    """
    bundled = Path(str(resources.files("procurelake"))) / "_bundled"
    if (bundled / "contracts" / "source").is_dir():
        return bundled
    repo = Path(__file__).resolve().parents[4]
    if (repo / "contracts" / "source").is_dir() and (repo / "pyproject.toml").is_file():
        return repo
    raise FileNotFoundError("contracts/source not found in the package or the repository")


def load_silver_specs(root: Path | None = None) -> dict[str, SilverTableSpec]:
    root = root or resource_root()
    return load_specs(
        load_contracts(root / "contracts" / "source"), root / "configs" / "pipelines" / "silver"
    )


def load_reference(root: Path | None = None) -> dict[str, ReferenceContract]:
    """Reference contracts (ADR 0044), keyed by contract name."""
    root = root or resource_root()
    return load_reference_contracts(root / "contracts" / "reference")
