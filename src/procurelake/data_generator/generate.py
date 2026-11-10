"""Deterministic synthetic procurement data generation.

Guarantees, verified by ``tests/unit/data_generator``:

* **Repeatable.** The same config and seed always produce identical rows. Each tenant has its
  own random stream (seeded from ``seed`` + ``tenant_id``), so adding a tenant does not change
  the others' data.
* **Same-tenant relationships.** Every foreign key points to a row of the same tenant.
* **Timeline-consistent.** Each record's status comes from its simulated timeline relative to
  ``as_of``, so nothing that happens after ``as_of`` is visible, and statuses agree with
  timestamps and amounts.
* **Money is consistent.** Purchase orders under an award never exceed the award amount.
  Invoices never exceed their purchase order. Every monetary row carries its currency, and
  invoices inherit the purchase order's currency.

Monetary amounts and distributions are illustrative only (see ``catalog``).
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from procurelake.data_generator import catalog
from procurelake.data_generator.config import GeneratorConfig, TenantConfig
from procurelake.data_generator.model import (
    Agency,
    Award,
    Dataset,
    Invoice,
    PurchaseOrder,
    Solicitation,
    Supplier,
    Tenant,
)

CENT = Decimal("0.01")
SUPPLIER_ONBOARDING_DAYS = 60

# Lifecycle probabilities (illustrative, not calibrated).
P_SOLICITATION_CANCELLED = 0.05
P_TWO_LOT_AWARD = 0.10
P_AWARD_CANCELLED = 0.03
P_PO_CANCELLED = 0.02
P_FULLY_INVOICED = 0.60
P_INVOICE_CANCELLED = 0.02
P_INVOICE_PARTIALLY_PAID = 0.10
SUPPLIER_POPULARITY_EXPONENT = 0.8  # Zipf-like skew so supplier concentration is non-trivial


def generate_dataset(config: GeneratorConfig, seed: int) -> Dataset:
    """Generate rows for every tenant in ``config``."""
    dataset = Dataset()
    for tenant_config in config.tenants:
        rng = random.Random(f"procurelake:{seed}:{tenant_config.tenant_id}")  # noqa: S311 - not crypto
        generator = _TenantGenerator(
            rng=rng,
            tenant=tenant_config,
            history_start=_utc_seconds(config.history_start),
            as_of=_utc_seconds(config.as_of),
        )
        dataset.extend(generator.generate())
    return dataset


def _utc_seconds(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


@dataclass
class _AwardContext:
    award: Award
    agency_id: uuid.UUID


class _TenantGenerator:
    def __init__(self, rng: random.Random, tenant: TenantConfig, history_start: datetime, as_of: datetime):
        self.rng = rng
        self.cfg = tenant
        self.tenant_id = tenant.tenant_id
        self.history_start = history_start
        self.as_of = as_of
        self.onboarding_end = history_start + timedelta(days=SUPPLIER_ONBOARDING_DAYS)
        self.out = Dataset()
        self.currency_codes = list(tenant.currencies)
        self.currency_weights = [tenant.currencies[c] for c in self.currency_codes]

    # ------------------------------------------------------------------ helpers
    def _uuid(self) -> uuid.UUID:
        return uuid.UUID(int=self.rng.getrandbits(128), version=4)

    def _between(self, start: datetime, end: datetime) -> datetime:
        span = int((end - start).total_seconds())
        return start + timedelta(seconds=self.rng.randint(0, max(span, 0)))

    def _days_after(self, start: datetime, low: float, high: float) -> datetime:
        return start + timedelta(seconds=int(self.rng.uniform(low, high) * 86_400))

    def _lognormal_amount(self, median: float, sigma: float, minimum: float = 500.0) -> Decimal:
        value = median * math.exp(sigma * self.rng.gauss(0.0, 1.0))
        return Decimal(str(max(value, minimum))).quantize(CENT, rounding=ROUND_HALF_UP)

    def _split(self, total: Decimal, parts: int) -> list[Decimal]:
        """Split ``total`` into ``parts`` positive cent amounts that sum exactly to ``total``."""
        if parts <= 1 or total < CENT * parts * 100:
            return [total]
        weights = [self.rng.uniform(0.5, 1.5) for _ in range(parts)]
        weight_sum = sum(weights)
        amounts = [(total * Decimal(str(w / weight_sum))).quantize(CENT) for w in weights[:-1]]
        amounts.append(total - sum(amounts))
        return amounts if all(a > 0 for a in amounts) else [total]

    def _currency(self) -> str:
        return self.rng.choices(self.currency_codes, weights=self.currency_weights)[0]

    def _clip(self, ts: datetime) -> datetime:
        return min(ts, self.as_of)

    def _timestamp_on(self, day: date) -> datetime:
        """A business-hours timestamp on ``day``, never later than as_of."""
        ts = datetime.combine(day, time(8, 0), tzinfo=UTC) + timedelta(minutes=self.rng.randint(0, 600))
        return self._clip(ts)

    # ------------------------------------------------------------------ generation
    def generate(self) -> Dataset:
        self._tenant()
        agency_ids = self._agencies()
        suppliers_by_category = self._suppliers()
        award_contexts = self._solicitations_and_awards(agency_ids, suppliers_by_category)
        self._purchase_orders_and_invoices(award_contexts, agency_ids, suppliers_by_category)
        return self.out

    def _tenant(self) -> None:
        self.out.tenants.append(
            Tenant(
                tenant_id=self.tenant_id,
                tenant_name=self.cfg.tenant_name,
                status="active",
                is_synthetic=True,
                created_at=self.history_start,
                updated_at=self.history_start,
            )
        )

    def _agencies(self) -> list[uuid.UUID]:
        ids = []
        for name in self.cfg.agency_names:
            agency_id = self._uuid()
            self.out.agencies.append(
                Agency(agency_id, self.tenant_id, name, self.history_start, self.history_start)
            )
            ids.append(agency_id)
        return ids

    def _supplier_name(self, category: catalog.Category, used: set[str]) -> str:
        for _ in range(50):
            name = (
                f"{self.rng.choice(catalog.SUPPLIER_PREFIXES)} "
                f"{self.rng.choice(category.supplier_cores)} "
                f"{self.rng.choice(catalog.SUPPLIER_SUFFIXES)}"
            )
            if name not in used:
                return name
        # Vocabulary exhausted (large scale factors): add a distinguishing number.
        base = f"{self.rng.choice(catalog.SUPPLIER_PREFIXES)} {self.rng.choice(category.supplier_cores)}"
        n = 2
        while f"{base} {n} Ltd." in used:
            n += 1
        return f"{base} {n} Ltd."

    def _near_duplicate_name(self, name: str) -> str:
        """A plausible alternate spelling of an existing supplier name."""
        variants = [
            name.rstrip("."),
            name.upper(),
            name.replace("Ltd.", "Limited").replace("Inc.", "Incorporated").replace("Corp.", "Corporation"),
            name.replace(" ", "  ", 1),
            f"{name} ",
        ]
        candidates = [v for v in variants if v != name]
        return self.rng.choice(candidates)

    def _suppliers(self) -> dict[str, list[Supplier]]:
        by_category: dict[str, list[Supplier]] = {c.name: [] for c in catalog.CATEGORIES}
        used: set[str] = set()
        for _ in range(self.cfg.supplier_count):
            category = self.rng.choice(catalog.CATEGORIES)
            name = self._supplier_name(category, used)
            used.add(name)
            created = self._between(self.history_start, self.onboarding_end)
            supplier = Supplier(self._uuid(), self.tenant_id, name, category.name, created, created)
            by_category[category.name].append(supplier)
            self.out.suppliers.append(supplier)

        originals = list(self.out.suppliers)
        for _ in range(round(self.cfg.supplier_count * self.cfg.near_duplicate_supplier_rate)):
            source = self.rng.choice(originals)
            created = self._between(source.created_at, self.onboarding_end)
            duplicate = Supplier(
                self._uuid(),
                self.tenant_id,
                self._near_duplicate_name(source.supplier_name),
                source.category,
                created,
                created,
            )
            by_category[source.category].append(duplicate)
            self.out.suppliers.append(duplicate)
        return by_category

    def _pick_supplier(self, pool: list[Supplier], exclude: set[uuid.UUID] = frozenset()) -> Supplier:
        candidates = [s for s in pool if s.supplier_id not in exclude] or pool
        weights = [1.0 / (rank + 1) ** SUPPLIER_POPULARITY_EXPONENT for rank in range(len(candidates))]
        return self.rng.choices(candidates, weights=weights)[0]

    def _supplier_pool(self, by_category: dict[str, list[Supplier]], category: str) -> list[Supplier]:
        return by_category[category] or self.out.suppliers

    def _solicitations_and_awards(
        self, agency_ids: list[uuid.UUID], by_category: dict[str, list[Supplier]]
    ) -> list[_AwardContext]:
        contexts: list[_AwardContext] = []
        for _ in range(self.cfg.solicitation_count):
            category = self.rng.choice(catalog.CATEGORIES)
            agency_id = self.rng.choice(agency_ids)
            title = self.rng.choice(category.solicitation_titles)
            currency = self._currency()

            created = self._between(self.onboarding_end, self.as_of)
            published = self._days_after(created, 1, 10)
            closing = self._days_after(published, 14, 45)
            award_time = self._days_after(closing, 5, 40)

            published_at: datetime | None = published
            closing_at: datetime | None = closing
            if published > self.as_of:
                status, updated = "draft", created
                published_at = closing_at = None
            elif self.rng.random() < P_SOLICITATION_CANCELLED:
                status = "cancelled"
                updated = self._clip(self._between(published, closing))
            elif closing > self.as_of:
                status, updated = "published", published
            elif award_time > self.as_of:
                status, updated = "closed", closing
            else:
                status, updated = "awarded", award_time

            solicitation = Solicitation(
                solicitation_id=self._uuid(),
                tenant_id=self.tenant_id,
                agency_id=agency_id,
                title=title,
                status=status,
                published_at=published_at,
                closing_at=closing_at,
                created_at=created,
                updated_at=updated,
            )
            self.out.solicitations.append(solicitation)

            if status == "awarded":
                contexts.extend(self._awards(solicitation, category, currency, award_time, by_category))
        return contexts

    def _awards(
        self,
        solicitation: Solicitation,
        category: catalog.Category,
        currency: str,
        awarded_at: datetime,
        by_category: dict[str, list[Supplier]],
    ) -> list[_AwardContext]:
        lots = 2 if self.rng.random() < P_TWO_LOT_AWARD else 1
        pool = self._supplier_pool(by_category, category.name)
        chosen: set[uuid.UUID] = set()
        contexts = []
        for _ in range(lots):
            supplier = self._pick_supplier(pool, exclude=chosen)
            chosen.add(supplier.supplier_id)
            status, updated = "active", awarded_at
            if self.rng.random() < P_AWARD_CANCELLED:
                cancelled_at = self._days_after(awarded_at, 1, 60)
                if cancelled_at <= self.as_of:
                    status, updated = "cancelled", cancelled_at
            award = Award(
                award_id=self._uuid(),
                tenant_id=self.tenant_id,
                solicitation_id=solicitation.solicitation_id,
                supplier_id=supplier.supplier_id,
                award_amount=self._lognormal_amount(category.award_median, category.award_sigma, 1_000.0),
                currency_code=currency,
                awarded_at=awarded_at,
                status=status,
                created_at=awarded_at,
                updated_at=updated,
            )
            self.out.awards.append(award)
            contexts.append(_AwardContext(award, solicitation.agency_id))
        return contexts

    def _purchase_orders_and_invoices(
        self,
        award_contexts: list[_AwardContext],
        agency_ids: list[uuid.UUID],
        by_category: dict[str, list[Supplier]],
    ) -> None:
        award_po_count = 0
        for ctx in award_contexts:
            award = ctx.award
            if award.status != "active":
                continue
            committed = (award.award_amount * Decimal(str(self.rng.uniform(0.5, 1.0)))).quantize(CENT)
            order_ts = award.awarded_at
            for amount in self._split(committed, self.rng.randint(1, 3)):
                order_ts = self._days_after(order_ts, 1, 30)
                if order_ts > self.as_of:
                    break
                self._purchase_order(
                    agency_id=ctx.agency_id,
                    supplier_id=award.supplier_id,
                    award_id=award.award_id,
                    order_date=order_ts.date(),
                    amount=amount,
                    currency=award.currency_code,
                )
                award_po_count += 1

        ratio = self.cfg.open_market_po_ratio
        open_market_count = round(award_po_count * ratio / (1 - ratio)) if award_po_count else 0
        for _ in range(open_market_count):
            category = self.rng.choice(catalog.CATEGORIES)
            supplier = self._pick_supplier(self._supplier_pool(by_category, category.name))
            order_day = self._between(max(self.onboarding_end, supplier.created_at), self.as_of).date()
            self._purchase_order(
                agency_id=self.rng.choice(agency_ids),
                supplier_id=supplier.supplier_id,
                award_id=None,
                order_date=order_day,
                amount=self._lognormal_amount(catalog.OPEN_MARKET_PO_MEDIAN, catalog.OPEN_MARKET_PO_SIGMA),
                currency=self._currency(),
            )

    def _purchase_order(
        self,
        *,
        agency_id: uuid.UUID,
        supplier_id: uuid.UUID,
        award_id: uuid.UUID | None,
        order_date: date,
        amount: Decimal,
        currency: str,
    ) -> None:
        po_id = self._uuid()
        created = self._timestamp_on(order_date)

        if self.rng.random() < P_PO_CANCELLED:
            status, updated = "cancelled", self._clip(self._days_after(created, 1, 10))
            invoices: list[Invoice] = []
        else:
            invoices = self._invoices(po_id, created, amount, currency)
            active = [i for i in invoices if i.payment_status != "cancelled"]
            fully_paid = (
                bool(active)
                and sum(i.invoice_amount for i in active) == amount
                and all(i.payment_status == "paid" for i in active)
            )
            if fully_paid:
                status = "closed"
                updated = max(i.paid_at for i in active if i.paid_at is not None)
            else:
                status, updated = "open", created

        self.out.purchase_orders.append(
            PurchaseOrder(
                po_id=po_id,
                tenant_id=self.tenant_id,
                agency_id=agency_id,
                supplier_id=supplier_id,
                award_id=award_id,
                order_date=order_date,
                amount=amount,
                currency_code=currency,
                status=status,
                created_at=created,
                updated_at=updated,
            )
        )
        self.out.invoices.extend(invoices)

    def _invoices(
        self, po_id: uuid.UUID, po_created: datetime, po_amount: Decimal, currency: str
    ) -> list[Invoice]:
        share = (
            Decimal("1") if self.rng.random() < P_FULLY_INVOICED else Decimal(str(self.rng.uniform(0.3, 0.9)))
        )
        to_invoice = (po_amount * share).quantize(CENT)
        invoices = []
        invoice_ts = po_created
        for amount in self._split(to_invoice, self.rng.randint(1, 3)):
            invoice_ts = self._days_after(invoice_ts, 7, 45)
            if invoice_ts > self.as_of:
                break
            paid_amount, payment_status, paid_at, updated = Decimal("0.00"), "pending", None, invoice_ts
            roll = self.rng.random()
            if roll < P_INVOICE_CANCELLED:
                payment_status = "cancelled"
                updated = self._clip(self._days_after(invoice_ts, 1, 10))
            else:
                payment_ts = self._days_after(invoice_ts, 5, 60)
                if payment_ts <= self.as_of:
                    paid_at = updated = payment_ts
                    if self.rng.random() < P_INVOICE_PARTIALLY_PAID and amount > Decimal("1.00"):
                        payment_status = "partially_paid"
                        paid_amount = (amount * Decimal(str(self.rng.uniform(0.2, 0.8)))).quantize(CENT)
                        paid_amount = min(max(paid_amount, CENT), amount - CENT)
                    else:
                        payment_status, paid_amount = "paid", amount
            invoices.append(
                Invoice(
                    invoice_id=self._uuid(),
                    tenant_id=self.tenant_id,
                    po_id=po_id,
                    invoice_date=invoice_ts.date(),
                    invoice_amount=amount,
                    paid_amount=paid_amount,
                    currency_code=currency,
                    payment_status=payment_status,
                    paid_at=paid_at,
                    created_at=invoice_ts,
                    updated_at=updated,
                )
            )
        return invoices
