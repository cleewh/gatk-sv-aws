#!/usr/bin/env python3
"""Launch AnnotateVcf — final stage of the GATK-SV pipeline.

Takes the cleaned cohort VCF produced by MakeCohortVcf-RemainingSteps on
EC2 and annotates it on HealthOmics with VEP-style functional consequences
plus gnomAD-SV allele frequencies.

AnnotateVcf doesn't use GroupedSVCluster/SvtkResolve, so it should work
on HealthOmics without the 47-s kill issue.
"""
import os
from __future__ import annotations

import sys
import time

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"
REF_BASE = (
    f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"
)

WORKFLOW_ID = "6832584"  # gatk-sv-annotate-vcf-v2 (already registered)

CLEANED_VCF = (
    f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch/"
    f"mcv-remaining-steps-ec2/cleaned_vcf/"
    f"gatk-sv-validation-2026q2.cleaned.vcf.gz"
)

GATK_DOCKER = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85"
SV_PIPELINE_DOCKER = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    f"gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604"
)
SV_BASE_MINI_DOCKER = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    f"gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52"
)


def main() -> None:
    omics = boto3.client("omics", region_name=REGION)

    wf = omics.get_workflow(id=WORKFLOW_ID)
    if wf["status"] != "ACTIVE":
        print(f"Workflow {WORKFLOW_ID} status: {wf['status']} — aborting")
        sys.exit(1)
    print(f"Using workflow {WORKFLOW_ID} ({wf['name']})")

    params = {
        "vcf": CLEANED_VCF,
        "prefix": "gatk-sv-validation-2026q2.annotated",
        "contig_list": f"{REF_BASE}/gs_primary_contigs.list",
        "ped_file": f"{REF_BASE}/cohort.ped",
        "noncoding_bed": f"{REF_BASE}/gs_noncoding.sort.hg38.bed",
        "par_bed": f"{REF_BASE}/gs_hg38.par.bed",
        "protein_coding_gtf": (
            f"{REF_BASE}/gs_gencode.v47.protein_coding.canonical.gtf"
        ),
        "external_af_ref_bed": f"{REF_BASE}/gs_gnomad_v4_SV.Freq.tsv.gz",
        "external_af_ref_prefix": "gnomad_v4.1_sv",
        "external_af_population": [
            "ALL", "AFR", "AMR", "EAS", "EUR",
            "MID", "FIN", "ASJ", "RMI", "SAS", "AMI",
        ],
        "sv_per_shard": 5000,
        "gatk_docker": GATK_DOCKER,
        "sv_pipeline_docker": SV_PIPELINE_DOCKER,
        "sv_base_mini_docker": SV_BASE_MINI_DOCKER,
    }

    ts = int(time.time())
    run = omics.start_run(
        workflowId=WORKFLOW_ID,
        name=f"annotate-vcf-{ts}",
        roleArn=ROLE_ARN,
        outputUri=(
            f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch/"
            "annotate-vcf/"
        ),
        parameters=params,
        storageType="DYNAMIC",
        logLevel="ALL",
        tags={
            "gatk-sv:module": "AnnotateVcf",
            "gatk-sv:version": "v1",
            "gatk-sv:cohort-id": "gatk-sv-validation-2026q2",
        },
    )
    print()
    print(f"Run id:      {run['id']}")
    print(f"Workflow id: {WORKFLOW_ID}")
    print(f"Output URI:  s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch/annotate-vcf/")


if __name__ == "__main__":
    main()
