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


def _load_workflow_ids_from_bootstrap() -> None:
    """Override WORKFLOWS values from workflow-ids.json if present.

    workflow-ids.json is produced by scripts/bootstrap/08_register_workflows.py
    when a customer registers workflows in their account. The dict maps
    upstream module name (e.g. 'EvidenceQC') -> {workflow_id, name, ...}.
    We translate these to the snake_case keys used in WORKFLOWS and patch
    the dict in place.
    """
    path = ROOT / "workflow-ids.json"
    if not path.exists():
        return
    try:
        registered = json.loads(path.read_text())
    except json.JSONDecodeError:
        return
    upstream_to_snake = {
        "GatherSampleEvidence": "gather_sample_evidence",
        "GatherBatchEvidence":  "gather_batch_evidence",
        "ClusterBatch":         "cluster_batch",
        "GenerateBatchMetrics": "generate_batch_metrics",
        "FilterBatch":          "filter_batch",
        "MergeBatchSites":      "merge_batch_sites",
        "GenotypeBatch":        "genotype_batch",
        "RegenotypeCNVs":       "regenotype_cnvs",
        "MakeCohortVcf":        "make_cohort_vcf",
        "AnnotateVcf":          "annotate_vcf",
        "EvidenceQC":           "evidence_qc",
        "RefineComplexVariants":"refine_complex_variants",
        "JoinRawCalls":         "join_raw_calls",
        "SVConcordance":        "sv_concordance",
        "ScoreGenotypes":       "score_genotypes",
        "FilterGenotypes":      "filter_genotypes",
        "MainVcfQC":            "main_vcf_qc",
        "VisualizeCnvs":        "visualize_cnvs",
    }
    n_loaded = 0
    for upstream_name, info in registered.items():
        snake = upstream_to_snake.get(upstream_name)
        if snake and isinstance(info, dict) and info.get("workflow_id"):
            WORKFLOWS[snake] = info["workflow_id"]
            n_loaded += 1
    if n_loaded:
        print(f"  Loaded {n_loaded} workflow IDs from {path.name}")


_load_workflow_ids_from_bootstrap()

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
# Module_Phase boundaries (Req 19.1–19.6, Req 14.1, 14.2)
# --------------------------------------------------------------------------- #
# Each StageRecord.stage value maps to exactly one of the four upstream
# GATK-SV v1.0 Module_Phase boundaries. The CLI exposes a `--skip-phase-*`
# flag for each boundary; failure of one phase blocks subsequent phases
# unless the corresponding `--skip-phase-*` flag was set.
#
#   Phase A (per-sample):     GSE fan-out, scramble-EC2, EvidenceQC
#   Phase B (cohort):         GBE -> ClusterBatch -> ... -> GenotypeBatch
#                             (RegenotypeCNVs activated when sample_count >= 100)
#   Phase C (post-processing): MakeCohortVcf hybrid + RefineComplexVariants
#                             + GQ_Recalibrator chain (JoinRawCalls, SVConcordance,
#                             ScoreGenotypes, FilterGenotypes)
#   Phase D (delivery):       AnnotateVcf, MainVcfQC, optional VisualizeCnvs
PHASE_A_STAGES = frozenset({"evidence_qc"})       # plus GSE:* and scramble_ec2:* prefixes
PHASE_B_STAGES = frozenset({
    "gather_batch_evidence", "cluster_batch", "generate_batch_metrics",
    "filter_batch", "merge_batch_sites", "genotype_batch", "regenotype_cnvs",
})
PHASE_C_STAGES = frozenset({
    "combinebatches_ec2", "remaining_steps_ec2",
    "refine_complex_variants", "join_raw_calls", "sv_concordance",
    "score_genotypes", "filter_genotypes",
})
PHASE_D_STAGES = frozenset({"annotate_vcf", "main_vcf_qc", "visualize_cnvs"})

# Stage-status values that indicate the stage finished successfully or was
# intentionally bypassed; anything else (FAILED, CANCELLED, TimedOut, …)
# blocks downstream phases unless the user opts in with --skip-phase-*.
_OK_STATUSES = frozenset({"COMPLETED", "Success", "SKIPPED"})


def _classify_phase(stage: str) -> str:
    """Return "A" | "B" | "C" | "D" | "" for the stage name on a StageRecord."""
    if stage.startswith("GSE:") or stage.startswith("scramble_ec2:"):
        return "A"
    if stage in PHASE_A_STAGES:
        return "A"
    if stage in PHASE_B_STAGES:
        return "B"
    if stage in PHASE_C_STAGES:
        return "C"
    if stage in PHASE_D_STAGES:
        return "D"
    return ""


def _summarize_phase(records: list["StageRecord"], phase: str) -> dict[str, Any]:
    """Build a {start, stop, status, records_count, ...} dict for one phase.

    Status rules:
      * if every record's status is in _OK_STATUSES -> COMPLETED (or SKIPPED
        when every record is SKIPPED)
      * else -> FAILED (with the first non-OK status surfaced as `error_status`)
      * if no records belong to the phase -> NOT_STARTED
    """
    phase_records = [r for r in records if _classify_phase(r.stage) == phase]
    if not phase_records:
        return {
            "phase": phase,
            "status": "NOT_STARTED",
            "records_count": 0,
            "start": None,
            "stop": None,
        }
    statuses = [r.status for r in phase_records]
    if all(s == "SKIPPED" for s in statuses):
        outcome = "SKIPPED"
        bad = None
    elif all(s in _OK_STATUSES for s in statuses):
        outcome = "COMPLETED"
        bad = None
    else:
        outcome = "FAILED"
        bad = next((s for s in statuses if s not in _OK_STATUSES), None)
    starts = [r.started_at for r in phase_records if r.started_at]
    stops = [r.finished_at for r in phase_records if r.finished_at]
    return {
        "phase": phase,
        "status": outcome,
        "records_count": len(phase_records),
        "start": min(starts) if starts else None,
        "stop": max(stops) if stops else None,
        "error_status": bad,
    }


