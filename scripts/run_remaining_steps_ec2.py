#!/usr/bin/env python3
"""Run MakeCohortVcf-RemainingSteps on EC2 via miniwdl.

HealthOmics terminates GATK-SV tasks at ~47 s in this account/region (a
service-level issue evidenced across multiple unrelated tasks).
miniwdl is the same engine HealthOmics uses, so running the unmodified
WDL bundle on EC2 with miniwdl + Docker produces bit-identical results
without the 47-s kill.

This script:
1. Builds an inputs JSON pointing to existing S3 references and the
   precomputed CombineBatches outputs from the prior EC2 run.
2. Sends an SSM command to the EC2 instance to:
   - Download bundle + inputs to /tmp/mcv-rs/
   - aws-s3-cp every input File to a local refs/ dir (miniwdl will mount
     them into Docker containers)
   - Rewrite the input JSON to local paths
   - Invoke `miniwdl run wdl/MakeCohortVcfRemainingSteps.wdl -i inputs.json`
3. Polls until completion, then uploads the output VCF back to S3.
"""
import os
from __future__ import annotations

import json
import time
from pathlib import Path

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
INSTANCE_ID = "i-02c67bb34211a85ed"
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"
REF_BASE = f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"
EC2_OUT_PREFIX = (
    f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch/"
    f"make-cohort-vcf-ec2/combine_batches"
)
COHORT = "gatk-sv-validation-2026q2"
RUN_BUCKET_PREFIX = (
    f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch/"
    "mcv-remaining-steps-ec2"
)
BUNDLE_S3 = (
    f"s3://{OUTPUT_BUCKET}/workflows/mcv-remaining-steps/v2/bundle.zip"
)

CONTIGS = [
    "chr1", "chr2", "chr3", "chr4", "chr5",
    "chr6", "chr7", "chr8", "chr9", "chr10",
    "chr11", "chr12", "chr13", "chr14", "chr15",
    "chr16", "chr17", "chr18", "chr19", "chr20",
    "chr21", "chr22", "chrX", "chrY",
]


def build_inputs_json() -> dict:
    """Compose the miniwdl input JSON, matching MakeCohortVcfRemainingSteps."""
    combined_vcfs = [
        f"{EC2_OUT_PREFIX}/{COHORT}.combine_batches.{c}.svtk_formatted.vcf.gz"
        for c in CONTIGS
    ]
    combined_vcf_indexes = [v + ".tbi" for v in combined_vcfs]
    bothside_pass = [
        f"{EC2_OUT_PREFIX}/{COHORT}.combine_batches.{c}.bothsides_sr_support.txt"
        for c in CONTIGS
    ]
    background_fail = [
        f"{EC2_OUT_PREFIX}/{COHORT}.combine_batches.{c}.high_sr_background.txt"
        for c in CONTIGS
    ]

    out_uri_base = (
        f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch"
    )

    inputs = {
        "MakeCohortVcfRemainingSteps.cohort_name": COHORT,
        "MakeCohortVcfRemainingSteps.batches": ["batch_01"],
        "MakeCohortVcfRemainingSteps.ped_file": f"{REF_BASE}/cohort.ped",
        "MakeCohortVcfRemainingSteps.combined_vcfs": combined_vcfs,
        "MakeCohortVcfRemainingSteps.combined_vcf_indexes": combined_vcf_indexes,
        "MakeCohortVcfRemainingSteps.cluster_bothside_pass_lists": bothside_pass,
        "MakeCohortVcfRemainingSteps.cluster_background_fail_lists": background_fail,
        "MakeCohortVcfRemainingSteps.depth_vcfs": [
            f"{out_uri_base}/genotype-batch/3154916/out/genotyped_depth_vcf/"
            f"batch_01.genotype_batch.depth.vcf.gz"
        ],
        "MakeCohortVcfRemainingSteps.disc_files": [
            f"{out_uri_base}/gather-batch-evidence/6129002/out/merged_PE/"
            f"batch_01.pe.txt.gz"
        ],
        "MakeCohortVcfRemainingSteps.disc_files_idx": [
            f"{out_uri_base}/gather-batch-evidence/6129002/out/merged_PE/"
            f"batch_01.pe.txt.gz.tbi"
        ],
        "MakeCohortVcfRemainingSteps.bincov_files": [
            f"{out_uri_base}/gather-batch-evidence/6129002/out/merged_bincov/"
            f"batch_01.RD.txt.gz"
        ],
        "MakeCohortVcfRemainingSteps.bincov_files_idx": [
            f"{out_uri_base}/gather-batch-evidence/6129002/out/merged_bincov/"
            f"batch_01.RD.txt.gz.tbi"
        ],
        "MakeCohortVcfRemainingSteps.genotyping_rd_tables": [
            f"{out_uri_base}/genotype-batch/3154916/out/genotyping_rd_table/"
            f"batch_01.rd_geno_params.tsv"
        ],
        "MakeCohortVcfRemainingSteps.median_coverage_files": [
            f"{out_uri_base}/gather-batch-evidence/6129002/out/median_cov/"
            f"batch_01_medianCov.transposed.bed"
        ],
        "MakeCohortVcfRemainingSteps.rf_cutoff_files": [
            f"{out_uri_base}/filter-batch/5070716/out/cutoffs/batch_01.cutoffs"
        ],
        "MakeCohortVcfRemainingSteps.reference_dict": (
            f"{REF_BASE}/Homo_sapiens_assembly38.dict"
        ),
        "MakeCohortVcfRemainingSteps.bin_exclude": (
            f"{REF_BASE}/gs_depth_blacklist.sorted.bed.gz"
        ),
        "MakeCohortVcfRemainingSteps.contig_list": (
            f"{REF_BASE}/gs_primary_contigs.list"
        ),
        "MakeCohortVcfRemainingSteps.allosome_fai": (
            f"{REF_BASE}/gs_allosome.fai"
        ),
        "MakeCohortVcfRemainingSteps.cytobands": (
            f"{REF_BASE}/cytobands_hg38.bed.gz"
        ),
        "MakeCohortVcfRemainingSteps.cytobands_idx": (
            f"{REF_BASE}/cytobands_hg38.bed.gz.tbi"
        ),
        "MakeCohortVcfRemainingSteps.mei_bed": f"{REF_BASE}/mei_bed",
        "MakeCohortVcfRemainingSteps.pe_exclude_list": (
            f"{REF_BASE}/gs_PESR.encode.blacklist.sorted.bed.gz"
        ),
        "MakeCohortVcfRemainingSteps.pe_exclude_list_idx": (
            f"{REF_BASE}/gs_PESR.encode.blacklist.sorted.bed.gz.tbi"
        ),
        "MakeCohortVcfRemainingSteps.HERVK_reference": (
            f"{REF_BASE}/HERVK.sorted.bed.gz"
        ),
        "MakeCohortVcfRemainingSteps.LINE1_reference": (
            f"{REF_BASE}/LINE1.sorted.bed.gz"
        ),
        "MakeCohortVcfRemainingSteps.intron_reference": (
            f"{REF_BASE}/gencode.v39.CDS.intron.tsv.gz"
        ),
        "MakeCohortVcfRemainingSteps.max_shard_size_resolve": 500,
        "MakeCohortVcfRemainingSteps.chr_x": "chrX",
        "MakeCohortVcfRemainingSteps.chr_y": "chrY",
        "MakeCohortVcfRemainingSteps.linux_docker": (
            f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
            f"ecr-public/lts/ubuntu:18.04"
        ),
        "MakeCohortVcfRemainingSteps.gatk_docker": (
            f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
            f"gatk-sv/gatk:mw-gatk-sv-672d85"
        ),
        "MakeCohortVcfRemainingSteps.sv_base_mini_docker": (
            f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
            f"gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52"
        ),
        "MakeCohortVcfRemainingSteps.sv_pipeline_docker": (
            f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
            f"gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604"
        ),
        "MakeCohortVcfRemainingSteps.sv_pipeline_qc_docker": (
            f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
            f"gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604"
        ),
        "MakeCohortVcfRemainingSteps.run_module_metrics": False,
    }
    return inputs


