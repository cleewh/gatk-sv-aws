#!/usr/bin/env python3
"""Launch the full optimized GatherSampleEvidence pipeline for one sample.

Runs all tasks in parallel:
- CollectCounts (8 CPU, pre-localize)
- CollectSVEvidence (8 CPU, pre-localize)
- Manta (16 CPU)
- Wham sharded (24 parallel per-chromosome runs)
- Scramble (as-is)

Usage:
    python3 gatk-sv-healthomics/scripts/run_optimized_gse.py
"""

import json
import os
import time

import boto3

REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/gatk-sv-healthomics-run-role"
CACHE_ID = os.environ.get("GATK_SV_RUN_CACHE_ID", "__RUN_CACHE_ID__")
SAMPLE_ID = "NA12878"
OUTPUT_BASE = f"s3://healthomics-outputs-{ACCOUNT_ID}-apse1/runs/gatk-sv-e2e/{SAMPLE_ID}/optimized"

# Buckets
COHORT_BUCKET = f"omics-cohorts-{REGION}-{ACCOUNT_ID}"
REF_BUCKET = f"omics-ref-{REGION}-{ACCOUNT_ID}"

# Input paths
CRAM = f"s3://{COHORT_BUCKET}/cohorts/gatk-sv-validation-2026q2/{SAMPLE_ID}.final.cram"
CRAI = f"s3://healthomics-outputs-{ACCOUNT_ID}-apse1/runs/gatk-sv-e2e/{SAMPLE_ID}/reindex/9773901/out/new_crai/{SAMPLE_ID}.cram.crai"
REF = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/Homo_sapiens_assembly38.fasta"
REF_FAI = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/Homo_sapiens_assembly38.fasta.fai"
REF_DICT = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/Homo_sapiens_assembly38.dict"
GATK_JAR = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/gatk-4.6.2.0-local.jar"
INTERVALS = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/gs_preprocessed_intervals.interval_list"
CONTIGS = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/gs_primary_contigs.list"
DBSNP = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/Homo_sapiens_assembly38.dbsnp138.vcf"
MANTA_BED = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/manta_region_bed"
MANTA_TBI = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/manta_region_bed.tbi"
MEI_BED = f"s3://{REF_BUCKET}/gatk-sv/reference/GRCh38/mei_bed"

# Images
SV_BASE = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base:2024-10-25-v0.29-beta-5ea22a52"
MANTA_IMG = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/manta:2023-09-14-v0.28.3-beta-3f22f94d"
WHAM_IMG = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/wham:2024-10-25-v0.29-beta-5ea22a52"
SCRAMBLE_IMG = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/scramble:2024-10-25-v0.29-beta-5ea22a52"

# Optimized workflow IDs (final validated config)
# Sequential scanners: no pre-localization, FUSE handles sequential reads fine
WF_COLLECT_COUNTS_OPT = "3901751"   # Original: 4 CPU, 7.5 GB
WF_COLLECT_SV_OPT = "7038412"      # Original: 4 CPU, 7.5 GB
# Random-access tools: pre-localize CRAM to local disk for fast random I/O
WF_MANTA_OPT = "4091926"           # 16 CPU, 32 GB, pre-localize (4x faster)
WF_WHAM_PARALLEL = "5369691"       # 48 CPU, 64 GB, pre-localize + 24 parallel whamg
WF_SCRAMBLE = "9489928"            # 2 CPU, 16 GB, pre-localize

# Primary contigs for Wham sharding
PRIMARY_CONTIGS = [
    "chr1", "chr2", "chr3", "chr4", "chr5", "chr6",
    "chr7", "chr8", "chr9", "chr10", "chr11", "chr12",
    "chr13", "chr14", "chr15", "chr16", "chr17", "chr18",
    "chr19", "chr20", "chr21", "chr22", "chrX", "chrY",
]

client = boto3.client("omics", region_name=REGION)


def start_run(workflow_id: str, name: str, output_uri: str, parameters: dict) -> str:
    """Submit a HealthOmics run and return the run ID."""
    response = client.start_run(
        workflowId=workflow_id,
        workflowType="PRIVATE",
        roleArn=ROLE_ARN,
        name=name,
        outputUri=output_uri,
        parameters=parameters,
        storageType="DYNAMIC",
        cacheId=CACHE_ID,
        cacheBehavior="CACHE_ALWAYS",
    )
    return response["id"]


