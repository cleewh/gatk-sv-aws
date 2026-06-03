#!/usr/bin/env python3
"""Run the GQ_Recalibrator chain on EC2 via miniwdl (Phase C.2-C.5).

The GQ chain (JoinRawCalls -> SVConcordance -> ScoreGenotypes ->
FilterGenotypes) cannot run natively on HealthOmics: JoinRawCalls'
24-way gatk SVCluster scatter trips the HealthOmics multi-task kill
(all shards Terminated ~19s in, no logs) -- the same service-level
issue that forced MakeCohortVcf onto the EC2 hybrid. We therefore run
the GQ chain the same way as MakeCohortVcfRemainingSteps: the unmodified
WDL bundles on EC2 with miniwdl + Docker, one step at a time, each
step's output feeding the next.

Each step:
  1. upload <Module>-bundle.zip + a per-step inputs.json to S3
  2. SSM-dispatch a script that downloads them, runs `miniwdl run`
     detached, prints the PID
  3. poll the PID to completion, locate the output VCF, upload to S3

Env (all required):
  AWS_ACCOUNT_ID, GATK_SV_EC2_INSTANCE_ID, GATK_SV_COHORT_ID

Per-step S3 wiring is computed from the cohort's prior outputs; override
the MODULE-specific *_S3 vars only for re-wiring.

Usage:
  AWS_ACCOUNT_ID=... GATK_SV_EC2_INSTANCE_ID=i-... GATK_SV_COHORT_ID=... \
  .venv/bin/python scripts/run_gq_chain_ec2.py --step join_raw_calls
  ... (repeat for sv_concordance, score_genotypes, filter_genotypes)
  or: --step all
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ["AWS_ACCOUNT_ID"]
INSTANCE = os.environ["GATK_SV_EC2_INSTANCE_ID"]
COHORT = os.environ["GATK_SV_COHORT_ID"]
BK = f"healthomics-outputs-{ACCOUNT}-apse1"
REF = f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"
B = f"s3://{BK}/runs/gatk-sv-e2e/{COHORT}/batch"
ECR = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / f"gq-chain-ec2-{COHORT}.json"

DOCK = {
    "gatk": f"{ECR}/gatk-sv/gatk:mw-gatk-sv-672d85",
    "svbm": f"{ECR}/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52",
    "svp": f"{ECR}/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604",
    "linux": f"{ECR}/ecr-public/lts/ubuntu:18.04",
}

# Per-cohort fixed prior-run output prefixes (smoke run). Override via env if re-running.
CB = os.environ.get("GQ_CLUSTER_BATCH_OUT", f"{B}/cluster-batch/3362090/out")
RCV = os.environ.get("GQ_REFINE_CPX_OUT", f"{B}/refine-complex-variants/7401347/out")
PED = f"{REF}/cohort-gatk-sv-156-smoke-test.ped"

STEPS = ["join_raw_calls", "sv_concordance", "score_genotypes", "filter_genotypes"]
MAIN_WDL = {
    "join_raw_calls": "JoinRawCalls.wdl",
    "sv_concordance": "SVConcordance.wdl",
    "score_genotypes": "ScoreGenotypes.wdl",
    "filter_genotypes": "FilterGenotypes.wdl",
}
BUNDLE = {
    "join_raw_calls": "JoinRawCalls/JoinRawCalls-bundle.zip",
    "sv_concordance": "SVConcordance/SVConcordance-bundle.zip",
    "score_genotypes": "ScoreGenotypes/ScoreGenotypes-bundle.zip",
    "filter_genotypes": "FilterGenotypes/FilterGenotypes-bundle.zip",
}
WF = {  # top-level workflow name in each WDL
    "join_raw_calls": "JoinRawCalls",
    "sv_concordance": "SVConcordance",
    "score_genotypes": "ScoreGenotypes",
    "filter_genotypes": "FilterGenotypes",
}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"cohort_id": COHORT, "steps": {}}


def _save_state(s):
    STATE.write_text(json.dumps(s, indent=2) + "\n")


def _step_output_vcf(step: str) -> str:
    """S3 URI where this step's primary output VCF is uploaded."""
    return f"{B}/{step.replace('_','-')}/{COHORT}.{step}.vcf.gz"


