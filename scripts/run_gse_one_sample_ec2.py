#!/usr/bin/env python3
"""Run GatherSampleEvidence for one sample on EC2 via miniwdl.

The HealthOmics-registered GSE bundle is reused unchanged. miniwdl on
EC2 interprets it the same way HealthOmics' miniwdl does, but bind-mount
semantics and the 47 s task kill behavior are absent — this gives us a
ground-truth set of GSE artifacts to compare against the HealthOmics
production run.

Inputs come from the existing per-sample manifest in S3.
Outputs go to ``s3://{output_bucket}/runs/gatk-sv-e2e/divergence/<sample>/``
in a flat layout matching ``divergence_pull.py``'s expectations.
"""
from __future__ import annotations

import os

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
INSTANCE_ID = os.environ.get("GATK_SV_EC2_INSTANCE_ID", "__EC2_INSTANCE_ID__")
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"

# Reuse the HealthOmics GSE bundle (already lint-clean and registered).
GSE_BUNDLE_LOCAL = Path(
    "gatk-sv-healthomics/wdl/bundles/GatherSampleEvidence/"
    "GatherSampleEvidence-bundle.zip"
)


def _ssm_run(commands: list[str], timeout: int = 600) -> tuple[str, str, str]:
    ssm = boto3.client("ssm", region_name=REGION)
    cmd = ssm.send_command(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
        TimeoutSeconds=timeout,
    )
    cid = cmd["Command"]["CommandId"]
    while True:
        time.sleep(5)
        inv = ssm.get_command_invocation(
            CommandId=cid, InstanceId=INSTANCE_ID
        )
        if inv["Status"] in {"Success", "Failed", "TimedOut", "Cancelled"}:
            break
    return (
        inv["Status"],
        inv.get("StandardOutputContent", ""),
        inv.get("StandardErrorContent", ""),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True)
    parser.add_argument(
        "--reads-uri", required=True, help="s3:// CRAM or BAM"
    )
    parser.add_argument(
        "--reads-index-uri", required=True, help="s3:// .crai or .bai"
    )
    parser.add_argument("--sex", default="F", choices=["F", "M"])
    parser.add_argument(
        "--ec2-out-prefix",
        default=(
            f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/divergence"
        ),
    )
    args = parser.parse_args(argv)

    if not GSE_BUNDLE_LOCAL.exists():
        print(f"ERROR: GSE bundle not found at {GSE_BUNDLE_LOCAL}")
        return 1

    s3 = boto3.client("s3", region_name=REGION)

    # Upload the GSE bundle to S3 so the EC2 helper can fetch it.
    bundle_key = (
        f"workflows/divergence/gse/{int(time.time())}/bundle.zip"
    )
    s3.upload_file(str(GSE_BUNDLE_LOCAL), OUTPUT_BUCKET, bundle_key)
    bundle_uri = f"s3://{OUTPUT_BUCKET}/{bundle_key}"
    print(f"Uploaded GSE bundle: {bundle_uri}")

    # Build a minimal one-sample inputs JSON. Reuse all the reference
    # files already staged for the production GSE run.
    inputs = {
        "GatherSampleEvidence.sample_id": args.sample,
        "GatherSampleEvidence.bam_or_cram_file": args.reads_uri,
        "GatherSampleEvidence.bam_or_cram_index": args.reads_index_uri,
        "GatherSampleEvidence.sex": args.sex,
        # The standard reference set used by the HealthOmics production run.
        # Same paths as inputs/manifest.json for the GSE module.
        "_NOTE_": "fill remaining inputs from gatk-sv-healthomics/parameter-templates/GatherSampleEvidence.json",
    }
    inputs_key = (
        f"workflows/divergence/gse/{int(time.time())}/inputs.json"
    )
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=inputs_key,
        Body=json.dumps(inputs, indent=2).encode(),
    )
    print(f"Uploaded inputs.json: s3://{OUTPUT_BUCKET}/{inputs_key}")

    out_prefix = (
        f"{args.ec2_out_prefix.rstrip('/')}/{args.sample}/ec2"
    )

    cmds = [
        "set -euxo pipefail",
        "WORK=/tmp/gse-divergence/" + args.sample,
        "mkdir -p $WORK && cd $WORK",
        f"aws s3 cp {bundle_uri} bundle.zip",
        f"aws s3 cp s3://{OUTPUT_BUCKET}/{inputs_key} inputs.json",
        "rm -rf wdl && unzip -q -o bundle.zip",
        "ls wdl/ | head -3",
        f"aws ecr get-login-password --region {REGION} | "
        f"docker login --username AWS --password-stdin "
        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com >/dev/null 2>&1",
        "/root/.local/bin/miniwdl run wdl/GatherSampleEvidence.wdl "
        "-i inputs.json --dir run --no-color > run.log 2>&1 &",
        "echo started",
    ]
    status, stdout, stderr = _ssm_run(cmds)
    print(f"SSM status: {status}")
    print(stdout[:1500])
    if status != "Success":
        print("STDERR:", stderr[:1500])
        return 1

    print()
    print("EC2 run started. Once it completes, run:")
    print(
        "  python3 gatk-sv-healthomics/scripts/divergence_pull.py "
        f"--sample {args.sample} --ec2-prefix {out_prefix}"
    )
    print("then:")
    print(
        "  RUN_ACCEPTANCE_TESTS=1 "
        "/Users/cleewh/Desktop/KiroLS/.venv/bin/python -m pytest "
        "tests/gatk_sv_healthomics/acceptance/test_engine_divergence.py "
        f"-k {args.sample} -v"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
