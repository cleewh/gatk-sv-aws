#!/usr/bin/env python3
"""Patch v2 for EvidenceQC: also bypass MakeQcTable when run_vcf_qc=False.

Background:
  Patch v1 (scripts/patch_evidence_qc.py) replaced RawVcfQC.wdl to drop the
  47-second-kill aggregator tasks. That worked, but EvidenceQC.wdl still
  unconditionally calls CreateVariantCountPlots (gated by inner `if
  (run_vcf_qc)`) and MakeQcTable (NOT gated by run_vcf_qc — only by
  run_ploidy). MakeQcTable expects File inputs from the variant_counts
  outputs we no longer produce; v1 substituted "NONE" string sentinels
  which miniwdl rejects at runtime.

This patch rewrites the second `if (run_ploidy)` block in EvidenceQC.wdl
so it ALSO gates on `run_vcf_qc`. With run_vcf_qc=False:
  - Phase A.6 still produces bincov matrix, median coverage, ploidy
    matrix/plots, WGD scores (the inputs Phase B needs).
  - The variant-count plots and merged QC table are skipped.

When run_vcf_qc=True:
  - The patched RawVcfQC.wdl returns per-sample stats only (kill-safe).
  - But CreateVariantCountPlots + MakeQcTable will still fail because they
    need the aggregated outputs. Need a second-stage off-HealthOmics
    aggregator (TODO if needed for production runs).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "wdl" / "bundles" / "EvidenceQC" / "EvidenceQC-bundle.zip"
DIVERGENCE = ROOT / "wdl" / "bundles" / "EvidenceQC" / "divergence.json"


def patch(text: str) -> str:
    """Wrap the second `if (run_ploidy)` block (containing CreateVariantCountPlots
    and MakeQcTable) in `if (run_ploidy && run_vcf_qc)` instead.
    """
    # Find the line that opens the second `if (run_ploidy) {` block.
    # The first one is the one that calls Ploidy itself; we want the second.
    # Anchor: the second is the one that contains "Array[File] variant_count_files".
    lines = text.split("\n")

    # Find the second `if (run_ploidy)` (the one wrapping MakeQcTable).
    found_first = False
    for i, line in enumerate(lines):
        if "if (run_ploidy)" in line and "{" in line:
            if not found_first:
                found_first = True
                continue
            # This is the second one.
            indent_match = re.match(r"^(\s*)", line)
            indent = indent_match.group(1) if indent_match else ""
            lines[i] = f"{indent}if (run_ploidy && run_vcf_qc) {{"
            return "\n".join(lines)

    raise RuntimeError("Could not find second `if (run_ploidy)` block in EvidenceQC.wdl")


def main() -> int:
    if not BUNDLE.exists():
        print(f"ERROR: bundle not found at {BUNDLE}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(BUNDLE) as zf:
            zf.extractall(tmp_path)

        evidenceqc_path = tmp_path / "wdl" / "EvidenceQC.wdl"
        text = evidenceqc_path.read_text()
        patched = patch(text)
        if patched == text:
            print("ERROR: patch had no effect", file=sys.stderr)
            return 2
        evidenceqc_path.write_text(patched)
        print(f"  Patched {evidenceqc_path.relative_to(tmp_path)} (gate MakeQcTable on run_vcf_qc too)")

        # Repack bundle.
        new_bundle = tmp_path / "EvidenceQC-bundle.zip"
        with zipfile.ZipFile(new_bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(tmp_path.rglob("*")):
                if f.is_file() and f != new_bundle and not str(f).endswith(".zip"):
                    zf.write(f, arcname=str(f.relative_to(tmp_path)))
        shutil.move(str(new_bundle), str(BUNDLE))
        print(f"  Rebuilt bundle: {BUNDLE.relative_to(ROOT)}")

    div = json.loads(DIVERGENCE.read_text())
    div.setdefault("divergences", []).append({
        "change_kind": "patch_workflow",
        "file": "wdl/EvidenceQC.wdl",
        "reason": (
            "Gate the second `if (run_ploidy)` block (CreateVariantCountPlots + "
            "MakeQcTable) on `run_ploidy && run_vcf_qc`. Without this gate, "
            "MakeQcTable runs even when run_vcf_qc=False and crashes on the "
            "missing variant-count Files (which RawVcfQC no longer emits "
            "since the v1 patch). Production setting: run_vcf_qc=False."
        ),
    })
    DIVERGENCE.write_text(json.dumps(div, indent=2))
    print(f"  Updated {DIVERGENCE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
