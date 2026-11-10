-- V0001: ProcureLake operational (OLTP) schema.
--
-- Simulated transactional source system for a procurement SaaS product.
-- This database is the primary CDC source (AWS DMS, Week 5).
-- Design decisions: docs/adr/0016-oltp-schema-design.md
--
-- Tenant integrity: every tenant-owned table exposes UNIQUE (tenant_id, <pk>) and every
-- child references its parent through a composite (tenant_id, <fk>) foreign key, so the
-- database itself rejects any relationship that crosses tenants.

CREATE SCHEMA IF NOT EXISTS procurement;

-- Maintains updated_at on every UPDATE. Used downstream as source_updated_at.
CREATE OR REPLACE FUNCTION procurement.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- tenants
-- ---------------------------------------------------------------------------
CREATE TABLE procurement.tenants (
    tenant_id     text        PRIMARY KEY,
    tenant_name   text        NOT NULL,
    status        text        NOT NULL DEFAULT 'active',
    is_synthetic  boolean     NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenants_tenant_id_format_chk  CHECK (tenant_id ~ '^tenant_[a-z][a-z0-9_]*$'),
    CONSTRAINT tenants_tenant_name_chk       CHECK (btrim(tenant_name) <> ''),
    CONSTRAINT tenants_status_chk            CHECK (status IN ('active', 'suspended', 'offboarded')),
    CONSTRAINT tenants_tenant_name_key       UNIQUE (tenant_name)
);

COMMENT ON COLUMN procurement.tenants.is_synthetic IS
    'TRUE for fictional tenants generated for this project. Synthetic tenants are never real customers.';

-- ---------------------------------------------------------------------------
-- agencies
-- ---------------------------------------------------------------------------
CREATE TABLE procurement.agencies (
    agency_id    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    text        NOT NULL REFERENCES procurement.tenants (tenant_id),
    agency_name  text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agencies_agency_name_chk       CHECK (btrim(agency_name) <> ''),
    CONSTRAINT agencies_tenant_agency_key     UNIQUE (tenant_id, agency_id),
    CONSTRAINT agencies_tenant_name_key       UNIQUE (tenant_id, agency_name)
);

-- ---------------------------------------------------------------------------
-- suppliers
-- Supplier names are intentionally NOT unique: real OLTP data contains near-duplicate
-- suppliers, and canonicalisation is a Silver/Gold responsibility (dim_supplier).
-- ---------------------------------------------------------------------------
CREATE TABLE procurement.suppliers (
    supplier_id    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      text        NOT NULL REFERENCES procurement.tenants (tenant_id),
    supplier_name  text        NOT NULL,
    category       text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT suppliers_supplier_name_chk     CHECK (btrim(supplier_name) <> ''),
    CONSTRAINT suppliers_category_chk          CHECK (btrim(category) <> ''),
    CONSTRAINT suppliers_tenant_supplier_key   UNIQUE (tenant_id, supplier_id)
);

-- ---------------------------------------------------------------------------
-- solicitations
-- ---------------------------------------------------------------------------
CREATE TABLE procurement.solicitations (
    solicitation_id  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        text        NOT NULL,
    agency_id        uuid        NOT NULL,
    title            text        NOT NULL,
    status           text        NOT NULL DEFAULT 'draft',
    published_at     timestamptz,
    closing_at       timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT solicitations_title_chk               CHECK (btrim(title) <> ''),
    CONSTRAINT solicitations_status_chk              CHECK (status IN ('draft', 'published', 'closed', 'awarded', 'cancelled')),
    -- Anything past draft must have been published (a draft may be cancelled before publication).
    CONSTRAINT solicitations_published_at_chk        CHECK (status IN ('draft', 'cancelled') OR published_at IS NOT NULL),
    CONSTRAINT solicitations_closing_after_publish_chk CHECK (closing_at IS NULL OR published_at IS NULL OR closing_at > published_at),
    CONSTRAINT solicitations_tenant_solicitation_key UNIQUE (tenant_id, solicitation_id),
    CONSTRAINT solicitations_agency_fk FOREIGN KEY (tenant_id, agency_id)
        REFERENCES procurement.agencies (tenant_id, agency_id)
);

