#!/usr/bin/env python3
"""
Launch MakeCohortVcf-v2 with the IndexFeatureFile fix in TasksClusterBatch.wdl.

Fix applied: Added `gatk IndexFeatureFile` before SVCluster command to index
intermediate VCFs that lack .tbi indexes (same fix as MergeBatchSites-v2).
Also bumped SVCluster default memory from 3.75 GiB to 8 GiB.

Usage:
    # Step 1: Create the workflow
    python launch_make_cohort_vcf_v2.py --create-workflow

    # Step 2: Start the run (after workflow is ACTIVE)
    python launch_make_cohort_vcf_v2.py --start-run --workflow-id <NEW_WORKFLOW_ID>

    # Or do both in sequence:
    python launch_make_cohort_vcf_v2.py --create-and-run

    # Check status:
    python launch_make_cohort_vcf_v2.py --check-workflow --workflow-id <ID>
    python launch_make_cohort_vcf_v2.py --check-run --run-id <ID>
"""

import os
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

# Bundle path (relative to repo root)
BUNDLE_PATH = Path("gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v2.zip")

# Exact parameters from failed run 8724741 — reused verbatim
RUN_PARAMS = {
    "cohort_name": "gatk-sv-validation-2026q2",
    "batches": ["batch_01"],
    "pesr_vcfs": [
        f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/batch/genotype-batch/3154916/out/genotyped_pesr_vcf/batch_01.genotype_batch.pesr.vcf.gz"
    ],
    "depth_vcfs": [
        f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/batch/genotype-batch/3154916/out/genotyped_depth_vcf/batch_01.genotype_batch.depth.vcf.gz"
    ],
    "rf_cutoff_files": [
        f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/batch/filter-batch/5070716/out/cutoffs/batch_01.cutoffs"
    ],
    "bincov_files": [
        f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/batch/gather-batch-evidence/6129002/out/merged_bincov/batch_01.RD.txt.gz"
    ],
    "disc_files": [
        f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/batch/gather-batch-evidence/6129002/out/merged_PE/batch_01.pe.txt.gz"
    ],
    "median_coverage_files": [
        f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/batch/gather-batch-evidence/6129002/out/median_cov/batch_01_medianCov.transposed.bed"
    ],
    "genotyping_rd_tables": [
        f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/batch/genotype-batch/3154916/out/genotyping_rd_table/batch_01.rd_geno_params.tsv"
    ],
    # Reference files
    "reference_fasta": f"{REF_BASE}/Homo_sapiens_assembly38.fasta",
    "reference_fasta_fai": f"{REF_BASE}/Homo_sapiens_assembly38.fasta.fai",
    "reference_dict": f"{REF_BASE}/Homo_sapiens_assembly38.dict",
    "contig_list": f"{REF_BASE}/gs_primary_contigs.list",
    "allosome_fai": f"{REF_BASE}/gs_allosome.fai",
    "cytobands": f"{REF_BASE}/cytoBand_hg38.txt",
    "ped_file": f"{REF_BASE}/cohort.ped",
    "mei_bed": f"{REF_BASE}/mei_bed",
    "bin_exclude": f"{REF_BASE}/gs_depth_blacklist.sorted.bed.gz",
    "pe_exclude_list": f"{REF_BASE}/gs_PESR.encode.blacklist.sorted.bed.gz",
    # MakeCohortVcf-specific reference files
    "HERVK_reference": f"{REF_BASE}/HERVK_reference.fa",
    "LINE1_reference": f"{REF_BASE}/LINE1_reference.fa",
    "intron_reference": f"{REF_BASE}/gs_gencode.v47.protein_coding.canonical.gtf",
    # Clustering and stratification configs
    "clustering_config_part1": f"{REF_BASE}/gs_clustering_config.part_one.tsv",
    "clustering_config_part2": f"{REF_BASE}/gs_clustering_config.part_two.tsv",
    "stratification_config_part1": f"{REF_BASE}/gs_stratification_config.part_one.tsv",
    "stratification_config_part2": f"{REF_BASE}/gs_stratification_config.part_two.tsv",
    # Track files
    "track_bed_files": [
        f"{REF_BASE}/segdups.bed.gz",
        f"{REF_BASE}/rmsk.bed.gz",
    ],
    "track_names": ["SEGDUP", "RMSK"],
    # Docker images
    "gatk_docker": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85",
    "linux_docker": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/ecr-public/lts/ubuntu:18.04",
    "sv_base_mini_docker": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52",
    "sv_pipeline_docker": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604",
    "sv_pipeline_qc_docker": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604",
    # Workflow parameters
    "chr_x": "chrX",
    "chr_y": "chrY",
    "max_shard_size_resolve": 500,
    "min_sr_background_fail_batches": 0.5,
    "merge_cluster_vcfs": False,
    "merge_complex_resolve_vcfs": False,
    "merge_complex_genotype_vcfs": False,
    "run_module_metrics": False,
}


