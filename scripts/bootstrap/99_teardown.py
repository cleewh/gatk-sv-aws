#!/usr/bin/env python3
"""Tear down everything the bootstrap scripts created.

Lists every resource that would be deleted, asks for confirmation, then deletes.

Resources removed:
  - HealthOmics workflows (all from workflow-ids.json)
  - HealthOmics run cache (gatk-sv-pipeline-cache)
  - EC2 instance tagged gatk-sv:role=ec2-hybrid (terminated)
  - EC2 security group gatk-sv-ec2-hybrid-sg
  - EC2 instance profile gatk-sv-ec2-hybrid + role + policies
  - IAM role gatk-sv-healthomics-run-role + inline policies
  - S3 buckets: omics-ref-*, omics-cohorts-*, omics-wdl-*, healthomics-outputs-*
    (only emptied + deleted with --delete-buckets; default is to leave them)

Use --confirm to actually run the deletions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="Actually perform the deletions (default is dry-run)")
    ap.add_argument("--delete-buckets", action="store_true",
                    help="Empty + delete the four S3 buckets too (default leaves them)")
    args = ap.parse_args()

    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID required", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parent.parent.parent
    wfs_path = repo_root / "workflow-ids.json"

    omics = boto3.client("omics", region_name=region)
    iam = boto3.client("iam")
    ec2 = boto3.client("ec2", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    plan: list[tuple[str, callable]] = []

    # 1. Workflows
    if wfs_path.exists():
        for module, info in json.loads(wfs_path.read_text()).items():
            wid = info.get("workflow_id")
            if wid:
                plan.append((f"DELETE workflow {module} (id={wid})",
                             lambda i=wid: omics.delete_workflow(id=i)))

    # 2. Run cache
    for c in omics.list_run_caches().get("items", []):
        if c.get("name") == "gatk-sv-pipeline-cache":
            plan.append((f"DELETE run cache (id={c['id']})",
                         lambda i=c["id"]: omics.delete_run_cache(id=i)))

    # 3. EC2 hybrid instance
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:gatk-sv:role", "Values": ["ec2-hybrid"]},
            {"Name": "instance-state-name",
             "Values": ["pending", "running", "stopping", "stopped"]},
        ],
    )
    for r in resp["Reservations"]:
        for inst in r["Instances"]:
            iid = inst["InstanceId"]
            plan.append((f"TERMINATE ec2 instance {iid}",
                         lambda i=iid: ec2.terminate_instances(InstanceIds=[i])))

    # 4. SG (after instance is gone)
    sgs = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["gatk-sv-ec2-hybrid-sg"]}],
    )["SecurityGroups"]
    for sg in sgs:
        plan.append((f"DELETE security group {sg['GroupId']}",
                     lambda g=sg["GroupId"]: ec2.delete_security_group(GroupId=g)))

    # 5. EC2 instance profile + role
    plan.append(("DELETE iam instance profile gatk-sv-ec2-hybrid",
                 lambda: _safe(lambda: iam.remove_role_from_instance_profile(
                     InstanceProfileName="gatk-sv-ec2-hybrid", RoleName="gatk-sv-ec2-hybrid"))))
    plan.append(("DELETE iam instance profile gatk-sv-ec2-hybrid (delete profile)",
                 lambda: _safe(lambda: iam.delete_instance_profile(InstanceProfileName="gatk-sv-ec2-hybrid"))))
    plan.append(("DELETE iam role gatk-sv-ec2-hybrid policies + role",
                 lambda: _delete_role_completely(iam, "gatk-sv-ec2-hybrid",
                                                 attached=["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"])))

    # 6. HealthOmics run role
    plan.append(("DELETE iam role gatk-sv-healthomics-run-role",
                 lambda: _delete_role_completely(iam, "gatk-sv-healthomics-run-role")))

    # 7. Buckets (optional)
    if args.delete_buckets:
        region_short = {
            "ap-southeast-1": "apse1", "ap-southeast-2": "apse2",
            "us-east-1": "use1", "us-west-2": "usw2", "eu-west-1": "euw1",
        }.get(region, region)
        for label, name in [
            ("ref",     f"omics-ref-{region}-{account}"),
            ("cohorts", f"omics-cohorts-{region}-{account}"),
            ("wdl",     f"omics-wdl-{region}-{account}"),
            ("outputs", f"healthomics-outputs-{account}-{region_short}"),
        ]:
            plan.append((f"EMPTY + DELETE bucket {name}",
                         lambda n=name: _empty_and_delete_bucket(s3, n)))

    print(f"Teardown plan ({len(plan)} actions):")
    for i, (desc, _) in enumerate(plan, 1):
        print(f"  {i:2d}. {desc}")
    print()

    if not args.confirm:
        print("Dry-run only. Re-run with --confirm to actually delete.")
        return 0

    print("Executing deletions...")
    for desc, fn in plan:
        try:
            fn()
            print(f"  OK   {desc}")
        except Exception as e:  # noqa: BLE001
            print(f"  WARN {desc}: {type(e).__name__}: {e}")
    print("\nDone.")
    return 0


def _safe(fn) -> None:
    try:
        fn()
    except ClientError:
        pass


def _delete_role_completely(iam, role_name: str, attached: list[str] | None = None) -> None:
    try:
        for p in iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", []):
            iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
        for p in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
            iam.delete_role_policy(RoleName=role_name, PolicyName=p)
        iam.delete_role(RoleName=role_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise


def _empty_and_delete_bucket(s3, name: str) -> None:
    try:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=name):
            objects = []
            for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
                objects.append({"Key": v["Key"], "VersionId": v["VersionId"]})
            for i in range(0, len(objects), 1000):
                s3.delete_objects(Bucket=name, Delete={"Objects": objects[i:i + 1000]})
        s3.delete_bucket(Bucket=name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchBucket":
            raise


if __name__ == "__main__":
    sys.exit(main())