-- ---------------------------------------------------------------------------
-- awards
-- The issuing agency is derived via solicitation_id (not duplicated here).
-- ---------------------------------------------------------------------------
CREATE TABLE procurement.awards (
    award_id         uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        text          NOT NULL,
    solicitation_id  uuid          NOT NULL,
    supplier_id      uuid          NOT NULL,
    award_amount     numeric(18,2) NOT NULL,
    currency_code    char(3)       NOT NULL,
    awarded_at       timestamptz   NOT NULL,
    status           text          NOT NULL DEFAULT 'active',
    created_at       timestamptz   NOT NULL DEFAULT now(),
    updated_at       timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT awards_award_amount_chk     CHECK (award_amount > 0),
    CONSTRAINT awards_currency_code_chk    CHECK (currency_code ~ '^[A-Z]{3}$'),
    CONSTRAINT awards_status_chk           CHECK (status IN ('active', 'cancelled')),
    CONSTRAINT awards_tenant_award_key     UNIQUE (tenant_id, award_id),
    CONSTRAINT awards_solicitation_fk FOREIGN KEY (tenant_id, solicitation_id)
        REFERENCES procurement.solicitations (tenant_id, solicitation_id),
    CONSTRAINT awards_supplier_fk FOREIGN KEY (tenant_id, supplier_id)
        REFERENCES procurement.suppliers (tenant_id, supplier_id)
);

-- ---------------------------------------------------------------------------
-- purchase_orders
-- agency_id is required so actual spend can be reported by agency (mart_actual_spend).
-- award_id is optional: not every purchase order originates from a competitive award.
-- ---------------------------------------------------------------------------
CREATE TABLE procurement.purchase_orders (
    po_id          uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      text          NOT NULL,
    agency_id      uuid          NOT NULL,
    supplier_id    uuid          NOT NULL,
    award_id       uuid,
    order_date     date          NOT NULL,
    amount         numeric(18,2) NOT NULL,
    currency_code  char(3)       NOT NULL,
    status         text          NOT NULL DEFAULT 'open',
    created_at     timestamptz   NOT NULL DEFAULT now(),
    updated_at     timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT purchase_orders_amount_chk         CHECK (amount > 0),
    CONSTRAINT purchase_orders_currency_code_chk  CHECK (currency_code ~ '^[A-Z]{3}$'),
    CONSTRAINT purchase_orders_status_chk         CHECK (status IN ('open', 'closed', 'cancelled')),
    CONSTRAINT purchase_orders_tenant_po_key      UNIQUE (tenant_id, po_id),
    -- Target for the invoice currency foreign key below.
    CONSTRAINT purchase_orders_tenant_po_currency_key UNIQUE (tenant_id, po_id, currency_code),
    CONSTRAINT purchase_orders_agency_fk FOREIGN KEY (tenant_id, agency_id)
        REFERENCES procurement.agencies (tenant_id, agency_id),
    CONSTRAINT purchase_orders_supplier_fk FOREIGN KEY (tenant_id, supplier_id)
        REFERENCES procurement.suppliers (tenant_id, supplier_id),
    CONSTRAINT purchase_orders_award_fk FOREIGN KEY (tenant_id, award_id)
        REFERENCES procurement.awards (tenant_id, award_id)
);

-- ---------------------------------------------------------------------------
-- invoices
-- An invoice is always in its purchase order's currency (enforced by the composite FK).
-- ---------------------------------------------------------------------------
CREATE TABLE procurement.invoices (
    invoice_id      uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       text          NOT NULL,
    po_id           uuid          NOT NULL,
    invoice_date    date          NOT NULL,
    invoice_amount  numeric(18,2) NOT NULL,
    paid_amount     numeric(18,2) NOT NULL DEFAULT 0,
    currency_code   char(3)       NOT NULL,
    payment_status  text          NOT NULL DEFAULT 'pending',
    paid_at         timestamptz,
    created_at      timestamptz   NOT NULL DEFAULT now(),
    updated_at      timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT invoices_invoice_amount_chk  CHECK (invoice_amount > 0),
    CONSTRAINT invoices_paid_amount_chk     CHECK (paid_amount >= 0 AND paid_amount <= invoice_amount),
    CONSTRAINT invoices_currency_code_chk   CHECK (currency_code ~ '^[A-Z]{3}$'),
    CONSTRAINT invoices_payment_status_chk  CHECK (payment_status IN ('pending', 'partially_paid', 'paid', 'cancelled')),
    -- payment_status must agree with the amounts actually paid.
    CONSTRAINT invoices_payment_consistency_chk CHECK (
           (payment_status = 'pending'        AND paid_amount = 0 AND paid_at IS NULL)
        OR (payment_status = 'partially_paid' AND paid_amount > 0 AND paid_amount < invoice_amount AND paid_at IS NOT NULL)
        OR (payment_status = 'paid'           AND paid_amount = invoice_amount AND paid_at IS NOT NULL)
        OR (payment_status = 'cancelled'      AND paid_amount = 0 AND paid_at IS NULL)
    ),
    CONSTRAINT invoices_tenant_invoice_key  UNIQUE (tenant_id, invoice_id),
    CONSTRAINT invoices_po_currency_fk FOREIGN KEY (tenant_id, po_id, currency_code)
        REFERENCES procurement.purchase_orders (tenant_id, po_id, currency_code)
);

