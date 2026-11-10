"""Vocabulary and amount distributions for synthetic procurement data.

These values are illustrative parameters for generating plausible-looking synthetic data.
They are NOT calibrated to any real procurement dataset. Do not use them as statistics.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    name: str
    # Middle word of supplier names in this category, e.g. "Granite Ridge <Data Systems> Inc."
    supplier_cores: tuple[str, ...]
    solicitation_titles: tuple[str, ...]
    # Award amount ~ lognormal(median, sigma), in the award's currency units.
    award_median: float
    award_sigma: float


CATEGORIES: tuple[Category, ...] = (
    Category(
        "IT services",
        ("Data Systems", "Digital Solutions", "Network Services", "Software Group"),
        (
            "Network infrastructure refresh",
            "Cloud hosting services",
            "Service desk outsourcing",
            "Cybersecurity assessment",
            "Enterprise software licensing",
        ),
        250_000,
        0.9,
    ),
    Category(
        "Construction",
        ("Builders", "Construction", "Civil Works", "Contracting"),
        (
            "Road resurfacing program",
            "Facility roof replacement",
            "Bridge rehabilitation",
            "Accessibility upgrades",
            "Parking structure repairs",
        ),
        900_000,
        1.0,
    ),
    Category(
        "Professional services",
        ("Consulting", "Advisory Partners", "Management Group", "Strategy Associates"),
        (
            "Program evaluation",
            "Organizational review",
            "Policy research services",
            "Change management support",
            "Audit advisory services",
        ),
        120_000,
        0.8,
    ),
    Category(
        "Medical supplies",
        ("Medical Supply", "Health Products", "Clinical Distribution", "Care Supplies"),
        (
            "Personal protective equipment",
            "Diagnostic consumables",
            "Clinic furniture",
            "First aid kits",
            "Patient monitoring supplies",
        ),
        80_000,
        0.9,
    ),
    Category(
        "Facilities maintenance",
        ("Facility Services", "Maintenance Group", "Building Services", "Property Care"),
        (
            "Janitorial services",
            "HVAC maintenance",
            "Elevator servicing",
            "Grounds maintenance",
            "Pest control services",
        ),
        150_000,
        0.7,
    ),
    Category(
        "Fleet and vehicles",
        ("Fleet Services", "Motors", "Vehicle Solutions", "Equipment Rentals"),
        (
            "Light-duty vehicle purchase",
            "Fleet maintenance services",
            "Snow clearing equipment",
            "Transit bus parts",
            "Vehicle telematics",
        ),
        300_000,
        0.9,
    ),
    Category(
        "Office supplies",
        ("Office Supply", "Business Products", "Stationers", "Print Services"),
        (
            "Office stationery standing offer",
            "Printing services",
            "Office furniture",
            "Toner and consumables",
            "Mailroom supplies",
        ),
        25_000,
        0.7,
    ),
    Category(
        "Engineering",
        ("Engineering", "Design Group", "Technical Services", "Infrastructure Partners"),
        (
            "Water main design study",
            "Structural inspection services",
            "Environmental assessment",
            "Traffic engineering study",
            "Stormwater modelling",
        ),
        200_000,
        0.8,
    ),
)

# Generic, place-like name prefixes for fictional suppliers.
SUPPLIER_PREFIXES: tuple[str, ...] = (
    "Granite Ridge",
    "Cedar Point",
    "Harbour View",
    "Summit Line",
    "Prairie Gate",
    "Lakeshore",
    "North Field",
    "River Bend",
    "Iron Valley",
    "Blue Water",
    "Stone Bridge",
    "Pine Hollow",
    "Silver Creek",
    "Elm Crossing",
    "Westgate",
    "Eastbrook",
    "Highland",
    "Fox Run",
)

SUPPLIER_SUFFIXES: tuple[str, ...] = ("Inc.", "Ltd.", "Corp.", "LLP", "Co.")

# Median amount for purchase orders with no competitive award behind them.
OPEN_MARKET_PO_MEDIAN = 8_000.0
OPEN_MARKET_PO_SIGMA = 0.8
