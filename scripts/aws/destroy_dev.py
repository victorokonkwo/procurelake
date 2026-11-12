"""Tear down the dev compute roots with safeguards (`make destroy-dev`, ADR 0038).

Steps, in order; any refusal stops before anything is destroyed:
1. Refuse while a Glue job run or Step Functions execution is in progress.
2. Back up every DynamoDB table of data_platform to the artifacts bucket (``teardown/``).
3. For each root (cdc, observability, security, data_platform): plan a destroy targeting every
   resource except the kept ones, and check the saved plan (deletes only, nothing kept).
4. Print the plan, ask for the typed confirmation, then apply the checked plans in order.

``--dry-run`` stops after step 3 (read-only: plans, no backup, no destroy).
Never touches bootstrap (state) or foundation (lake data, KMS, catalog, budget).

Usage: uv run python scripts/aws/destroy_dev.py [--dry-run] [--roots cdc,security,...]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.operations.teardown import (
    CONFIRMATION,
    ROOT_ORDER,
    TeardownRefused,
    active_work,
    backup_tables,
    check_roots,
    managed_addresses,
    select_targets,
    verify_destroy_plan,
)

LIVE = Path("infrastructure/live/dev")
# Variables a destroy plan must still be given (the root's terraform.tfvars supplies the rest).
EXTRA_VARS = {"cdc": ["-var", "enable_dms=true"]}


def tf(root: str, *args: str, capture: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 - terraform with arguments built here
        ["terraform", f"-chdir={LIVE / root}", *args],  # noqa: S607 - terraform on PATH
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout if capture else ""


def plan_root(root: str) -> tuple[list[str], list[str], Path | None]:
    tf(root, "init", "-input=false", "-backend-config=backend.hcl")
    selection = select_targets(managed_addresses(tf(root, "state", "list").splitlines()))
    if not selection.targets:
        return [], selection.kept, None
    plan_file = "destroy-dev.tfplan"
    targets = [f"-target={a}" for a in selection.targets]
    tf(
        root,
        "plan",
        "-destroy",
        "-input=false",
        "-lock=true",
        *EXTRA_VARS.get(root, []),
        *targets,
        f"-out={plan_file}",
    )
    plan = json.loads(tf(root, "show", "-json", plan_file))
    check = verify_destroy_plan(plan, selection.kept)
    return check.deletes, selection.kept, LIVE / root / plan_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--roots", default=",".join(ROOT_ORDER))
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args(argv)

    try:
        roots = check_roots(r for r in args.roots.split(",") if r)
        session = build_session(region=args.region)
        busy = active_work(session.client("glue"), session.client("stepfunctions"))
        if busy:
            raise TeardownRefused("work in progress, wait for it:\n  " + "\n  ".join(busy))

        plans: list[tuple[str, Path]] = []
        for root in roots:
            deletes, kept, plan_file = plan_root(root)
            print(f"\n== {root}: {len(deletes)} to destroy, {len(kept)} kept")
            for address in kept:
                print(f"   keep    {address}")
            for address in deletes:
                print(f"   destroy {address}")
            if plan_file:
                plans.append((root, plan_file))
    except TeardownRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if not plans:
        print("\nNothing to destroy.")
        return 0
    if args.dry_run:
        for _, plan_file in plans:
            plan_file.unlink(missing_ok=True)
        print("\nDry run: plans checked, nothing backed up or destroyed.")
        return 0

    if "data_platform" in roots:
        tables = json.loads(tf("data_platform", "output", "-json"))
        names = [v["value"] for k, v in tables.items() if k.endswith("_table_name")]
        bucket = json.loads(tf("foundation", "output", "-json", "bucket_names"))["artifacts"]
        for b in backup_tables(
            session.client("dynamodb"), session.client("s3"), names, bucket, datetime.now(UTC)
        ):
            print(f"backed up {b.table}: {b.items} items -> {b.s3_uri}")

    answer = input(f'\nType "{CONFIRMATION}" to destroy the resources above: ')
    if answer.strip() != CONFIRMATION:
        for _, plan_file in plans:
            plan_file.unlink(missing_ok=True)
        print("Not confirmed; nothing destroyed.")
        return 1
    for root, plan_file in plans:
        print(f"\n== destroying {root}")
        tf(root, "apply", "-input=false", plan_file.name, capture=False)
        plan_file.unlink(missing_ok=True)
    print("\nDone. Restore with the deploy-dev workflow (or the per-root plans); see ADR 0038.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