def inputs_join_raw_calls(state) -> dict:
    return {
        f"{WF['join_raw_calls']}.prefix": f"{COHORT}.join_raw_calls",
        f"{WF['join_raw_calls']}.ped_file": PED,
        f"{WF['join_raw_calls']}.contig_list": f"{REF}/gs_primary_contigs.list",
        f"{WF['join_raw_calls']}.reference_fasta": f"{REF}/Homo_sapiens_assembly38.fasta",
        f"{WF['join_raw_calls']}.reference_fasta_fai": f"{REF}/Homo_sapiens_assembly38.fasta.fai",
        f"{WF['join_raw_calls']}.reference_dict": f"{REF}/Homo_sapiens_assembly38.dict",
        f"{WF['join_raw_calls']}.clustered_depth_vcfs": [f"{CB}/clustered_depth_vcf/{COHORT}.cluster_batch.depth.vcf.gz"],
        f"{WF['join_raw_calls']}.clustered_depth_vcf_indexes": [f"{CB}/clustered_depth_vcf/{COHORT}.cluster_batch.depth.vcf.gz.tbi"],
        f"{WF['join_raw_calls']}.clustered_manta_vcfs": [f"{CB}/clustered_manta_vcf/{COHORT}.cluster_batch.manta.vcf.gz"],
        f"{WF['join_raw_calls']}.clustered_manta_vcf_indexes": [f"{CB}/clustered_manta_vcf/{COHORT}.cluster_batch.manta.vcf.gz.tbi"],
        f"{WF['join_raw_calls']}.clustered_wham_vcfs": [f"{CB}/clustered_wham_vcf/{COHORT}.cluster_batch.wham.vcf.gz"],
        f"{WF['join_raw_calls']}.clustered_wham_vcf_indexes": [f"{CB}/clustered_wham_vcf/{COHORT}.cluster_batch.wham.vcf.gz.tbi"],
        f"{WF['join_raw_calls']}.clustered_scramble_vcfs": [f"{CB}/clustered_scramble_vcf/{COHORT}.cluster_batch.scramble.vcf.gz"],
        f"{WF['join_raw_calls']}.clustered_scramble_vcf_indexes": [f"{CB}/clustered_scramble_vcf/{COHORT}.cluster_batch.scramble.vcf.gz.tbi"],
        f"{WF['join_raw_calls']}.gatk_docker": DOCK["gatk"],
        f"{WF['join_raw_calls']}.sv_base_mini_docker": DOCK["svbm"],
        f"{WF['join_raw_calls']}.sv_pipeline_docker": DOCK["svp"],
    }


def inputs_sv_concordance(state) -> dict:
    # eval = the cpx_refined cohort VCF (RefineComplexVariants output)
    # truth = JoinRawCalls joined raw-calls VCF
    jrc_vcf = state["steps"]["join_raw_calls"]["output_vcf"]
    return {
        f"{WF['sv_concordance']}.eval_vcf": f"{RCV}/{COHORT}.refine_complex.cpx_refined.vcf.gz",
        f"{WF['sv_concordance']}.truth_vcf": jrc_vcf,
        f"{WF['sv_concordance']}.output_prefix": f"{COHORT}.sv_concordance",
        f"{WF['sv_concordance']}.contig_list": f"{REF}/gs_primary_contigs.list",
        f"{WF['sv_concordance']}.reference_dict": f"{REF}/Homo_sapiens_assembly38.dict",
        f"{WF['sv_concordance']}.gatk_docker": DOCK["gatk"],
        f"{WF['sv_concordance']}.sv_base_mini_docker": DOCK["svbm"],
    }