def main():
    print(f"=== Optimized GatherSampleEvidence: {SAMPLE_ID} ===")
    print(f"Region: {REGION}")
    print()

    runs = {}

    # 1. CollectCounts (optimized: 8 CPU + pre-localize)
    run_id = start_run(
        WF_COLLECT_COUNTS_OPT,
        f"cc-opt-{SAMPLE_ID}",
        f"{OUTPUT_BASE}/collect-counts/",
        {
            "cram_or_bam": CRAM,
            "cram_or_bam_idx": CRAI,
            "sample_id": SAMPLE_ID,
            "ref_fasta": REF,
            "ref_fasta_fai": REF_FAI,
            "ref_fasta_dict": REF_DICT,
            "gatk_jar": GATK_JAR,
            "intervals": INTERVALS,
            "docker": SV_BASE,
        },
    )
    runs["CollectCounts"] = run_id
    print(f"  CollectCounts (8 CPU, pre-localize): {run_id}")

    # 2. CollectSVEvidence (optimized: 8 CPU + pre-localize)
    run_id = start_run(
        WF_COLLECT_SV_OPT,
        f"cse-opt-{SAMPLE_ID}",
        f"{OUTPUT_BASE}/collect-sv-evidence/",
        {
            "cram_or_bam": CRAM,
            "cram_or_bam_idx": CRAI,
            "sample_id": SAMPLE_ID,
            "ref_fasta": REF,
            "ref_fasta_fai": REF_FAI,
            "ref_fasta_dict": REF_DICT,
            "gatk_jar": GATK_JAR,
            "preprocessed_intervals": INTERVALS,
            "primary_contigs_list": CONTIGS,
            "sd_locs_vcf": DBSNP,
            "docker": SV_BASE,
        },
    )
    runs["CollectSVEvidence"] = run_id
    print(f"  CollectSVEvidence (8 CPU, pre-localize): {run_id}")

    # 3. Manta (optimized: 16 CPU)
    run_id = start_run(
        WF_MANTA_OPT,
        f"manta-opt-{SAMPLE_ID}",
        f"{OUTPUT_BASE}/manta/",
        {
            "cram_or_bam": CRAM,
            "cram_or_bam_idx": CRAI,
            "sample_id": SAMPLE_ID,
            "ref_fasta": REF,
            "ref_fasta_fai": REF_FAI,
            "manta_region_bed": MANTA_BED,
            "manta_region_bed_index": MANTA_TBI,
            "manta_docker": MANTA_IMG,
        },
    )
    runs["Manta"] = run_id
    print(f"  Manta (16 CPU): {run_id}")

    # 4. Scramble (with symlink fix)
    run_id = start_run(
        WF_SCRAMBLE,
        f"scramble-opt-{SAMPLE_ID}",
        f"{OUTPUT_BASE}/scramble/",
        {
            "cram_or_bam": CRAM,
            "cram_or_bam_idx": CRAI,
            "sample_id": SAMPLE_ID,
            "ref_fasta": REF,
            "ref_fasta_fai": REF_FAI,
            "mei_bed": MEI_BED,
            "scramble_docker": SCRAMBLE_IMG,
        },
    )
    runs["Scramble"] = run_id
    print(f"  Scramble (symlink fix): {run_id}")

    # 5. Wham parallel (single large instance, 24 parallel whamg processes)
    run_id = start_run(
        WF_WHAM_PARALLEL,
        f"wham-parallel-{SAMPLE_ID}",
        f"{OUTPUT_BASE}/wham/",
        {
            "cram_or_bam": CRAM,
            "cram_or_bam_idx": CRAI,
            "sample_id": SAMPLE_ID,
            "ref_fasta": REF,
            "ref_fasta_fai": REF_FAI,
            "primary_contigs_list": CONTIGS,
            "wham_docker": WHAM_IMG,
        },
    )
    runs["Wham"] = run_id
    print(f"  Wham parallel (48 CPU, 24 processes): {run_id}")

    print()
    print(f"Total runs launched: 5")
    print()

    # Save run IDs for later status checking
    output_file = "gatk-sv-healthomics/optimized-run-ids.json"
    with open(output_file, "w") as f:
        json.dump(runs, f, indent=2)
    print(f"Run IDs saved to: {output_file}")
    print()
    print("Monitor with:")
    print(f"  aws omics list-runs --region {REGION} --query \"items[?contains(name, 'opt')].{{name:name,status:status}}\"")


if __name__ == "__main__":
    main()
