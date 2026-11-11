"""Summarise a Terraform plan for a public CI log: resource addresses and actions only (ADR 0035).

Usage: terraform show -json plan.tfplan | uv run python scripts/ci/tf_plan_summary.py ROOT

Prints a JSON line ``{"root", "changes", "fingerprint", "markdown"}``:

* ``markdown``: one line per changed resource, e.g. ``- **update** `module.x.aws_glue_job.this` ``.
  No attribute values are printed, because the repository and its Actions logs are public.
* ``fingerprint``: SHA-256 over the sorted (action, address, hash of planned values) list. The
  deploy job re-plans with the deploy role and applies only if its fingerprint equals the one the
  reviewer approved, so a different update to the same resource does not pass. Plan files are
  never uploaded (public repository), so this is what ties the apply to the reviewed plan.

Resources with no change, and data-source reads, are left out.
"""

from __future__ import annotations

import hashlib
import json
import sys


def action_label(actions: list[str]) -> str | None:
    if actions in (["no-op"], ["read"]):
        return None
    if sorted(actions) == ["create", "delete"]:
        return "replace"
    if len(actions) == 1 and actions[0] in {"create", "update", "delete", "forget"}:
        return actions[0]
    raise ValueError(f"unexpected plan actions {actions!r}")


def _values_hash(change: dict) -> str:
    """Hash of the planned values (never printed): an approved "update" must be the same update."""
    planned = {k: change.get(k) for k in ("after", "after_unknown", "after_sensitive")}
    return hashlib.sha256(json.dumps(planned, sort_keys=True).encode()).hexdigest()


def summarise(plan: dict, root: str) -> dict:
    hashed = sorted(
        (label, rc["address"], _values_hash(rc["change"]))
        for rc in plan.get("resource_changes", [])
        if (label := action_label(rc["change"]["actions"])) is not None
    )
    changes = [(label, address) for label, address, _ in hashed]
    fingerprint = hashlib.sha256(json.dumps(hashed).encode()).hexdigest()
    if changes:
        lines = [f"- **{label}** `{address}`" for label, address in changes]
        counts = {
            k: sum(1 for label, _ in changes if label == k)
            for k in ("create", "update", "replace", "delete", "forget")
        }
        head = ", ".join(f"{n} to {k}" for k, n in counts.items() if n)
        markdown = f"### {root}: {head}\n\n" + "\n".join(lines) + "\n"
    else:
        markdown = f"### {root}: no changes\n"
    return {"root": root, "changes": len(changes), "fingerprint": fingerprint, "markdown": markdown}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit(__doc__.splitlines()[2])
    print(json.dumps(summarise(json.load(sys.stdin), argv[0])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
