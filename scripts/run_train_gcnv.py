#!/usr/bin/env python3
"""
Launch TrainGCNV on the validation cohort counts files.

Usage:
    python run_train_gcnv.py [--dry-run]

Requires: All 10 CollectCounts runs to have completed first.
"""

import os
import argparse
import json
import sys
from pathlib import Path

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
OUTPUT_BASE = f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e"
REF_BASE = f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"

WORKFLOW_ID = "2282352"
COHORT_ID = "gatk-sv-validation-2026q2"

# Docker images
DOCKER = {
    "gatk": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85",
    "linux": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/ecr-public/lts/ubuntu:18.04",
    "sv_base_mini": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52",
}

# Samples in the validation cohort
SAMPLES = [
    "NA12878", "HG00096", "HG00097", "HG00099", "HG00100",
    "HG00101", "HG00102", "NA19238", "NA19239", "HG00513",
]


def get_counts_uri(sample_id: str) -> str:
    """Get the expected counts file URI for a sample.
    
    Checks multiple possible output locations from GSE runs.
    """
    # The CC runs output to different paths depending on which workflow was used.
    # NA12878 has a known path from the previous run.
    # Other samples use the cohort GSE script output path.
    if sample_id == "NA12878":
        return f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/NA12878/optimized/collect-counts/7634786/out/counts/NA12878.counts.tsv.gz"
    else:
        # From run_gse_cohort.py, CC outputs go to:
        # s3://healthomics-outputs-.../runs/gatk-sv-e2e/{sample}/gse/cc/{run_id}/out/counts/{sample}.counts.tsv.gz
        # We don't know the run_id yet, so we'll need to discover it.
        # For now, use a placeholder pattern that the launch script will resolve.
        return f"PENDING:{sample_id}"


def discover_counts_files(s3_client) -> dict:
    """Discover completed counts files for all samples."""
    counts = {}
    
    for sample_id in SAMPLES:
        if sample_id == "NA12878":
            # Known path from previous successful run
            counts[sample_id] = f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/NA12878/optimized/collect-counts/7634786/out/counts/NA12878.counts.tsv.gz"
            continue
        
        # Search for the counts file in the GSE output path
        prefix = f"runs/gatk-sv-e2e/{sample_id}/gse/cc/"
        bucket = f"healthomics-outputs-{ACCOUNT}-apse1"
        
        try:
            paginator = s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    if obj['Key'].endswith('.counts.tsv.gz'):
                        counts[sample_id] = f"s3://{bucket}/{obj['Key']}"
                        break
                if sample_id in counts:
                    break
        except Exception as e:
            print(f"  Warning: Could not find counts for {sample_id}: {e}")
    
    return counts


def main():
    parser = argparse.ArgumentParser(description="Launch TrainGCNV")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="Only check if counts files exist")
    args = parser.parse_args()

    s3_client = boto3.client("s3", region_name=REGION)
    omics_client = boto3.client("omics", region_name=REGION)

    print("Discovering counts files for all samples...")
    counts = discover_counts_files(s3_client)
    
    print(f"\nFound counts for {len(counts)}/{len(SAMPLES)} samples:")
    for sample_id in SAMPLES:
        status = "✓" if sample_id in counts else "✗ MISSING"
        uri = counts.get(sample_id, "NOT FOUND")
        print(f"  {status} {sample_id}: {uri}")
    
    missing = [s for s in SAMPLES if s not in counts]
    if missing:
        print(f"\n⚠️  Missing counts for {len(missing)} samples: {', '.join(missing)}")
        print("   Wait for GSE CollectCounts runs to complete, then retry.")
        if args.check_only:
            sys.exit(0)
        sys.exit(1)
    
    if args.check_only:
        print("\n✓ All counts files available. Ready to run TrainGCNV.")
        sys.exit(0)

    # Build parameters
    params = {
        "samples": SAMPLES,
        "count_files": [counts[s] for s in SAMPLES],
        "cohort": COHORT_ID,
        "reference_fasta": f"{REF_BASE}/Homo_sapiens_assembly38.fasta",
        "reference_index": f"{REF_BASE}/Homo_sapiens_assembly38.fasta.fai",
        "reference_dict": f"{REF_BASE}/Homo_sapiens_assembly38.dict",
        "contig_ploidy_priors": f"{REF_BASE}/gs_contig_ploidy_priors.tsv",
        "num_intervals_per_scatter": 100000,
        "ref_copy_number_autosomal_contigs": 2,
        "allosomal_contigs": ["chrX", "chrY"],
        "min_interval_size": 101,
        "max_interval_size": 2000,
        "filter_intervals": False,
        "gatk_docker": DOCKER["gatk"],
        "linux_docker": DOCKER["linux"],
        "sv_base_mini_docker": DOCKER["sv_base_mini"],
    }

    print(f"\nTrainGCNV parameters:")
    print(f"  Workflow ID: {WORKFLOW_ID}")
    print(f"  Cohort: {COHORT_ID}")
    print(f"  Samples: {len(SAMPLES)}")
    print(f"  num_intervals_per_scatter: 1000")
    print(f"  ref_copy_number_autosomal_contigs: 2")

    if args.dry_run:
        print("\n[DRY RUN] Would launch TrainGCNV with above parameters.")
        print(json.dumps(params, indent=2))
        return

    # Launch the run
    output_uri = f"{OUTPUT_BASE}/batch/train-gcnv/"
    
    resp = omics_client.start_run(
        workflowId=WORKFLOW_ID,
        name=f"train-gcnv-{COHORT_ID}",
        roleArn=ROLE_ARN,
        outputUri=output_uri,
        parameters=params,
        storageType="DYNAMIC",
    )
    
    run_id = resp["id"]
    print(f"\n✓ TrainGCNV launched: run {run_id}")
    print(f"  Output: {output_uri}")
    
    # Save run info
    output_file = Path(__file__).parent.parent / "train-gcnv-run.json"
    with open(output_file, "w") as f:
        json.dump({
            "run_id": run_id,
            "workflow_id": WORKFLOW_ID,
            "cohort": COHORT_ID,
            "samples": SAMPLES,
            "output_uri": output_uri,
        }, f, indent=2)
    print(f"  Run info saved to {output_file}")


if __name__ == "__main__":
    main()