def create_workflow(client):
    """Create a new workflow from the v2 bundle."""
    bundle_bytes = BUNDLE_PATH.read_bytes()

    print(f"Creating workflow from {BUNDLE_PATH} ({len(bundle_bytes):,} bytes)...")
    print(f"  Main WDL: wdl/MakeCohortVcf.wdl")
    print(f"  Fix: IndexFeatureFile before SVCluster + 8 GiB memory")

    response = client.create_workflow(
        name="MakeCohortVcf-v2",
        description=(
            "MakeCohortVcf with IndexFeatureFile fix in TasksClusterBatch.wdl. "
            "SVCluster now indexes intermediate VCFs before clustering. "
            "Default SVCluster memory bumped to 8 GiB."
        ),
        engine="WDL",
        definitionZip=bundle_bytes,
        main="wdl/MakeCohortVcf.wdl",
        storageCapacity=20,
        tags={
            "gatk-sv:module": "MakeCohortVcf",
            "gatk-sv:fix": "IndexFeatureFile-before-SVCluster",
            "gatk-sv:version": "v2",
        },
    )

    workflow_id = response["id"]
    status = response.get("status", "CREATING")
    print(f"\n✓ Workflow created: {workflow_id}")
    print(f"  Status: {status}")
    return workflow_id


def check_workflow(client, workflow_id):
    """Check workflow status."""
    response = client.get_workflow(id=workflow_id)
    status = response.get("status", "UNKNOWN")
    print(f"Workflow {workflow_id}: {status}")
    if status == "FAILED":
        print(f"  Error: {response.get('statusMessage', 'unknown')}")
    return status


def start_run(client, workflow_id):
    """Start a MakeCohortVcf run with the fixed workflow."""
    print(f"Starting run with workflow {workflow_id}...")
    print(f"  Parameters: same as failed run 8724741")

    response = client.start_run(
        workflowId=workflow_id,
        name="make-cohort-vcf-v2-gatk-sv-validation-2026q2",
        roleArn=ROLE_ARN,
        outputUri=f"{OUTPUT_BASE}/batch/make-cohort-vcf/",
        parameters=RUN_PARAMS,
        storageType="DYNAMIC",
        tags={
            "gatk-sv:module": "MakeCohortVcf",
            "gatk-sv:version": "v2",
            "gatk-sv:fix": "IndexFeatureFile-SVCluster",
        },
    )

    run_id = response["id"]
    print(f"\n✓ MakeCohortVcf-v2 run started!")
    print(f"  Run ID: {run_id}")
    print(f"  Workflow: {workflow_id}")
    print(f"  Output: {OUTPUT_BASE}/batch/make-cohort-vcf/{run_id}/out/")
    print(f"\n  Monitor:")
    print(f"  python launch_make_cohort_vcf_v2.py --check-run --run-id {run_id}")
    return run_id