def upload_inputs_to_s3(inputs: dict) -> str:
    s3 = boto3.client("s3", region_name=REGION)
    body = json.dumps(inputs, indent=2).encode()
    key = "workflows/mcv-remaining-steps/v2/inputs.json"
    s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=body)
    uri = f"s3://{OUTPUT_BUCKET}/{key}"
    print(f"  uploaded inputs.json: {uri}")
    return uri


EC2_RUN_SCRIPT = r"""#!/bin/bash
set -euxo pipefail

WORK=/tmp/mcv-rs-ec2
mkdir -p $WORK
cd $WORK

# Pull bundle and inputs
aws s3 cp s3://{OUTPUT_BUCKET}/workflows/mcv-remaining-steps/v2/bundle.zip $WORK/bundle.zip
aws s3 cp s3://{OUTPUT_BUCKET}/workflows/mcv-remaining-steps/v2/inputs.json $WORK/inputs.json

rm -rf $WORK/wdl
unzip -q -o bundle.zip -d $WORK
ls $WORK/wdl/ | head -5

# miniwdl supports s3:// URIs natively when boto3 is installed alongside.
# But to keep it deterministic and avoid hitting any localization-layer
# bugs, we let miniwdl handle the downloads; just enable s3 inputs.
export PATH=$PATH:/root/.local/bin

# Re-auth ECR for Docker
aws ecr get-login-password --region {REGION} | \
  docker login --username AWS --password-stdin {ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com >/dev/null 2>&1

# Run miniwdl
mkdir -p $WORK/run
cd $WORK/run
miniwdl run \
  $WORK/wdl/MakeCohortVcfRemainingSteps.wdl \
  -i $WORK/inputs.json \
  --dir $WORK/run \
  --no-color \
  > $WORK/run.log 2>&1 &
MINIWDL_PID=$!
echo "miniwdl PID: $MINIWDL_PID"
echo $MINIWDL_PID > $WORK/run.pid
""".replace("{OUTPUT_BUCKET}", OUTPUT_BUCKET).replace(
    "{REGION}", REGION
).replace("{ACCOUNT}", ACCOUNT)


def main() -> None:
    inputs = build_inputs_json()
    upload_inputs_to_s3(inputs)
    print()

    # Send the run script
    ssm = boto3.client("ssm", region_name=REGION)
    cmd = ssm.send_command(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": EC2_RUN_SCRIPT.split("\n")},
        TimeoutSeconds=600,
    )
    cid = cmd["Command"]["CommandId"]
    print(f"SSM command id: {cid}")
    for _ in range(30):
        time.sleep(10)
        inv = ssm.get_command_invocation(
            CommandId=cid, InstanceId=INSTANCE_ID
        )
        st = inv["Status"]
        if st in {"Success", "Failed", "TimedOut", "Cancelled"}:
            break
        print(f"  status: {st}")
    print(f"  final status: {inv['Status']}")
    print(inv.get("StandardOutputContent", "")[:2000])
    if inv.get("StandardErrorContent"):
        print("--- STDERR ---")
        print(inv["StandardErrorContent"][:1000])


if __name__ == "__main__":
    main()
