# ADR 0009: Textract + Bedrock for document intelligence

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
Contract PDFs must become validated, structured, traceable records.

## Decision
Digitally generated PDFs use direct text extraction. Scanned documents use Amazon Textract. Amazon Bedrock then extracts typed fields along with source evidence. Output is validated against a Pydantic/JSON schema. Prompt and model versions are recorded, sensitive data is redacted where needed, and uncertain results go to human review.

## Consequences
- A model confidence score is never treated as proof of accuracy.
- Values the source doesn't support are left empty, never fabricated.
- The Bedrock model and its `ca-central-1` availability are open (open question 18).

## Alternatives considered
None recorded in the plan.
