#!/usr/bin/env python3
"""Create the HealthOmics run role with synthesized least-privilege policy.

Reads:
  iam/policies/gatk-sv-run-role-trust.json  (trust policy)
  iam/policies/gatk-sv-run-role.json        (least-privilege permissions)

Both should already be filled with the customer's account id by
00_substitute_placeholders.py.

Creates the role ``gatk-sv-healthomics-run-role`` (idempotent: PutRolePolicy if
already present).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROLE_NAME = "gatk-sv-healthomics-run-role"
INLINE_POLICY_NAME = "gatk-sv-run-role"


def main() -> int:
    account = os.environ.get("AWS_ACCOUNT_ID")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parent.parent.parent
    trust_path = repo_root / "iam" / "policies" / "gatk-sv-run-role-trust.json"
    policy_path = repo_root / "iam" / "policies" / "gatk-sv-run-role.json"

    trust_doc = trust_path.read_text()
    policy_doc = policy_path.read_text()

    if "__ACCOUNT_ID__" in trust_doc or "__ACCOUNT_ID__" in policy_doc:
        print("ERROR: __ACCOUNT_ID__ placeholder still present.", file=sys.stderr)
        print("       Run 00_substitute_placeholders.py first.", file=sys.stderr)
        return 2

    iam = boto3.client("iam")

    # Create the role (or skip if exists).
    try:
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=trust_doc,
            Description="HealthOmics run role for GATK-SV pipeline (least-privilege)",
            Tags=[
                {"Key": "gatk-sv:resource", "Value": "run-role"},
                {"Key": "gatk-sv:environment", "Value": "production"},
            ],
        )
        print(f"Created role {ROLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            print(f"Role {ROLE_NAME} already exists; updating policies.")
        else:
            raise

    # Always reapply the trust + permissions policies (idempotent).
    iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=trust_doc)
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=INLINE_POLICY_NAME,
        PolicyDocument=policy_doc,
    )
    print(f"  trust policy + inline permissions updated")

    role = iam.get_role(RoleName=ROLE_NAME)["Role"]
    print(f"\nRole ARN: {role['Arn']}")
    print(f"\nNext: export this for downstream scripts that don't already use it:")
    print(f"  export GATK_SV_HEALTHOMICS_ROLE_ARN={role['Arn']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
