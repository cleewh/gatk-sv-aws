#!/usr/bin/env python3
"""
Launch GatherSampleEvidence (all 5 modules) for a cohort of samples on AWS HealthOmics.

Usage:
    python run_gse_cohort.py [--samples SAMPLE1,SAMPLE2,...] [--modules wham,manta,cc,scramble,cse]
    python run_gse_cohort.py --all  # Run all samples from manifest

Requires: boto3, configured AWS credentials for ap-southeast-1
"""

import os
import argparse
import json
import sys
import time
from pathlib import Path

import boto3
import botocore.exceptions

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
OUTPUT_BASE = f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e"
REF_BASE = f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"
COHORT_BASE = f"s3://omics-cohorts-{REGION}-{ACCOUNT}/cohorts/gatk-sv-validation-2026q2"

# Production workflow IDs
WORKFLOWS = {
    "wham": {
        "id": "2723477",
        "storage_type": "STATIC",
        "storage_capacity": 1200,
        "tiered": True,
    },
    "manta": {
        "id": "4091926",
        "storage_type": "DYNAMIC",
    },
    "cc": {
        "id": "8771956",  # pre-localize version (proven). Use 1635194 for FUSE/2-CPU version.
        "storage_type": "DYNAMIC",
    },
    "scramble": {
        "id": "3973675",
        "storage_type": "STATIC",
        "storage_capacity": 1200,
    },
    "cse": {
        "id": "7038412",  # no-prelocalize, 30 GiB, proven working
        "storage_type": "DYNAMIC",
    },
}

# Tiered wham memory provisioning
WHAM_SIZE_THRESHOLD_BYTES: int = 21_474_836_480  # 20 GiB

WHAM_TIERS = {
    "standard": {
        "id": "2723477",
        "memory_gib": 16,
        "label": "Standard_Tier",
    },
    "high_memory": {
        "id": "6217382",
        "memory_gib": 30,
        "label": "High_Memory_Tier",
    },
}

# Docker images
DOCKER = {
    "wham": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/wham:fast-v5",
    "manta": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/manta:2023-09-14-v0.28.3-beta-3f22f94d",
    "sv_base": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base:2024-10-25-v0.29-beta-5ea22a52",
    "scramble": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/scramble:2024-10-25-v0.29-beta-5ea22a52",
}

# Reference files
REF = {
    "fasta": f"{REF_BASE}/Homo_sapiens_assembly38.fasta",
    "fai": f"{REF_BASE}/Homo_sapiens_assembly38.fasta.fai",
    "dict": f"{REF_BASE}/Homo_sapiens_assembly38.dict",
    "gatk_jar": f"{REF_BASE}/gatk-4.6.2.0-local.jar",
    "intervals": f"{REF_BASE}/gs_preprocessed_intervals.interval_list",
    "primary_contigs_list": f"{REF_BASE}/gs_primary_contigs.list",
    "manta_region_bed": f"{REF_BASE}/manta_region_bed",
    "manta_region_bed_tbi": f"{REF_BASE}/manta_region_bed.tbi",
    "mei_bed": f"{REF_BASE}/mei_bed",
    "sd_locs_vcf": f"{REF_BASE}/Homo_sapiens_assembly38.dbsnp138.vcf",
}


def get_sample_params(sample_id: str) -> dict:
    """Get CRAM/CRAI paths for a sample."""
    return {
        "cram": f"{COHORT_BASE}/{sample_id}.final.cram",
        "crai": f"{COHORT_BASE}/{sample_id}.final.cram.crai",
    }


def build_params(module: str, sample_id: str) -> dict:
    """Build workflow parameters for a module + sample."""
    s = get_sample_params(sample_id)

    if module == "wham":
        return {
            "cram_or_bam": s["cram"],
            "cram_or_bam_idx": s["crai"],
            "ref_fasta": REF["fasta"],
            "ref_fasta_fai": REF["fai"],
            "sample_id": sample_id,
            "wham_docker": DOCKER["wham"],
        }
    elif module == "manta":
        return {
            "cram_or_bam": s["cram"],
            "cram_or_bam_idx": s["crai"],
            "ref_fasta": REF["fasta"],
            "ref_fasta_fai": REF["fai"],
            "manta_docker": DOCKER["manta"],
            "manta_region_bed": REF["manta_region_bed"],
            "manta_region_bed_index": REF["manta_region_bed_tbi"],
            "sample_id": sample_id,
        }
    elif module == "cc":
        return {
            "cram_or_bam": s["cram"],
            "cram_or_bam_idx": s["crai"],
            "ref_fasta": REF["fasta"],
            "ref_fasta_fai": REF["fai"],
            "ref_fasta_dict": REF["dict"],
            "gatk_jar": REF["gatk_jar"],
            "intervals": REF["intervals"],
            "docker": DOCKER["sv_base"],
            "sample_id": sample_id,
        }
    elif module == "scramble":
        return {
            "cram_or_bam": s["cram"],
            "cram_or_bam_idx": s["crai"],
            "ref_fasta": REF["fasta"],
            "ref_fasta_fai": REF["fai"],
            "mei_bed": REF["mei_bed"],
            "primary_contigs_list": REF["primary_contigs_list"],
            "scramble_docker": DOCKER["scramble"],
            "sample_id": sample_id,
        }
    elif module == "cse":
        return {
            "cram_or_bam": s["cram"],
            "cram_or_bam_idx": s["crai"],
            "ref_fasta": REF["fasta"],
            "ref_fasta_fai": REF["fai"],
            "ref_fasta_dict": REF["dict"],
            "gatk_jar": REF["gatk_jar"],
            "preprocessed_intervals": REF["intervals"],
            "primary_contigs_list": REF["primary_contigs_list"],
            "sd_locs_vcf": REF["sd_locs_vcf"],
            "docker": DOCKER["sv_base"],
            "sample_id": sample_id,
        }
    else:
        raise ValueError(f"Unknown module: {module}")


