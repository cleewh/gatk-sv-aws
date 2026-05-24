#!/usr/bin/env python3
"""Launch MakeCohortVcf v17 — serialize the failing recluster step."""
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
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v17.zip"
)


def main() -> None:
    client = boto3.client("omics", region_name=REGION)

    # Reuse the v16 run parameters as the baseline (same flat tracks shape).
    prior = client.get_run(id="1041904")
    params = dict(prior.get("parameters", {}))
    # Make sure no leftover tarball/array params remain.
    for k in ("track_bed_tarball", "track_bed_files", "track_names"):
        params.pop(k, None)

    bundle_bytes = BUNDLE.read_bytes()
    print(f"Creating MakeCohortVcf-v17 ({len(bundle_bytes):,} bytes)…")
    print("  Recluster step serialized (single task processes all 24 contigs)")
    resp = client.create_workflow(
        name="MakeCohortVcf-v17",
        description=(
            "v17: All 24 contigs reclustered sequentially in one task. "
            "Avoids HealthOmics terminating concurrent GroupedSVCluster scatters."
        ),
        engine="WDL",
        definitionZip=bundle_bytes,
        main="wdl/MakeCohortVcf.wdl",
        storageCapacity=20,
        tags={
            "gatk-sv:module": "MakeCohortVcf",
            "gatk-sv:version": "v17-serial-recluster",
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
        name="make-cohort-vcf-v17-serial",
        roleArn=ROLE_ARN,
        outputUri=f"{OUTPUT_BASE}/batch/make-cohort-vcf/",
        parameters=params,
        storageType="DYNAMIC",
        logLevel="ALL",
        tags={
            "gatk-sv:module": "MakeCohortVcf",
            "gatk-sv:version": "v17-serial-recluster",
        },
    )
    print()
    print(f"Run id:      {run['id']}")
    print(f"Workflow id: {wf_id}")


if __name__ == "__main__":
    main()
