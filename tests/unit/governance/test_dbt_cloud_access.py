"""dbt Cloud user policy check (ADR 0051). The fake IAM client below is a test double: it
returns recorded decisions, so these tests cover the request shapes and the report, not IAM."""

from __future__ import annotations

from procurelake.governance import dbt_cloud_access as dca

NAMES = dca.Names(
    region="ca-central-1",
    account_id="123456789012",
    lake_bucket="procurelake-dev-lake-123456789012-ca-central-1",
    raw_bucket="procurelake-dev-raw-123456789012-ca-central-1",
    artifacts_bucket="procurelake-dev-artifacts-123456789012-ca-central-1",
    silver_database="procurelake_dev_silver",
    gold_database="procurelake_dev_gold",
    sandbox_database="procurelake_dev_dbt_cloud",
    dbt_cloud_workgroup="procurelake-dev-dbt-cloud",
    engineering_workgroup="procurelake-dev-engineering",
    data_key_arn="arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000",
    dbt_cloud_ip="52.45.144.63",
)


class FakeIam:
    """Test double for iam.simulate_principal_policy: answers from the expected decisions,
    except for the actions listed in ``wrong``."""

    def __init__(self, cases, wrong=()):
        self.expected = {
            (c.action, c.resource, c.source_ip, str(c.via_aws_service).lower()): c.allowed for c in cases
        }
        self.wrong = set(wrong)
        self.calls = []

    def simulate_principal_policy(self, **kwargs):
        self.calls.append(kwargs)
        (action,), (resource,) = kwargs["ActionNames"], kwargs["ResourceArns"]
        context = {e["ContextKeyName"]: e["ContextKeyValues"][0] for e in kwargs["ContextEntries"]}
        key = (action, resource, context["aws:SourceIp"], context["aws:ViaAWSService"])
        allowed = self.expected[key] != (action in self.wrong)
        return {"EvaluationResults": [{"EvalDecision": "allowed" if allowed else "explicitDeny"}]}


def test_nothing_on_gold_bronze_or_identities_is_expected_allowed():
    for case in dca.expected_cases(NAMES):
        if case.allowed:
            assert NAMES.gold_database not in case.resource and "/gold/" not in case.resource
            assert NAMES.raw_bucket not in case.resource
            assert not case.action.startswith("iam:")


def test_direct_calls_from_outside_dbt_cloud_are_expected_denied():
    outside_direct = [
        c for c in dca.expected_cases(NAMES) if c.source_ip == dca.OUTSIDE_IP and not c.via_aws_service
    ]
    assert outside_direct
    assert all(not c.allowed for c in outside_direct)


def test_each_case_sends_its_request_context():
    cases = dca.expected_cases(NAMES)
    iam = FakeIam(cases)
    dca.simulate(iam, "arn:aws:iam::123456789012:user/procurelake/procurelake-dev-dbt-cloud", cases)
    assert len(iam.calls) == len(cases)
    kms = next(
        call for call, case in zip(iam.calls, cases, strict=True) if case.kms_via_service and case.allowed
    )
    keys = {e["ContextKeyName"]: (e["ContextKeyType"], e["ContextKeyValues"]) for e in kms["ContextEntries"]}
    assert keys["kms:ViaService"] == ("string", ["s3.ca-central-1.amazonaws.com"])
    assert keys["aws:ViaAWSService"] == ("boolean", ["true"])
    assert keys["aws:SourceIp"][0] == "ip"


def test_report_passes_when_every_decision_matches():
    cases = dca.expected_cases(NAMES)
    result = dca.report(dca.simulate(FakeIam(cases), "arn:user", cases))
    assert result["passed"] and result["failures"] == [] and result["checked"] == len(cases)


def test_report_fails_on_an_unexpected_allow_or_deny():
    cases = dca.expected_cases(NAMES)
    result = dca.report(dca.simulate(FakeIam(cases, wrong={"glue:DeleteTable"}), "arn:user", cases))
    assert not result["passed"]
    expected = {(f["case"], f["expected"]) for f in result["failures"]}
    assert ("drop a Gold table", "denied") in expected
    assert ("drop a sandbox table", "allowed") in expected