def check_run(client, run_id):
    """Check run status and list failed tasks if any."""
    response = client.get_run(id=run_id)
    status = response.get("status", "UNKNOWN")
    print(f"Run {run_id}: {status}")

    if status == "FAILED":
        msg = response.get("statusMessage", "unknown")
        print(f"  Error: {msg[:300]}")
        # List tasks to find which failed
        try:
            tasks = client.list_run_tasks(id=run_id)
            failed_tasks = [t for t in tasks.get("items", []) if t.get("status") == "FAILED"]
            if failed_tasks:
                print(f"\n  Failed tasks:")
                for t in failed_tasks[:10]:
                    print(f"    - {t.get('name', 'unknown')}: {t.get('statusMessage', '')[:100]}")
        except Exception:
            pass
    elif status == "COMPLETED":
        print(f"  Completed at: {response.get('stopTime', 'unknown')}")
    elif status == "RUNNING":
        print(f"  Started at: {response.get('startTime', 'unknown')}")
        # Show running tasks
        try:
            tasks = client.list_run_tasks(id=run_id)
            running = [t for t in tasks.get("items", []) if t.get("status") == "RUNNING"]
            completed = [t for t in tasks.get("items", []) if t.get("status") == "COMPLETED"]
            print(f"  Tasks: {len(completed)} completed, {len(running)} running")
        except Exception:
            pass

    return status


def create_and_run(client):
    """Create workflow and start run, waiting for workflow to become ACTIVE."""
    workflow_id = create_workflow(client)

    print(f"\nWaiting for workflow {workflow_id} to become ACTIVE...")
    for i in range(60):  # Wait up to 10 minutes
        time.sleep(10)
        status = check_workflow(client, workflow_id)
        if status == "ACTIVE":
            print()
            break
        elif status in ("FAILED", "DELETED"):
            print("\n❌ Workflow creation failed!")
            sys.exit(1)
        if (i + 1) % 3 == 0:
            print(f"  Still {status}... ({(i+1)*10}s)")
    else:
        print("\n❌ Timeout waiting for workflow to become ACTIVE")
        sys.exit(1)

    return start_run(client, workflow_id)


def main():
    parser = argparse.ArgumentParser(
        description="Launch MakeCohortVcf-v2 (IndexFeatureFile fix)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create-workflow", action="store_true",
                       help="Create the v2 workflow from the fixed bundle")
    group.add_argument("--start-run", action="store_true",
                       help="Start a run with the specified workflow")
    group.add_argument("--create-and-run", action="store_true",
                       help="Create workflow and start run (waits for ACTIVE)")
    group.add_argument("--check-workflow", action="store_true",
                       help="Check workflow status")
    group.add_argument("--check-run", action="store_true",
                       help="Check run status")
    group.add_argument("--show-params", action="store_true",
                       help="Print the run parameters (for review)")

    parser.add_argument("--workflow-id",
                        help="Workflow ID (for --start-run or --check-workflow)")
    parser.add_argument("--run-id",
                        help="Run ID (for --check-run)")
    args = parser.parse_args()

    client = boto3.client("omics", region_name=REGION)

    if args.create_workflow:
        create_workflow(client)
    elif args.start_run:
        if not args.workflow_id:
            print("ERROR: --workflow-id required for --start-run")
            sys.exit(1)
        start_run(client, args.workflow_id)
    elif args.create_and_run:
        create_and_run(client)
    elif args.check_workflow:
        if not args.workflow_id:
            print("ERROR: --workflow-id required")
            sys.exit(1)
        check_workflow(client, args.workflow_id)
    elif args.check_run:
        if not args.run_id:
            print("ERROR: --run-id required")
            sys.exit(1)
        check_run(client, args.run_id)
    elif args.show_params:
        print(json.dumps(RUN_PARAMS, indent=2))


if __name__ == "__main__":
    main()
