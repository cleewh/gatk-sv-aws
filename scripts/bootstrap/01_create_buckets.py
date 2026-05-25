#!/usr/bin/env python3
"""Create the four S3 buckets the pipeline expects.

Buckets:
  omics-ref-{region}-{account}        Reference bundle (FASTA, BEDs, gCNV models, ...)
  omics-cohorts-{region}-{account}    Per-cohort sample inputs (CRAM/BAM + indexes)
  omics-wdl-{region}-{account}        Workflow definition ZIPs (registrar uses this)
  healthomics-outputs-{account}-{short_region}   Run outputs + cache + cost reports

Region mappings used in the output bucket name (HealthOmics historical convention):
  ap-southeast-1 -> apse1
  ap-southeast-2 -> apse2
  ap-northeast-1 -> apne1
  us-east-1 -> use1
  us-west-2 -> usw2
  eu-west-1 -> euw1

For other regions, falls back to the full region string.

Sets:
  - Encryption: AES256 (default, can be upgraded to SSE-KMS post-creation)
  - Versioning: enabled on the outputs bucket only (cohort runs care about old versions)
  - Intelligent-Tiering: default storage class on the outputs bucket
  - Block public access: enabled on all four (HealthOmics doesn't need public)

Idempotent: skips buckets that already exist.
"""
from __future__ import annotations

import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION_SHORT = {
    "ap-southeast-1": "apse1",
    "ap-southeast-2": "apse2",
    "ap-northeast-1": "apne1",
    "ap-northeast-2": "apne2",
    "ap-south-1": "aps1",
    "us-east-1": "use1",
    "us-east-2": "use2",
    "us-west-1": "usw1",
    "us-west-2": "usw2",
    "eu-west-1": "euw1",
    "eu-west-2": "euw2",
    "eu-central-1": "euc1",
}


def short(region: str) -> str:
    return REGION_SHORT.get(region, region)


def bucket_names(account: str, region: str) -> dict[str, str]:
    return {
        "ref":      f"omics-ref-{region}-{account}",
        "cohorts":  f"omics-cohorts-{region}-{account}",
        "wdl":      f"omics-wdl-{region}-{account}",
        "outputs":  f"healthomics-outputs-{account}-{short(region)}",
    }


def create_bucket(s3, name: str, region: str) -> bool:
    """Create the bucket if it doesn't exist. Returns True if created, False if pre-existing."""
    try:
        s3.head_bucket(Bucket=name)
        return False
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise

    kwargs = {"Bucket": name}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kwargs)
    return True


def configure_outputs_bucket(s3, name: str) -> None:
    """Enable versioning + Intelligent-Tiering as default storage class."""
    s3.put_bucket_versioning(
        Bucket=name,
        VersioningConfiguration={"Status": "Enabled"},
    )
    # Intelligent-Tiering is set per-object at upload time. We can configure
    # a bucket-wide IT lifecycle that transitions all objects to IT after 1 day.
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "transition-to-intelligent-tiering",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Transitions": [
                        {"Days": 1, "StorageClass": "INTELLIGENT_TIERING"},
                    ],
                },
            ],
        },
    )


def block_public_access(s3, name: str) -> None:
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )


def encrypt(s3, name: str) -> None:
    s3.put_bucket_encryption(
        Bucket=name,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}},
            ],
        },
    )


def main() -> int:
    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1

    s3 = boto3.client("s3", region_name=region)
    names = bucket_names(account, region)

    print(f"Creating S3 buckets in {region}, account {account}")
    print()
    for label, name in names.items():
        print(f"  {label:<10s} {name}")
        try:
            created = create_bucket(s3, name, region)
            if created:
                print(f"    created")
            else:
                print(f"    already exists, configuring")
            block_public_access(s3, name)
            encrypt(s3, name)
            if label == "outputs":
                configure_outputs_bucket(s3, name)
        except ClientError as e:
            print(f"    ERROR: {e.response['Error']['Code']} {e.response['Error']['Message']}")
            return 2
    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