def _check_phase_prereq(
    phase_outcomes: dict[str, dict[str, Any]],
    current: str,
    required: list[str],
) -> None:
    """Halt unless every required prior phase is COMPLETED or SKIPPED.

    A phase is considered satisfied if its `status` is COMPLETED or SKIPPED.
    A phase that ran but FAILED (or has any record with a non-OK status)
    blocks the current phase. The user can explicitly opt out by passing
    --skip-phase-<prior> on the CLI.
    """
    blockers = []
    for prior in required:
        outcome = phase_outcomes.get(prior, {})
        st = outcome.get("status", "NOT_STARTED")
        if st not in {"COMPLETED", "SKIPPED"}:
            blockers.append((prior, st))
    if blockers:
        names = ", ".join(f"Phase {p}={s}" for p, s in blockers)
        flags = " ".join(f"--skip-phase-{p.lower()}" for p, _ in blockers)
        raise RuntimeError(
            f"Cannot start Phase {current}: prerequisite phase(s) did not complete: "
            f"{names}. Re-run with {flags} to acknowledge the missing prior phase(s) "
            "and bypass the prerequisite check."
        )


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
    if getattr(args, "cohort_base", None):
        cmd += ["--cohort-base", args.cohort_base]
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
def _derive_cohorts_prefix(
    args: argparse.Namespace,
    cohort_base_override: str | None = None,
) -> str:
    """Resolve the cohort S3 prefix used by run_scramble_ec2.sh as COHORTS_PREFIX.

    Resolution order:
      1. ``cohort_base_override`` (explicit caller arg) — full S3 URI; we extract the path.
      2. ``args.cohort_base`` (CLI override) — full S3 URI; same extraction.
      3. The manifest's ``destination_base`` — full S3 URI; same extraction.
      4. The legacy default ``cohorts/gatk-sv-validation-2026q2`` (matches
         run_scramble_ec2.sh's hardcoded default; preserves prior behaviour).

    The returned value is a *path* (no scheme, no bucket), e.g.
    ``cohorts/gatk-sv-156``, because run_scramble_ec2.sh constructs the
    full URI by combining COHORTS_BUCKET and COHORTS_PREFIX.
    """
    candidate = cohort_base_override or getattr(args, "cohort_base", None)
    if not candidate:
        try:
            manifest = json.loads(Path(args.manifest).read_text())
            candidate = manifest.get("destination_base")
        except (OSError, json.JSONDecodeError):
            candidate = None
    if not candidate:
        return "cohorts/gatk-sv-validation-2026q2"
    # candidate is an s3:// URI; strip the scheme + bucket to leave the prefix.
    if candidate.startswith("s3://"):
        without_scheme = candidate[len("s3://"):]
        # drop the leading bucket segment
        if "/" in without_scheme:
            return without_scheme.split("/", 1)[1].rstrip("/")
        return ""
    return candidate.lstrip("/").rstrip("/")


def _gse_runs_by_sample_module(
    gse_run_results: dict[str, Any] | None,
) -> dict[str, dict[str, str]]:
    """Group Phase A run records by ``(sample, module)``.

    ``gse_run_results`` is the dict returned by ``phase_a_gse_fanout`` —
    ``{"manifest": <manifest dict>, "run_results": <run_id -> get_run info>}``.
    Returns ``{sample_id: {module: run_id, ...}, ...}`` so callers can build
    per-sample COUNTS_S3 / MANTA_S3 paths from the actual cc/manta run IDs.
    """
    out: dict[str, dict[str, str]] = {}
    if not gse_run_results:
        return out
    manifest = gse_run_results.get("manifest") or {}
    for run in manifest.get("runs", []):
        sid = run.get("sample")
        mod = run.get("module")
        rid = run.get("id")
        if not (sid and mod and rid):
            continue
        out.setdefault(sid, {})[mod] = rid
    return out


def phase_a5_scramble_ec2(
    args: argparse.Namespace,
    output_base: str,
    gse_run_results: dict[str, Any] | None = None,
    cohort_base: str | None = None,
) -> list[StageRecord]:
    """Run scramble for every sample as direct docker on EC2 via SSM.

    HealthOmics terminates 2+ task workflows at 47s, so the upstream multi-task
    Scramble.wdl can't run there. We dispatch scripts/run_scramble_ec2.sh once
    per sample.

    When ``gse_run_results`` is supplied (Phase A produced cc + manta runs in
    this orchestration), the SSM env block carries explicit per-sample
    COUNTS_S3 and MANTA_S3 paths derived from the actual run IDs, so
    run_scramble_ec2.sh does not fall back to its hardcoded prior-rerun
    defaults. When ``gse_run_results`` is None / empty (skip-gse case), the
    shell falls back to its own defaults — preserving prior behaviour.
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

    cohorts_prefix = _derive_cohorts_prefix(args, cohort_base_override=cohort_base)
    runs_by_sample = _gse_runs_by_sample_module(gse_run_results)

    # Dispatch SERIALLY: send one SSM command, poll it to terminal status,
    # then send the next. Concurrent dispatch on a single EC2 instance
    # caused network/disk saturation when 9+ samples raced to download the
    # 3GB reference FASTA and per-sample CRAMs simultaneously (see
    # docs/context-transfer-session6.md "scramble parallel dispatch
    # postmortem"). The make_scramble_vcf step is single-threaded per
    # sample anyway, so net wall-clock time is roughly equivalent but
    # reliable.
    records: list[StageRecord] = []
    overall_started = time.time()
    for sid in samples:
        env_pairs = [
            f"AWS_ACCOUNT_ID={ACCOUNT}",
            f"AWS_DEFAULT_REGION={REGION}",
            f"SAMPLE={sid}",
            f"GATK_SV_COHORT_ID={args.cohort_id}",
            f"OUT_PREFIX=runs/gatk-sv-e2e/{args.cohort_id}/{sid}/scramble-real-ec2",
            f"COHORTS_PREFIX={cohorts_prefix}",
        ]
        sample_runs = runs_by_sample.get(sid, {})
        cc_run_id = sample_runs.get("cc")
        manta_run_id = sample_runs.get("manta")
        if cc_run_id:
            counts_s3 = (
                f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/{args.cohort_id}/{sid}/"
                f"gse/cc/{cc_run_id}/out/counts/{sid}.counts.tsv.gz"
            )
            env_pairs.append(f"COUNTS_S3={counts_s3}")
        if manta_run_id:
            manta_s3 = (
                f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/{args.cohort_id}/{sid}/"
                f"gse/manta/{manta_run_id}/out/manta_vcf/{sid}.manta.vcf.gz"
            )
            env_pairs.append(f"MANTA_S3={manta_s3}")
        env_lines = " ".join(env_pairs)
        commands = [
            f"aws s3 cp s3://{OUTPUT_BUCKET}/{sh_key} /tmp/run_scramble_ec2.sh --region {REGION}",
            "chmod +x /tmp/run_scramble_ec2.sh",
            f"export {env_lines} && bash /tmp/run_scramble_ec2.sh",
        ]
        started_at = _now_iso()
        resp = ssm.send_command(
            InstanceIds=[EC2_INSTANCE_ID],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands},
            Comment=f"scramble-ec2-{sid}-{args.cohort_id}",
            TimeoutSeconds=21_600,
        )
        cmd_id = resp["Command"]["CommandId"]
        print(f"  [{sid}] SSM command id: {cmd_id}")

        # Poll THIS command to terminal status before dispatching the next.
        print(f"--- polling scramble-ec2 for {sid} (cmd {cmd_id}) ---")
        inv_started = time.time()
        last = None
        inv: dict[str, Any] = {}
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
            started_at=started_at,
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
          f"(total elapsed {int(time.time() - overall_started)}s)")
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
def _gq_dockers() -> dict[str, str]:
    """ECR docker URIs for the post-processing modules (this account/region)."""
    ecr = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
    return {
        "gatk": f"{ecr}/gatk-sv/gatk:mw-gatk-sv-672d85",
        "svbm": f"{ecr}/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52",
        "svp": f"{ecr}/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604",
        "linux": f"{ecr}/ecr-public/lts/ubuntu:18.04",
    }


# --------------------------------------------------------------------------- #
# HealthOmics native-output resolver + index co-location
# --------------------------------------------------------------------------- #
# HealthOmics writes each run's outputs under a NESTED, run-id-scoped layout:
#
#     {outputUri}{run_id}/out/{output_decl_name}/{file}
#
# (the orchestrator passes outputUri=".../batch/{module_key}/", so the actual
# output of a run lives at ".../batch/{module_key}/{run_id}/out/{decl}/{file}").
#
# A second wrinkle: for a `File`-typed VCF output declaration, HealthOmics
# sometimes places the companion `.tbi` ONLY in a sibling
# `{decl}_index/` folder — NOT next to the data `.vcf.gz`. The downstream
# WDL's `vcf + ".tbi"` localizer then fails with INPUT_URI_NOT_FOUND. The
# native e2e run hit this on every inter-step hand-off (C.3->C.4, C.4->C.5,
# C.5->D.1, D.1->D.2) and required a manual `aws s3 cp` of the index next to
# the data VCF. `_resolve_output_and_colocate_index` automates that copy so
# the native Phase C/D chain runs hands-free.
def _split_s3(uri: str) -> tuple[str, str]:
    """Split an s3://bucket/key URI into (bucket, key)."""
    assert uri.startswith("s3://"), f"not an s3 uri: {uri}"
    without_scheme = uri[len("s3://"):]
    bucket, _, key = without_scheme.partition("/")
    return bucket, key