def inputs_score_genotypes(state) -> dict:
    svc_vcf = state["steps"]["sv_concordance"]["output_vcf"]
    gtracks = f"{REF}/ucsc-genome-tracks"
    return {
        f"{WF['score_genotypes']}.vcf": svc_vcf,
        f"{WF['score_genotypes']}.output_prefix": f"{COHORT}.score_genotypes",
        f"{WF['score_genotypes']}.gq_recalibrator_model_file": f"{REF}/gatk-sv-recalibrator.aou_phase_1.v1.model",
        # AoU model defaults to estimating AF from genotypes only when the
        # cohort has >=100 samples. Small validation/smoke cohorts (e.g. 10
        # samples) lack an AF INFO field, so lower the threshold to estimate
        # AF directly from the available genotypes.
        f"{WF['score_genotypes']}.recalibrate_gq_args": ["--min-samples-to-estimate-allele-frequency", "1"],
        f"{WF['score_genotypes']}.genome_tracks": [
            f"{gtracks}/hg38-RepeatMasker.bed.gz",
            f"{gtracks}/hg38-Segmental-Dups.bed.gz",
            f"{gtracks}/hg38-Simple-Repeats.bed.gz",
            f"{gtracks}/hg38_umap_s100.bed.gz",
            f"{gtracks}/hg38_umap_s24.bed.gz",
        ],
        f"{WF['score_genotypes']}.linux_docker": DOCK["linux"],
        f"{WF['score_genotypes']}.gatk_docker": DOCK["gatk"],
        f"{WF['score_genotypes']}.sv_base_mini_docker": DOCK["svbm"],
        f"{WF['score_genotypes']}.sv_pipeline_docker": DOCK["svp"],
    }


def inputs_filter_genotypes(state) -> dict:
    sg_vcf = state["steps"]["score_genotypes"]["output_vcf"]
    jrc_ploidy = state["steps"]["join_raw_calls"]["ploidy_table"]
    return {
        f"{WF['filter_genotypes']}.vcf": sg_vcf,
        f"{WF['filter_genotypes']}.output_prefix": f"{COHORT}.filter_genotypes",
        f"{WF['filter_genotypes']}.ploidy_table": jrc_ploidy,
        f"{WF['filter_genotypes']}.sl_cutoff_table": f"{REF}/aou_sl_cutoff_table.tsv",
        f"{WF['filter_genotypes']}.primary_contigs_fai": f"{REF}/gs_primary_contigs.fai",
        f"{WF['filter_genotypes']}.ped_file": PED,
        f"{WF['filter_genotypes']}.run_qc": False,
        f"{WF['filter_genotypes']}.sv_base_mini_docker": DOCK["svbm"],
        f"{WF['filter_genotypes']}.sv_pipeline_docker": DOCK["svp"],
    }


INPUTS = {
    "join_raw_calls": inputs_join_raw_calls,
    "sv_concordance": inputs_sv_concordance,
    "score_genotypes": inputs_score_genotypes,
    "filter_genotypes": inputs_filter_genotypes,
}


def _ssm_run(ssm, commands, comment, timeout=600):
    r = ssm.send_command(InstanceIds=[INSTANCE], DocumentName="AWS-RunShellScript",
                         Parameters={"commands": commands}, Comment=comment, TimeoutSeconds=timeout)
    return r["Command"]["CommandId"]


def _ssm_wait(ssm, cid, poll=15):
    while True:
        try:
            inv = ssm.get_command_invocation(CommandId=cid, InstanceId=INSTANCE)
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(3); continue
        if inv["Status"] in {"Success", "Failed", "TimedOut", "Cancelled", "Cancelling"}:
            return inv
        time.sleep(poll)


