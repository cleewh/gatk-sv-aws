#!/usr/bin/env python3
"""End-to-end orchestrator: GatherSampleEvidence -> AnnotateVcf for one cohort.

This is the single-command path that wraps everything currently scattered
across `run_gse_cohort_tagged.py`, the per-stage cohort launchers, the EC2
scramble + EC2 hybrid (CombineBatches + RemainingSteps), and `launch_annotate_vcf.py`.

It is intentionally a fat Python script -- not yet Step Functions.
The Step Functions orchestrator is specced in
`.kiro/specs/step-functions-orchestrator/` but isn't deployed; this script
is the imperative-Python proof-of-life that the same pipeline runs in one
command end-to-end.

What it does:

  Phase A    Per-sample GSE fan-out -- 4 sub-tools (cc, cse, manta, wham)
             x N samples, all submitted in parallel and polled to completion.
             scramble is intentionally NOT in Phase A: HealthOmics terminates
             2+ task workflows at 47 s, so the upstream multi-task Scramble.wdl
             can't run there. wham reverted to upstream Whamg.wdl 2026-05-26
             after the "fast" build was found to diverge by ~17 % of records.
  Phase A.5  scramble on EC2 -- one SSM dispatch of scripts/run_scramble_ec2.sh
             per sample (cluster_identifier 12-parallel + SCRAMble.R +
             make_scramble_vcf.py via direct docker run).
  Phase B    Cohort modules on HealthOmics (sequential):
             GBE -> ClusterBatch -> GenerateBatchMetrics -> FilterBatch
                 -> MergeBatchSites -> GenotypeBatch
             For each module the parameter dict is built either from a "template"
             run (an earlier successful run we copy) or from explicit inputs.
  Phase C    MakeCohortVcf hybrid (EC2 + miniwdl):
             1. SSM run scripts/run_combinebatches_ec2.sh   (CombineBatches on EC2 bash + Docker)
             2. SSM run scripts/run_remaining_steps_ec2.py  (Resolve / Genotype / Clean / QC via miniwdl)
  Phase D    AnnotateVcf on HealthOmics.
  Phase E    Cost report -- write cost-report.json with per-stage runtime + Cost Explorer summary.

Every resource-creating call carries the Property-10 cost-tag set so
Cost Explorer reports per-cohort totals cleanly.

Usage:
    AWS_ACCOUNT_ID=<your-12-digit-account-id> \\
    AWS_DEFAULT_REGION=ap-southeast-1 \\
    .venv/bin/python scripts/run_cohort_e2e.py \\
        --cohort-id gatk-sv-validation-2026q2-rerun-2026-05-25 \\
        --manifest validation-cohort/inputs/manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3

# --------------------------------------------------------------------------- #
# Shared cost-tag helper
# --------------------------------------------------------------------------- #
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cost_tags import cost_tags  # noqa: E402

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
REGION = "ap-southeast-1"
ACCOUNT = os.environ["AWS_ACCOUNT_ID"]
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role"
# Run cache id. Set GATK_SV_RUN_CACHE_ID once you've created your cache.
RUN_CACHE_ID = os.environ.get("GATK_SV_RUN_CACHE_ID", "__RUN_CACHE_ID__")
RUN_CACHE_BEHAVIOR = "CACHE_ALWAYS"
ROOT = Path(__file__).resolve().parent.parent

REF_BASE = f"s3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38"
COHORTS_BASE = f"s3://omics-cohorts-{REGION}-{ACCOUNT}/cohorts"
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"
OUTPUT_BASE_TPL = f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/{{cohort}}"

# Production-validated workflow IDs (latest, from session-4..6).
#
# Modules registered as HealthOmics workflows (the "8 + 8 + GQ chain"
# coverage of upstream GATK-SV v1.0):
#
#   Phase A    GSE per-tool runs (cc, cse, manta, wham) via run_gse_cohort_tagged.py
#   Phase A.5  scramble on EC2 (run_scramble_ec2.sh, dispatched via SSM)
#   Phase A.6  EvidenceQC (per-sample QC; gates entry to Phase B)
#   Phase B    cohort modules: GBE -> ClusterBatch -> ... -> GenotypeBatch (-> RegenotypeCNVs if >=100 samples)
#   Phase C    MakeCohortVcf hybrid (CombineBatches + RemainingSteps on EC2)
#   Phase C.1  RefineComplexVariants
#   Phase C.2  JoinRawCalls       \
#   Phase C.3  SVConcordance       \  GQ_Recalibrator chain (Req 19)
#   Phase C.4  ScoreGenotypes       /
#   Phase C.5  FilterGenotypes    /
#   Phase D    AnnotateVcf
#   Phase D.2  MainVcfQC (cohort-level QC plots)
#   Phase D.3  VisualizeCnvs (optional, gated by --include-visualize-cnvs)
#
# Workflow IDs marked None/empty are NOT YET registered with HealthOmics
# in this account. The Phase 8 packager (scripts/migrate_v1_modules.py)
# produces the bundles; scripts/bootstrap/08_register_workflows.py
# registers them and writes the resulting IDs to workflow-ids.json,
# which run_cohort_e2e.py reads at startup.
WORKFLOWS = {
    "gather_batch_evidence": "1575165",   # v5
    "cluster_batch": "2641017",           # v3
    "generate_batch_metrics": "5339393",
    "filter_batch": "3328339",            # v3
    "merge_batch_sites": "3326995",       # v2
    "genotype_batch": "9542089",
    "annotate_vcf": "6832584",

    # Phase 8 (Req 19) modules — registered when the packager output is
    # accepted by 08_register_workflows.py. Until then, these workflows
    # are skipped with a documented "not yet registered" message at runtime.
    "evidence_qc": None,
    "regenotype_cnvs": None,           # Was packaged earlier; not active.
    "refine_complex_variants": None,
    "join_raw_calls": None,
    "sv_concordance": None,
    "score_genotypes": None,
    "filter_genotypes": None,
    "main_vcf_qc": None,
    "visualize_cnvs": None,
}

# Reference template runs -- we copy these runs' parameter dicts and then
# swap the s3 URIs to point at the new cohort's outputs.  These IDs are
# the historical successful runs from the 2026q2 validation cohort.
TEMPLATE_RUNS = {
    "gather_batch_evidence": "6129002",
    "cluster_batch": "2870194",
    "generate_batch_metrics": "2916467",
    "filter_batch": "5070716",
    "merge_batch_sites": "7287325",
    "genotype_batch": "3154916",
    "annotate_vcf": "9839171",
}

# EC2 instance for the MakeCohortVcf hybrid path.
# Set GATK_SV_EC2_INSTANCE_ID to the instance id provisioned by
# scripts/bootstrap/01_provision_ec2_hybrid.py (or your equivalent).
EC2_INSTANCE_ID = os.environ.get("GATK_SV_EC2_INSTANCE_ID", "__EC2_INSTANCE_ID__")

# Polling cadence.
POLL_INTERVAL_SEC = 60

# Terminal HealthOmics run statuses.
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "DELETED"}


# --------------------------------------------------------------------------- #
# Run record + JSON dump helper
# --------------------------------------------------------------------------- #
@dataclass
class StageRecord:
    """One stage in the pipeline (a HealthOmics run, or an SSM command)."""
    stage: str
    kind: str  # "healthomics" or "ssm"
    id: str
    name: str
    started_at: str
    finished_at: str | None = None
    status: str | None = None
    duration_sec: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# HealthOmics polling
# --------------------------------------------------------------------------- #
def poll_healthomics_run(
    omics_client, run_id: str, *, label: str, poll_interval: int = POLL_INTERVAL_SEC
) -> dict[str, Any]:
    """Block until a HealthOmics run reaches terminal status. Return the get_run response."""
    started = time.time()
    last = None
    while True:
        info = omics_client.get_run(id=run_id)
        status = info.get("status")
        if status != last:
            print(
                f"  [{label}] run {run_id} status={status} "
                f"(elapsed={int(time.time() - started)}s)"
            )
            last = status
        if status in TERMINAL_STATUSES:
            return info
        time.sleep(poll_interval)


def poll_runs_until_done(
    omics_client, run_ids: list[str], *, label: str, poll_interval: int = POLL_INTERVAL_SEC
) -> dict[str, dict[str, Any]]:
    """Block until every HealthOmics run reaches terminal status. Return run_id -> info."""
    pending = set(run_ids)
    results: dict[str, dict[str, Any]] = {}
    started = time.time()
    while pending:
        for rid in list(pending):
            info = omics_client.get_run(id=rid)
            status = info.get("status")
            if status in TERMINAL_STATUSES:
                results[rid] = info
                pending.discard(rid)
                duration = int(time.time() - started)
                print(
                    f"  [{label}] {rid} -> {status} "
                    f"(remaining={len(pending)}, elapsed={duration}s)"
                )
        if pending:
            time.sleep(poll_interval)
    return results


# --------------------------------------------------------------------------- #
# Phase A : per-sample GSE fan-out
# --------------------------------------------------------------------------- #
def phase_a_gse_fanout(args: argparse.Namespace) -> dict[str, Any]:
    """Launch the GSE per-tool fan-out via run_gse_cohort_tagged.py and poll all 50 runs."""
    print("=" * 78)
    print(f"PHASE A: GatherSampleEvidence per-sample fan-out  ({args.cohort_id})")
    print("=" * 78)

    # Defer to the existing tagged launcher to submit the 50 per-tool runs.
    cmd = [
        ".venv/bin/python",
        str(ROOT / "scripts" / "run_gse_cohort_tagged.py"),
        "--cohort-id", args.cohort_id,
    ]
    if args.samples:
        cmd += ["--samples", args.samples]
    if args.modules:
        cmd += ["--modules", args.modules]
    print("Launching:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    manifest_path = ROOT / f"gse-cohort-runs-{args.cohort_id}.json"
    manifest = json.loads(manifest_path.read_text())
    run_ids = [r["id"] for r in manifest["runs"]]
    print(f"\n  Polling {len(run_ids)} GSE runs ...")

    omics = boto3.client("omics", region_name=REGION)
    results = poll_runs_until_done(omics, run_ids, label="GSE")

    failed = [rid for rid, info in results.items() if info["status"] != "COMPLETED"]
    if failed:
        raise RuntimeError(f"GSE phase had {len(failed)} non-COMPLETED runs: {failed}")
    print(f"  All {len(run_ids)} GSE runs COMPLETED.")
    return {"manifest": manifest, "run_results": results}


# --------------------------------------------------------------------------- #
# Phase A.5 : scramble on EC2 (one SSM dispatch per sample)
# --------------------------------------------------------------------------- #
def phase_a5_scramble_ec2(args: argparse.Namespace, output_base: str) -> list[StageRecord]:
    """Run scramble for every sample as direct docker on EC2 via SSM.

    HealthOmics terminates 2+ task workflows at 47s, so the upstream multi-task
    Scramble.wdl can't run there. We dispatch scripts/run_scramble_ec2.sh once
    per sample.
    """
    print("=" * 78)
    print(f"PHASE A.5: scramble on EC2 (SSM)  ({args.cohort_id})")
    print("=" * 78)

    samples = (
        args.samples.split(",") if args.samples
        else [s["sample_id"] for s in json.loads(Path(args.manifest).read_text())["samples"]]
    )
    sample_count = len(samples)

    s3 = boto3.client("s3", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)

    sh_local = ROOT / "scripts" / "run_scramble_ec2.sh"
    sh_key = f"workflows/run-scramble-ec2/{args.cohort_id}/run_scramble_ec2.sh"
    s3.put_object(Bucket=OUTPUT_BUCKET, Key=sh_key, Body=sh_local.read_bytes())
    print(f"  uploaded scramble shell to s3://{OUTPUT_BUCKET}/{sh_key}")

    pending: dict[str, dict[str, str]] = {}
    for sid in samples:
        env_lines = " ".join([
            f"AWS_ACCOUNT_ID={ACCOUNT}",
            f"AWS_DEFAULT_REGION={REGION}",
            f"SAMPLE={sid}",
            f"GATK_SV_COHORT_ID={args.cohort_id}",
            f"OUT_PREFIX=runs/gatk-sv-e2e/{args.cohort_id}/{sid}/scramble-real-ec2",
        ])
        commands = [
            f"aws s3 cp s3://{OUTPUT_BUCKET}/{sh_key} /tmp/run_scramble_ec2.sh --region {REGION}",
            "chmod +x /tmp/run_scramble_ec2.sh",
            f"export {env_lines} && bash /tmp/run_scramble_ec2.sh",
        ]
        resp = ssm.send_command(
            InstanceIds=[EC2_INSTANCE_ID],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands},
            Comment=f"scramble-ec2-{sid}-{args.cohort_id}",
            TimeoutSeconds=21_600,
        )
        cmd_id = resp["Command"]["CommandId"]
        pending[cmd_id] = {"sample": sid, "started_at": _now_iso()}
        print(f"  [{sid}] SSM command id: {cmd_id}")

    records: list[StageRecord] = []
    started = time.time()
    for cmd_id, meta in pending.items():
        sid = meta["sample"]
        print(f"\n--- polling scramble-ec2 for {sid} (cmd {cmd_id}) ---")
        inv_started = time.time()
        last = None
        while True:
            try:
                inv = ssm.get_command_invocation(
                    CommandId=cmd_id, InstanceId=EC2_INSTANCE_ID
                )
            except ssm.exceptions.InvocationDoesNotExist:
                time.sleep(2)
                continue
            st = inv["Status"]
            if st != last:
                elapsed = int(time.time() - inv_started)
                print(f"  [{sid}] status={st} (elapsed={elapsed}s)")
                last = st
            if st in {"Success", "Failed", "TimedOut", "Cancelled", "Cancelling"}:
                break
            time.sleep(POLL_INTERVAL_SEC)
        records.append(StageRecord(
            stage=f"scramble_ec2:{sid}",
            kind="ssm",
            id=cmd_id,
            name=f"run_scramble_ec2.sh:{sid}",
            started_at=meta["started_at"],
            finished_at=_now_iso(),
            status=inv["Status"],
            duration_sec=time.time() - inv_started,
            extra={
                "stdout_tail": (inv.get("StandardOutputContent") or "")[-300:],
                "output_uri": f"{output_base}/{sid}/scramble-real-ec2/",
            },
        ))
        if inv["Status"] != "Success":
            print("STDERR tail:", (inv.get("StandardErrorContent") or "")[-1500:])
            raise RuntimeError(f"scramble-ec2 for sample {sid} ended in status {inv['Status']}")

    print(f"\n  All {sample_count} scramble-ec2 SSM commands Succeeded "
          f"(total elapsed {int(time.time() - started)}s)")
    return records


# --------------------------------------------------------------------------- #
# Phase A.6 : EvidenceQC (per-sample QC after Phase A; gates entry to Phase B)
# --------------------------------------------------------------------------- #
def _maybe_skip_phase(label: str, module_key: str) -> StageRecord | None:
    """Return a "skipped" StageRecord if the workflow ID is None.

    Phase 8 modules (Req 19) are pre-registered in WORKFLOWS as None until
    08_register_workflows.py uploads their bundles to HealthOmics. When a
    cohort runs before that registration step, log the skip and continue.
    """
    if WORKFLOWS.get(module_key):
        return None
    print(f"  [SKIP] {label} ({module_key}) — not yet registered with HealthOmics")
    return StageRecord(
        stage=module_key, kind="healthomics",
        id="(not yet registered)",
        name=label,
        started_at=_now_iso(),
        finished_at=_now_iso(),
        status="SKIPPED",
        duration_sec=0.0,
        extra={"reason": f"WORKFLOWS[{module_key!r}] is None; run 08_register_workflows.py to register the Phase 8 (Req 19) modules first"},
    )


def phase_a6_evidence_qc(args: argparse.Namespace, output_base: str) -> StageRecord:
    """Run EvidenceQC on the cohort. Per-sample QC after Phase A; produces
    QC metrics that gate entry to the more expensive Phase B."""
    print("=" * 78)
    print(f"PHASE A.6: EvidenceQC  ({args.cohort_id})")
    print("=" * 78)

    skipped = _maybe_skip_phase("EvidenceQC", "evidence_qc")
    if skipped is not None:
        return skipped

    omics = boto3.client("omics", region_name=REGION)
    rec = _start_cohort_module(
        omics,
        module_key="evidence_qc",
        cohort_id=args.cohort_id,
        sample_count=args.sample_count,
        output_base=output_base,
    )
    print(f"  Started run {rec.id} ({rec.name})")
    info = poll_healthomics_run(omics, rec.id, label="evidence_qc")
    rec.finished_at = _now_iso()
    rec.status = info.get("status")
    rec.duration_sec = _wall_clock(info)
    rec.extra = {"output_uri": info.get("outputUri", "")}
    if rec.status != "COMPLETED":
        raise RuntimeError(f"EvidenceQC run {rec.id} ended in status {rec.status}")
    return rec


# --------------------------------------------------------------------------- #
# Phase C.1 - C.5 : RefineComplexVariants + GQ_Recalibrator chain (Req 19)
# --------------------------------------------------------------------------- #
def phase_c_post_processing(args: argparse.Namespace, output_base: str) -> list[StageRecord]:
    """Run the post-processing chain on the cohort VCF:

      C.1 RefineComplexVariants  (refines complex SV calls)
      C.2 JoinRawCalls           (start of GQ recalibrator chain)
      C.3 SVConcordance          (annotates concordance with raw calls)
      C.4 ScoreGenotypes         (GQ recalibrator scoring)
      C.5 FilterGenotypes        (drops low-confidence calls)

    Each step's output feeds the next; failure aborts the chain.
    """
    print("=" * 78)
    print(f"PHASE C.1-C.5: post-processing chain  ({args.cohort_id})")
    print("=" * 78)

    omics = boto3.client("omics", region_name=REGION)
    sequence = [
        ("C.1 RefineComplexVariants", "refine_complex_variants"),
        ("C.2 JoinRawCalls",          "join_raw_calls"),
        ("C.3 SVConcordance",         "sv_concordance"),
        ("C.4 ScoreGenotypes",        "score_genotypes"),
        ("C.5 FilterGenotypes",       "filter_genotypes"),
    ]
    records: list[StageRecord] = []
    for label, module_key in sequence:
        print(f"\n--- {label} ---")
        skipped = _maybe_skip_phase(label, module_key)
        if skipped is not None:
            records.append(skipped)
            continue
        rec = _start_cohort_module(
            omics,
            module_key=module_key,
            cohort_id=args.cohort_id,
            sample_count=args.sample_count,
            output_base=output_base,
        )
        print(f"  Started run {rec.id} ({rec.name})")
        info = poll_healthomics_run(omics, rec.id, label=module_key)
        rec.finished_at = _now_iso()
        rec.status = info.get("status")
        rec.duration_sec = _wall_clock(info)
        rec.extra = {"output_uri": info.get("outputUri", "")}
        records.append(rec)
        if rec.status != "COMPLETED":
            raise RuntimeError(f"{label} run {rec.id} ended in status {rec.status}")
    return records


# --------------------------------------------------------------------------- #
# Phase D.2 / D.3 : MainVcfQC + (optional) VisualizeCnvs (Req 19)
# --------------------------------------------------------------------------- #
def phase_d2_main_vcf_qc(args: argparse.Namespace, output_base: str) -> StageRecord:
    """Run MainVcfQC on the post-AnnotateVcf cohort VCF (cohort-level QC plots)."""
    print("=" * 78)
    print(f"PHASE D.2: MainVcfQC  ({args.cohort_id})")
    print("=" * 78)

    skipped = _maybe_skip_phase("MainVcfQC", "main_vcf_qc")
    if skipped is not None:
        return skipped

    omics = boto3.client("omics", region_name=REGION)
    rec = _start_cohort_module(
        omics,
        module_key="main_vcf_qc",
        cohort_id=args.cohort_id,
        sample_count=args.sample_count,
        output_base=output_base,
    )
    print(f"  Started run {rec.id} ({rec.name})")
    info = poll_healthomics_run(omics, rec.id, label="main_vcf_qc")
    rec.finished_at = _now_iso()
    rec.status = info.get("status")
    rec.duration_sec = _wall_clock(info)
    rec.extra = {"output_uri": info.get("outputUri", "")}
    if rec.status != "COMPLETED":
        # MainVcfQC failures are non-fatal; the cohort VCF is still valid.
        # Log loudly and continue.
        print(f"  WARNING: MainVcfQC run {rec.id} ended in status {rec.status} "
              f"(continuing — QC plots are non-essential)")
    return rec


def phase_d3_visualize_cnvs(args: argparse.Namespace, output_base: str) -> StageRecord:
    """Optional: per-CNV PNG visualization. Gated by --include-visualize-cnvs."""
    print("=" * 78)
    print(f"PHASE D.3: VisualizeCnvs  ({args.cohort_id})")
    print("=" * 78)

    skipped = _maybe_skip_phase("VisualizeCnvs", "visualize_cnvs")
    if skipped is not None:
        return skipped

    omics = boto3.client("omics", region_name=REGION)
    rec = _start_cohort_module(
        omics,
        module_key="visualize_cnvs",
        cohort_id=args.cohort_id,
        sample_count=args.sample_count,
        output_base=output_base,
    )
    print(f"  Started run {rec.id} ({rec.name})")
    info = poll_healthomics_run(omics, rec.id, label="visualize_cnvs")
    rec.finished_at = _now_iso()
    rec.status = info.get("status")
    rec.duration_sec = _wall_clock(info)
    rec.extra = {"output_uri": info.get("outputUri", "")}
    if rec.status != "COMPLETED":
        # VisualizeCnvs is opt-in; failures are non-fatal.
        print(f"  WARNING: VisualizeCnvs run {rec.id} ended in status {rec.status} "
              f"(continuing — visualization is opt-in)")
    return rec


# --------------------------------------------------------------------------- #
# Phase B : cohort HealthOmics modules (sequential)
# --------------------------------------------------------------------------- #
def _swap_uris(params: dict[str, Any], from_cohort: str, to_cohort: str) -> dict[str, Any]:
    """Recursively rewrite s3 URIs that mention `from_cohort` to use `to_cohort` instead."""
    out: Any
    if isinstance(params, dict):
        out = {k: _swap_uris(v, from_cohort, to_cohort) for k, v in params.items()}
    elif isinstance(params, list):
        out = [_swap_uris(v, from_cohort, to_cohort) for v in params]
    elif isinstance(params, str) and from_cohort in params:
        out = params.replace(from_cohort, to_cohort)
    else:
        out = params
    return out


def _start_cohort_module(
    omics, *,
    module_key: str,
    cohort_id: str,
    sample_count: int,
    output_base: str,
    parameter_overrides: dict[str, Any] | None = None,
) -> StageRecord:
    """Start a HealthOmics run for a cohort module, copying params from the template run.

    For Phase 8 (Req 19) modules that don't yet have a TEMPLATE_RUNS entry,
    `parameter_overrides` MUST be provided; the helper otherwise raises a
    KeyError to make the missing-template case visible.
    """
    workflow_id = WORKFLOWS[module_key]
    if workflow_id is None:
        raise RuntimeError(
            f"WORKFLOWS[{module_key!r}] is None — workflow not yet registered. "
            "Run scripts/bootstrap/08_register_workflows.py first or call "
            "_maybe_skip_phase() before _start_cohort_module()."
        )

    if module_key in TEMPLATE_RUNS:
        template = omics.get_run(id=TEMPLATE_RUNS[module_key])
        params = dict(template.get("parameters", {}))
        # Original validation cohort id used in the template run; the orchestrator
        # rewrites these to point at the rerun's outputs.
        params = _swap_uris(params, "gatk-sv-validation-2026q2", cohort_id)
    else:
        # Phase 8 (Req 19) modules don't have a template-run reference yet;
        # rely on parameter_overrides to fully specify the run.
        params = {}
    if parameter_overrides:
        params.update(parameter_overrides)

    output_uri = f"{output_base}/batch/{module_key}/"
    name = f"{module_key}-{cohort_id}"

    started_at = _now_iso()
    resp = omics.start_run(
        workflowId=workflow_id,
        name=name,
        roleArn=ROLE_ARN,
        outputUri=output_uri,
        parameters=params,
        storageType="DYNAMIC",
        cacheId=RUN_CACHE_ID,
        cacheBehavior=RUN_CACHE_BEHAVIOR,
        tags=cost_tags(
            cohort_id=cohort_id,
            workflow_version=f"{module_key}-{workflow_id}",
            module=module_key,
            sample_count=sample_count,
        ),
    )
    return StageRecord(
        stage=module_key, kind="healthomics", id=resp["id"], name=name, started_at=started_at,
    )


def phase_b_cohort_modules(args: argparse.Namespace, output_base: str) -> list[StageRecord]:
    """Run GBE -> ... -> GenotypeBatch sequentially on HealthOmics."""
    print("=" * 78)
    print(f"PHASE B: cohort modules on HealthOmics  ({args.cohort_id})")
    print("=" * 78)

    omics = boto3.client("omics", region_name=REGION)
    sample_count = args.sample_count
    sequence = [
        "gather_batch_evidence",
        "cluster_batch",
        "generate_batch_metrics",
        "filter_batch",
        "merge_batch_sites",
        "genotype_batch",
    ]

    records: list[StageRecord] = []
    for module_key in sequence:
        print(f"\n--- {module_key} ---")
        rec = _start_cohort_module(
            omics,
            module_key=module_key,
            cohort_id=args.cohort_id,
            sample_count=sample_count,
            output_base=output_base,
        )
        print(f"  Started run {rec.id} ({rec.name})")
        info = poll_healthomics_run(omics, rec.id, label=module_key)
        rec.finished_at = _now_iso()
        rec.status = info.get("status")
        rec.duration_sec = _wall_clock(info)
        rec.extra = {"output_uri": info.get("outputUri", "")}
        records.append(rec)
        if rec.status != "COMPLETED":
            raise RuntimeError(
                f"{module_key} run {rec.id} ended in status {rec.status}"
            )
    return records


def _wall_clock(info: dict[str, Any]) -> float | None:
    """Best-effort wall-clock (seconds) from a get_run response."""
    start = info.get("startTime") or info.get("creationTime")
    stop = info.get("stopTime")
    if not start or not stop:
        return None
    if hasattr(start, "timestamp"):
        return stop.timestamp() - start.timestamp()
    return None


# --------------------------------------------------------------------------- #
# Phase C : MakeCohortVcf hybrid (EC2 + miniwdl via SSM)
# --------------------------------------------------------------------------- #
def _send_ssm(ssm, *, instance_id: str, commands: list[str], label: str,
              env: dict[str, str] | None = None) -> str:
    """Fire-and-watch an SSM RunShellScript. Returns the SSM command id."""
    if env:
        envline = " ".join(f"{k}={v}" for k, v in env.items())
        commands = [f"export {envline}"] + commands
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
        TimeoutSeconds=86_400,  # 24 h; SSM caps the *queue* time at 48h
    )
    cid = resp["Command"]["CommandId"]
    print(f"  [{label}] SSM command id: {cid}")
    return cid


def _poll_ssm(ssm, *, instance_id: str, command_id: str, label: str,
              poll_interval: int = 30) -> dict[str, Any]:
    """Block until an SSM command reaches a terminal status and return the invocation."""
    started = time.time()
    last = None
    while True:
        try:
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(2)
            continue
        st = inv["Status"]
        if st != last:
            print(
                f"  [{label}] SSM {command_id} status={st} "
                f"(elapsed={int(time.time() - started)}s)"
            )
            last = st
        if st in {"Success", "Failed", "TimedOut", "Cancelled", "Cancelling"}:
            return inv
        time.sleep(poll_interval)


def phase_c_makecohortvcf_hybrid(
    args: argparse.Namespace, output_base: str
) -> list[StageRecord]:
    print("=" * 78)
    print(f"PHASE C: MakeCohortVcf hybrid (EC2 + miniwdl)  ({args.cohort_id})")
    print("=" * 78)

    ssm = boto3.client("ssm", region_name=REGION)
    sample_count = args.sample_count
    common_env = {
        "AWS_ACCOUNT_ID": ACCOUNT,
        "AWS_DEFAULT_REGION": REGION,
        "GATK_SV_COHORT_ID": args.cohort_id,
        "GATK_SV_SAMPLE_COUNT": str(sample_count),
        "GATK_SV_ENVIRONMENT": "validation",
    }

    records: list[StageRecord] = []

    # ---- C.0: stage the shell script onto EC2 --------------------------- #
    print("\n--- staging combinebatches shell script onto EC2 ---")
    s3 = boto3.client("s3", region_name=REGION)
    sh_local = ROOT / "scripts" / "run_combinebatches_ec2.sh"
    sh_key = f"workflows/run-combinebatches-ec2/{args.cohort_id}/run_combinebatches_ec2.sh"
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=sh_key,
        Body=sh_local.read_bytes(),
    )
    print(f"  uploaded shell script to s3://{OUTPUT_BUCKET}/{sh_key}")

    # ---- C.1: CombineBatches via run_combinebatches_ec2.sh ---------------- #
    print("\n--- CombineBatches on EC2 ---")
    started_at = _now_iso()
    started = time.time()
    cmd_id = _send_ssm(
        ssm,
        instance_id=EC2_INSTANCE_ID,
        commands=[
            f"aws s3 cp s3://{OUTPUT_BUCKET}/{sh_key} /tmp/run_combinebatches_ec2.sh "
            "--region " + REGION,
            "chmod +x /tmp/run_combinebatches_ec2.sh",
            "bash /tmp/run_combinebatches_ec2.sh",
        ],
        label="combinebatches",
        env=common_env,
    )
    inv = _poll_ssm(ssm, instance_id=EC2_INSTANCE_ID, command_id=cmd_id, label="combinebatches")
    records.append(StageRecord(
        stage="combinebatches_ec2",
        kind="ssm",
        id=cmd_id,
        name="run_combinebatches_ec2.sh",
        started_at=started_at,
        finished_at=_now_iso(),
        status=inv["Status"],
        duration_sec=time.time() - started,
        extra={"stdout_tail": (inv.get("StandardOutputContent") or "")[-500:]},
    ))
    if inv["Status"] != "Success":
        print("STDERR:", (inv.get("StandardErrorContent") or "")[-2000:])
        raise RuntimeError("CombineBatches EC2 stage failed")

    # ---- C.2: RemainingSteps via miniwdl on EC2 -------------------------- #
    print("\n--- RemainingSteps on EC2 (miniwdl) ---")
    started_at = _now_iso()
    started = time.time()
    cmd = [
        ".venv/bin/python",
        str(ROOT / "scripts" / "run_remaining_steps_ec2.py"),
    ]
    env = {**os.environ, "GATK_SV_COHORT_ID": args.cohort_id}
    subprocess.run(cmd, check=True, cwd=str(ROOT), env=env)
    records.append(StageRecord(
        stage="remaining_steps_ec2",
        kind="ssm",
        id="(SSM dispatched by run_remaining_steps_ec2.py)",
        name="run_remaining_steps_ec2.py",
        started_at=started_at,
        finished_at=_now_iso(),
        status="Success",
        duration_sec=time.time() - started,
    ))
    return records


# --------------------------------------------------------------------------- #
# Phase D : AnnotateVcf
# --------------------------------------------------------------------------- #
def phase_d_annotate_vcf(args: argparse.Namespace, output_base: str) -> StageRecord:
    print("=" * 78)
    print(f"PHASE D: AnnotateVcf  ({args.cohort_id})")
    print("=" * 78)

    omics = boto3.client("omics", region_name=REGION)
    rec = _start_cohort_module(
        omics,
        module_key="annotate_vcf",
        cohort_id=args.cohort_id,
        sample_count=args.sample_count,
        output_base=output_base,
    )
    print(f"  Started run {rec.id}")
    info = poll_healthomics_run(omics, rec.id, label="annotate_vcf")
    rec.finished_at = _now_iso()
    rec.status = info.get("status")
    rec.duration_sec = _wall_clock(info)
    rec.extra = {"output_uri": info.get("outputUri", "")}
    if rec.status != "COMPLETED":
        raise RuntimeError(f"AnnotateVcf run {rec.id} ended in status {rec.status}")
    return rec


# --------------------------------------------------------------------------- #
# Phase E : cost report (best-effort)
# --------------------------------------------------------------------------- #
def phase_e_cost_report(
    args: argparse.Namespace, output_base: str, records: list[StageRecord]
) -> Path:
    print("=" * 78)
    print(f"PHASE E: cost report  ({args.cohort_id})")
    print("=" * 78)

    report = {
        "cohort_id": args.cohort_id,
        "sample_count": args.sample_count,
        "region": REGION,
        "generated_at": _now_iso(),
        "stages": [r.__dict__ for r in records],
        "note": (
            "Cost values are NOT included here -- query Cost Explorer with "
            f"the tag gatk-sv:cohort-id={args.cohort_id} once spend has "
            "settled (~24h after run completion)."
        ),
    }
    out = ROOT / f"cost-report-{args.cohort_id}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"  wrote {out}")
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--cohort-id", required=True, help="Stable id used for cost tagging")
    ap.add_argument("--manifest", default=str(ROOT / "validation-cohort" / "inputs" / "manifest.json"))
    ap.add_argument("--samples", default=None, help="Override sample list (comma-separated)")
    ap.add_argument("--modules", default=None, help="GSE sub-tools (default all 5)")
    ap.add_argument("--skip-gse", action="store_true", help="Skip Phase A (GSE outputs already exist)")
    ap.add_argument("--skip-scramble-ec2", action="store_true",
                    help="Skip Phase A.5 (scramble on EC2). Use only if scramble outputs "
                         "already exist for every sample under <output_base>/<sample>/scramble-real-ec2/.")
    ap.add_argument("--skip-evidence-qc", action="store_true",
                    help="Skip Phase A.6 (EvidenceQC).")
    ap.add_argument("--skip-cohort", action="store_true", help="Skip Phase B (cohort modules)")
    ap.add_argument("--skip-makecohortvcf", action="store_true", help="Skip Phase C (EC2 hybrid)")
    ap.add_argument("--skip-post-processing", action="store_true",
                    help="Skip Phase C.1-C.5 (RefineComplexVariants + GQ_Recalibrator chain).")
    ap.add_argument("--skip-annotate", action="store_true", help="Skip Phase D (AnnotateVcf)")
    ap.add_argument("--skip-main-vcf-qc", action="store_true",
                    help="Skip Phase D.2 (MainVcfQC cohort-level QC plots).")
    ap.add_argument("--include-visualize-cnvs", action="store_true",
                    help="Run Phase D.3 (VisualizeCnvs per-CNV PNGs). Default off.")
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    args.sample_count = (
        len(args.samples.split(","))
        if args.samples
        else len(manifest["samples"])
    )

    output_base = OUTPUT_BASE_TPL.format(cohort=args.cohort_id)
    print(f"Cohort:       {args.cohort_id}")
    print(f"Sample count: {args.sample_count}")
    print(f"Output base:  {output_base}")
    print(f"Run cache:    {RUN_CACHE_ID}  ({RUN_CACHE_BEHAVIOR})")
    print()

    all_records: list[StageRecord] = []
    pipeline_started = time.time()

    if not args.skip_gse:
        gse = phase_a_gse_fanout(args)
        for run in gse["manifest"]["runs"]:
            info = gse["run_results"].get(run["id"], {})
            all_records.append(StageRecord(
                stage=f"GSE:{run['module']}:{run['sample']}",
                kind="healthomics",
                id=run["id"],
                name=run["name"],
                started_at="(see manifest)",
                finished_at=_now_iso(),
                status=info.get("status"),
                duration_sec=_wall_clock(info),
            ))

    if not args.skip_scramble_ec2:
        all_records.extend(phase_a5_scramble_ec2(args, output_base))

    if not args.skip_evidence_qc:
        all_records.append(phase_a6_evidence_qc(args, output_base))

    if not args.skip_cohort:
        all_records.extend(phase_b_cohort_modules(args, output_base))

    if not args.skip_makecohortvcf:
        all_records.extend(phase_c_makecohortvcf_hybrid(args, output_base))

    if not args.skip_post_processing:
        all_records.extend(phase_c_post_processing(args, output_base))

    if not args.skip_annotate:
        all_records.append(phase_d_annotate_vcf(args, output_base))

    if not args.skip_main_vcf_qc:
        all_records.append(phase_d2_main_vcf_qc(args, output_base))

    if args.include_visualize_cnvs:
        all_records.append(phase_d3_visualize_cnvs(args, output_base))

    print()
    print(f"=== Pipeline elapsed: {int(time.time() - pipeline_started)}s ===")
    phase_e_cost_report(args, output_base, all_records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
