#!/usr/bin/env python3
"""
GATK-SV End-to-End Pipeline Orchestrator for AWS HealthOmics.

Runs the full 10-module GATK-SV pipeline for a cohort, chaining outputs
import os
from each module as inputs to the next.

Usage:
    python run_pipeline.py --stage gbe        # Run GatherBatchEvidence
    python run_pipeline.py --stage cluster    # Run ClusterBatch
    python run_pipeline.py --stage all        # Run all remaining stages
    python run_pipeline.py --status           # Check pipeline status

Requires: boto3, completed GSE + TrainGCNV outputs
"""

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
OUTPUT_BASE = f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e"
REF_BASE = f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"

COHORT_ID = "gatk-sv-validation-2026q2"
BATCH_ID = "batch_01"

SAMPLES = [
    "NA12878", "HG00096", "HG00097", "HG00099", "HG00100",
    "HG00101", "HG00102", "NA19238", "NA19239", "HG00513",
]

# Workflow IDs for each pipeline module
WORKFLOWS = {
    "train_gcnv": "7318208",
    "gather_batch_evidence": "1575165",
    "cluster_batch": "6529905",
    "generate_batch_metrics": "5339393",
    "filter_batch": "6118948",
    "merge_batch_sites": "1825208",
    "genotype_batch": "9542089",
    "regenotype_cnvs": "8299455",
    "make_cohort_vcf": "3584634",  # v3: IndexFeatureFile fix for SVCluster + GroupedSVCluster, 8 GiB
    "annotate_vcf": "6832584",
}

# Docker images
DOCKER = {
    "gatk": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85",
    "linux": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/ecr-public/lts/ubuntu:18.04",
    "sv_base": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base:2024-10-25-v0.29-beta-5ea22a52",
    "sv_base_mini": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52",
    "sv_pipeline": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604",
    "cnmops": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/cnmops:2025-09-02-v1.0.5-f091af0b",
}

# Reference files
REF = {
    "fasta": f"{REF_BASE}/Homo_sapiens_assembly38.fasta",
    "fai": f"{REF_BASE}/Homo_sapiens_assembly38.fasta.fai",
    "dict": f"{REF_BASE}/Homo_sapiens_assembly38.dict",
    "primary_contigs_list": f"{REF_BASE}/gs_primary_contigs.list",
    "primary_contigs_fai": f"{REF_BASE}/gs_primary_contigs.fai",
    "autosome_fai": f"{REF_BASE}/gs_autosome.fai",
    "allosome_fai": f"{REF_BASE}/gs_allosome.fai",
    "cytoband": f"{REF_BASE}/cytoBand_hg38.txt",
    "ped_file": f"{REF_BASE}/cohort.ped",
    "mei_bed": f"{REF_BASE}/mei_bed",
    "genome_file": f"{REF_BASE}/gs_hg38.genome",
    "contig_ploidy_priors": f"{REF_BASE}/gs_contig_ploidy_priors.tsv",
    "depth_blacklist": f"{REF_BASE}/gs_depth_blacklist.sorted.bed.gz",
    "pesr_blacklist": f"{REF_BASE}/gs_PESR.encode.blacklist.sorted.bed.gz",
    "noncoding_bed": f"{REF_BASE}/gs_noncoding.sort.hg38.bed",
    "gnomad_sv_freq": f"{REF_BASE}/gs_gnomad_v4_SV.Freq.tsv.gz",
    "sd_locs_vcf": f"{REF_BASE}/Homo_sapiens_assembly38.dbsnp138.vcf",
}


