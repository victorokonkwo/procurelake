"""Week 10 analytical query benchmark on Athena (ADR 0034).

Usage:
    uv run python scripts/aws/bench_queries.py run --label unpartitioned --silver DB --gold DB \
        --workgroup WG [--repeats 5] [--out build/evidence/bench_queries_<label>.json]
    uv run python scripts/aws/bench_queries.py compare BEFORE.json AFTER.json

`run` uses the standard AWS chain (`make bench-queries` wraps it with the data-engineer role).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from procurelake.benchmarks.athena_queries import compare, load_queries, markdown, run_benchmark
from procurelake.common.aws_clients import DEFAULT_REGION, build_session

REPO = Path(__file__).resolve().parents[2]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--silver", required=True)
    r.add_argument("--gold", required=True)
    r.add_argument("--workgroup", required=True)
    r.add_argument("--repeats", type=int, default=5)
    r.add_argument("--queries", default=str(REPO / "configs" / "benchmarks" / "athena_queries.yaml"))
    r.add_argument("--out")
    r.add_argument("--region", default=DEFAULT_REGION)
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    args = p.parse_args(argv)

    if args.cmd == "compare":
        before, after = (json.loads(Path(f).read_text()) for f in (args.before, args.after))
        print(markdown(compare(before, after), before["label"], after["label"]), end="")
        return 0

    athena = build_session(region=args.region).client("athena")
    body = run_benchmark(
        athena,
        args.workgroup,
        load_queries(Path(args.queries)),
        args.silver,
        args.gold,
        args.repeats,
        args.label,
    )
    out = Path(args.out or REPO / "build" / "evidence" / f"bench_queries_{args.label}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2) + "\n")
    for q in body["queries"]:
        s = q["summary"]
        print(
            f"{q['name']:<34} rows={q['rows']:<5} scanned={s['scanned_bytes']['median']:>12,.0f} B "
            f"engine_ms median={s['engine_ms']['median']:>7,.0f} (min {s['engine_ms']['min']:,}, "
            f"max {s['engine_ms']['max']:,})"
        )
    print(f"evidence: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
