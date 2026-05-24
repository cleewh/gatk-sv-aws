"""Unit tests for the IAM Role Synthesizer (Task 3.5.4).

Covers each broadness rule with one positive (accepted) and one negative
(rejected) example (Req 12.6, Design §IAM & Security).
"""

from __future__ import annotations

from gatk_sv_aws.iam import (
    SID_ECR_AUTH,
    SID_ECR_PULL_MAPPED_REPOS,
    SID_LOGS_WRITE_OMICS,
    SID_S3_READ_REFS_AND_INPUTS,
    SID_S3_WRITE_OUTPUTS,
    check_broadness,
    policy_copy,
    synthesize_run_role,
)
from gatk_sv_aws.models import RoleScope

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


def _scope() -> RoleScope:
    return RoleScope(
        region="ap-southeast-1",
        reference_bucket="refs",
        reference_prefix="gatk-sv/refs",
        input_bucket="inputs",
        input_prefix="gatk-sv/in",
        output_bucket="outputs",
        output_prefix="gatk-sv/out",
        wdl_zip_bucket="wdl",
        wdl_zip_prefix="gatk-sv/wdl",
        ecr_account_id="123456789012",
        ecr_repositories=["gatk-sv/sv-pipeline", "gatk-sv/manta"],
        log_group_prefix="/aws/omics/",
    )


def _find_statement(policy, sid):
    for stmt in policy["Statement"]:
        if stmt.get("Sid") == sid:
            return stmt
    raise AssertionError(f"Sid {sid} not found in policy")


# ---------------------------------------------------------------------------
# Positive case — clean synthesis
# ---------------------------------------------------------------------------


def test_synthesize_clean_policy_has_no_violations() -> None:
    scope = _scope()
    policies = synthesize_run_role(scope)
    assert policies.broadness_violations == []

    # Five statements: S3 read, S3 write, ECR pull, ECR auth, Logs.
    sids = {s["Sid"] for s in policies.permissions_policy["Statement"]}
    assert sids == {
        SID_S3_READ_REFS_AND_INPUTS,
        SID_S3_WRITE_OUTPUTS,
        SID_ECR_PULL_MAPPED_REPOS,
        SID_ECR_AUTH,
        SID_LOGS_WRITE_OMICS,
    }


def test_trust_policy_allows_omics_service() -> None:
    policies = synthesize_run_role(_scope())
    principal = policies.trust_policy["Statement"][0]["Principal"]
    assert principal == {"Service": "omics.amazonaws.com"}
    assert policies.trust_policy["Statement"][0]["Action"] == "sts:AssumeRole"


def test_ecr_auth_wildcard_resource_is_exempt() -> None:
    policies = synthesize_run_role(_scope())
    ecr_auth = _find_statement(policies.permissions_policy, SID_ECR_AUTH)
    assert ecr_auth["Resource"] == "*"
    assert policies.broadness_violations == []


# ---------------------------------------------------------------------------
# Rule 1 — wildcard Resource
# ---------------------------------------------------------------------------


def test_broadness_flags_wildcard_resource_outside_ecr_auth() -> None:
    scope = _scope()
    policies = synthesize_run_role(scope)
    mutated = policy_copy(policies.permissions_policy)
    _find_statement(mutated, SID_S3_WRITE_OUTPUTS)["Resource"] = "*"

    violations = check_broadness(mutated, scope)

    assert any(
        v.statement_sid == SID_S3_WRITE_OUTPUTS and "wildcard Resource" in v.reason
        for v in violations
    )


# ---------------------------------------------------------------------------
# Rule 2 — wildcarded Action
# ---------------------------------------------------------------------------


def test_broadness_flags_service_wildcard_action() -> None:
    scope = _scope()
    policies = synthesize_run_role(scope)
    mutated = policy_copy(policies.permissions_policy)
    _find_statement(mutated, SID_S3_READ_REFS_AND_INPUTS)["Action"] = "s3:*"

    violations = check_broadness(mutated, scope)

    assert any(
        v.statement_sid == SID_S3_READ_REFS_AND_INPUTS
        and "wildcarded Action" in v.reason
        for v in violations
    )


def test_broadness_flags_global_wildcard_action() -> None:
    scope = _scope()
    policies = synthesize_run_role(scope)
    mutated = policy_copy(policies.permissions_policy)
    _find_statement(mutated, SID_ECR_PULL_MAPPED_REPOS)["Action"] = "*"

    violations = check_broadness(mutated, scope)

    assert any(
        v.statement_sid == SID_ECR_PULL_MAPPED_REPOS
        and "wildcarded Action" in v.reason
        for v in violations
    )


# ---------------------------------------------------------------------------
# Rule 3 / 3b — bucket-without-prefix (prefix ARN dropped)
# ---------------------------------------------------------------------------


def test_broadness_flags_bucket_without_prefix() -> None:
    scope = _scope()
    policies = synthesize_run_role(scope)
    mutated = policy_copy(policies.permissions_policy)
    stmt = _find_statement(mutated, SID_S3_WRITE_OUTPUTS)
    # Strip the prefix, leaving only the bare bucket ARN.
    stmt["Resource"] = [r.split("/")[0] for r in stmt["Resource"]]

    violations = check_broadness(mutated, scope)

    assert any(
        v.statement_sid == SID_S3_WRITE_OUTPUTS
        and "dropped declared prefix ARN" in v.reason
        for v in violations
    )


# ---------------------------------------------------------------------------
# Rule 4 — resource outside declared scope
# ---------------------------------------------------------------------------


def test_broadness_flags_outside_declared_arn_scope() -> None:
    scope = _scope()
    policies = synthesize_run_role(scope)
    mutated = policy_copy(policies.permissions_policy)
    stmt = _find_statement(mutated, SID_ECR_PULL_MAPPED_REPOS)
    stmt["Resource"] = [
        "arn:aws:ecr:ap-southeast-1:123456789012:repository/unrelated/repo"
    ]

    violations = check_broadness(mutated, scope)

    assert any(
        v.statement_sid == SID_ECR_PULL_MAPPED_REPOS
        and "not in declared ECR repository ARN set" in v.reason
        for v in violations
    )


# ---------------------------------------------------------------------------
# Rule 5 — action outside declared set
# ---------------------------------------------------------------------------


def test_broadness_flags_action_outside_declared_set() -> None:
    scope = _scope()
    policies = synthesize_run_role(scope)
    mutated = policy_copy(policies.permissions_policy)
    stmt = _find_statement(mutated, SID_S3_READ_REFS_AND_INPUTS)
    stmt["Action"] = ["s3:GetObject", "s3:DeleteObject"]  # DeleteObject is not declared

    violations = check_broadness(mutated, scope)

    assert any(
        v.statement_sid == SID_S3_READ_REFS_AND_INPUTS
        and "not in declared action set" in v.reason
        for v in violations
    )
