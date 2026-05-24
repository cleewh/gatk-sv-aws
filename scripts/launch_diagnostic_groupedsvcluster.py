#!/usr/bin/env python3
"""Register and launch the GroupedSVCluster diagnostic on HealthOmics.

Strategy:
- Use the chr1 cluster_sites.vcf produced by the EC2 CombineBatches run as
  the input. That same input takes ~13s on EC2; if HealthOmics fails on it,
  the bug is squarely in HealthOmics.
- Wire the diagnostic task to push pre-run checksums, post-run logs, and
  every 5s a live tail of GATK stderr to a sentinel S3 prefix that we
  control independently of HealthOmics' own log delivery.
"""
import os
from __future__ import annotations

import json
import time
from pathlib import Path

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"
REF_BUCKET = f"omics-ref-{REGION}-{ACCOUNT}"
REF_PREFIX = "gatk-sv/reference/GRCh38"
EC2_OUT_PREFIX = "runs/gatk-sv-e2e/batch/make-cohort-vcf-ec2/combine_batches"
COHORT = "gatk-sv-validation-2026q2"
GATK_DOCKER = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    "gatk-sv/gatk:mw-gatk-sv-672d85"
)
BUNDLE = Path(
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/"
    "GroupedSVClusterDiag-bundle.zip"
)


def upload_bundle() -> str:
    s3 = boto3.client("s3", region_name=REGION)
    key = f"workflows/diagnostic/GroupedSVClusterDiag/{int(time.time())}.zip"
    s3.upload_file(str(BUNDLE), OUTPUT_BUCKET, key)
    uri = f"s3://{OUTPUT_BUCKET}/{key}"
    print(f"✓ Bundle uploaded to {uri}")
    return uri


def stage_chr1_input() -> str:
    """The chr1 cluster_sites.vcf is an EC2 work-dir intermediate; copy it
    out of EC2 to S3 so the WDL can reference it."""
    import boto3 as _b3
    ssm = _b3.client("ssm", region_name=REGION)
    cmd = ssm.send_command(
        InstanceIds=["i-02c67bb34211a85ed"],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": [
                f"aws s3 cp /tmp/combinebatches-ec2/work/"
                f"{COHORT}.combine_batches.chr1.cluster_sites.vcf.gz "
                f"s3://{OUTPUT_BUCKET}/{EC2_OUT_PREFIX}/cluster_sites/ "
                f"--region {REGION}",
                f"aws s3 cp /tmp/combinebatches-ec2/work/"
                f"{COHORT}.combine_batches.chr1.cluster_sites.vcf.gz.tbi "
                f"s3://{OUTPUT_BUCKET}/{EC2_OUT_PREFIX}/cluster_sites/ "
                f"--region {REGION}",
                f"aws s3 cp /tmp/combinebatches-ec2/work/cohort.ploidy.tsv "
                f"s3://{OUTPUT_BUCKET}/{EC2_OUT_PREFIX}/cluster_sites/ "
                f"--region {REGION}",
            ],
        },
    )
    cid = cmd["Command"]["CommandId"]
    for _ in range(20):
        time.sleep(5)
        inv = ssm.get_command_invocation(
            CommandId=cid, InstanceId="i-02c67bb34211a85ed"
        )
        if inv["Status"] in {"Success", "Failed", "Cancelled", "TimedOut"}:
            break
    print(f"  EC2 stage status: {inv['Status']}")
    print(inv.get("StandardOutputContent", "")[:1000])
    if inv["Status"] != "Success":
        print(inv.get("StandardErrorContent", "")[:1000])
        raise SystemExit(1)
    return f"s3://{OUTPUT_BUCKET}/{EC2_OUT_PREFIX}/cluster_sites"


def register_workflow(definition_uri: str) -> str:
    omics = boto3.client("omics", region_name=REGION)
    resp = omics.create_workflow(
        name=f"GroupedSVClusterDiag-{int(time.time())}",
        engine="WDL",
        definitionUri=definition_uri,
        main="wdl/GroupedSVClusterDiag.wdl",
        parameterTemplate={
            "cluster_sites_vcf": {"description": "chr1 cluster_sites.vcf.gz from EC2"},
            "cluster_sites_vcf_index": {"description": ".tbi"},
            "ploidy_table": {"description": "cohort.ploidy.tsv"},
            "reference_fasta": {"description": "GRCh38 fasta"},
            "reference_fasta_fai": {"description": ".fai"},
            "reference_dict": {"description": ".dict"},
            "clustering_config": {"description": "gs_clustering_config.part_one.tsv"},
            "stratification_config": {"description": "stratify_config.v2.part_one.tsv"},
            "track_simrep": {"description": "hg38.SimpRep.sorted.pad_100.merged.bed.gz"},
            "track_simrep_idx": {"description": ".tbi"},
            "track_segdups": {"description": "segdups.bed.gz"},
            "track_segdups_idx": {"description": ".tbi"},
            "track_rmsk": {"description": "rmsk.bed.gz"},
            "track_rmsk_idx": {"description": ".tbi"},
            "s3_diag_prefix": {"description": "s3:// prefix for diagnostic logs"},
            "gatk_docker": {"description": "GATK ECR image"},
            "output_prefix": {"description": "Output filename prefix", "optional": True},
        },
        storageType="DYNAMIC",
    )
    wf_id = resp["id"]
    print(f"✓ Workflow id: {wf_id}")
    while True:
        wf = omics.get_workflow(id=wf_id)
        st = wf["status"]
        print(f"  status: {st}")
        if st == "ACTIVE":
            return wf_id
        if st in {"FAILED", "INACTIVE"}:
            print(json.dumps(wf.get("statusMessage"), default=str))
            raise SystemExit(1)
        time.sleep(10)


