"""Command line for contract intelligence (ADR 0032): ``python -m procurelake.ai.cli <command>``.

Commands and the role each assumes (``--role-arn``):

* ``land``: download catalog documents into Bronze (the ingestion role);
* ``extract``: Textract + Bedrock extraction into the review queue (the AI extraction role);
* ``review list|show|decide``: the human review queue; ``decide`` also writes Silver
  (the AI extraction role, with the reviewer's own name);
* ``init-silver``: create the empty Silver table (before the first dbt run that reads it);
* ``publish``: re-write every decided review to Silver (idempotent MERGE);
* ``evaluate``: score the latest extractions against the ground-truth labels.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from procurelake.ai import review
from procurelake.ai.catalog import load_catalog
from procurelake.ai.documents import land_document, landed_documents
from procurelake.ai.evaluate import evaluate, load_ground_truth
from procurelake.ai.extract import ExtractionError, load_model_config
from procurelake.ai.pipeline import run_extraction
from procurelake.ai.silver import SilverTarget, ensure_table, write_decision
from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.ingestion.batch import code_version
from procurelake.ingestion.http import HttpClient
from procurelake.ingestion.landing import S3LandingStore

REPO = Path(__file__).resolve().parents[3]
AI_CONFIG = REPO / "configs" / "ai"
EVIDENCE = REPO / "build" / "evidence"


def _session(args, name: str):
    return build_session(region=args.region, role_arn=args.role_arn, session_name=name)


def cmd_land(args) -> int:
    catalog = load_catalog(AI_CONFIG / "public_contract_documents.yaml")
    session = _session(args, "ai-land")
    s3 = session.client("s3")
    store = S3LandingStore(s3, args.raw_bucket)
    manifests = S3LandingStore(s3, args.artifacts_bucket)
    docs = catalog.documents if args.all else [catalog.get(d) for d in args.document_id]
    http = HttpClient()
    for doc in docs:
        landed = land_document(doc, catalog, store, manifests, http, code_version())
        print(f"{doc.document_id}: sha256={landed.sha256[:16]}… {landed.pdf_key}", flush=True)
    return 0


def _review_table(session, args):
    return session.resource("dynamodb").Table(args.review_table)


def cmd_extract(args) -> int:
    from anthropic import AnthropicBedrock

    config, prompt = load_model_config(AI_CONFIG)
    session = _session(args, "ai-extract")
    creds = session.get_credentials().get_frozen_credentials()
    bedrock = AnthropicBedrock(
        aws_access_key=creds.access_key,
        aws_secret_key=creds.secret_key,
        aws_session_token=creds.token,
        aws_region=config.region,
    )
    raw = S3LandingStore(session.client("s3"), args.raw_bucket)
    audit = S3LandingStore(session.client("s3"), args.artifacts_bucket)
    landed = landed_documents(audit)
    wanted = sorted(landed) if args.all else args.document_id
    failures = 0
    for document_id in wanted:
        if document_id not in landed:
            print(f"{document_id}: not landed (run land first)", file=sys.stderr)
            failures += 1
            continue
        try:
            run = run_extraction(
                landed[document_id], raw, audit, session.client("textract"), bedrock,
                _review_table(session, args), config, prompt,
            )  # fmt: skip
        except ExtractionError as exc:
            print(f"{document_id}: extraction failed: {exc}", file=sys.stderr)
            failures += 1
            continue
        r = run.result
        print(
            f"{document_id}: {run.extraction_id} failed_checks={run.checks.failed} ocr_pages={run.ocr_pages} "
            f"tokens={r.input_tokens}/{r.output_tokens} latency={r.latency_ms}ms cost≈${r.cost_usd:.5f}",
            flush=True,
        )
    return 1 if failures else 0


def cmd_review(args) -> int:
    session = _session(args, "ai-review")
    table = _review_table(session, args)
    if args.review_command == "list":
        for item in review.list_items(table, args.status):
            failed = [f for f, c in item.checks.items() if c["status"] == "failed"]
            print(f"{item.document_id}  {item.extraction_id}  {item.status}  failed={failed}")
        return 0
    item = review.get(table, args.document_id, args.extraction_id)
    if args.review_command == "show":
        for name, ev in item.extraction.items():
            if name == "document_type":
                print(f"document_type: {ev}")
                continue
            check = item.checks[name]
            print(
                f"{name}: {ev['value']!r} (page {ev['page']}) [{check['status']}]"
                f" {check['reasons']} {check['notes']}"
            )
            if ev["quote"]:
                print(f"    quote: {ev['quote']!r}")
        print(f"status: {item.status}  audit: s3://{args.artifacts_bucket}/{item.record['audit_prefix']}/")
        return 0

    decisions = {}
    for spec in args.decision or []:
        name, _, payload = spec.partition("=")
        decisions[name] = review.FieldDecision.model_validate(json.loads(payload))
    caller = session.client("sts").get_caller_identity()["Arn"]
    decided = review.decide(
        table, item, args.reviewer, caller, args.outcome, decisions,
        accept_passed=args.accept_passed, comment=args.comment,
    )  # fmt: skip
    rows = write_decision(_silver(session, args), decided)
    print(f"{decided.document_id}/{decided.extraction_id}: {decided.status}; {rows} Silver rows written")
    return 0


def _silver(session, args) -> SilverTarget:
    return SilverTarget(
        athena=session.client("athena"),
        glue=session.client("glue"),
        workgroup=args.workgroup,
        database=args.silver_database,
        location=f"s3://{args.lake_bucket}/silver/contract_intelligence/",
    )


def cmd_init_silver(args) -> int:
    created = ensure_table(_silver(_session(args, "ai-init"), args))
    print(
        "created silver_contract_intelligence" if created else "silver_contract_intelligence already exists"
    )
    return 0


def cmd_publish(args) -> int:
    session = _session(args, "ai-publish")
    target = _silver(session, args)
    for item in review.list_items(_review_table(session, args)):
        if item.status in ("approved", "rejected"):
            print(f"{item.document_id}/{item.extraction_id}: {write_decision(target, item)} rows")
    return 0


def cmd_evaluate(args) -> int:
    session = _session(args, "ai-evaluate")
    truth = load_ground_truth(AI_CONFIG / "evaluation" / "ground_truth.yaml")
    report = evaluate(truth, review.list_items(_review_table(session, args)))
    result = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "summary": report.summary(),
        "by_field": {k: dict(v) for k, v in report.by_field().items()},
        "documents": [
            {
                "document_id": d.document_id,
                "extraction_id": d.extraction_id,
                "document_type_correct": d.document_type_correct,
                "outcomes": d.outcomes,
                "checks": d.checks,
                "latency_ms": d.latency_ms,
                "tokens": [d.input_tokens, d.output_tokens],
                "cost_usd_estimate": str(d.cost_usd),
            }
            for d in report.documents
        ],
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / f"ai_evaluation_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))
    print(json.dumps(result["by_field"], indent=2))
    print(f"written {out}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="procurelake.ai.cli", description=__doc__.splitlines()[0])
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--role-arn", required=True)
    sub = p.add_subparsers(dest="command", required=True)

    land = sub.add_parser("land")
    land.add_argument("--raw-bucket", required=True)
    land.add_argument("--artifacts-bucket", required=True)
    group = land.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--document-id", action="append")

    ext = sub.add_parser("extract")
    for name in ("--raw-bucket", "--artifacts-bucket", "--review-table"):
        ext.add_argument(name, required=True)
    group = ext.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--document-id", action="append")

    rev = sub.add_parser("review")
    rev.add_argument("--review-table", required=True)
    rev.add_argument("--artifacts-bucket", default="")
    rsub = rev.add_subparsers(dest="review_command", required=True)
    lst = rsub.add_parser("list")
    lst.add_argument("--status")
    for name in ("show", "decide"):
        cmd = rsub.add_parser(name)
        cmd.add_argument("--document-id", required=True)
        cmd.add_argument("--extraction-id", required=True)
    dec = rsub.choices["decide"]
    dec.add_argument("--reviewer", required=True)
    dec.add_argument("--outcome", choices=["approved", "rejected"], required=True)
    dec.add_argument("--accept-passed", action="store_true")
    dec.add_argument(
        "--decision", action="append",
        help='FIELD=JSON, e.g. contract_amount=\'{"action":"correct","value":"35000000.00","page":1,'
        '"currency":"USD","amount_type":"modification_change"}\'',
    )  # fmt: skip
    dec.add_argument("--comment")
    for name in ("--workgroup", "--silver-database", "--lake-bucket"):
        dec.add_argument(name, required=True)

    init = sub.add_parser("init-silver")
    for name in ("--workgroup", "--silver-database", "--lake-bucket"):
        init.add_argument(name, required=True)

    pub = sub.add_parser("publish")
    for name in ("--review-table", "--workgroup", "--silver-database", "--lake-bucket"):
        pub.add_argument(name, required=True)

    ev = sub.add_parser("evaluate")
    ev.add_argument("--review-table", required=True)
    return p


COMMANDS = {
    "land": cmd_land,
    "extract": cmd_extract,
    "review": cmd_review,
    "init-silver": cmd_init_silver,
    "publish": cmd_publish,
    "evaluate": cmd_evaluate,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
