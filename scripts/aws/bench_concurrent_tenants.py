"""Run the concurrent tenant access benchmark on dev Gold; evidence in build/evidence/.

Usage: uv run python scripts/aws/bench_concurrent_tenants.py --roles JSON --workgroups JSON
         --gold DB [--rounds 3]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from procurelake.benchmarks.concurrent_tenants import run_benchmark
from procurelake.common.aws_clients import DEFAULT_REGION, build_session

REPO = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--roles", required=True, help="tenant -> role ARN (security output)")
    p.add_argument("--workgroups", required=True, help="tenant -> workgroup (security output)")
    p.add_argument("--gold", required=True)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--region", default=DEFAULT_REGION)
    args = p.parse_args(argv)
    roles, workgroups = json.loads(args.roles), json.loads(args.workgroups)
    clients = {
        t: build_session(
            region=args.region, role_arn=arn, session_name=f"bench-{t.replace('_', '-')}"
        ).client("athena")
        for t, arn in roles.items()
    }
    body = run_benchmark(clients, workgroups, args.gold, rounds=args.rounds)
    out = REPO / "build" / "evidence" / "bench_concurrent_tenants.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2))
    for phase, per_tenant in body["summary"].items():
        for tenant, s in per_tenant.items():
            print(
                f"{phase:<11} {tenant:<14} n={s['queries']:<3} "
                f"engine_ms median={s['engine_ms']['median']:>6} "
                f"queue_ms median={s['queue_ms']['median']:>5} "
                f"total_ms median={s['total_ms']['median']:>6} (max {s['total_ms']['max']})"
            )
    print(body["isolation"])
    print(f"evidence: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
