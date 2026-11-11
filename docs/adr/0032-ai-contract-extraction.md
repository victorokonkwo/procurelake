# ADR 0032: AI contract extraction: Claude Haiku 5.5 in Canada, evidence checks, human review

- **Status:** Accepted (implementation built; live extraction blocked on Bedrock model access, see Consequences)
- **Date:** 2026-11-11
- **Implementation:**
  - Python: `src/procurelake/ai/` (catalog, documents, text, redact, schema, extract, checks, review, silver, evaluate, pipeline, cli)
  - Config: `configs/ai/model_config.yaml`, `configs/ai/prompts/contract_extraction_v1.md`, `configs/ai/extraction_schema.json`, `configs/ai/public_contract_documents.yaml`, `configs/ai/evaluation/ground_truth.yaml`
  - Terraform: `infrastructure/live/dev/data_platform` (review table `procurelake-dev-ai-review`, role `procurelake-dev-ai-extraction`)
  - dbt: `dbt/models/staging/contract_intelligence/`, `dbt/models/marts/ai_intelligence/mart_ai_contract_intelligence.sql`
  - Product contract: `contracts/products/contract_intelligence.yaml`
  - Tests: `tests/unit/ai/`, `infrastructure/live/dev/data_platform/tests/`
- **Resolves:** open question 18

## Context
Work package F (Week 9): a public contract PDF must flow through extraction, validation and
approval into a queryable Gold model, with provenance and quality-evaluation results recorded.
AGENTS.md section 2.6 sets the rules: evidence for every field, schema validation, prompt and
model versions recorded, redaction where needed, uncertain results to human review, a model
confidence score is never proof, and missing values are never fabricated.

The owner decided on 2026-11-11:
- **Model:** Claude Haiku 5.5 through the Canada cross-region inference profile.
- **Documents:** US federal contract documents linked to USAspending awards.
- **Review:** a DynamoDB queue with a command-line reviewer.
- **Ground truth:** labels drafted by the assistant from the documents, spot-checked by the
  owner. USAspending is the independent reference for the contract number.

## What was verified before building (2026-11-11, account 697191766499)
- **Bedrock in ca-central-1.** `ca.anthropic.claude-haiku-5-5` is the only current Claude model
  with a Canada inference profile. It routes to ca-central-1 and ca-west-1. Sonnet 5.5 and
  Opus 5.5 are offered only through global or US profiles.
- **The Anthropic SDK's Mantle client** (`AnthropicBedrockMantle`, the SDK's recommended Bedrock
  client) returned "model does not exist" for both IDs in ca-central-1. The SDK's
  `AnthropicBedrock` client (bedrock-runtime) is used instead.
- **Textract `DetectDocumentText`** in ca-central-1 accepts a single-page PDF as bytes. It was
  measured on the 2-page image-only NASA modification: 585 and 156 words.
- **Pricing.** The model agreement's rate card for the Canada profile (dimension `CAN1_*_standard`)
  is $0.11 per million input tokens and $0.55 per million output, with long-context rates of
  $0.55 and $2.75.
- **USAspending award records** (`/api/v2/awards/<id>/`) give the PIID, the parent award's PIID,
  the recipient and the agency for each catalog document.

## Decision
1. **The documents are a public reference dataset.** Twelve documents (NASA 6, DOE 3,
   DHS/USCIS 3) were found and checked on 2026-11-11. Each is linked to its USAspending award.
   The mix is 3 digital, 2 image-only, 1 hybrid and 6 scans that carry an OCR text layer; 4
   have redacted amounts, and 1 order number is shared with an unrelated award. They are never
   tenant documents (`procurement.contract_documents` stays synthetic and empty).
2. **Bronze landing, create-only.** Each document lands as:
   - `documents/public_contracts/document_id=<id>/sha256=<hex>/source.pdf`;
   - the USAspending award record, stored as returned;
   - a manifest in the artifacts bucket with the source URL, landing page, licence basis
     (17 U.S.C. 105), retrieval time and checksums.

   The ingestion role lands them. A re-run with the same bytes is a no-op.
3. **Page text.** Pages with a text layer of at least 15 words keep it as delivered, including
   noisy OCR layers, whose quality is part of what the evaluation measures. Other pages go to
   Textract one page at a time as bytes, so access to the encrypted Bronze object is never
   delegated. Email addresses and phone numbers are masked before anything is sent or stored.
4. **Extraction.**
   - One request per document, with all pages, through the `AnthropicBedrock` client: Haiku 5.5,
     effort `medium`, structured outputs (`output_config.format`) with
     `configs/ai/extraction_schema.json`.
   - The reply is validated again with Pydantic. Value, page and quote must be all present or
     all null; dates are ISO; amounts are plain decimals with an ISO currency and an amount
     type.
   - A refusal, a reply cut off at `max_tokens`, or a reply that fails validation fails the
     extraction. Nothing partial is queued. Documents over 80 pages are refused, not truncated.
   - Fields: document type, contract number, supplier, issuing agency, effective date,
     performance start and end, category, and amount (with currency and amount type). The
     model returns no confidence score.
