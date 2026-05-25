#!/usr/bin/env python3
"""Create the HealthOmics run cache for the customer's account.

Idempotent: if a run cache with the same name already exists, returns its id
without creating a new one.
"""
from __future__ import annotations

import os
import sys

import boto3
from botocore.exceptions import ClientError

CACHE_NAME = "gatk-sv-pipeline-cache"


def main() -> int:
    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1

    omics = boto3.client("omics", region_name=region)

    # Region-short suffix for the outputs bucket name.
    region_short = {
        "ap-southeast-1": "apse1", "ap-southeast-2": "apse2",
        "ap-northeast-1": "apne1", "us-east-1": "use1",
        "us-west-2": "usw2", "eu-west-1": "euw1",
    }.get(region, region)
    bucket = f"healthomics-outputs-{account}-{region_short}"
    cache_s3 = f"s3://{bucket}/run-cache/"

    # Look for an existing cache with the same name.
    paginator = omics.get_paginator("list_run_caches")
    for page in paginator.paginate():
        for c in page.get("items", []):
            if c.get("name") == CACHE_NAME:
                cache_id = c["id"]
                print(f"Run cache already exists: id={cache_id}")
                print(f"\nNext step: export GATK_SV_RUN_CACHE_ID={cache_id}")
                return 0

    resp = omics.create_run_cache(
        name=CACHE_NAME,
        cacheBehavior="CACHE_ALWAYS",
        cacheS3Location=cache_s3,
        description="Run cache for GATK-SV HealthOmics pipeline",
        tags={
            "gatk-sv:resource": "run-cache",
            "gatk-sv:environment": "production",
        },
    )
    print(f"Created run cache id={resp['id']} status={resp['status']}")
    print(f"\nNext step: export GATK_SV_RUN_CACHE_ID={resp['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