def _resolve_output_and_colocate_index(
    s3,
    run_output_uri: str,
    decl_folder: str,
    *,
    suffix: str = ".vcf.gz",
    index_suffix: str = ".tbi",
) -> str:
    """Resolve a run's primary output file and guarantee its index is co-located.

    ``run_output_uri`` is the run-scoped base (``get_run``'s ``runOutputUri``,
    i.e. ``{outputUri}{run_id}``). The data file is found under
    ``{run_output_uri}/out/{decl_folder}/`` by matching ``suffix``; if its
    ``index_suffix`` companion is absent there, this looks in the sibling
    ``{decl_folder}_index/`` folder and copies the index next to the data file.

    Returns the resolved data-file S3 URI (the value to feed into the next
    module's input parameter). For index-less outputs (e.g. a ``.tsv`` ploidy
    table), pass ``index_suffix=""`` to skip the co-location step.
    """
    base = run_output_uri.rstrip("/")
    bucket, _ = _split_s3(base + "/")
    prefix = f"{base[len('s3://') + len(bucket) + 1:]}/out/{decl_folder}/"

    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    contents = resp.get("Contents", [])
    keys = [obj["Key"] for obj in contents]
    data_keys = [
        k for k in keys
        if k.endswith(suffix) and not k.endswith(index_suffix)
    ] if index_suffix else [k for k in keys if k.endswith(suffix)]
    if not data_keys:
        raise RuntimeError(
            f"no '{suffix}' output under s3://{bucket}/{prefix} "
            f"(found {len(keys)} object(s): {keys[:5]})"
        )
    # Prefer the shortest match (the primary data file, not an index).
    data_key = sorted(data_keys, key=len)[0]
    data_uri = f"s3://{bucket}/{data_key}"

    if not index_suffix:
        return data_uri

    index_key = data_key + index_suffix
    if index_key in keys:
        return data_uri  # index already co-located

    # Index missing next to the data file: look in the sibling _index folder.
    sib_prefix = f"{base[len('s3://') + len(bucket) + 1:]}/out/{decl_folder}_index/"
    sib = s3.list_objects_v2(Bucket=bucket, Prefix=sib_prefix)
    sib_index = [
        obj["Key"] for obj in sib.get("Contents", [])
        if obj["Key"].endswith(index_suffix)
    ]
    if not sib_index:
        raise RuntimeError(
            f"index {data_uri}{index_suffix} not found next to the data VCF "
            f"and no '{index_suffix}' object under sibling "
            f"s3://{bucket}/{sib_prefix}"
        )
    src_index_key = sib_index[0]
    s3.copy_object(
        Bucket=bucket,
        Key=index_key,
        CopySource={"Bucket": bucket, "Key": src_index_key},
    )
    print(
        f"    co-located index: s3://{bucket}/{src_index_key} "
        f"-> s3://{bucket}/{index_key}"
    )
    return data_uri


# Output-declaration folder name for each Phase C/D module's primary VCF.
# These are the WDL output decl names HealthOmics uses as the `out/<name>/`
# sub-folder (observed in the native e2e run, 2026-06-08).
_PRIMARY_VCF_DECL = {
    "refine_complex_variants": "cpx_refined_vcf",
    "join_raw_calls": "joined_raw_calls_vcf",
    "sv_concordance": "concordance_vcf",
    "score_genotypes": "unfiltered_recalibrated_vcf",
    "filter_genotypes": "filtered_vcf",
    "annotate_vcf": "annotated_vcf",
}