def select_wham_tier(size_bytes: int, threshold: int = WHAM_SIZE_THRESHOLD_BYTES) -> dict:
    """Select the wham tier based on CRAM file size.

    Args:
        size_bytes: CRAM file size in bytes (must be >= 0).
        threshold: Size boundary in bytes. Defaults to 20 GiB.

    Returns:
        Tier dict with keys: id, memory_gib, label
    """
    if size_bytes <= threshold:
        return WHAM_TIERS["standard"]
    return WHAM_TIERS["high_memory"]


def get_cram_size_bytes(s3_client, bucket: str, key: str) -> int:
    """Return the size in bytes of an S3 object via HEAD request.

    Raises:
        botocore.exceptions.ClientError on S3 failures (404, 403, etc.)
    """
    response = s3_client.head_object(Bucket=bucket, Key=key)
    return response["ContentLength"]


def launch_run(client, module: str, sample_id: str, dry_run: bool = False) -> dict:
    """Launch a single HealthOmics run."""
    wf = WORKFLOWS[module]
    params = build_params(module, sample_id)
    run_name = f"{module}-{sample_id}"
    output_uri = f"{OUTPUT_BASE}/{sample_id}/gse/{module}/"

    workflow_id = wf["id"]
    tier_label = None

    # Tiered wham selection
    if wf.get("tiered"):
        # Parse bucket and key from COHORT_BASE
        cohort_uri = COHORT_BASE  # s3://omics-cohorts-{REGION}-{ACCOUNT}/cohorts/...
        uri_no_scheme = cohort_uri[len("s3://"):]
        bucket = uri_no_scheme.split("/", 1)[0]
        prefix = uri_no_scheme.split("/", 1)[1]
        key = f"{prefix}/{sample_id}.final.cram"

        try:
            s3 = boto3.client("s3", region_name=REGION)
            size_bytes = get_cram_size_bytes(s3, bucket, key)
            tier = select_wham_tier(size_bytes)
            print(f"  [{sample_id}] CRAM {size_bytes / (1024**3):.1f} GiB \u2192 {tier['label']} (workflow {tier['id']})")
            workflow_id = tier["id"]
            tier_label = tier["label"]
        except botocore.exceptions.ClientError as e:
            print(f"  [{sample_id}] ERROR: Failed to query CRAM size: {e}. Skipping wham.")
            return None

    kwargs = {
        "workflowId": workflow_id,
        "name": run_name,
        "roleArn": ROLE_ARN,
        "outputUri": output_uri,
        "parameters": params,
        "storageType": wf["storage_type"],
    }
    if wf.get("storage_capacity"):
        kwargs["storageCapacity"] = wf["storage_capacity"]

    if dry_run:
        print(f"  [DRY RUN] {run_name} -> workflow {workflow_id}")
        return {"id": "dry-run", "name": run_name}

    resp = client.start_run(**kwargs)
    run_id = resp["id"]
    print(f"  \u2713 {run_name} -> run {run_id}")
    result = {"id": run_id, "name": run_name, "module": module, "sample": sample_id}

    if tier_label is not None:
        result["workflow_id"] = workflow_id
        result["tier"] = tier_label

    return result


def load_samples() -> list:
    """Load sample IDs from the validation cohort manifest."""
    manifest_path = Path(__file__).parent.parent / "validation-cohort" / "inputs" / "manifest.json"
    with open(manifest_path) as f:
        manifest = json.load(f)
    return [s["sample_id"] for s in manifest["samples"]]


def main():
    parser = argparse.ArgumentParser(description="Launch GSE for cohort")
    parser.add_argument("--samples", help="Comma-separated sample IDs (default: all from manifest)")
    parser.add_argument("--modules", default="wham,manta,cc,scramble,cse",
                        help="Comma-separated modules to run (default: all)")
    parser.add_argument("--exclude", help="Comma-separated sample IDs to exclude")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be launched")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Seconds between launches (avoid throttling)")
    args = parser.parse_args()

    # Resolve samples
    if args.samples:
        samples = args.samples.split(",")
    else:
        samples = load_samples()

    if args.exclude:
        exclude = set(args.exclude.split(","))
        samples = [s for s in samples if s not in exclude]

    modules = args.modules.split(",")

    print(f"Launching GSE for {len(samples)} samples × {len(modules)} modules = {len(samples) * len(modules)} runs")
    print(f"Samples: {', '.join(samples)}")
    print(f"Modules: {', '.join(modules)}")
    print()

    client = boto3.client("omics", region_name=REGION)
    launched = []

    for sample_id in samples:
        print(f"[{sample_id}]")
        for module in modules:
            result = launch_run(client, module, sample_id, dry_run=args.dry_run)
            if result is not None:
                launched.append(result)
            if not args.dry_run:
                time.sleep(args.delay)
        print()

    # Save run manifest
    if not args.dry_run:
        output_file = Path(__file__).parent.parent / "gse-cohort-runs.json"
        with open(output_file, "w") as f:
            json.dump({
                "launched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "samples": samples,
                "modules": modules,
                "runs": launched,
                "total_runs": len(launched),
            }, f, indent=2)
        print(f"Run manifest saved to {output_file}")

    print(f"\nTotal: {len(launched)} runs launched")
    if not args.dry_run:
        est_cost = len(samples) * 3.08
        print(f"Estimated cost: ${est_cost:.2f} ({len(samples)} × $3.08/sample)")


if __name__ == "__main__":
    main()
