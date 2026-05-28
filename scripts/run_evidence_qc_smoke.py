#!/usr/bin/env python3
"""Targeted smoke test for EvidenceQC (workflow id 9572643).

Submits EvidenceQC against the existing 10-sample 2026q2 cohort GSE outputs
(HG00096, HG00097, HG00099, HG00100, HG00101, HG00102, HG00513, NA12878,
NA19238, NA19239). All 10 samples have complete cc/manta/wham/scramble
outputs at:

  s3://healthomics-outputs-<acct>-apse1/runs/gatk-sv-e2e/
      gatk-sv-validation-2026q2-rerun-2026-05-25/<sample>/gse/<tool>/<run_id>/...

Goals:
  1. Validate that the freshly-registered EvidenceQC workflow (Phase 8 / Req 19)
     actually runs end-to-end on HealthOmics with real inputs.
  2. Capture wall-clock + per-task instance metadata for cost calculation.
  3. Confirm output artifacts (QC table, ploidy plots, WGD scores) land cleanly.

This is the targeted variant of Task 8.10 — full-pipeline 10-sample smoke
deferred until orchestrator wiring gaps are closed.

Usage:
    AWS_ACCOUNT_ID=<account> AWS_DEFAULT_REGION=ap-southeast-1 \\
    .venv/bin/python scripts/run_evidence_qc_smoke.py
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

# 10 samples from the original 2026q2 cohort. All have complete GSE outputs.
SAMPLES = [
    "HG00096", "HG00097", "HG00099", "HG00100", "HG00101",
    "HG00102", "HG00513", "NA12878", "NA19238", "NA19239",
]

# Per-sample per-tool S3 paths (discovered by inspecting the bucket).
GSE_PATHS = {
    "HG00096": {
        "cc":       f"{GSE_PREFIX}/HG00096/gse/cc/6242515/out/counts/HG00096.counts.tsv.gz",
        "manta":    f"{GSE_PREFIX}/HG00096/gse/manta/2571368/out/manta_vcf/HG00096.manta.vcf.gz",
        "wham":     f"{GSE_PREFIX}/HG00096/gse/wham/7674770/out/vcf/HG00096.wham.vcf.gz",
        "scramble": f"{GSE_PREFIX}/HG00096/gse/scramble/5022287/out/scramble_vcf/HG00096.scramble.vcf.gz",
    },
    # Others will be discovered at runtime.
}


def discover_gse_paths(samples: list[str]) -> dict[str, dict[str, str]]:
    """Walk the GSE output prefix and return {sample: {tool: s3_uri}} for each sample."""
    s3 = boto3.client("s3", region_name=REGION)
    paths: dict[str, dict[str, str]] = {}
    for sid in samples:
        sp: dict[str, str] = {}
        for tool, suffix in [("cc", ".counts.tsv.gz"), ("manta", ".vcf.gz"),
                              ("wham", ".vcf.gz"), ("scramble", ".vcf.gz")]:
            sample_prefix = f"runs/gatk-sv-e2e/gatk-sv-validation-2026q2-rerun-2026-05-25/{sid}/gse/{tool}/"
            page = s3.list_objects_v2(
                Bucket=OUTPUT_BUCKET, Prefix=sample_prefix, Delimiter="/"
            )
            prefixes = [p["Prefix"] for p in (page.get("CommonPrefixes") or [])]
            if not prefixes:
                continue
            run_id_prefix = prefixes[0]
            files = s3.list_objects_v2(
                Bucket=OUTPUT_BUCKET, Prefix=f"{run_id_prefix}out/"
            )
            keys = [o["Key"] for o in (files.get("Contents") or [])]
            match = next((k for k in keys if k.endswith(suffix)), None)
            if match:
                sp[tool] = f"s3://{OUTPUT_BUCKET}/{match}"
        paths[sid] = sp
    return paths


def main() -> int:
    print("Discovering GSE outputs for the 10-sample cohort...")
    paths = discover_gse_paths(SAMPLES)

    incomplete = [s for s in SAMPLES if not all(t in paths.get(s, {}) for t in ["cc", "manta", "wham", "scramble"])]
    if incomplete:
        print(f"ERROR: incomplete GSE outputs for: {incomplete}", file=sys.stderr)
        return 1
    print(f"  All 10 samples have complete cc/manta/wham/scramble outputs.")
    print()

    # Build the input arrays (parallel; same order as SAMPLES).
    counts_files = [paths[s]["cc"] for s in SAMPLES]
    manta_vcfs = [paths[s]["manta"] for s in SAMPLES]
    wham_vcfs = [paths[s]["wham"] for s in SAMPLES]
    scramble_vcfs = [paths[s]["scramble"] for s in SAMPLES]

    # Reference + dockers
    parameters = {
        "batch": "evidence-qc-smoke-2026-05-26",
        "samples": SAMPLES,
        "counts": counts_files,
        "manta_vcfs": manta_vcfs,
        "wham_vcfs": wham_vcfs,
        "scramble_vcfs": scramble_vcfs,
        # Disable RawVcfQC. The patched RawVcfQC.wdl drops the 47-second-kill
        # MergeVariantCounts + PickOutliers tasks, but downstream
        # CreateVariantCountPlots + MakeQcTable still expect their outputs.
        # Set run_vcf_qc=False so that whole cascade is skipped — we keep
        # the core gating outputs (bincov matrix, median coverage, ploidy,
        # WGD) which is what Phase B actually consumes.
        # Variant-count plots can be regenerated off-HealthOmics from the
        # per-sample VCFs if needed.
        "run_vcf_qc": False,
        "run_ploidy": True,
        "genome_file": f"{REF_BASE}/gs_hg38.genome",
        "wgd_scoring_mask": f"{REF_BASE}/gs_wgd_scoring_mask.bed",
        # Docker images (mirrored to private ECR via container-registry-map).
        # Note: as of upstream v1.1, sv_pipeline_qc_docker is consolidated
        # into sv_pipeline_docker (per dockers.json). Both use the same image.
        "sv_base_mini_docker":   f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52",
        "sv_base_docker":        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base:2024-10-25-v0.29-beta-5ea22a52",
        "sv_pipeline_docker":    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604",
        "sv_pipeline_qc_docker": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604",
    }

    # Read the EvidenceQC workflow id from workflow-ids.json.
    workflow_ids = json.loads((ROOT / "workflow-ids.json").read_text())
    workflow_id = workflow_ids["EvidenceQC"]["workflow_id"]
    print(f"EvidenceQC workflow id: {workflow_id}")

    omics = boto3.client("omics", region_name=REGION)
    output_uri = f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/evidence-qc-smoke-2026-05-26/"

    print(f"Submitting EvidenceQC run...")
    print(f"  workflowId:  {workflow_id}")
    print(f"  outputUri:   {output_uri}")
    print(f"  sample count: {len(SAMPLES)}")
    print()

    resp = omics.start_run(
        workflowId=workflow_id,
        name="evidence-qc-smoke-2026-05-26",
        roleArn=ROLE_ARN,
        outputUri=output_uri,
        parameters=parameters,
        storageType="DYNAMIC",
        cacheId=os.environ.get("GATK_SV_RUN_CACHE_ID", "9564200"),
        cacheBehavior="CACHE_ALWAYS",
        tags={
            "gatk-sv:cohort-id":        "evidence-qc-smoke-2026-05-26",
            "gatk-sv:workflow-version": f"evidence-qc-{workflow_id}",
            "gatk-sv:module":           "EvidenceQC",
            "gatk-sv:sample-count":     str(len(SAMPLES)),
            "gatk-sv:environment":      "validation",
        },
    )
    run_id = resp["id"]
    print(f"Run started: {run_id}")
    print(f"  arn: {resp['arn']}")
    print()
    print(f"To poll status:")
    print(f"  aws omics get-run --id {run_id} --region {REGION}")
    print()
    # Persist for later inspection.
    record = {
        "run_id": run_id,
        "arn": resp["arn"],
        "workflow_id": workflow_id,
        "output_uri": output_uri,
        "sample_count": len(SAMPLES),
        "samples": SAMPLES,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (ROOT / "evidence-qc-smoke-runs.json").write_text(json.dumps(record, indent=2))
    print(f"Run record: evidence-qc-smoke-runs.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