def _phase_c_overrides(
    module_key: str,
    cohort_id: str,
    output_base: str,
    resolved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the HealthOmics `parameters` dict for a Phase C post-processing
    module, wiring each step's input to the prior step's S3 output.

    These modules have no TEMPLATE_RUNS entry (they're the Phase 8 GQ chain),
    so the orchestrator must fully specify their inputs. Mirrors the input
    builders in scripts/run_gq_chain_ec2.py — the difference is execution
    engine (native HealthOmics + WDL_LENIENT) vs the former EC2/miniwdl hybrid.

    ``resolved`` carries the *actual* run-scoped S3 URIs of upstream Phase C
    outputs (produced by `_resolve_output_and_colocate_index` after each step
    completes), keyed by the producing module (plus a `join_raw_calls_ploidy`
    entry for the ploidy table). When a key is present it is used in place of
    the static path guess, so the chain follows HealthOmics' nested
    `{run_id}/out/{decl}/` layout rather than a hand-built path.
    """
    resolved = resolved or {}
    d = _gq_dockers()
    batch = f"{output_base}/batch"
    ref = REF_BASE
    ped = f"{ref}/cohort-{cohort_id}.ped"

    if module_key == "refine_complex_variants":
        # Consumes the MakeCohortVcf cleaned cohort VCF (EC2-hybrid output)
        # plus per-batch PE metrics / depth beds from GatherBatchEvidence and
        # the batch sample list. Single-batch cohort -> 1-element arrays.
        gbe = f"{batch}/gather-batch-evidence"
        return {
            "RefineComplexVariants.vcf": f"{batch}/make-cohort-vcf-ec2/cleaned/{cohort_id}.cleaned.vcf.gz",
            "RefineComplexVariants.prefix": f"{cohort_id}.refine_complex",
            "RefineComplexVariants.batch_name_list": [cohort_id],
            "RefineComplexVariants.batch_sample_lists": [
                f"{batch}/refine-complex-variants/inputs/{cohort_id}.samples.list"
            ],
            "RefineComplexVariants.PE_metrics": [f"{gbe}/merged_PE/{cohort_id}.pe.txt.gz"],
            "RefineComplexVariants.PE_metrics_indexes": [f"{gbe}/merged_PE/{cohort_id}.pe.txt.gz.tbi"],
            "RefineComplexVariants.Depth_DEL_beds": [f"{gbe}/merged_dels/{cohort_id}.DEL.bed.gz"],
            "RefineComplexVariants.Depth_DUP_beds": [f"{gbe}/merged_dups/{cohort_id}.DUP.bed.gz"],
            "RefineComplexVariants.n_per_split": 5000,
            "RefineComplexVariants.linux_docker": d["linux"],
            "RefineComplexVariants.sv_base_mini_docker": d["svbm"],
            "RefineComplexVariants.sv_pipeline_docker": d["svp"],
        }

    if module_key == "join_raw_calls":
        cb = f"{batch}/cluster_batch/out"
        return {
            "JoinRawCalls.prefix": f"{cohort_id}.join_raw_calls",
            "JoinRawCalls.ped_file": ped,
            "JoinRawCalls.contig_list": f"{ref}/gs_primary_contigs.list",
            "JoinRawCalls.reference_fasta": f"{ref}/Homo_sapiens_assembly38.fasta",
            "JoinRawCalls.reference_fasta_fai": f"{ref}/Homo_sapiens_assembly38.fasta.fai",
            "JoinRawCalls.reference_dict": f"{ref}/Homo_sapiens_assembly38.dict",
            "JoinRawCalls.clustered_depth_vcfs": [f"{cb}/clustered_depth_vcf/{cohort_id}.cluster_batch.depth.vcf.gz"],
            "JoinRawCalls.clustered_depth_vcf_indexes": [f"{cb}/clustered_depth_vcf/{cohort_id}.cluster_batch.depth.vcf.gz.tbi"],
            "JoinRawCalls.clustered_manta_vcfs": [f"{cb}/clustered_manta_vcf/{cohort_id}.cluster_batch.manta.vcf.gz"],
            "JoinRawCalls.clustered_manta_vcf_indexes": [f"{cb}/clustered_manta_vcf/{cohort_id}.cluster_batch.manta.vcf.gz.tbi"],
            "JoinRawCalls.clustered_wham_vcfs": [f"{cb}/clustered_wham_vcf/{cohort_id}.cluster_batch.wham.vcf.gz"],
            "JoinRawCalls.clustered_wham_vcf_indexes": [f"{cb}/clustered_wham_vcf/{cohort_id}.cluster_batch.wham.vcf.gz.tbi"],
            "JoinRawCalls.clustered_scramble_vcfs": [f"{cb}/clustered_scramble_vcf/{cohort_id}.cluster_batch.scramble.vcf.gz"],
            "JoinRawCalls.clustered_scramble_vcf_indexes": [f"{cb}/clustered_scramble_vcf/{cohort_id}.cluster_batch.scramble.vcf.gz.tbi"],
            "JoinRawCalls.gatk_docker": d["gatk"],
            "JoinRawCalls.sv_base_mini_docker": d["svbm"],
            "JoinRawCalls.sv_pipeline_docker": d["svp"],
        }

    if module_key == "sv_concordance":
        return {
            "SVConcordance.eval_vcf": resolved.get(
                "refine_complex_variants",
                f"{batch}/refine_complex_variants/{cohort_id}.refine_complex.cpx_refined.vcf.gz",
            ),
            "SVConcordance.truth_vcf": resolved.get(
                "join_raw_calls",
                f"{batch}/join_raw_calls/{cohort_id}.join_raw_calls.vcf.gz",
            ),
            "SVConcordance.output_prefix": f"{cohort_id}.sv_concordance",
            "SVConcordance.contig_list": f"{ref}/gs_primary_contigs.list",
            "SVConcordance.reference_dict": f"{ref}/Homo_sapiens_assembly38.dict",
            "SVConcordance.gatk_docker": d["gatk"],
            "SVConcordance.sv_base_mini_docker": d["svbm"],
        }

    if module_key == "score_genotypes":
        gt = f"{ref}/ucsc-genome-tracks"
        return {
            "ScoreGenotypes.vcf": resolved.get(
                "sv_concordance",
                f"{batch}/sv_concordance/{cohort_id}.sv_concordance.vcf.gz",
            ),
            "ScoreGenotypes.output_prefix": f"{cohort_id}.score_genotypes",
            "ScoreGenotypes.gq_recalibrator_model_file": f"{ref}/gatk-sv-recalibrator.aou_phase_1.v1.model",
            # AoU model estimates AF from genotypes only for >=100-sample cohorts;
            # lower the threshold so small/validation cohorts (which lack an AF
            # INFO field) can be scored. Harmless for large cohorts.
            "ScoreGenotypes.recalibrate_gq_args": ["--min-samples-to-estimate-allele-frequency", "1"],
            "ScoreGenotypes.genome_tracks": [
                f"{gt}/hg38-RepeatMasker.bed.gz",
                f"{gt}/hg38-Segmental-Dups.bed.gz",
                f"{gt}/hg38-Simple-Repeats.bed.gz",
                f"{gt}/hg38_umap_s100.bed.gz",
                f"{gt}/hg38_umap_s24.bed.gz",
            ],
            "ScoreGenotypes.linux_docker": d["linux"],
            "ScoreGenotypes.gatk_docker": d["gatk"],
            "ScoreGenotypes.sv_base_mini_docker": d["svbm"],
            "ScoreGenotypes.sv_pipeline_docker": d["svp"],
        }

    if module_key == "filter_genotypes":
        return {
            "FilterGenotypes.vcf": resolved.get(
                "score_genotypes",
                f"{batch}/score_genotypes/{cohort_id}.score_genotypes.gq_recalibrated.vcf.gz",
            ),
            "FilterGenotypes.output_prefix": f"{cohort_id}.filter_genotypes",
            "FilterGenotypes.ploidy_table": resolved.get(
                "join_raw_calls_ploidy",
                f"{batch}/join_raw_calls/{cohort_id}.join_raw_calls.ploidy.tsv",
            ),
            "FilterGenotypes.sl_cutoff_table": f"{ref}/aou_sl_cutoff_table.tsv",
            "FilterGenotypes.primary_contigs_fai": f"{ref}/gs_primary_contigs.fai",
            "FilterGenotypes.ped_file": ped,
            "FilterGenotypes.run_qc": False,
            "FilterGenotypes.sv_base_mini_docker": d["svbm"],
            "FilterGenotypes.sv_pipeline_docker": d["svp"],
        }

    raise KeyError(f"no Phase C override builder for module {module_key!r}")


def phase_c_post_processing(
    args: argparse.Namespace, output_base: str
) -> tuple[list[StageRecord], dict[str, str]]:
    """Run the post-processing chain on the cohort VCF natively on HealthOmics:

      C.1 RefineComplexVariants  (refines complex SV calls)
      C.2 JoinRawCalls           (start of GQ recalibrator chain)
      C.3 SVConcordance          (annotates concordance with raw calls)
      C.4 ScoreGenotypes         (GQ recalibrator scoring)
      C.5 FilterGenotypes        (drops low-confidence calls)

    Each step's output feeds the next; failure aborts the chain.

    These five run natively on HealthOmics using WDL_LENIENT-engine workflows
    (validated 2026-06-08: runs 1591648 / 3918249 / 2619129 / 8071264 and the
    SVConcordance/MainVcfQC retests). Lenient mode clears the ~47s multi-task
    kill for these scatter/aggregate modules. Only MakeCohortVcf.CombineBatches
    still requires the EC2 hybrid (see phase_c_makecohortvcf_hybrid).
    """
    print("=" * 78)
    print(f"PHASE C.1-C.5: post-processing chain  ({args.cohort_id})")
    print("=" * 78)

    omics = boto3.client("omics", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    sequence = [
        ("C.1 RefineComplexVariants", "refine_complex_variants"),
        ("C.2 JoinRawCalls",          "join_raw_calls"),
        ("C.3 SVConcordance",         "sv_concordance"),
        ("C.4 ScoreGenotypes",        "score_genotypes"),
        ("C.5 FilterGenotypes",       "filter_genotypes"),
    ]
    records: list[StageRecord] = []
    # resolved[module_key] -> the actual run-scoped primary-VCF S3 URI that
    # the module produced (with its index co-located). Threaded forward so
    # each step consumes its predecessor's real nested output path rather
    # than a static guess. `join_raw_calls_ploidy` carries the ploidy table.
    resolved: dict[str, str] = {}
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
            parameter_overrides=_phase_c_overrides(
                module_key, args.cohort_id, output_base, resolved=resolved
            ),
        )
        print(f"  Started run {rec.id} ({rec.name})")
        info = poll_healthomics_run(omics, rec.id, label=module_key)
        rec.finished_at = _now_iso()
        rec.status = info.get("status")
        rec.duration_sec = _wall_clock(info)
        run_output_uri = info.get("runOutputUri", "")
        rec.extra = {"output_uri": info.get("outputUri", ""),
                     "run_output_uri": run_output_uri}
        records.append(rec)
        if rec.status != "COMPLETED":
            raise RuntimeError(f"{label} run {rec.id} ended in status {rec.status}")
        # Resolve the real nested output path and co-locate the .tbi so the
        # next step's `vcf + ".tbi"` localizer finds it (avoids the manual
        # `aws s3 cp` the native e2e run originally needed).
        decl = _PRIMARY_VCF_DECL.get(module_key)
        if decl and run_output_uri:
            try:
                data_uri = _resolve_output_and_colocate_index(
                    s3, run_output_uri, decl
                )
                resolved[module_key] = data_uri
                print(f"  resolved {module_key} output: {data_uri}")
                if module_key == "join_raw_calls":
                    # JoinRawCalls also emits the ploidy table FilterGenotypes
                    # needs (no index). Resolve it from its own decl folder.
                    resolved["join_raw_calls_ploidy"] = (
                        _resolve_output_and_colocate_index(
                            s3, run_output_uri, "ploidy_table",
                            suffix=".tsv", index_suffix="",
                        )
                    )
                    print(f"  resolved ploidy table: "
                          f"{resolved['join_raw_calls_ploidy']}")
            except RuntimeError as exc:
                # Don't hard-fail the chain on a resolver miss; fall back to
                # the static path guess (next step's override has a default).
                print(f"  WARNING: output resolve/co-locate failed for "
                      f"{module_key}: {exc}")
    return records, resolved


# --------------------------------------------------------------------------- #
# Phase D.2 / D.3 : MainVcfQC + (optional) VisualizeCnvs (Req 19)
# --------------------------------------------------------------------------- #
def phase_d2_main_vcf_qc(
    args: argparse.Namespace,
    output_base: str,
    resolved: dict[str, str] | None = None,
) -> StageRecord:
    """Run MainVcfQC on the post-AnnotateVcf cohort VCF (cohort-level QC plots)."""
    print("=" * 78)
    print(f"PHASE D.2: MainVcfQC  ({args.cohort_id})")
    print("=" * 78)

    skipped = _maybe_skip_phase("MainVcfQC", "main_vcf_qc")
    if skipped is not None:
        return skipped

    resolved = resolved or {}
    d = _gq_dockers()
    annotated_vcf = resolved.get(
        "annotate_vcf",
        f"{output_base}/batch/annotate_vcf/{args.cohort_id}.annotated.vcf.gz",
    )
    overrides = {
        "MainVcfQc.vcfs": [annotated_vcf],
        "MainVcfQc.prefix": f"{args.cohort_id}.main_vcf_qc",
        "MainVcfQc.primary_contigs_fai": f"{REF_BASE}/gs_primary_contigs.fai",
        "MainVcfQc.ped_file": f"{REF_BASE}/cohort-{args.cohort_id}.ped",
        "MainVcfQc.sv_per_shard": 2500,
        "MainVcfQc.samples_per_shard": 600,
        "MainVcfQc.do_per_sample_qc": True,
        "MainVcfQc.sv_base_mini_docker": d["svbm"],
        "MainVcfQc.sv_pipeline_docker": d["svp"],
        "MainVcfQc.sv_pipeline_qc_docker": d["svp"],
    }

    omics = boto3.client("omics", region_name=REGION)
    rec = _start_cohort_module(
        omics,
        module_key="main_vcf_qc",
        cohort_id=args.cohort_id,
        sample_count=args.sample_count,
        output_base=output_base,
        parameter_overrides=overrides,
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
        # Two-step substitution: the template's S3 URIs embed either the long
        # rerun string ("gatk-sv-validation-2026q2-rerun-2026-05-25") OR the
        # short cohort string ("gatk-sv-validation-2026q2"). Substitute the
        # longer string first so both forms collapse to the new cohort_id;
        # otherwise URIs like `.../gatk-sv-validation-2026q2-rerun-2026-05-25/...`
        # would only have their short prefix replaced and the resulting path
        # `<cohort_id>-rerun-2026-05-25/...` would not exist.
        params = _swap_uris(
            params, "gatk-sv-validation-2026q2-rerun-2026-05-25", cohort_id
        )
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
    # Activate RegenotypeCNVs only on cohorts >= 100 samples (Req 19.6).
    # On smaller cohorts the module finds no eligible variants
    # (regeno_max_allele_freq=0.01) and the WDL doesn't handle empty output.
    records: list[StageRecord] = []
    if sample_count >= 100:
        sequence.append("regenotype_cnvs")
    else:
        skip_msg = (
            f"sample_count={sample_count} < 100; RegenotypeCNVs is only "
            "activated for cohorts of 100+ samples per Req 19.6 "
            "(regeno_max_allele_freq=0.01 yields no eligible variants on "
            "small cohorts and the WDL does not handle empty output)."
        )
        print(f"  [SKIP] RegenotypeCNVs — {skip_msg}")
        # Record the skip in the run report so the cost-report JSON
        # carries an auditable rationale for cohorts below the threshold.
        now = _now_iso()
        records.append(StageRecord(
            stage="regenotype_cnvs",
            kind="healthomics",
            id="(skipped: cohort < 100 samples)",
            name="RegenotypeCNVs",
            started_at=now,
            finished_at=now,
            status="SKIPPED",
            duration_sec=0.0,
            extra={
                "skip_reason": skip_msg,
                "requirement": "Req 19.6",
                "sample_count": sample_count,
                "threshold": 100,
            },
        ))
    for module_key in sequence:
        print(f"\n--- {module_key} ---")
        skipped = _maybe_skip_phase(module_key, module_key)
        if skipped is not None:
            records.append(skipped)
            continue
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
def phase_d_annotate_vcf(
    args: argparse.Namespace,
    output_base: str,
    resolved: dict[str, str] | None = None,
) -> tuple[StageRecord, dict[str, str]]:
    print("=" * 78)
    print(f"PHASE D: AnnotateVcf  ({args.cohort_id})")
    print("=" * 78)

    resolved = dict(resolved or {})
    omics = boto3.client("omics", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)

    # AnnotateVcf consumes the FilterGenotypes output. When Phase C ran in this
    # same orchestration, `resolved["filter_genotypes"]` holds the real nested
    # path (index already co-located); otherwise fall back to the static guess.
    overrides: dict[str, Any] = {}
    fg_vcf = resolved.get("filter_genotypes")
    if fg_vcf:
        overrides["AnnotateVcf.vcf"] = fg_vcf

    rec = _start_cohort_module(
        omics,
        module_key="annotate_vcf",
        cohort_id=args.cohort_id,
        sample_count=args.sample_count,
        output_base=output_base,
        parameter_overrides=overrides or None,
    )
    print(f"  Started run {rec.id}")
    info = poll_healthomics_run(omics, rec.id, label="annotate_vcf")
    rec.finished_at = _now_iso()
    rec.status = info.get("status")
    rec.duration_sec = _wall_clock(info)
    run_output_uri = info.get("runOutputUri", "")
    rec.extra = {"output_uri": info.get("outputUri", ""),
                 "run_output_uri": run_output_uri}
    if rec.status != "COMPLETED":
        raise RuntimeError(f"AnnotateVcf run {rec.id} ended in status {rec.status}")
    # Resolve the annotated VCF + co-locate its index for MainVcfQC.
    if run_output_uri:
        try:
            resolved["annotate_vcf"] = _resolve_output_and_colocate_index(
                s3, run_output_uri, _PRIMARY_VCF_DECL["annotate_vcf"]
            )
            print(f"  resolved annotate_vcf output: {resolved['annotate_vcf']}")
        except RuntimeError as exc:
            print(f"  WARNING: output resolve/co-locate failed for "
                  f"annotate_vcf: {exc}")
    return rec, resolved


# --------------------------------------------------------------------------- #
# Phase E : cost report (best-effort)
# --------------------------------------------------------------------------- #
def phase_e_cost_report(
    args: argparse.Namespace, output_base: str, records: list[StageRecord]
) -> Path:
    print("=" * 78)
    print(f"PHASE E: cost report  ({args.cohort_id})")
    print("=" * 78)

    # Compute precise per-stage cost using boto3 metering data (no Cost
    # Explorer wait — see scripts/precise_cost.py for methodology).
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from precise_cost import cost_for_run as _precise_cost_for_run
    except ImportError:
        _precise_cost_for_run = None

    omics = boto3.client("omics", region_name=REGION)
    annotated_stages: list[dict[str, Any]] = []
    cohort_compute = 0.0
    cohort_storage = 0.0
    for r in records:
        d = dict(r.__dict__)
        if r.kind == "healthomics" and r.id and r.id != "(not yet registered)" and _precise_cost_for_run:
            try:
                pc = _precise_cost_for_run(omics, r.id)
                if pc.get("status") == "COMPLETED":
                    d["compute_cost_usd"] = pc.get("compute_cost_usd")
                    d["storage_cost_usd"] = pc.get("storage_cost_usd")
                    d["total_cost_usd"] = pc.get("total_cost_usd")
                    d["wall_clock_min"] = pc.get("wall_clock_min")
                    d["by_instance"] = pc.get("by_instance")
                    cohort_compute += pc.get("compute_cost_usd", 0.0)
                    cohort_storage += pc.get("storage_cost_usd", 0.0)
            except Exception as e:  # pragma: no cover - defensive
                d["cost_error"] = str(e)
        annotated_stages.append(d)

    # Print the per-stage time + cost table.
    print()
    print(f"{'stage':<35s} {'kind':<11s} {'status':<11s} {'duration':>10s} {'cost (USD)':>12s}")
    print("-" * 80)
    for s in annotated_stages:
        dur_sec = s.get("duration_sec") or 0.0
        dur_min = dur_sec / 60.0 if dur_sec else 0.0
        cost = s.get("total_cost_usd", "")
        cost_str = f"${cost:.2f}" if isinstance(cost, (int, float)) else "(n/a)"
        print(f"{s['stage']:<35s} {s['kind']:<11s} {str(s['status']):<11s} "
              f"{dur_min:>8.1f} m {cost_str:>12s}")
    print("-" * 80)
    cohort_total = cohort_compute + cohort_storage
    n_samples = args.sample_count or 1
    per_sample = cohort_total / n_samples if n_samples else 0.0
    print(f"{'TOTAL HealthOmics':<35s} {'':<11s} {'':<11s} "
          f"{'':>10s} {f'${cohort_total:.2f}':>12s}")
    print(f"  per sample (n={n_samples}): ${per_sample:.2f}")
    print(f"  compute: ${cohort_compute:.2f}, storage: ${cohort_storage:.2f}")
    print(
        "  (EC2 hybrid stages — scramble, MakeCohortVcf — are not in this "
        "total; query Cost Explorer with the cohort tag for the full picture "
        "after ~24h.)"
    )

    report = {
        "cohort_id": args.cohort_id,
        "sample_count": args.sample_count,
        "region": REGION,
        "generated_at": _now_iso(),
        "phase_summary": [
            _summarize_phase(records, p) for p in ("A", "B", "C", "D")
        ],
        "stages": annotated_stages,
        "totals": {
            "healthomics_compute_cost_usd": round(cohort_compute, 4),
            "healthomics_storage_cost_usd": round(cohort_storage, 4),
            "healthomics_total_cost_usd": round(cohort_total, 4),
            "per_sample_cost_usd": round(per_sample, 4),
        },
        "note": (
            "HealthOmics costs computed from boto3 metering data via "
            "scripts/precise_cost.py: per-task instanceType x duration x "
            "ap-se-1 published on-demand rate, plus storage GiB x wall-clock. "
            "Accuracy ~95-99% of Cost Explorer (intra-region data-transfer "
            "is $0, not included). EC2 hybrid stages are not included; "
            "query Cost Explorer with tag gatk-sv:cohort-id=" + args.cohort_id +
            " after ~24h for the full picture."
        ),
    }
    out = ROOT / f"cost-report-{args.cohort_id}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n  wrote {out.name}")
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. Factored out so unit tests can introspect
    the flag set without invoking main()."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--cohort-id", required=True, help="Stable id used for cost tagging")
    ap.add_argument("--manifest", default=str(ROOT / "validation-cohort" / "inputs" / "manifest.json"))
    ap.add_argument("--samples", default=None, help="Override sample list (comma-separated)")
    ap.add_argument("--modules", default=None, help="GSE sub-tools (default all 5)")
    ap.add_argument(
        "--cohort-base",
        default=None,
        help="S3 base URI for the staged CRAM/CRAI cohort "
             "(e.g. s3://omics-cohorts-ap-southeast-1-<account>/cohorts/gatk-sv-156). "
             "When omitted, derived from the manifest's `destination_base` field.",
    )

    # Module_Phase boundary skip flags (Req 19.1–19.6, Req 14.1–14.2).
    # Each flag bypasses an entire upstream Module_Phase boundary AND
    # acknowledges that the prerequisite check for the next phase should
    # not block on its records.
    phase_group = ap.add_argument_group(
        "Module_Phase boundaries (Req 19.1–19.6)",
        "Skip an entire phase. Phase A blocks Phase B blocks Phase C "
        "blocks Phase D unless the corresponding --skip-phase-* flag is set.",
    )
    phase_group.add_argument("--skip-phase-a", action="store_true",
                             help="Skip Phase A (per-sample: GSE, scramble-EC2, EvidenceQC).")
    phase_group.add_argument("--skip-phase-b", action="store_true",
                             help="Skip Phase B (cohort modules: GBE → GenotypeBatch).")
    phase_group.add_argument("--skip-phase-c", action="store_true",
                             help="Skip Phase C (post-processing: MakeCohortVcf hybrid + "
                                  "RefineComplexVariants + GQ_Recalibrator chain).")
    phase_group.add_argument("--skip-phase-d", action="store_true",
                             help="Skip Phase D (delivery: AnnotateVcf, MainVcfQC, VisualizeCnvs).")

    # Fine-grained sub-phase flags (kept for backward compatibility and for
    # operators who need to skip a single sub-step inside a phase).
    sub_group = ap.add_argument_group(
        "Fine-grained sub-phase skips (legacy)",
        "These flags skip an individual sub-step within a phase. "
        "Prefer --skip-phase-{a,b,c,d} for whole-phase skips.",
    )
    sub_group.add_argument("--skip-gse", action="store_true",
                           help="Skip Phase A.1 (GSE outputs already exist).")
    sub_group.add_argument("--skip-scramble-ec2", action="store_true",
                           help="Skip Phase A.5 (scramble on EC2). Use only if scramble outputs "
                                "already exist for every sample under <output_base>/<sample>/scramble-real-ec2/.")
    sub_group.add_argument("--skip-evidence-qc", action="store_true",
                           help="Skip Phase A.6 (EvidenceQC).")
    sub_group.add_argument("--skip-cohort", action="store_true",
                           help="Skip Phase B (cohort modules). [Equivalent to --skip-phase-b.]")
    sub_group.add_argument("--skip-makecohortvcf", action="store_true",
                           help="Skip Phase C MakeCohortVcf hybrid (EC2).")
    sub_group.add_argument("--skip-post-processing", action="store_true",
                           help="Skip Phase C.1-C.5 (RefineComplexVariants + GQ_Recalibrator chain).")
    sub_group.add_argument("--skip-annotate", action="store_true",
                           help="Skip Phase D.1 (AnnotateVcf).")
    sub_group.add_argument("--skip-main-vcf-qc", action="store_true",
                           help="Skip Phase D.2 (MainVcfQC cohort-level QC plots).")
    sub_group.add_argument("--include-visualize-cnvs", action="store_true",
                           help="Run Phase D.3 (VisualizeCnvs per-CNV PNGs). Default off.")
    return ap


def main() -> int:
    ap = _build_parser()
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

    # Resolved upstream Phase C output URIs (real nested HealthOmics paths with
    # indexes co-located), threaded from Phase C into Phase D. Initialized here
    # so it exists even when Phase C is skipped.
    phase_c_resolved: dict[str, str] = {}

    # phase_outcomes tracks the rolled-up status for each Module_Phase
    # boundary so subsequent phases can enforce the A→B→C→D prereq chain.
    phase_outcomes: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Phase A : per-sample (GSE fan-out, scramble-EC2, EvidenceQC)
    # ------------------------------------------------------------------ #
    if args.skip_phase_a:
        print("[SKIP] Phase A (per-sample) — --skip-phase-a")
        phase_outcomes["A"] = {"status": "SKIPPED",
                               "reason": "--skip-phase-a"}
    else:
        phase_a_records: list[StageRecord] = []
        gse_run_results: dict[str, Any] | None = None
        try:
            if not args.skip_gse:
                gse = phase_a_gse_fanout(args)
                gse_run_results = gse
                for run in gse["manifest"]["runs"]:
                    info = gse["run_results"].get(run["id"], {})
                    phase_a_records.append(StageRecord(
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
                phase_a_records.extend(
                    phase_a5_scramble_ec2(
                        args,
                        output_base,
                        gse_run_results=gse_run_results,
                        cohort_base=args.cohort_base,
                    )
                )
            if not args.skip_evidence_qc:
                phase_a_records.append(phase_a6_evidence_qc(args, output_base))
        finally:
            all_records.extend(phase_a_records)
            phase_outcomes["A"] = _summarize_phase(phase_a_records, "A")

    # ------------------------------------------------------------------ #
    # Phase B : cohort modules
    # ------------------------------------------------------------------ #
    if args.skip_phase_b or args.skip_cohort:
        reason = "--skip-phase-b" if args.skip_phase_b else "--skip-cohort"
        print(f"[SKIP] Phase B (cohort) — {reason}")
        phase_outcomes["B"] = {"status": "SKIPPED", "reason": reason}
    else:
        _check_phase_prereq(phase_outcomes, current="B", required=["A"])
        phase_b_records: list[StageRecord] = []
        try:
            phase_b_records.extend(phase_b_cohort_modules(args, output_base))
        finally:
            all_records.extend(phase_b_records)
            phase_outcomes["B"] = _summarize_phase(phase_b_records, "B")

    # ------------------------------------------------------------------ #
    # Phase C : post-processing (MakeCohortVcf hybrid + Refine + GQ chain)
    # ------------------------------------------------------------------ #
    if args.skip_phase_c:
        print("[SKIP] Phase C (post-processing) — --skip-phase-c")
        phase_outcomes["C"] = {"status": "SKIPPED",
                               "reason": "--skip-phase-c"}
    else:
        _check_phase_prereq(phase_outcomes, current="C", required=["B"])
        phase_c_records: list[StageRecord] = []
        try:
            if not args.skip_makecohortvcf:
                phase_c_records.extend(phase_c_makecohortvcf_hybrid(args, output_base))
            if not args.skip_post_processing:
                pp_records, phase_c_resolved = phase_c_post_processing(args, output_base)
                phase_c_records.extend(pp_records)
        finally:
            all_records.extend(phase_c_records)
            phase_outcomes["C"] = _summarize_phase(phase_c_records, "C")

    # ------------------------------------------------------------------ #
    # Phase D : delivery (AnnotateVcf, MainVcfQC, optional VisualizeCnvs)
    # ------------------------------------------------------------------ #
    if args.skip_phase_d:
        print("[SKIP] Phase D (delivery) — --skip-phase-d")
        phase_outcomes["D"] = {"status": "SKIPPED",
                               "reason": "--skip-phase-d"}
    else:
        _check_phase_prereq(phase_outcomes, current="D", required=["C"])
        phase_d_records: list[StageRecord] = []
        phase_d_resolved: dict[str, str] = dict(phase_c_resolved)
        try:
            if not args.skip_annotate:
                anno_rec, phase_d_resolved = phase_d_annotate_vcf(
                    args, output_base, resolved=phase_d_resolved
                )
                phase_d_records.append(anno_rec)
            if not args.skip_main_vcf_qc:
                phase_d_records.append(
                    phase_d2_main_vcf_qc(args, output_base, resolved=phase_d_resolved)
                )
            if args.include_visualize_cnvs:
                phase_d_records.append(phase_d3_visualize_cnvs(args, output_base))
        finally:
            all_records.extend(phase_d_records)
            phase_outcomes["D"] = _summarize_phase(phase_d_records, "D")

    print()
    print(f"=== Pipeline elapsed: {int(time.time() - pipeline_started)}s ===")
    print("=== Module_Phase summary ===")
    for p in ("A", "B", "C", "D"):
        s = phase_outcomes.get(p, {"status": "NOT_STARTED"})
        print(f"  Phase {p}: {s.get('status'):<11s} "
              f"records={s.get('records_count', 0)}")
    phase_e_cost_report(args, output_base, all_records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
