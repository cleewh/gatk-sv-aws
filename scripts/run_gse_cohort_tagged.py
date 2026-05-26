#!/usr/bin/env python3
"""Launch GSE for the validation cohort with Property-10 cost tags.

Wraps run_gse_cohort.py logic but:
  * Sets a fresh cohort_id so Cost Explorer can isolate this rerun
  * Tags every StartRun call with the full Property-10 tag set
  * Records the tier label for tiered (wham) workflows in the manifest
  * Writes the run manifest to a per-rerun JSON so the original
    gse-cohort-runs.json (history) is preserved.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boto3
import botocore.exceptions

REGION = "ap-southeast-1"
ACCOUNT = os.environ["AWS_ACCOUNT_ID"]
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
OUTPUT_BASE_TPL = (
    f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e/{{cohort}}"
)
REF_BASE = f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"
COHORT_BASE = (
    f"s3://omics-cohorts-{REGION}-{ACCOUNT}/cohorts/gatk-sv-validation-2026q2"
)

WORKFLOWS = {
    # wham: upstream Broad Whamg.wdl (single-task, single-threaded full-genome).
    # Validated 2026-05-26 against the previous "fast" build (whamg-fast -x 16);
    # the fast build was reverted due to a 17% record divergence.
    # Wall-clock per sample ~3 h on omics.m.xlarge.
    "wham": {"id": "8098138", "storage_type": "DYNAMIC"},
    "manta": {"id": "4091926", "storage_type": "DYNAMIC"},
    "cc": {"id": "8771956", "storage_type": "DYNAMIC"},
    # scramble: NOT registered. Runs via scripts/run_scramble_ec2.sh because
    # HealthOmics terminates 2+ task workflows at 47 s. See docs/wdl-audit.md.
    "cse": {"id": "7038412", "storage_type": "DYNAMIC"},
}

DOCKER = {
    # Upstream Broad GATK-SV wham (single-threaded whamg).
    "wham": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/wham:2024-10-25-v0.29-beta-5ea22a52",
    "manta": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/manta:2023-09-14-v0.28.3-beta-3f22f94d",
    "sv_base": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/sv-base:2024-10-25-v0.29-beta-5ea22a52",
    "scramble": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/gatk-sv/scramble:2024-10-25-v0.29-beta-5ea22a52",
}

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


def sample_files(sample_id):
    return {
        "cram": f"{COHORT_BASE}/{sample_id}.final.cram",
        "crai": f"{COHORT_BASE}/{sample_id}.final.cram.crai",
    }


def build_params(module, sample_id):
    s = sample_files(sample_id)
    if module == "wham":
        return {
            "cram_or_bam": s["cram"], "cram_or_bam_idx": s["crai"],
            "ref_fasta": REF["fasta"], "ref_fasta_fai": REF["fai"],
            "sample_id": sample_id, "wham_docker": DOCKER["wham"],
        }
    if module == "manta":
        return {
            "cram_or_bam": s["cram"], "cram_or_bam_idx": s["crai"],
            "ref_fasta": REF["fasta"], "ref_fasta_fai": REF["fai"],
            "manta_docker": DOCKER["manta"],
            "manta_region_bed": REF["manta_region_bed"],
            "manta_region_bed_index": REF["manta_region_bed_tbi"],
            "sample_id": sample_id,
        }
    if module == "cc":
        return {
            "cram_or_bam": s["cram"], "cram_or_bam_idx": s["crai"],
            "ref_fasta": REF["fasta"], "ref_fasta_fai": REF["fai"],
            "ref_fasta_dict": REF["dict"], "gatk_jar": REF["gatk_jar"],
            "intervals": REF["intervals"], "docker": DOCKER["sv_base"],
            "sample_id": sample_id,
        }
    if module == "cse":
        return {
            "cram_or_bam": s["cram"], "cram_or_bam_idx": s["crai"],
            "ref_fasta": REF["fasta"], "ref_fasta_fai": REF["fai"],
            "ref_fasta_dict": REF["dict"], "gatk_jar": REF["gatk_jar"],
            "preprocessed_intervals": REF["intervals"],
            "primary_contigs_list": REF["primary_contigs_list"],
            "sd_locs_vcf": REF["sd_locs_vcf"],
            "docker": DOCKER["sv_base"],
            "sample_id": sample_id,
        }
    raise ValueError(
        f"Unknown module: {module!r}. scramble is not a HealthOmics workflow; "
        "run scripts/run_scramble_ec2.sh instead (or use scripts/run_cohort_e2e.py "
        "which dispatches it automatically)."
    )


def cost_tags(cohort_id, workflow_version, module, sample_count, environment="validation"):
    """Property-10 cost-tag set."""
    return {
        "gatk-sv:cohort-id": cohort_id,
        "gatk-sv:workflow-version": workflow_version,
        "gatk-sv:module": module,
        "gatk-sv:sample-count": str(sample_count),
        "gatk-sv:environment": environment,
    }


def launch_run(omics, s3, module, sample_id, cohort_id, sample_count, output_base):
    wf = WORKFLOWS[module]
    workflow_id = wf["id"]

    params = build_params(module, sample_id)
    run_name = f"{module}-{sample_id}"
    output_uri = f"{output_base}/{sample_id}/gse/{module}/"

    kwargs = {
        "workflowId": workflow_id,
        "name": run_name,
        "roleArn": ROLE_ARN,
        "outputUri": output_uri,
        "parameters": params,
        "storageType": wf["storage_type"],
        "tags": cost_tags(cohort_id, f"gse-{module}-{workflow_id}", f"GatherSampleEvidence:{module}", sample_count),
    }
    if wf.get("storage_capacity"):
        kwargs["storageCapacity"] = wf["storage_capacity"]

    resp = omics.start_run(**kwargs)
    run_id = resp["id"]
    print(f"  \u2713 {run_name} -> run {run_id}")
    return {
        "id": run_id, "name": run_name, "module": module, "sample": sample_id,
        "workflow_id": workflow_id,
    }


def load_samples():
    p = Path(__file__).parent.parent / "validation-cohort" / "inputs" / "manifest.json"
    return [s["sample_id"] for s in json.loads(p.read_text())["samples"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort-id", required=True)
    ap.add_argument("--samples", help="Comma-separated; default = all from manifest")
    ap.add_argument("--modules", default="wham,manta,cc,cse",
                    help="Comma-separated GSE modules. Default omits scramble; "
                         "scramble is launched separately via scripts/run_scramble_ec2.sh "
                         "(or run_cohort_e2e.py).")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--output", default=None,
                    help="Run manifest output path (default: gse-cohort-runs-<cohort-id>.json)")
    args = ap.parse_args()

    samples = args.samples.split(",") if args.samples else load_samples()
    modules = args.modules.split(",")
    if "scramble" in modules:
        sys.exit(
            "ERROR: scramble is not a HealthOmics workflow. Run scripts/run_scramble_ec2.sh "
            "for each sample (or use scripts/run_cohort_e2e.py which dispatches it "
            "automatically).\n"
            "Refusing to launch a scramble HealthOmics workflow."
        )
    sample_count = len(samples)
    output_base = OUTPUT_BASE_TPL.format(cohort=args.cohort_id)

    print(f"Cohort:  {args.cohort_id}")
    print(f"Samples: {sample_count}  ({', '.join(samples)})")
    print(f"Modules: {', '.join(modules)}")
    print(f"Total:   {sample_count * len(modules)} runs")
    print()

    omics = boto3.client("omics", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    launched = []

    for sid in samples:
        print(f"[{sid}]")
        for mod in modules:
            try:
                rec = launch_run(omics, s3, mod, sid, args.cohort_id, sample_count, output_base)
                launched.append(rec)
            except Exception as e:
                print(f"  \u2717 {mod}-{sid}: {e}")
            time.sleep(args.delay)
        print()

    out = Path(args.output) if args.output else (
        Path(__file__).parent.parent / f"gse-cohort-runs-{args.cohort_id}.json"
    )
    out.write_text(json.dumps({
        "cohort_id": args.cohort_id,
        "launched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "samples": samples,
        "modules": modules,
        "runs": launched,
        "total_runs": len(launched),
    }, indent=2))
    print(f"Run manifest: {out}")
    print(f"Total launched: {len(launched)}")


if __name__ == "__main__":
    main()