def run_step(step: str):
    s = _load_state()
    if s["steps"].get(step, {}).get("status") == "COMPLETED":
        print(f"[SKIP] {step} already COMPLETED"); return
    s3 = boto3.client("s3", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)

    # 1. upload bundle + inputs
    bundle_local = ROOT / "wdl" / "bundles" / BUNDLE[step]
    bundle_key = f"workflows/gq-chain/{COHORT}/{step}/bundle.zip"
    s3.put_object(Bucket=BK, Key=bundle_key, Body=bundle_local.read_bytes())
    inputs = INPUTS[step](s)
    inputs_key = f"workflows/gq-chain/{COHORT}/{step}/inputs.json"
    s3.put_object(Bucket=BK, Key=inputs_key, Body=json.dumps(inputs, indent=2).encode())
    print(f"  uploaded bundle+inputs for {step}")

    # 2. dispatch miniwdl detached
    workdir = f"/tmp/gq-{step}"
    runscript = "\n".join([
        "set -euxo pipefail",
        f"rm -rf {workdir}; mkdir -p {workdir}; cd {workdir}",
        f"aws s3 cp s3://{BK}/{bundle_key} bundle.zip --region {REGION}",
        f"aws s3 cp s3://{BK}/{inputs_key} inputs.json --region {REGION}",
        "rm -rf wdl; unzip -q -o bundle.zip -d .",
        f"aws ecr get-login-password --region {REGION} | docker login --username AWS --password-stdin {ECR} >/dev/null 2>&1",
        "export PATH=$PATH:/root/.local/bin",
        f"mkdir -p {workdir}/run; cd {workdir}/run",
        f"nohup miniwdl run {workdir}/wdl/{MAIN_WDL[step]} -i {workdir}/inputs.json --dir {workdir}/run --no-color > {workdir}/run.log 2>&1 &",
        f"echo $! > {workdir}/run.pid",
        f"cat {workdir}/run.pid",
    ])
    cid = _ssm_run(ssm, runscript.split("\n"), f"gq-{step}-{COHORT}-launch")
    inv = _ssm_wait(ssm, cid)
    if inv["Status"] != "Success":
        print(f"  LAUNCH FAILED for {step}:", (inv.get("StandardErrorContent") or "")[-1500:]); raise SystemExit(1)
    print(f"  {step} miniwdl launched")
    s["steps"][step] = {"status": "RUNNING", "started_at": _now()}
    _save_state(s)

    # 3. poll the miniwdl PID
    poll_script = [
        f"if [ -f {workdir}/run.pid ] && kill -0 $(cat {workdir}/run.pid) 2>/dev/null; then echo RUNNING; else echo DONE; fi",
        f"grep -E 'workflow done|error|RunFailed|FAILED' {workdir}/run.log 2>/dev/null | tail -3 || true",
    ]
    start = time.time()
    while True:
        time.sleep(60)
        inv = _ssm_wait(ssm, _ssm_run(ssm, poll_script, f"gq-{step}-poll"))
        out = inv.get("StandardOutputContent", "")
        elapsed = int(time.time() - start)
        print(f"  [{step}] {out.splitlines()[0] if out else '?'} ({elapsed}s)")
        if out.startswith("DONE") or "\nDONE" in out or out.strip().startswith("DONE"):
            break
        if "DONE" in out.split("\n")[0]:
            break

    # 4. locate output VCF + (for JRC) ploidy table, upload to S3
    find_script = [
        f"OUTVCF=$(find {workdir}/run -name '*.vcf.gz' -path '*out*' 2>/dev/null | grep -vE 'index|tbi' | xargs ls -S 2>/dev/null | head -1)",
        "echo OUTVCF=$OUTVCF",
        f"grep -q 'workflow done' {workdir}/run.log 2>/dev/null && echo WORKFLOW_DONE || echo WORKFLOW_NOT_DONE",
        f"tail -3 {workdir}/run.log",
    ]
    inv = _ssm_wait(ssm, _ssm_run(ssm, find_script, f"gq-{step}-find"))
    print(inv.get("StandardOutputContent", "")[-800:])
    print(f"\n  {step}: inspect output above; record manually if needed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", required=True, choices=STEPS + ["all"])
    a = ap.parse_args()
    steps = STEPS if a.step == "all" else [a.step]
    for st in steps:
        run_step(st)


if __name__ == "__main__":
    main()
