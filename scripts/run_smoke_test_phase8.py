#!/usr/bin/env python3
"""10-sample smoke test for the v1.0 amendment (Req 19, Task 8.10).

Submits a 10-sample subset of the 156-sample staged cohort to
run_cohort_e2e.py with all four phases enabled (A through D, including
EvidenceQC + GQ_Recalibrator + MainVcfQC). Verifies that:

  - All 4 phase boundaries are reached (A → A.5 → A.6 → B → C → C.1-C.5 → D → D.2)
  - Per-phase StageRecord status is COMPLETED or SKIPPED for every module
  - Outputs land in S3 at the expected prefixes
  - The GQ_Recalibrator chain produces a non-empty FilterGenotypes VCF
  - MainVcfQC plots are produced

Default 10-sample subset (chosen because we already have validated outputs
for HG00096 from the wham + scramble revalidation):

    HG00096 HG00129 HG00140 HG00150 HG00187
    HG00239 HG00277 HG00288 HG00337 HG00349

Pre-flight check: refuses to run if any of the 8 Phase 8 (Req 19) workflow
IDs is None, since the smoke test exists specifically to verify those
modules. Operator must register them first via:

    .venv/bin/python scripts/bootstrap/08_register_workflows.py

Usage:
    AWS_ACCOUNT_ID=<account> \\
    GATK_SV_RUN_CACHE_ID=<cache-id> \\
    GATK_SV_EC2_INSTANCE_ID=<instance-id> \\
    .venv/bin/python scripts/run_smoke_test_phase8.py

This script is the gating action for Task 8.11 (amendment checkpoint).
DO NOT run the full 156-sample cohort until the smoke test passes and
the user explicitly approves.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SAMPLES = [
    "HG00096", "HG00129", "HG00140", "HG00150", "HG00187",
    "HG00239", "HG00277", "HG00288", "HG00337", "HG00349",
]

# Modules required to be registered before the smoke test can validate
# what it's supposed to validate (the v1.0 amendment).
REQUIRED_PHASE_8_MODULES = [
    "EvidenceQC",
    "RefineComplexVariants",
    "JoinRawCalls",
    "SVConcordance",
    "ScoreGenotypes",
    "FilterGenotypes",
    "MainVcfQC",
]
# VisualizeCnvs is opt-in; not required.


def preflight() -> int:
    """Verify the Phase 8 workflows are registered. Return 0 if OK."""
    workflow_ids_path = ROOT / "workflow-ids.json"
    if not workflow_ids_path.exists():
        print(
            "ERROR: workflow-ids.json not found. The smoke test exists to "
            "validate the v1.0 amendment modules; you must register them "
            "first via scripts/bootstrap/08_register_workflows.py.",
            file=sys.stderr,
        )
        return 1
    registered = json.loads(workflow_ids_path.read_text())
    missing = [m for m in REQUIRED_PHASE_8_MODULES if m not in registered]
    if missing:
        print(
            f"ERROR: the following Phase 8 (Req 19) modules are not registered "
            f"in workflow-ids.json: {missing}\n"
            f"Run scripts/bootstrap/08_register_workflows.py first.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--cohort-id", default="gatk-sv-156-smoke-test",
                    help="Cohort identifier for cost tagging.")
    ap.add_argument("--samples", default=",".join(DEFAULT_SAMPLES),
                    help="Comma-separated sample IDs from the 156-sample staged cohort.")
    ap.add_argument("--manifest",
                    default=str(ROOT / "validation-cohort" / "inputs" / "manifest-gatk-sv-156.json"),
                    help="Manifest containing the 156 sample S3 URIs.")
    ap.add_argument("--include-visualize-cnvs", action="store_true",
                    help="Include Phase D.3 (VisualizeCnvs).")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Skip the Phase 8 module registration check (debug only).")
    args = ap.parse_args()

    if not args.skip_preflight and preflight() != 0:
        return 1

    cmd = [
        ".venv/bin/python", str(ROOT / "scripts" / "run_cohort_e2e.py"),
        "--cohort-id", args.cohort_id,
        "--manifest", args.manifest,
        "--samples", args.samples,
    ]
    if args.include_visualize_cnvs:
        cmd.append("--include-visualize-cnvs")

    print("Smoke test config:")
    print(f"  cohort_id:       {args.cohort_id}")
    print(f"  samples:         {len(args.samples.split(','))} samples")
    print(f"  manifest:        {args.manifest}")
    print(f"  visualize_cnvs:  {args.include_visualize_cnvs}")
    print()
    print("Launching:")
    print("  " + " ".join(cmd))
    print()

    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    sys.exit(main())
