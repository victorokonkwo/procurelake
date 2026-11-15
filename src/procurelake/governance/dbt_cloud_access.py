"""What the dbt Cloud development user may and may not do (ADR 0051), checked with the IAM policy simulator.

The user's access key lives in dbt Cloud, outside AWS, so its policy is the control. This module
lists the decisions that policy must produce (each with the request context dbt Cloud or a thief
would present) and checks them with ``iam:SimulatePrincipalPolicy``, which evaluates the user's
real attached policies without calling Glue, S3 or Athena. It is read-only and costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# A documentation address (RFC 5737 TEST-NET-2): stands for any caller outside dbt Cloud.
OUTSIDE_IP = "198.51.100.7"


@dataclass(frozen=True)
class Names:
    """Account-specific names, read from the foundation and dbt_cloud Terraform outputs."""

    region: str
    account_id: str
    lake_bucket: str
    raw_bucket: str
    artifacts_bucket: str
    silver_database: str
    gold_database: str
    sandbox_database: str
    dbt_cloud_workgroup: str
    engineering_workgroup: str
    data_key_arn: str
    dbt_cloud_ip: str


@dataclass(frozen=True)
class Case:
    """One simulated request and the decision the policy must give."""

    description: str
    action: str
    resource: str
    allowed: bool
    source_ip: str
    via_aws_service: bool = False
    kms_via_service: str | None = None


def expected_cases(n: Names) -> list[Case]:
    glue = f"arn:aws:glue:{n.region}:{n.account_id}"
    athena = f"arn:aws:athena:{n.region}:{n.account_id}:workgroup"
    lake = f"arn:aws:s3:::{n.lake_bucket}"
    art = f"arn:aws:s3:::{n.artifacts_bucket}"
    results = f"{art}/athena_results/{n.dbt_cloud_workgroup}/q.csv"
    ip = n.dbt_cloud_ip
    s3_service = f"s3.{n.region}.amazonaws.com"

    return [
        # What dbt Cloud needs, from its own addresses.
        Case(
            "query in the dbt Cloud workgroup",
            "athena:StartQueryExecution",
            f"{athena}/{n.dbt_cloud_workgroup}",
            True,
            ip,
        ),
        Case(
            "read a Silver table definition",
            "glue:GetTable",
            f"{glue}:table/{n.silver_database}/silver_awards",
            True,
            ip,
        ),
        Case(
            "create a sandbox table",
            "glue:CreateTable",
            f"{glue}:table/{n.sandbox_database}/fct_awards",
            True,
            ip,
        ),
        Case(
            "drop a sandbox table",
            "glue:DeleteTable",
            f"{glue}:table/{n.sandbox_database}/fct_awards",
            True,
            ip,
        ),
        Case(
            "delete old sandbox data",
            "s3:DeleteObject",
            f"{lake}/dbt_cloud/procurelake_dev_dbt_cloud/fct_awards/x.parquet",
            True,
            ip,
        ),
        # What Athena does on the user's behalf (forward access sessions).
        Case(
            "Athena reads Silver data",
            "s3:GetObject",
            f"{lake}/silver/silver_awards/data/x.parquet",
            True,
            OUTSIDE_IP,
            via_aws_service=True,
        ),
        Case(
            "Athena writes sandbox data",
            "s3:PutObject",
            f"{lake}/dbt_cloud/procurelake_dev_dbt_cloud/fct_awards/x.parquet",
            True,
            OUTSIDE_IP,
            via_aws_service=True,
        ),
        Case("Athena writes query results", "s3:PutObject", results, True, OUTSIDE_IP, via_aws_service=True),
        Case(
            "S3 decrypts with the data key",
            "kms:Decrypt",
            n.data_key_arn,
            True,
            OUTSIDE_IP,
            via_aws_service=True,
            kms_via_service=s3_service,
        ),
        # Never: Gold, Bronze, writes to Silver, other workgroups, identities, databases.
        Case(
            "read a Gold table definition",
            "glue:GetTable",
            f"{glue}:table/{n.gold_database}/mart_awarded_value",
            False,
            ip,
        ),
        Case(
            "drop a Gold table",
            "glue:DeleteTable",
            f"{glue}:table/{n.gold_database}/mart_awarded_value",
            False,
            ip,
        ),
        Case(
            "create a table in Silver", "glue:CreateTable", f"{glue}:table/{n.silver_database}/x", False, ip
        ),
        Case("create a database", "glue:CreateDatabase", f"{glue}:database/x", False, ip),
        Case(
            "read Gold data",
            "s3:GetObject",
            f"{lake}/gold/mart_awarded_value/data/x.parquet",
            False,
            OUTSIDE_IP,
            via_aws_service=True,
        ),
        Case(
            "write Silver data",
            "s3:PutObject",
            f"{lake}/silver/silver_awards/data/x.parquet",
            False,
            OUTSIDE_IP,
            via_aws_service=True,
        ),
        Case(
            "read Bronze data",
            "s3:GetObject",
            f"arn:aws:s3:::{n.raw_bucket}/bronze/x.json",
            False,
            OUTSIDE_IP,
            via_aws_service=True,
        ),
        Case(
            "query in the engineering workgroup",
            "athena:StartQueryExecution",
            f"{athena}/{n.engineering_workgroup}",
            False,
            ip,
        ),
        Case(
            "create another access key",
            "iam:CreateAccessKey",
            f"arn:aws:iam::{n.account_id}:user/procurelake/x",
            False,
            ip,
        ),
        Case("use the data key directly", "kms:Decrypt", n.data_key_arn, False, ip),
        # A stolen key used from anywhere else.
        Case(
            "stolen key: read a Silver table definition",
            "glue:GetTable",
            f"{glue}:table/{n.silver_database}/silver_awards",
            False,
            OUTSIDE_IP,
        ),
        Case(
            "stolen key: start a query",
            "athena:StartQueryExecution",
            f"{athena}/{n.dbt_cloud_workgroup}",
            False,
            OUTSIDE_IP,
        ),
        Case(
            "stolen key: read Silver data directly",
            "s3:GetObject",
            f"{lake}/silver/silver_awards/data/x.parquet",
            False,
            OUTSIDE_IP,
        ),
    ]


def _context_entries(case: Case) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = [
        {"ContextKeyName": "aws:SourceIp", "ContextKeyValues": [case.source_ip], "ContextKeyType": "ip"},
        {
            "ContextKeyName": "aws:ViaAWSService",
            "ContextKeyValues": ["true" if case.via_aws_service else "false"],
            "ContextKeyType": "boolean",
        },
    ]
    if case.kms_via_service:
        entries.append(
            {
                "ContextKeyName": "kms:ViaService",
                "ContextKeyValues": [case.kms_via_service],
                "ContextKeyType": "string",
            }
        )
    return entries


@dataclass(frozen=True)
class Result:
    case: Case
    decision: str

    @property
    def ok(self) -> bool:
        return (self.decision == "allowed") == self.case.allowed


def simulate(iam_client: Any, user_arn: str, cases: list[Case]) -> list[Result]:
    """Run each case through SimulatePrincipalPolicy (one call per case: the contexts differ)."""
    results = []
    for case in cases:
        response = iam_client.simulate_principal_policy(
            PolicySourceArn=user_arn,
            ActionNames=[case.action],
            ResourceArns=[case.resource],
            ContextEntries=_context_entries(case),
        )
        (evaluation,) = response["EvaluationResults"]
        results.append(Result(case, evaluation["EvalDecision"]))
    return results


def report(results: list[Result]) -> dict[str, Any]:
    return {
        "passed": all(r.ok for r in results),
        "checked": len(results),
        "failures": [
            {
                "case": r.case.description,
                "action": r.case.action,
                "resource": r.case.resource,
                "expected": "allowed" if r.case.allowed else "denied",
                "decision": r.decision,
            }
            for r in results
            if not r.ok
        ],
        "cases": [
            {
                "case": r.case.description,
                "expected": "allowed" if r.case.allowed else "denied",
                "decision": r.decision,
            }
            for r in results
        ],
    }
