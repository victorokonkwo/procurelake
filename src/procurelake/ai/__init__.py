"""AI procurement intelligence: contract documents to validated, reviewed fields (ADR 0032).

Flow: public contract PDF -> Bronze (``documents``) -> page text (PDF text layer, or Textract
for pages without one) -> Bedrock typed-field extraction with evidence (``extract``) ->
schema and evidence checks (``checks``) -> human review queue (``review``) -> Silver
``silver_contract_intelligence`` (``silver``) -> Gold ``mart_ai_contract_intelligence`` (dbt).

The model's output is never trusted on its own: every field must quote its source page, the
quote must be on that page, the value must agree with the quote, and a person approves every
document before it reaches Gold.
"""
