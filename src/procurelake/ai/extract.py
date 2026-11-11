"""Bedrock typed-field extraction (Claude through the Anthropic SDK's Bedrock client).

One request per document: the (masked) page text goes in as numbered ``<page>`` blocks, and
the response is constrained to ``configs/ai/extraction_schema.json`` with structured outputs
(``output_config.format``). The reply is then validated again with Pydantic
(``ContractExtraction``): structured outputs shape the JSON, the validators enforce the rules
the JSON Schema cannot express (evidence present together, ISO dates, decimal amounts).

A refusal, a reply cut off at ``max_tokens``, or a reply that fails validation raises
``ExtractionError``; nothing partial is returned.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from procurelake.ai.schema import SCHEMA_VERSION, ContractExtraction, json_schema
from procurelake.ai.text import PageText


class ExtractionError(RuntimeError):
    def __init__(self, message: str, raw_text: str | None = None, request_id: str | None = None):
        super().__init__(message)
        self.raw_text = raw_text
        self.request_id = request_id


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input: Decimal
    output: Decimal
    input_long_context: Decimal
    output_long_context: Decimal
    long_context_threshold_input_tokens: int

    def cost_usd(self, input_tokens: int, output_tokens: int) -> Decimal:
        long = input_tokens > self.long_context_threshold_input_tokens
        rate_in = self.input_long_context if long else self.input
        rate_out = self.output_long_context if long else self.output
        return (rate_in * input_tokens + rate_out * output_tokens) / Decimal(1_000_000)


class TextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_words_per_page: int


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: int
    provider: str
    region: str
    model_id: str
    foundation_model: str
    effort: str
    max_tokens: int
    prompt_file: str
    prompt_version: str
    schema_version: str
    max_pages: int
    text: TextConfig
    pricing_usd_per_million_tokens: Pricing
    auto_approve: bool


def load_model_config(ai_config_dir: Path) -> tuple[ModelConfig, str]:
    """The model config and the system prompt it names."""
    config = ModelConfig.model_validate(yaml.safe_load((ai_config_dir / "model_config.yaml").read_text()))
    if config.schema_version != SCHEMA_VERSION:
        raise ValueError(f"model_config schema_version {config.schema_version!r} != code {SCHEMA_VERSION!r}")
    prompt = (ai_config_dir / config.prompt_file).read_text()
    return config, prompt


@dataclass(frozen=True)
class ExtractionResult:
    extraction: ContractExtraction
    raw_text: str
    model_id: str  # the profile requested
    response_model: str  # the model the response reports
    request_id: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: Decimal


def render_document(document_id: str, pages: list[PageText]) -> str:
    blocks = "\n".join(f'<page number="{p.page}">\n{p.text}\n</page>' for p in pages)
    return f'<document id="{document_id}" pages="{len(pages)}">\n{blocks}\n</document>'


def extract(
    client,
    config: ModelConfig,
    system_prompt: str,
    document_id: str,
    pages: list[PageText],
    clock=time.monotonic,
) -> ExtractionResult:
    """Call the model once and return the validated extraction. ``client`` is an
    ``anthropic.AnthropicBedrock`` (or anything with the same ``messages.create``)."""
    if len(pages) > config.max_pages:
        raise ExtractionError(f"{len(pages)} pages is over max_pages={config.max_pages}; not truncating")
    started = clock()
    response = client.messages.create(
        model=config.model_id,
        max_tokens=config.max_tokens,
        system=system_prompt,
        output_config={"effort": config.effort, "format": {"type": "json_schema", "schema": json_schema()}},
        messages=[
            {
                "role": "user",
                "content": render_document(document_id, pages) + "\n\nExtract the fields from this document.",
            }
        ],
    )
    latency_ms = round((clock() - started) * 1000)
    request_id = getattr(response, "_request_id", None)
    text = "".join(block.text for block in response.content if block.type == "text")

    if response.stop_reason == "refusal":
        raise ExtractionError("the model declined the request (stop_reason=refusal)", text, request_id)
    if response.stop_reason == "max_tokens":
        raise ExtractionError(f"reply cut off at max_tokens={config.max_tokens}", text, request_id)
    if response.stop_reason != "end_turn":
        raise ExtractionError(f"unexpected stop_reason {response.stop_reason!r}", text, request_id)
    try:
        extraction = ContractExtraction.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ExtractionError(f"reply failed schema validation: {exc}", text, request_id) from exc

    usage = response.usage
    return ExtractionResult(
        extraction=extraction,
        raw_text=text,
        model_id=config.model_id,
        response_model=response.model,
        request_id=request_id,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        latency_ms=latency_ms,
        cost_usd=config.pricing_usd_per_million_tokens.cost_usd(usage.input_tokens, usage.output_tokens),
    )
