#!/usr/bin/env python3
"""Launch MakeCohortVcf v16 — track files passed as separate File inputs.

This is the cleanup of the GroupedSVCluster diagnostic finding (run 5601461,
workflow 8667186): GroupedSVCluster ran fine on HealthOmics when track .bed.gz
files were passed as individual File inputs rather than extracted from a
runtime tarball. v16 swaps the tarball for three flat File pairs.
"""
import os
from __future__ import annotations

import sys
import time
from pathlib import Path

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
OUTPUT_BASE = (
    f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e"
)
REF_BASE = (
    f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"
)
BUNDLE = Path(
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v16.zip"
)


def main() -> None:
    client = boto3.client("omics", region_name=REGION)

    # Pull v15 run parameters as the baseline (same upstream commit, same
    # reference paths, same VCF inputs).
    prior = client.get_run(id="8724741")
    params = dict(prior.get("parameters", {}))

    # Drop the v15 tarball/array shape; substitute six concrete files.
    for k in ("track_bed_tarball", "track_bed_files", "track_names"):
        params.pop(k, None)
    params["track_simrep"] = (
        f"{REF_BASE}/hg38.SimpRep.sorted.pad_100.merged.bed.gz"
    )
    params["track_simrep_idx"] = (
        f"{REF_BASE}/hg38.SimpRep.sorted.pad_100.merged.bed.gz.tbi"
    )
    params["track_segdups"] = f"{REF_BASE}/segdups.bed.gz"
    params["track_segdups_idx"] = f"{REF_BASE}/segdups.bed.gz.tbi"
    params["track_rmsk"] = f"{REF_BASE}/rmsk.bed.gz"
    params["track_rmsk_idx"] = f"{REF_BASE}/rmsk.bed.gz.tbi"

    # Use the corrected stratification configs validated in session 5.
    params["stratification_config_part1"] = (
        f"{REF_BASE}/stratify_config.v2.part_one.tsv"
    )
    params["stratification_config_part2"] = (
        f"{REF_BASE}/stratify_config.v2.part_two.tsv"
    )

    bundle_bytes = BUNDLE.read_bytes()
    print(f"Creating MakeCohortVcf-v16 ({len(bundle_bytes):,} bytes)…")
    print("  Track files passed as flat File inputs (no runtime tarball)")
    resp = client.create_workflow(
        name="MakeCohortVcf-v16",
        description=(
            "v16: track files as separate File inputs. Diagnostic run "
            "5601461 proved this shape works on HealthOmics."
        ),
        engine="WDL",
        definitionZip=bundle_bytes,
        main="wdl/MakeCohortVcf.wdl",
        storageCapacity=20,
        tags={
            "gatk-sv:module": "MakeCohortVcf",
            "gatk-sv:version": "v16-flat-tracks",
        },
    )
    wf_id = resp["id"]
    print(f"  Workflow id: {wf_id}")

    for _ in range(60):
        time.sleep(10)
        info = client.get_workflow(id=wf_id)
        status = info.get("status", "UNKNOWN")
        if status == "ACTIVE":
            print("  Workflow ACTIVE")
            break
        if status in {"FAILED", "INACTIVE", "DELETED"}:
            print(
                f"  Workflow status {status}: "
                f"{info.get('statusMessage','')[:500]}"
            )
            sys.exit(1)
    else:
        print("  Timed out waiting for workflow to become ACTIVE")
        sys.exit(1)

    run = client.start_run(
        workflowId=wf_id,
        name="make-cohort-vcf-v16-flat-tracks",
        roleArn=ROLE_ARN,
        outputUri=f"{OUTPUT_BASE}/batch/make-cohort-vcf/",
        parameters=params,
        storageType="DYNAMIC",
        logLevel="ALL",
        tags={
            "gatk-sv:module": "MakeCohortVcf",
            "gatk-sv:version": "v16-flat-tracks",
        },
    )
    print()
    print(f"Run id:      {run['id']}")
    print(f"Workflow id: {wf_id}")


if __name__ == "__main__":
    main()
