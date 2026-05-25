#!/usr/bin/env python3
"""Launch MakeCohortVcf-RemainingSteps v2 — sibling indexes patched."""
from __future__ import annotations

import os

import sys
import time
from pathlib import Path

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"
REF_BASE = (
    f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"
)
EC2_PREFIX = (
    f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch/"
    f"make-cohort-vcf-ec2/combine_batches"
)
COHORT = "gatk-sv-validation-2026q2"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cost_tags import cohort_id_from_env, cost_tags  # noqa: E402

SAMPLE_COUNT = 10
BUNDLE = Path(
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/"
    "MakeCohortVcf-RemainingSteps-bundle-v2.zip"
)

CONTIGS = [
    "chr1", "chr2", "chr3", "chr4", "chr5",
    "chr6", "chr7", "chr8", "chr9", "chr10",
    "chr11", "chr12", "chr13", "chr14", "chr15",
    "chr16", "chr17", "chr18", "chr19", "chr20",
    "chr21", "chr22", "chrX", "chrY",
]


def ec2_outputs() -> dict[str, list[str]]:
    combined_vcfs, combined_vcf_indexes = [], []
    bothside_pass, background_fail = [], []
    for c in CONTIGS:
        base = f"{EC2_PREFIX}/{COHORT}.combine_batches.{c}"
        combined_vcfs.append(f"{base}.svtk_formatted.vcf.gz")
        combined_vcf_indexes.append(f"{base}.svtk_formatted.vcf.gz.tbi")
        bothside_pass.append(f"{base}.bothsides_sr_support.txt")
        background_fail.append(f"{base}.high_sr_background.txt")
    return {
        "combined_vcfs": combined_vcfs,
        "combined_vcf_indexes": combined_vcf_indexes,
        "cluster_bothside_pass_lists": bothside_pass,
        "cluster_background_fail_lists": background_fail,
    }


def main() -> None:
    omics = boto3.client("omics", region_name=REGION)

    prior = omics.get_run(id="1041904")
    prior_params = dict(prior.get("parameters", {}))
    drop = {
        "clustering_config_part1", "clustering_config_part2",
        "stratification_config_part1", "stratification_config_part2",
        "track_simrep", "track_simrep_idx",
        "track_segdups", "track_segdups_idx",
        "track_rmsk", "track_rmsk_idx",
        "min_sr_background_fail_batches",
        "merge_cluster_vcfs",
        "track_bed_tarball", "track_names",
        "pesr_vcfs",
        "reference_fasta", "reference_fasta_fai",
    }
    params = {k: v for k, v in prior_params.items() if k not in drop}
    params.update(ec2_outputs())

    needed_refs = {
        "HERVK_reference": f"{REF_BASE}/HERVK_reference.fa",
        "LINE1_reference": f"{REF_BASE}/LINE1_reference.fa",
        "intron_reference": f"{REF_BASE}/gs_gencode.v47.protein_coding.canonical.gtf",
        "cytobands": f"{REF_BASE}/cytoBand_hg38.txt",
        "mei_bed": f"{REF_BASE}/mei_bed",
    }
    for k, v in needed_refs.items():
        params.setdefault(k, v)

    # New v2 explicit sibling indexes
    # cytobands is plain text (.txt); no .tbi needed/exists. Skip.
    params["pe_exclude_list_idx"] = (
        f"{REF_BASE}/gs_PESR.encode.blacklist.sorted.bed.gz.tbi"
    )
    # disc_files come from gather-batch-evidence; their .tbi siblings live
    # in the same S3 prefix.
    params["disc_files_idx"] = [
        f"{u}.tbi" for u in params["disc_files"]
    ]
    # Same for bincov_files
    params["bincov_files_idx"] = [
        f"{u}.tbi" for u in params["bincov_files"]
    ]

    required = {
        "cohort_name", "batches", "ped_file",
        "combined_vcfs", "combined_vcf_indexes",
        "cluster_bothside_pass_lists", "cluster_background_fail_lists",
        "depth_vcfs", "disc_files", "disc_files_idx", "bincov_files",
        "genotyping_rd_tables", "median_coverage_files", "rf_cutoff_files",
        "reference_dict", "bin_exclude", "contig_list", "allosome_fai",
        "cytobands", "mei_bed",
        "pe_exclude_list", "pe_exclude_list_idx",
        "HERVK_reference", "LINE1_reference", "intron_reference",
        "max_shard_size_resolve", "chr_x", "chr_y",
        "linux_docker", "gatk_docker", "sv_base_mini_docker",
        "sv_pipeline_docker", "sv_pipeline_qc_docker",
    }
    missing = required - set(params.keys())
    if missing:
        print(f"ERROR missing required params: {sorted(missing)}")
        sys.exit(1)

    print(f"Param keys: {len(params)}")
    print(f"  combined_vcfs: {len(params['combined_vcfs'])}")
    print(f"  disc_files: {params['disc_files']}")
    print(f"  disc_files_idx: {params['disc_files_idx']}")

    bundle_bytes = BUNDLE.read_bytes()
    print(f"\nCreating workflow ({len(bundle_bytes):,} bytes)…")
    cohort = cohort_id_from_env(default=COHORT)
    create_tags = cost_tags(
        cohort_id=cohort,
        workflow_version="mcv-remaining-steps-v2",
        module="MakeCohortVcfRemainingSteps",
        sample_count=SAMPLE_COUNT,
    )
    resp = omics.create_workflow(
        name=f"MakeCohortVcfRemainingSteps-v2-{int(time.time())}",
        description=(
            "v2: skips CombineBatches (precomputed on EC2). "
            "Patches sibling-index string-concat patterns to explicit File inputs."
        ),
        engine="WDL",
        definitionZip=bundle_bytes,
        main="wdl/MakeCohortVcfRemainingSteps.wdl",
        storageCapacity=20,
        tags=create_tags,
    )
    wf_id = resp["id"]
    print(f"Workflow id: {wf_id}")

    for _ in range(60):
        time.sleep(10)
        info = omics.get_workflow(id=wf_id)
        st = info.get("status", "UNKNOWN")
        if st == "ACTIVE":
            print("  status: ACTIVE")
            break
        if st in {"FAILED", "INACTIVE", "DELETED"}:
            print(f"  FAILED: {info.get('statusMessage','')[:600]}")
            sys.exit(1)
    else:
        print("  Timed out waiting ACTIVE")
        sys.exit(1)

    run = omics.start_run(
        workflowId=wf_id,
        name=f"mcv-remaining-steps-v2-{int(time.time())}",
        roleArn=ROLE_ARN,
        outputUri=(
            f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch/"
            "mcv-remaining-steps/"
        ),
        parameters=params,
        storageType="DYNAMIC",
        logLevel="ALL",
        tags=cost_tags(
            cohort_id=cohort,
            workflow_version=f"mcv-remaining-steps-v2-{wf_id}",
            module="MakeCohortVcfRemainingSteps",
            sample_count=SAMPLE_COUNT,
        ),
    )
    print()
    print(f"Run id:      {run['id']}")
    print(f"Workflow id: {wf_id}")


if __name__ == "__main__":
    main()