5. **Deterministic checks decide pass or fail; no model is involved.** A field fails when:
   - the cited page does not exist;
   - the quote is not on that page;
   - the value is not shown by the quote;
   - for the contract number, it matches neither the USAspending PIID nor its parent's PIID.

   A supplier name that differs from USAspending's recipient is a note for the reviewer, not a
   failure.
6. **Human review of every document.**
   - `auto_approve` is false. A reviewer approves or rejects each extraction once (a
     conditional update), and their name and STS identity are recorded.
   - A field whose checks failed cannot be accepted as is: it must be corrected (with the page
     it was read from) or rejected.
   - Turning on automatic approval would need measured evaluation results and a new decision.
7. **Silver** `silver_contract_intelligence`: Iceberg, created and written through Athena.
   - One row per document, extraction and field, holding the model value and evidence, check
     status, review action, final value, provenance, and the model, prompt and schema versions.
   - Approved and rejected reviews are both written, so it is the full audit record.
   - Writes use an idempotent `MERGE` on the grain.
8. **Gold** `mart_ai_contract_intelligence`, built by dbt.
   - One row per document, from its latest approved extraction, with accepted or corrected
     values only. Every value carries its page.
   - The amount is named `stated_amount`, with `amount_type`: it is neither awarded value nor
     spend.
   - It is rebuilt in full each run (it has no Lake Formation objects) and is not granted to
     tenant roles.
9. **Audit trail.** For each extraction, the artifacts bucket keeps, create-only:
   - the masked page text;
   - the raw Textract responses;
   - the request (versions and SHA-256 of the exact prompt and document text);
   - the raw reply, or the error.
10. **Least privilege.** `procurelake-dev-ai-extraction` may:
    - invoke only the Canada profile and the model in ca-central-1 and ca-west-1;
    - call Textract;
    - read the documents prefix and its manifests, and write its audit prefix and Athena
      results;
    - read and write only `silver/contract_intelligence/` in the lake, through Glue access to
      that one table;
    - use the review table.

    Bronze deletes are denied. A Terraform validation rejects any profile that is not `ca.`.
11. **Evaluation.**
    - Each field is scored against the labels as correct, correct empty, wrong, missed or
      fabricated. Fabrication is reported separately, because the rules forbid it.
    - Latency, tokens and a cost estimate from measured tokens are recorded.
    - The labels' own provenance (drafted by the assistant, checked by whom) is printed with
      every result.
12. **Dependencies.** A separate uv group `ai`, outside the Glue wheel: `anthropic[bedrock]==2.3.0`
    and `pypdf==6.21.4`. Each was the newest release at least two weeks old on 2026-11-11.

## Consequences
- **Blocked on model access.** On 2026-11-11 the owner submitted Anthropic's use-case form and
  created the model agreement (`agreementAvailability: AVAILABLE`). Invocations still returned
  `AccessDeniedException: anthropic.claude-haiku-5-5 is not available for this account` through
  both the Canada and global profiles, while Amazon Nova Lite answered through its Canada
  profile. Until access is granted, `make ai-extract` cannot run. The completion gate is not
  met, and no extraction accuracy, latency or cost has been measured. Everything else runs: 56
  unit tests use test doubles for Textract, Bedrock and DynamoDB, and Textract was exercised
  live.
- **The ground truth is assistant-drafted** until the owner checks it. Results will say so, and
  `checked_by` stays null until then.
- **One request per document** keeps page references exact and avoids chunking logic. Very long
  documents (over 80 pages) are refused; none in the catalog is.
- **Every extraction needs a person**, so throughput is limited by review. That is deliberate
  until the evaluation justifies more.
- **Not built:** Step Functions orchestration of extraction (it runs from the command line), a
  review UI, Amazon A2I, and Bedrock Guardrails.

## Alternatives considered
- **Sonnet 5.5 through a global profile.** More capable, but document text would be processed
  outside Canada. Not chosen by the owner.
- **Amazon Nova Lite** (works today through its Canada profile). It would replace the Anthropic
  SDK with the boto3 Converse API. Offered while access was blocked; the owner kept Haiku 5.5.
- **Textract asynchronous APIs on the S3 object.** These need Textract to read KMS-encrypted
  Bronze objects; the synchronous per-page call avoids that.
- **Amazon A2I** for review. It needs a private workforce (Cognito), and its availability in
  ca-central-1 was not verified. The CLI queue is enough for this volume.
- **Model-reported confidence** as a routing signal. Not used (AGENTS.md section 2.6).
