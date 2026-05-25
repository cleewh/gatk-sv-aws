#!/usr/bin/env python3
"""Validate that the customer's account is fully provisioned.

Reads only — does not modify anything. Exits 0 if everything is in place.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "[OK]" if ok else "[FAIL]"
    print(f"  {mark:<6s} {label}: {detail}")
    return ok


def main() -> int:
    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID required", file=sys.stderr)
        return 1

    region_short = {
        "ap-southeast-1": "apse1", "ap-southeast-2": "apse2",
        "us-east-1": "use1", "us-west-2": "usw2", "eu-west-1": "euw1",
    }.get(region, region)

    s3 = boto3.client("s3", region_name=region)
    iam = boto3.client("iam")
    ec2 = boto3.client("ec2", region_name=region)
    omics = boto3.client("omics", region_name=region)

    all_ok = True
    repo_root = Path(__file__).resolve().parent.parent.parent

    print(f"Validating account {account} in {region}\n")
    print("=== S3 buckets ===")
    for label, name in [
        ("ref",     f"omics-ref-{region}-{account}"),
        ("cohorts", f"omics-cohorts-{region}-{account}"),
        ("wdl",     f"omics-wdl-{region}-{account}"),
        ("outputs", f"healthomics-outputs-{account}-{region_short}"),
    ]:
        try:
            s3.head_bucket(Bucket=name)
            all_ok &= check(f"  bucket {label}", True, name)
        except ClientError as e:
            all_ok &= check(f"  bucket {label}", False, f"{name}: {e.response['Error']['Code']}")

    print("\n=== IAM run role ===")
    try:
        role = iam.get_role(RoleName="gatk-sv-healthomics-run-role")["Role"]
        all_ok &= check("run role", True, role["Arn"])
    except ClientError as e:
        all_ok &= check("run role", False, str(e.response["Error"]["Code"]))

    print("\n=== HealthOmics run cache ===")
    cache_id_env = os.environ.get("GATK_SV_RUN_CACHE_ID")
    if cache_id_env:
        try:
            c = omics.get_run_cache(id=cache_id_env)
            all_ok &= check("cache", c["status"] == "ACTIVE",
                            f"id={cache_id_env} status={c['status']}")
        except ClientError as e:
            all_ok &= check("cache", False, str(e.response["Error"]["Code"]))
    else:
        all_ok &= check("cache", False, "GATK_SV_RUN_CACHE_ID not set")

    print("\n=== EC2 hybrid instance ===")
    iid_env = os.environ.get("GATK_SV_EC2_INSTANCE_ID")
    if iid_env:
        try:
            r = ec2.describe_instances(InstanceIds=[iid_env])
            inst = r["Reservations"][0]["Instances"][0]
            all_ok &= check("ec2 hybrid", True,
                            f"{iid_env} state={inst['State']['Name']} type={inst['InstanceType']}")
        except ClientError as e:
            all_ok &= check("ec2 hybrid", False, str(e.response["Error"]["Code"]))
    else:
        all_ok &= check("ec2 hybrid", False, "GATK_SV_EC2_INSTANCE_ID not set")

    print("\n=== HealthOmics workflows ===")
    workflow_ids_path = repo_root / "workflow-ids.json"
    if workflow_ids_path.exists():
        wfs = json.loads(workflow_ids_path.read_text())
        for module, info in wfs.items():
            try:
                w = omics.get_workflow(id=info["workflow_id"])
                all_ok &= check(f"  workflow {module}", w["status"] == "ACTIVE",
                                f"id={info['workflow_id']} status={w['status']}")
            except ClientError as e:
                all_ok &= check(f"  workflow {module}", False, str(e.response["Error"]["Code"]))
    else:
        all_ok &= check("workflow-ids.json", False, "missing; run 08_register_workflows.py")

    print()
    if all_ok:
        print("All checks passed. Account is ready to run a cohort.")
        return 0
    print("Some checks failed. Re-run the missing bootstrap step(s).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