def start_run(wf_id: str, ec2_prefix: str) -> str:
    omics = boto3.client("omics", region_name=REGION)
    ts = int(time.time())
    diag_prefix = (
        f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch/"
        f"groupedsvcluster-diag/{ts}/diag"
    )
    out_prefix = (
        f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/batch/"
        f"groupedsvcluster-diag/{ts}/out/"
    )
    # HealthOmics validates any s3:// string param as an existing object,
    # so seed the diag prefix with an empty marker.
    s3 = boto3.client("s3", region_name=REGION)
    marker_key = (
        f"runs/gatk-sv-e2e/batch/groupedsvcluster-diag/"
        f"{ts}/diag/.placeholder"
    )
    s3.put_object(Bucket=OUTPUT_BUCKET, Key=marker_key, Body=b"diag")
    print(f"  seeded {marker_key}")
    params = {
        "cluster_sites_vcf": (
            f"{ec2_prefix}/{COHORT}.combine_batches.chr1.cluster_sites.vcf.gz"
        ),
        "cluster_sites_vcf_index": (
            f"{ec2_prefix}/{COHORT}.combine_batches.chr1.cluster_sites.vcf.gz.tbi"
        ),
        "ploidy_table": f"{ec2_prefix}/cohort.ploidy.tsv",
        "reference_fasta": f"s3://{REF_BUCKET}/{REF_PREFIX}/Homo_sapiens_assembly38.fasta",
        "reference_fasta_fai": f"s3://{REF_BUCKET}/{REF_PREFIX}/Homo_sapiens_assembly38.fasta.fai",
        "reference_dict": f"s3://{REF_BUCKET}/{REF_PREFIX}/Homo_sapiens_assembly38.dict",
        "clustering_config": f"s3://{REF_BUCKET}/{REF_PREFIX}/gs_clustering_config.part_one.tsv",
        "stratification_config": f"s3://{REF_BUCKET}/{REF_PREFIX}/stratify_config.v2.part_one.tsv",
        "track_simrep": f"s3://{REF_BUCKET}/{REF_PREFIX}/hg38.SimpRep.sorted.pad_100.merged.bed.gz",
        "track_simrep_idx": f"s3://{REF_BUCKET}/{REF_PREFIX}/hg38.SimpRep.sorted.pad_100.merged.bed.gz.tbi",
        "track_segdups": f"s3://{REF_BUCKET}/{REF_PREFIX}/segdups.bed.gz",
        "track_segdups_idx": f"s3://{REF_BUCKET}/{REF_PREFIX}/segdups.bed.gz.tbi",
        "track_rmsk": f"s3://{REF_BUCKET}/{REF_PREFIX}/rmsk.bed.gz",
        "track_rmsk_idx": f"s3://{REF_BUCKET}/{REF_PREFIX}/rmsk.bed.gz.tbi",
        "s3_diag_prefix": diag_prefix,
        "gatk_docker": GATK_DOCKER,
        "output_prefix": "diag-chr1",
    }
    resp = omics.start_run(
        workflowId=wf_id,
        roleArn=ROLE_ARN,
        name=f"groupedsvcluster-diag-{ts}",
        parameters=params,
        outputUri=out_prefix,
        storageType="DYNAMIC",
    )
    print(f"✓ Run started: {resp['id']}")
    print(f"  diag prefix: {diag_prefix}")
    print(f"  output uri:  {out_prefix}")
    return resp["id"]


def main() -> None:
    print(f"Bundle: {BUNDLE} ({BUNDLE.stat().st_size:,} bytes)")
    print()
    print("Step 1: Stage EC2 chr1 cluster_sites.vcf to S3")
    ec2_prefix = stage_chr1_input()
    print()
    print("Step 2: Upload diagnostic bundle to S3")
    def_uri = upload_bundle()
    print()
    print("Step 3: Register HealthOmics workflow")
    wf_id = register_workflow(def_uri)
    print()
    print("Step 4: Start run")
    run_id = start_run(wf_id, ec2_prefix)
    print()
    print(f"Workflow: {wf_id}")
    print(f"Run:      {run_id}")


if __name__ == "__main__":
    main()
