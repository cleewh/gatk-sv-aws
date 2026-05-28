#!/usr/bin/env python3
"""Smoke test for MainVcfQC (workflow id 1551065).

Goal: verify the registered MainVcfQC workflow runs end-to-end on
HealthOmics. We don't have a real cohort SV VCF available — the cohort
modules (Phase B) haven't run end-to-end on HealthOmics in this account
in a consolidated location. So we use the 10 per-sample manta VCFs as
a stand-in for the `vcfs` input array.

This isn't a domain-quality test (manta per-sample VCFs aren't a true
cohort VCF), but it WILL surface workflow-level HealthOmics issues:
  - 47-second kill on multi-task scatter/gather
  - Parameter template mismatches
  - Docker image accessibility
  - IAM permissions

Usage:
    AWS_ACCOUNT_ID=<account> AWS_DEFAULT_REGION=ap-southeast-1 \\
    .venv/bin/python scripts/run_main_vcf_qc_smoke.py
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

REF_BASE = f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
GSE_PREFIX = (
    f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/"
    "gatk-sv-validation-2026q2-rerun-2026-05-25"
)
SAMPLES = [
    "HG00096", "HG00097", "HG00099", "HG00100", "HG00101",
    "HG00102", "HG00513", "NA12878", "NA19238", "NA19239",
]


def discover_manta_vcfs() -> list[str]:
    """Build the list of manta vcf.gz S3 URIs for the 10 sample cohort."""
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
    print(f"Discovering manta VCFs for {len(SAMPLES)} samples...")
    vcfs = discover_manta_vcfs()
    print(f"  Found {len(vcfs)} VCFs")
    print()

    workflow_ids = json.loads((ROOT / "workflow-ids.json").read_text())
    workflow_id = workflow_ids["MainVcfQC"]["workflow_id"]
    print(f"MainVcfQC workflow id: {workflow_id}")

    # Smaller fai for the smoke test to dodge the per-contig scatter
    # blowing up. Use gs_primary_contigs.fai (24 chroms, no decoys).
    parameters = {
        "vcfs": vcfs,
        "vcf_format_has_cn": False,  # manta VCFs don't have CN field
        "prefix": "main-vcf-qc-smoke-2026-05-27",
        "sv_per_shard": 5000,         # Shard size for vcf-stats sub-tasks
        "samples_per_shard": 10,      # 10 samples; one shard
        "do_per_sample_qc": False,    # skip per-sample QC (avoids ped_file requirement)
        "primary_contigs_fai": f"{REF_BASE}/gs_primary_contigs.fai",
        "sv_base_mini_docker":   f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52",
        "sv_pipeline_docker":    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604",
        "sv_pipeline_qc_docker": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604",
    }

    omics = boto3.client("omics", region_name=REGION)
    output_uri = f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/main-vcf-qc-smoke-2026-05-27/"

    print(f"Submitting MainVcfQC run...")
    print(f"  workflowId:  {workflow_id}")
    print(f"  outputUri:   {output_uri}")
    print()

    resp = omics.start_run(
        workflowId=workflow_id,
        name="main-vcf-qc-smoke-2026-05-27",
        roleArn=ROLE_ARN,
        outputUri=output_uri,
        parameters=parameters,
        storageType="DYNAMIC",
        cacheId=os.environ.get("GATK_SV_RUN_CACHE_ID", "9564200"),
        cacheBehavior="CACHE_ALWAYS",
        tags={
            "gatk-sv:cohort-id":        "main-vcf-qc-smoke-2026-05-27",
            "gatk-sv:workflow-version": f"main-vcf-qc-{workflow_id}",
            "gatk-sv:module":           "MainVcfQC",
            "gatk-sv:sample-count":     str(len(SAMPLES)),
            "gatk-sv:environment":      "validation",
        },
    )
    run_id = resp["id"]
    print(f"Run started: {run_id}")
    print(f"  arn: {resp['arn']}")
    record = {
        "run_id": run_id,
        "arn": resp["arn"],
        "workflow_id": workflow_id,
        "output_uri": output_uri,
        "input_vcf_count": len(vcfs),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (ROOT / "main-vcf-qc-smoke-runs.json").write_text(json.dumps(record, indent=2))
    print(f"Run record: main-vcf-qc-smoke-runs.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
