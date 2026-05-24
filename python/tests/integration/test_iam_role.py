"""Integration test: synthesized HealthOmics run role exists (Req 12).

Confirms the role lives in IAM, its trust policy permits
``omics.amazonaws.com:sts:AssumeRole``, and its inline policy is the
5-Sid shape the IAM Role Synthesizer produces.
"""

from __future__ import annotations

import json

import pytest

EXPECTED_SIDS = {
    "S3ReadReferencesAndInputs",
    "S3WriteOutputs",
    "EcrPullMappedReposOnly",
    "EcrAuth",
    "LogsWriteOmicsOnly",
}


@pytest.mark.aws_integration
def test_run_role_exists_and_trusts_healthomics() -> None:
    import boto3

    iam = boto3.client("iam")  # type: ignore[attr-defined]
    try:
        role = iam.get_role(RoleName="gatk-sv-healthomics-run-role")["Role"]
    except iam.exceptions.NoSuchEntityException:
        pytest.skip(
            "gatk-sv-healthomics-run-role not yet created; run deploy.py step 10."
        )

    trust = role["AssumeRolePolicyDocument"]
    statements = trust.get("Statement", [])
    services = {
        s.get("Principal", {}).get("Service") for s in statements if s.get("Effect") == "Allow"
    }
    assert "omics.amazonaws.com" in services


@pytest.mark.aws_integration
def test_run_role_policy_has_expected_sids() -> None:
    import boto3

    iam = boto3.client("iam")  # type: ignore[attr-defined]
    try:
        response = iam.get_role_policy(
            RoleName="gatk-sv-healthomics-run-role",
            PolicyName="gatk-sv-run-policy",
        )
    except iam.exceptions.NoSuchEntityException:
        pytest.skip("gatk-sv-run-policy not attached; run deploy.py step 10.")

    # PolicyDocument can come back URL-encoded string or dict depending on SDK.
    doc = response["PolicyDocument"]
    if isinstance(doc, str):
        doc = json.loads(doc)
    sids = {stmt["Sid"] for stmt in doc["Statement"] if "Sid" in stmt}
    assert sids == EXPECTED_SIDS, f"expected {EXPECTED_SIDS}, got {sids}"
