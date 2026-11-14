"""Check the pinned RDS CA bundle against the committed file and the official AWS download (ADR 0050).

Exits 0 when the committed bundle matches the pinned SHA-256 and the bundle AWS serves today has
the same checksum. Exits 1 on any difference: AWS added or rotated a root CA, or the committed
file changed. A difference is reviewed by hand (which roots changed, and why) before re-pinning;
this script never overwrites the committed bundle. No AWS credentials are needed.

Usage: uv run python scripts/aws/rds_ca_check.py
"""

from __future__ import annotations

import json
import sys
import urllib.request

from procurelake.common.rds_tls import BUNDLE_PATH, BUNDLE_SHA256, BUNDLE_URL, sha256_of


def main() -> int:
    committed = sha256_of(BUNDLE_PATH.read_bytes())
    # Fixed https URL from the AWS RDS User Guide (no user input reaches it).
    with urllib.request.urlopen(BUNDLE_URL, timeout=30) as response:  # noqa: S310
        served = sha256_of(response.read())
    report = {
        "url": BUNDLE_URL,
        "pinned_sha256": BUNDLE_SHA256,
        "committed_sha256": committed,
        "served_sha256": served,
        "committed_matches_pin": committed == BUNDLE_SHA256,
        "served_matches_pin": served == BUNDLE_SHA256,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["committed_matches_pin"] and report["served_matches_pin"] else 1


if __name__ == "__main__":
    sys.exit(main())
