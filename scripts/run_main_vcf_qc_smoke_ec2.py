#!/usr/bin/env python3
"""Smoke test for the EC2 hybrid MainVcfQC.

Dispatches scripts/run_main_vcf_qc_ec2.sh via SSM to run the
IdentifyDuplicates + MergeDuplicates portion of MainVcfQC against the
10-sample validation cohort's manta VCFs.

Usage:
    AWS_ACCOUNT_ID=687677765589 AWS_DEFAULT_REGION=ap-southeast-1 \\
    .venv/bin/python scripts/run_main_vcf_qc_smoke_ec2.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = os.environ["AWS_ACCOUNT_ID"]
REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
EC2_INSTANCE_ID = os.environ.get("GATK_SV_EC2_INSTANCE_ID", "i-02c67bb34211a85ed")
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"

SAMPLES = [
    "HG00096", "HG00097", "HG00099", "HG00100", "HG00101",
    "HG00102", "HG00513", "NA12878", "NA19238", "NA19239",
]


def discover_manta_vcfs() -> list[str]:
    s3 = boto3.client("s3", region_name=REGION)
    vcfs = []
    for sid in SAMPLES:
        page = s3.list_objects_v2(
            Bucket=OUTPUT_BUCKET,
            Prefix=f"runs/gatk-sv-e2e/gatk-sv-validation-2026q2-rerun-2026-05-25/{sid}/gse/manta/",
            Delimiter="/",
        )
        prefixes = [p["Prefix"] for p in (page.get("CommonPrefixes") or [])]
        if not prefixes:
            raise RuntimeError(f"No manta runs for {sid}")
        files = s3.list_objects_v2(
            Bucket=OUTPUT_BUCKET, Prefix=f"{prefixes[0]}out/"
        )
        keys = [o["Key"] for o in (files.get("Contents") or [])]
        match = next((k for k in keys if k.endswith(".vcf.gz")), None)
        if match:
            vcfs.append(f"s3://{OUTPUT_BUCKET}/{match}")
    return vcfs


def main() -> int:
    cohort_prefix = "main-vcf-qc-smoke-2026-05-27-ec2"
    print(f"Discovering manta VCFs for the 10-sample cohort...")
    vcfs = discover_manta_vcfs()
    if len(vcfs) != 10:
        print(f"ERROR: expected 10 VCFs, found {len(vcfs)}", file=sys.stderr)
        return 1
    print(f"  Found {len(vcfs)} VCFs")
    print()

    s3 = boto3.client("s3", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)

    sh_local = ROOT / "scripts" / "run_main_vcf_qc_ec2.sh"
    sh_key = f"workflows/run-main-vcf-qc-ec2/{cohort_prefix}/run_main_vcf_qc_ec2.sh"
    s3.put_object(Bucket=OUTPUT_BUCKET, Key=sh_key, Body=sh_local.read_bytes())
    print(f"  Uploaded shell to s3://{OUTPUT_BUCKET}/{sh_key}")

    vcfs_newline = "\n".join(vcfs)
    env_block = f"""\
export AWS_ACCOUNT_ID={ACCOUNT}
export AWS_DEFAULT_REGION={REGION}
export COHORT_PREFIX={cohort_prefix}
export GATK_SV_COHORT_ID={cohort_prefix}
export VCFS='{vcfs_newline}'
"""
    commands = [
        f"aws s3 cp s3://{OUTPUT_BUCKET}/{sh_key} /tmp/run_main_vcf_qc_ec2.sh --region {REGION}",
        "chmod +x /tmp/run_main_vcf_qc_ec2.sh",
        env_block + "bash /tmp/run_main_vcf_qc_ec2.sh",
    ]
    resp = ssm.send_command(
        InstanceIds=[EC2_INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
        Comment=f"main-vcf-qc-ec2-{cohort_prefix}",
        TimeoutSeconds=3600,
    )
    cmd_id = resp["Command"]["CommandId"]
    print(f"  SSM command id: {cmd_id}")
    print(f"  Instance:       {EC2_INSTANCE_ID}")
    print()
    record = {
        "ssm_command_id": cmd_id,
        "instance_id": EC2_INSTANCE_ID,
        "cohort_prefix": cohort_prefix,
        "input_vcf_count": len(vcfs),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (ROOT / "main-vcf-qc-smoke-ec2-runs.json").write_text(json.dumps(record, indent=2))
    print(f"  Run record:     main-vcf-qc-smoke-ec2-runs.json")
    print()
    print("To poll status:")
    print(f"  aws ssm get-command-invocation --command-id {cmd_id} \\")
    print(f"      --instance-id {EC2_INSTANCE_ID} --region {REGION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