-- ---------------------------------------------------------------------------
-- contract_documents
-- ---------------------------------------------------------------------------
CREATE TABLE procurement.contract_documents (
    document_id      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        text        NOT NULL,
    award_id         uuid        NOT NULL,
    document_s3_key  text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contract_documents_s3_key_chk        CHECK (btrim(document_s3_key) <> '' AND document_s3_key !~ '^s3://'),
    CONSTRAINT contract_documents_s3_key_key        UNIQUE (document_s3_key),
    CONSTRAINT contract_documents_tenant_document_key UNIQUE (tenant_id, document_id),
    CONSTRAINT contract_documents_award_fk FOREIGN KEY (tenant_id, award_id)
        REFERENCES procurement.awards (tenant_id, award_id)
);

COMMENT ON COLUMN procurement.contract_documents.document_s3_key IS
    'Object key within the documents bucket/prefix (no s3:// scheme or bucket name).';

-- ---------------------------------------------------------------------------
-- Indexes supporting foreign keys (child-side lookups and parent deletes).
-- Composite UNIQUE constraints already index (tenant_id, <pk>).
-- ---------------------------------------------------------------------------
CREATE INDEX agencies_tenant_idx              ON procurement.agencies (tenant_id);
CREATE INDEX suppliers_tenant_idx             ON procurement.suppliers (tenant_id);
CREATE INDEX solicitations_agency_idx         ON procurement.solicitations (tenant_id, agency_id);
CREATE INDEX awards_solicitation_idx          ON procurement.awards (tenant_id, solicitation_id);
CREATE INDEX awards_supplier_idx              ON procurement.awards (tenant_id, supplier_id);
CREATE INDEX purchase_orders_agency_idx       ON procurement.purchase_orders (tenant_id, agency_id);
CREATE INDEX purchase_orders_supplier_idx     ON procurement.purchase_orders (tenant_id, supplier_id);
CREATE INDEX purchase_orders_award_idx        ON procurement.purchase_orders (tenant_id, award_id);
CREATE INDEX invoices_po_idx                  ON procurement.invoices (tenant_id, po_id);
CREATE INDEX contract_documents_award_idx     ON procurement.contract_documents (tenant_id, award_id);

-- Incremental-extraction support: rows changed since a watermark.
CREATE INDEX awards_updated_at_idx            ON procurement.awards (updated_at);
CREATE INDEX purchase_orders_updated_at_idx   ON procurement.purchase_orders (updated_at);
CREATE INDEX invoices_updated_at_idx          ON procurement.invoices (updated_at);

-- ---------------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------------
CREATE TRIGGER tenants_set_updated_at            BEFORE UPDATE ON procurement.tenants            FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();
CREATE TRIGGER agencies_set_updated_at           BEFORE UPDATE ON procurement.agencies           FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();
CREATE TRIGGER suppliers_set_updated_at          BEFORE UPDATE ON procurement.suppliers          FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();
CREATE TRIGGER solicitations_set_updated_at      BEFORE UPDATE ON procurement.solicitations      FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();
CREATE TRIGGER awards_set_updated_at             BEFORE UPDATE ON procurement.awards             FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();
CREATE TRIGGER purchase_orders_set_updated_at    BEFORE UPDATE ON procurement.purchase_orders    FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();
CREATE TRIGGER invoices_set_updated_at           BEFORE UPDATE ON procurement.invoices           FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();
CREATE TRIGGER contract_documents_set_updated_at BEFORE UPDATE ON procurement.contract_documents FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();
