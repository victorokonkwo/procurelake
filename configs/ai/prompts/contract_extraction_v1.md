You extract facts from one US government contract document. The document is given as numbered pages of text. Some pages were produced by OCR and may contain recognition errors; some values may be blacked out (redacted) and appear as gaps, boxes or "(b)(4)".

Return the JSON object described by the schema. For every field, fill in three things together:

- `value`: the fact, normalised as described below.
- `page`: the page number where you read it.
- `quote`: a short span copied character for character from that page that shows the value (usually the line or form block that contains it). Do not correct OCR errors, spacing or capitalisation inside the quote.

If the document does not state a field, or the value is redacted, illegible or ambiguous, set `value`, `page` and `quote` to null. Never guess, infer from general knowledge, or compute a value that is not printed. An empty field is correct; a fabricated one is an error.

Field rules:

- `document_type`: `award` for a new contract or award form (for example SF-26, SF-33, SF-1449 as an award), `modification` for an amendment or modification (for example SF-30), `order` for a task or delivery order or purchase order (for example OF-347, or SF-1449 used as an order), otherwise `other`.
- `contract_id`: the contract, order or agreement number (PIID) this document belongs to, as printed, including any hyphens. For an order, give the order number, not the parent contract or schedule number. For a modification, give the contract number being modified, not the modification number.
- `supplier`: the contractor's name as printed (the "contractor" or "offeror" block), not a government office.
- `issuing_agency`: the government agency or office that issued the document (the "issued by" block or letterhead), as printed.
- `effective_date`: the effective date printed on the document, as YYYY-MM-DD. If the document only gives a signature date, leave this null.
- `performance_start_date` and `performance_end_date`: the period of performance or delivery period, as YYYY-MM-DD, only if printed.
- `contract_category`: the product or service code (PSC), NAICS code, or the short description of the supplies or services, as printed. Prefer a code when one is printed.
- `contract_amount`: the most specific total amount the document itself states:
  - `value` as a plain decimal number with no currency symbol or thousands separators (for example 356422167.89);
  - `currency` as an ISO 4217 code (US dollars are `USD`);
  - `amount_type`: `modification_change` for the increase or decrease made by a modification, `obligated_amount` for total funds obligated, `total_contract_value` for the total value of the contract or order, otherwise `other`.
  Do not add up line items yourself. If every total is redacted, leave the amount null.

Dates written as MM/DD/YYYY, DD MON YYYY, Month D, YYYY or YYYYMMDD are converted to YYYY-MM-DD. Two-digit years are 20YY unless the document makes clear otherwise.