def discover_gse_outputs(s3_client) -> dict:
    """Discover all GSE outputs for the cohort.

    Iterates over SAMPLES in order and selects exactly one file per sample
    per output type. If multiple matches exist (e.g., from prior test runs),
    the file with the latest LastModified timestamp is selected.

    Raises:
        FileNotFoundError: If a sample is missing a required output file.
        AssertionError: If final arrays do not have len(SAMPLES) elements.
    """
    bucket = f"healthomics-outputs-{ACCOUNT}-apse1"
    outputs = {"counts": [], "pe_files": [], "sr_files": [], "sd_files": [],
               "manta_vcfs": [], "wham_vcfs": [], "scramble_vcfs": []}

    # Define suffix patterns for each output type
    suffix_patterns = {
        "counts": lambda key: key.endswith('.counts.tsv.gz'),
        "pe_files": lambda key: key.endswith('.pe.txt.gz'),
        "sr_files": lambda key: key.endswith('.sr.txt.gz'),
        "sd_files": lambda key: key.endswith('.sd.txt.gz'),
        "manta_vcfs": lambda key: 'manta' in key and key.endswith('.vcf.gz') and 'tbi' not in key,
        "wham_vcfs": lambda key: 'wham' in key and key.endswith('.vcf.gz'),
        "scramble_vcfs": lambda key: 'scramble' in key and key.endswith('.vcf.gz'),
    }

    for sample_id in SAMPLES:
        # Construct the correct prefix for this sample
        if sample_id == "NA12878":
            prefix = f"runs/gatk-sv-e2e/{sample_id}/optimized/"
        else:
            prefix = f"runs/gatk-sv-e2e/{sample_id}/gse/"

        # List all objects under this sample's prefix
        all_objects = []
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                all_objects.append(obj)

        # For each output type, find matching files and select exactly one
        for output_type, match_fn in suffix_patterns.items():
            matches = [
                obj for obj in all_objects if match_fn(obj['Key'])
            ]

            if len(matches) == 0:
                raise FileNotFoundError(
                    f"No {output_type} file found for sample '{sample_id}' "
                    f"under prefix '{prefix}'"
                )

            if len(matches) == 1:
                selected = matches[0]
            else:
                # Multiple matches: select the file with the latest LastModified
                selected = max(matches, key=lambda obj: obj['LastModified'])

            uri = f"s3://{bucket}/{selected['Key']}"
            outputs[output_type].append(uri)

    # Safety check: all arrays must have exactly len(SAMPLES) elements
    for output_type, files in outputs.items():
        assert len(files) == len(SAMPLES), (
            f"Expected {len(SAMPLES)} {output_type} files, got {len(files)}"
        )

    return outputs


def discover_train_gcnv_outputs(s3_client) -> dict:
    """Discover TrainGCNV outputs."""
    bucket = f"healthomics-outputs-{ACCOUNT}-apse1"
    prefix = "runs/gatk-sv-e2e/batch/train-gcnv/"
    outputs = {"contig_ploidy_model_tar": None, "gcnv_model_tars": []}

    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            uri = f"s3://{bucket}/{key}"
            if 'contig-ploidy-model' in key and key.endswith('.tar.gz'):
                outputs["contig_ploidy_model_tar"] = uri
            elif 'gcnv-model-shard' in key and key.endswith('.tar.gz'):
                outputs["gcnv_model_tars"].append(uri)

    return outputs


def check_pipeline_status():
    """Check the status of all pipeline stages."""
    omics = boto3.client("omics", region_name=REGION)

    # Check TrainGCNV
    try:
        run_info = json.loads(Path("gatk-sv-healthomics/train-gcnv-run.json").read_text())
        run = omics.get_run(id=run_info["run_id"])
        print(f"TrainGCNV (run {run_info['run_id']}): {run['status']}")
        if run['status'] == 'COMPLETED':
            print(f"  Completed at: {run.get('stopTime', 'unknown')}")
        elif run['status'] == 'FAILED':
            print(f"  Failed: {run.get('statusMessage', 'unknown')[:100]}")
    except Exception as e:
        print(f"TrainGCNV: {e}")

    # Check for GBE run
    print("\nPipeline stages:")
    stages = ["gather_batch_evidence", "cluster_batch", "generate_batch_metrics",
              "filter_batch", "merge_batch_sites", "genotype_batch",
              "regenotype_cnvs", "make_cohort_vcf", "annotate_vcf"]
    for stage in stages:
        print(f"  {stage}: Not started")


