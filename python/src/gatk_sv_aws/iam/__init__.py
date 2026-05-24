"""Component (e): IAM Role Synthesizer for the GATK-SV migration.

Implements design §Components and interfaces → (e) IAM Role Synthesizer.
Synthesizes a HealthOmics run role scoped to Target_Region with read/write
access limited to the declared S3 prefixes, ECR pull scoped to the mapped
repositories, and CloudWatch Logs writes scoped to ``/aws/omics/*``. Runs a
broadness check that rejects any statement broader than the declared scope.

Advances Requirement 12 (IAM Role and Least Privilege).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from gatk_sv_aws.models import (
    BroadnessViolation,
    RolePolicies,
    RoleScope,
)


# ---------------------------------------------------------------------------
# Statement SIDs (stable strings so tests and operators can cite them)
# ---------------------------------------------------------------------------

SID_S3_READ_REFS_AND_INPUTS = "S3ReadReferencesAndInputs"
SID_S3_WRITE_OUTPUTS = "S3WriteOutputs"
SID_ECR_PULL_MAPPED_REPOS = "EcrPullMappedReposOnly"
SID_ECR_AUTH = "EcrAuth"
SID_LOGS_WRITE_OMICS = "LogsWriteOmicsOnly"

# Actions/resources that are legitimately wildcarded by the AWS APIs.
# ``ecr:GetAuthorizationToken`` MUST use ``Resource: "*"`` per the ECR API
# contract; the broadness check knowingly exempts it.
_ALLOWED_WILDCARD_SIDS = frozenset({SID_ECR_AUTH})

# Permission-level wildcards that are always rejected outside the exempt set.
_FORBIDDEN_ACTION_WILDCARDS = (
    "s3:*",
    "ecr:*",
    "logs:*",
    "*",
)


# ---------------------------------------------------------------------------
# Policy synthesis
# ---------------------------------------------------------------------------


def _s3_prefix_arn(bucket: str, prefix: str) -> str:
    """Return the ``arn:aws:s3:::bucket/prefix/*`` form used in Resource lists."""
    prefix = prefix.rstrip("/")
    return f"arn:aws:s3:::{bucket}/{prefix}/*"


def _s3_bucket_arn(bucket: str) -> str:
    return f"arn:aws:s3:::{bucket}"


def _ecr_repo_arn(region: str, account_id: str, repo_name: str) -> str:
    return f"arn:aws:ecr:{region}:{account_id}:repository/{repo_name}"


def synthesize_run_role(scope: RoleScope) -> RolePolicies:
    """Synthesize least-privilege permissions + trust policies for the cohort scope.

    Implementation target of Task 3.5.1 (Req 12.1–12.5).
    """
    region = scope.region
    account_id = scope.ecr_account_id
    log_prefix = scope.log_group_prefix.rstrip("/")

    # --- S3 read (references, inputs, WDL zips) -----------------------------
    s3_read_resources = [
        _s3_prefix_arn(scope.reference_bucket, scope.reference_prefix),
        _s3_bucket_arn(scope.reference_bucket),
        _s3_prefix_arn(scope.input_bucket, scope.input_prefix),
        _s3_bucket_arn(scope.input_bucket),
        _s3_prefix_arn(scope.wdl_zip_bucket, scope.wdl_zip_prefix),
        _s3_bucket_arn(scope.wdl_zip_bucket),
    ]

    # --- S3 write (outputs only) --------------------------------------------
    s3_write_resources = [
        _s3_prefix_arn(scope.output_bucket, scope.output_prefix),
    ]

    # --- ECR pull (mapped repos only) ---------------------------------------
    ecr_pull_resources = [
        _ecr_repo_arn(region, account_id, repo_name)
        for repo_name in scope.ecr_repositories
    ]

    # --- CloudWatch Logs write (/aws/omics/* only) --------------------------
    logs_resource = f"arn:aws:logs:{region}:{account_id}:log-group:{log_prefix}/*"

    permissions_policy: dict[str, Any] = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": SID_S3_READ_REFS_AND_INPUTS,
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": s3_read_resources,
            },
            {
                "Sid": SID_S3_WRITE_OUTPUTS,
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": s3_write_resources,
            },
            {
                "Sid": SID_ECR_PULL_MAPPED_REPOS,
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": ecr_pull_resources,
            },
            {
                "Sid": SID_ECR_AUTH,
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*",
            },
            {
                "Sid": SID_LOGS_WRITE_OMICS,
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                "Resource": logs_resource,
            },
        ],
    }

    trust_policy: dict[str, Any] = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OmicsAssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": "omics.amazonaws.com"},
                "Action": "sts:AssumeRole",
            },
        ],
    }

    violations = check_broadness(permissions_policy, scope)

    return RolePolicies(
        permissions_policy=permissions_policy,
        trust_policy=trust_policy,
        broadness_violations=violations,
    )


# ---------------------------------------------------------------------------
# Broadness check (Property 7, Req 12.6)
# ---------------------------------------------------------------------------


def _collect_resources(stmt_resource: Any) -> list[str]:
    if isinstance(stmt_resource, list):
        return [str(r) for r in stmt_resource]
    return [str(stmt_resource)]


def _collect_actions(stmt_action: Any) -> list[str]:
    if isinstance(stmt_action, list):
        return [str(a) for a in stmt_action]
    return [str(stmt_action)]


def _declared_resources_for_stmt(sid: str, scope: RoleScope) -> tuple[list[str], str]:
    """Return (allowed_resources, human_label) for a given statement Sid.

    The returned resources are the maximum ARN surface a statement with the
    given Sid may reference. Any resource outside this surface is considered
    a broadness violation.
    """
    region = scope.region
    account_id = scope.ecr_account_id
    log_prefix = scope.log_group_prefix.rstrip("/")

    if sid == SID_S3_READ_REFS_AND_INPUTS:
        return (
            [
                _s3_prefix_arn(scope.reference_bucket, scope.reference_prefix),
                _s3_bucket_arn(scope.reference_bucket),
                _s3_prefix_arn(scope.input_bucket, scope.input_prefix),
                _s3_bucket_arn(scope.input_bucket),
                _s3_prefix_arn(scope.wdl_zip_bucket, scope.wdl_zip_prefix),
                _s3_bucket_arn(scope.wdl_zip_bucket),
            ],
            "S3 read prefix",
        )
    if sid == SID_S3_WRITE_OUTPUTS:
        return (
            [_s3_prefix_arn(scope.output_bucket, scope.output_prefix)],
            "S3 write prefix",
        )
    if sid == SID_ECR_PULL_MAPPED_REPOS:
        return (
            [
                _ecr_repo_arn(region, account_id, repo_name)
                for repo_name in scope.ecr_repositories
            ],
            "ECR repository ARN",
        )
    if sid == SID_LOGS_WRITE_OMICS:
        return (
            [f"arn:aws:logs:{region}:{account_id}:log-group:{log_prefix}/*"],
            "CloudWatch Logs log-group prefix",
        )
    # Unknown Sid: no declared surface — broader than declared by definition
    # unless the API mandates wildcard (handled separately).
    return ([], f"unknown Sid {sid!r}")


def _declared_actions_for_stmt(sid: str) -> set[str]:
    """Return the exact set of actions a statement with ``sid`` may list."""
    if sid == SID_S3_READ_REFS_AND_INPUTS:
        return {"s3:GetObject", "s3:ListBucket"}
    if sid == SID_S3_WRITE_OUTPUTS:
        return {"s3:PutObject", "s3:AbortMultipartUpload"}
    if sid == SID_ECR_PULL_MAPPED_REPOS:
        return {
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchCheckLayerAvailability",
        }
    if sid == SID_ECR_AUTH:
        return {"ecr:GetAuthorizationToken"}
    if sid == SID_LOGS_WRITE_OMICS:
        return {"logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"}
    return set()


def _is_bucket_arn_without_prefix(arn: str) -> bool:
    """True iff ``arn`` is ``arn:aws:s3:::bucket`` with no ``/key`` component."""
    return arn.startswith("arn:aws:s3:::") and "/" not in arn[len("arn:aws:s3:::") :]


def check_broadness(
    policy: dict[str, Any], scope: RoleScope
) -> list[BroadnessViolation]:
    """Return every policy statement broader than the declared scope.

    Rules enforced (Design §IAM & Security; Req 12.6):
    1. ``Resource`` is ``"*"`` for any non-exempt Sid.
    2. ``Action`` contains a service-wildcard (``s3:*``, ``ecr:*``, ``logs:*``)
       or the global ``"*"``.
    3. For S3 statements: any bare bucket ARN in a Sid whose declared scope
       uses a prefix (bucket ARNs are allowed alongside prefix ARNs in READ
       statements per AWS S3 conventions; we only flag them where the
       declared scope doesn't permit them).
    4. Any ARN that doesn't appear in, and isn't a prefix-match of, the
       declared ARN surface for the statement's Sid.
    5. Any Action outside the declared action set for the statement's Sid.

    Returns an empty list when the policy is within the declared scope.
    """
    violations: list[BroadnessViolation] = []
    statements = policy.get("Statement", [])

    for stmt in statements:
        sid = stmt.get("Sid", "UNKNOWN")
        resources = _collect_resources(stmt.get("Resource", []))
        actions = _collect_actions(stmt.get("Action", []))

        allowed_arns, label = _declared_resources_for_stmt(sid, scope)
        allowed_actions = _declared_actions_for_stmt(sid)
        allowed_arn_set = set(allowed_arns)

        # Rule 1: wildcard Resource
        for resource in resources:
            if resource == "*":
                if sid in _ALLOWED_WILDCARD_SIDS:
                    continue  # exempt; ECR auth intentionally wildcards
                violations.append(
                    BroadnessViolation(
                        statement_sid=sid,
                        resource=resource,
                        declared_scope=", ".join(allowed_arns) or "(no declared scope)",
                        reason="wildcard Resource",
                    )
                )
                continue

        # Rule 2: wildcarded Action
        for action in actions:
            if action in _FORBIDDEN_ACTION_WILDCARDS:
                violations.append(
                    BroadnessViolation(
                        statement_sid=sid,
                        resource=", ".join(resources),
                        declared_scope=", ".join(sorted(allowed_actions))
                        or "(no declared actions)",
                        reason=f"wildcarded Action {action!r}",
                    )
                )

        # Rule 3: bare bucket ARN in a Sid whose declared surface has no bucket-only ARN.
        for resource in resources:
            if resource == "*":
                continue  # already handled by Rule 1
            if _is_bucket_arn_without_prefix(resource):
                if resource not in allowed_arn_set:
                    violations.append(
                        BroadnessViolation(
                            statement_sid=sid,
                            resource=resource,
                            declared_scope=", ".join(allowed_arns)
                            or "(no declared scope)",
                            reason=(
                                f"bucket ARN without prefix; {label} requires a prefix"
                            ),
                        )
                    )
                    continue

        # Rule 3b: every declared PREFIX ARN must be present in the candidate.
        # When a candidate drops a prefix ARN (path-stripping widening), the
        # grant collapses to a bucket-wide grant, broader than the declared scope.
        declared_prefix_arns = {arn for arn in allowed_arns if "/" in arn}
        if declared_prefix_arns and not any(
            "/" in r for r in resources if r != "*"
        ):
            for declared in sorted(declared_prefix_arns):
                violations.append(
                    BroadnessViolation(
                        statement_sid=sid,
                        resource=", ".join(resources),
                        declared_scope=declared,
                        reason=(
                            f"candidate dropped declared prefix ARN {declared!r}; "
                            f"grant now broader than declared {label}"
                        ),
                    )
                )
                break  # one violation per Sid is enough signal

        # Rule 4: ARN outside the declared surface.
        for resource in resources:
            if resource == "*":
                continue
            if resource in allowed_arn_set:
                continue
            # Allow exact match only — strict prefixes of a declared ARN are
            # considered widened (they grant more than the declared prefix).
            violations.append(
                BroadnessViolation(
                    statement_sid=sid,
                    resource=resource,
                    declared_scope=", ".join(allowed_arns)
                    or "(no declared scope)",
                    reason=f"Resource {resource!r} not in declared {label} set",
                )
            )

        # Rule 5: Action outside the declared action set.
        if allowed_actions:
            for action in actions:
                if action in _FORBIDDEN_ACTION_WILDCARDS:
                    continue  # already flagged by Rule 2
                if action not in allowed_actions:
                    violations.append(
                        BroadnessViolation(
                            statement_sid=sid,
                            resource=", ".join(resources),
                            declared_scope=", ".join(sorted(allowed_actions)),
                            reason=f"Action {action!r} not in declared action set",
                        )
                    )

    return violations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def policy_copy(policy: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy suitable for test-time mutation."""
    return deepcopy(policy)


__all__ = [
    "BroadnessViolation",
    "RolePolicies",
    "RoleScope",
    "SID_S3_READ_REFS_AND_INPUTS",
    "SID_S3_WRITE_OUTPUTS",
    "SID_ECR_PULL_MAPPED_REPOS",
    "SID_ECR_AUTH",
    "SID_LOGS_WRITE_OMICS",
    "synthesize_run_role",
    "check_broadness",
    "policy_copy",
]
