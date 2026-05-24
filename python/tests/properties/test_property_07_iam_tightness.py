# Feature: gatk-sv-healthomics-migration, Property 7: IAM policy tightness
"""Property 7 — IAM policy tightness.

For any declared cohort RoleScope (Reference_Bundle prefix, input prefix,
output prefix, ECR repository list, log group prefix), the synthesized
run-role policy SHALL grant S3, ECR, and CloudWatch Logs actions only
against resources within the declared scope; for any candidate policy
that introduces a statement broader than the declared scope — whether a
wildcarded ``Resource``, a strict prefix of the declared prefix, or a
wildcarded action set — the broadness checker SHALL reject the policy
and name each offending statement.

See design §Correctness Properties → Property 7 and §IAM & Security.

**Validates: Requirements 12.2, 12.3, 12.4, 12.5, 12.6**

This test is RED until Task 3.5.1 implements ``synthesize_run_role`` and
Task 3.5.2 implements ``check_broadness``.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from gatk_sv_aws.iam import (
    check_broadness,
    synthesize_run_role,
)
from gatk_sv_aws.models import RoleScope

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_bucket = st.from_regex(r"\A[a-z0-9][a-z0-9-]{2,30}[a-z0-9]\Z", fullmatch=True)
# Prefix has at least one slash-delimited segment and never ends in '*' (RoleScope validator).
_prefix = st.from_regex(r"\A[a-z0-9][a-z0-9/_-]{1,40}\Z", fullmatch=True).filter(
    lambda s: not s.endswith("*")
)
_repo = st.from_regex(r"\A[a-z0-9][a-z0-9/_-]{1,30}[a-z0-9]\Z", fullmatch=True)
_account_id = st.integers(min_value=10**11, max_value=10**12 - 1).map(str)


@st.composite
def role_scope_strategy(draw: st.DrawFn) -> RoleScope:
    return RoleScope(
        region="ap-southeast-1",
        reference_bucket=draw(_bucket),
        reference_prefix=draw(_prefix),
        input_bucket=draw(_bucket),
        input_prefix=draw(_prefix),
        output_bucket=draw(_bucket),
        output_prefix=draw(_prefix),
        wdl_zip_bucket=draw(_bucket),
        wdl_zip_prefix=draw(_prefix),
        ecr_account_id=draw(_account_id),
        ecr_repositories=draw(st.lists(_repo, min_size=1, max_size=4, unique=True)),
        log_group_prefix="/aws/omics/",
    )


def _inject_broadness(policy: dict, kind: str) -> tuple[dict, str]:
    """Return a policy with one statement widened plus the Sid of the widening."""
    mutated = {**policy, "Statement": [dict(s) for s in policy["Statement"]]}
    target = mutated["Statement"][0]
    sid = target.get("Sid", "Statement0")
    if kind == "wildcard_resource":
        target["Resource"] = "*"
    elif kind == "wildcard_action":
        target["Action"] = "s3:*"
    elif kind == "bucket_without_prefix":
        if isinstance(target.get("Resource"), list):
            target["Resource"] = [r.split("/")[0] for r in target["Resource"]]
        else:
            target["Resource"] = target["Resource"].split("/")[0]
    return mutated, sid


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------


@given(scope=role_scope_strategy())
def test_property_07a_clean_policy_has_no_violations(scope: RoleScope) -> None:
    """A freshly synthesized policy passes the broadness check."""

    policies = synthesize_run_role(scope)
    violations = check_broadness(policies.permissions_policy, scope)

    assert violations == [] or (hasattr(violations, "__len__") and len(violations) == 0)


@given(
    scope=role_scope_strategy(),
    kind=st.sampled_from(["wildcard_resource", "wildcard_action", "bucket_without_prefix"]),
)
def test_property_07b_widened_policy_flagged(scope: RoleScope, kind: str) -> None:
    """Any widened statement is flagged by the broadness check and its Sid named."""

    policies = synthesize_run_role(scope)
    widened, sid = _inject_broadness(policies.permissions_policy, kind)

    violations = check_broadness(widened, scope)

    assert len(violations) >= 1, f"broadness check missed {kind}"
    assert any(v.statement_sid == sid for v in violations), (
        f"broadness check did not name the offending statement Sid={sid}"
    )