def main():
    parser = argparse.ArgumentParser(description="GATK-SV Pipeline Orchestrator")
    parser.add_argument("--stage", choices=["gbe", "cluster", "metrics", "filter",
                                            "merge", "genotype", "regenotype",
                                            "cohort_vcf", "annotate", "all"],
                        help="Pipeline stage to run")
    parser.add_argument("--status", action="store_true", help="Check pipeline status")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.status:
        check_pipeline_status()
        return

    if not args.stage:
        parser.print_help()
        return

    print(f"GATK-SV Pipeline Orchestrator")
    print(f"Cohort: {COHORT_ID}")
    print(f"Samples: {len(SAMPLES)}")
    print(f"Stage: {args.stage}")
    print()

    if args.stage == "gbe":
        print("GatherBatchEvidence requires TrainGCNV outputs.")
        print("Checking TrainGCNV status...")

        s3 = boto3.client("s3", region_name=REGION)
        gcnv_outputs = discover_train_gcnv_outputs(s3)

        if not gcnv_outputs["contig_ploidy_model_tar"]:
            print("❌ TrainGCNV outputs not found. Wait for TrainGCNV to complete.")
            sys.exit(1)

        print(f"✓ contig_ploidy_model_tar: {gcnv_outputs['contig_ploidy_model_tar']}")
        print(f"✓ gcnv_model_tars: {len(gcnv_outputs['gcnv_model_tars'])} shards")

        gse_outputs = discover_gse_outputs(s3)
        print(f"\nGSE outputs:")
        for k, v in gse_outputs.items():
            print(f"  {k}: {len(v)} files")

        # Build GBE parameters
        params = {
            "batch": BATCH_ID,
            "samples": SAMPLES,
            "counts": gse_outputs["counts"],
            "PE_files": gse_outputs["pe_files"],
            "SR_files": gse_outputs["sr_files"],
            "SD_files": gse_outputs["sd_files"],
            "manta_vcfs": gse_outputs["manta_vcfs"],
            "wham_vcfs": gse_outputs["wham_vcfs"],
            "scramble_vcfs": gse_outputs["scramble_vcfs"],
            "contig_ploidy_model_tar": gcnv_outputs["contig_ploidy_model_tar"],
            "gcnv_model_tars": gcnv_outputs["gcnv_model_tars"],
            "ped_file": REF["ped_file"],
            "genome_file": REF["genome_file"],
            "primary_contigs_fai": REF["primary_contigs_fai"],
            "ref_dict": REF["dict"],
            "cytoband": REF["cytoband"],
            "mei_bed": REF["mei_bed"],
            "cnmops_chrom_file": REF["autosome_fai"],
            "cnmops_allo_file": REF["allosome_fai"],
            "cnmops_exclude_list": REF["pesr_blacklist"],
            "sd_locs_vcf": REF["sd_locs_vcf"],
            "runtime_attr_ploidy": {"mem_gb": 60, "cpu_cores": 4, "disk_gb": 50, "boot_disk_gb": 10, "preemptible_tries": 0, "max_retries": 1},
            "runtime_attr_postprocess": {"mem_gb": 30, "cpu_cores": 2, "disk_gb": 50, "boot_disk_gb": 10, "preemptible_tries": 0, "max_retries": 1},
            "matrix_qc_distance": 1000000,
            "min_svsize": 50,
            "min_interval_size": 101,
            "max_interval_size": 2000,
            "run_matrix_qc": False,
            "run_ploidy": False,
            "rename_samples": False,
            "append_first_sample_to_ped": False,
            "subset_primary_contigs": False,
            "ref_copy_number_autosomal_contigs": 2,
            "gcnv_qs_cutoff": 30,
            "gatk_docker": DOCKER["gatk"],
            "linux_docker": DOCKER["linux"],
            "sv_base_docker": DOCKER["sv_base"],
            "sv_base_mini_docker": DOCKER["sv_base_mini"],
            "sv_pipeline_docker": DOCKER["sv_pipeline"],
            "sv_pipeline_qc_docker": DOCKER["sv_pipeline"],
            "cnmops_docker": DOCKER["cnmops"],
        }

        if args.dry_run:
            print("\n[DRY RUN] Would launch GatherBatchEvidence with:")
            print(f"  Workflow: {WORKFLOWS['gather_batch_evidence']}")
            print(f"  Samples: {len(SAMPLES)}")
            print(f"  Counts: {len(params['counts'])}")
            print(f"  gCNV model shards: {len(params['gcnv_model_tars'])}")
            return

        omics = boto3.client("omics", region_name=REGION)
        resp = omics.start_run(
            workflowId=WORKFLOWS["gather_batch_evidence"],
            name=f"gbe-{COHORT_ID}",
            roleArn=ROLE_ARN,
            outputUri=f"{OUTPUT_BASE}/batch/gather-batch-evidence/",
            parameters=params,
            storageType="DYNAMIC",
        )
        print(f"\n✓ GatherBatchEvidence launched: run {resp['id']}")

    else:
        print(f"Stage '{args.stage}' not yet implemented. Coming soon.")


if __name__ == "__main__":
    main()
