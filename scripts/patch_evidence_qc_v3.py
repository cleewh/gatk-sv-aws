#!/usr/bin/env python3
"""Patch v3 for EvidenceQC: strip the orphan output declarations.

Patches v1+v2 dodge the 47-second kill and skip the MakeQcTable cascade,
but the workflow's output { } block still contains 15 declarations of the
form:

    File? dragen_qc_low = "NONE"
    File? dragen_qc_high = "NONE"
    File? dragen_variant_counts = "NONE"
    ... (15 total: 5 callers x {qc_low, qc_high, variant_counts})
    File? qc_table = MakeQcTable.qc_table   <- new MakeQcTable orphan
    File? ploidy_plots = if run_ploidy then select_first([
        CreateVariantCountPlots.ploidy_plots, Ploidy.ploidy_plots
    ]) else NONE_FILE_

These get evaluated even when run_vcf_qc=False, and miniwdl's [file_io]
input check rejects the literal "NONE" string as a File.

This patch:
  1. Removes the 15 `File? <caller>_qc_*/<caller>_variant_counts = "NONE"`
     declarations.
  2. Replaces `ploidy_plots = if run_ploidy then ... else NONE_FILE_`
     with `ploidy_plots = Ploidy.ploidy_plots`.
  3. Removes the `qc_table = MakeQcTable.qc_table` declaration.
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
    out = text
    # 1. Remove the 15 "NONE" sentinel output declarations.
    none_decl = re.compile(
        r'^\s*File\?\s+\w+(?:_qc_low|_qc_high|_variant_counts)\s*=\s*"NONE"\s*$',
        re.MULTILINE,
    )
    out = none_decl.sub("", out)

    # 2. Replace the ploidy_plots conditional that references CreateVariantCountPlots.
    # Pattern is multi-line with `if run_ploidy then select_first([...]) else NONE_FILE_`.
    # Just strip the conditional and use Ploidy.ploidy_plots directly.
    ploidy_plots_decl = re.compile(
        r"File\?\s+ploidy_plots\s*=\s*if\s+run_ploidy\s+then\s+"
        r"select_first\(\[CreateVariantCountPlots\.ploidy_plots,\s*Ploidy\.ploidy_plots\]\)\s+"
        r"else\s+NONE_FILE_",
        re.DOTALL,
    )
    out = ploidy_plots_decl.sub(
        "File? ploidy_plots = Ploidy.ploidy_plots",
        out,
    )

    # 3. Remove the qc_table declaration.
    qc_table_decl = re.compile(
        r'^\s*File\?\s+qc_table\s*=\s*MakeQcTable\.qc_table\s*$',
        re.MULTILINE,
    )
    out = qc_table_decl.sub("", out)

    return out


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
        print(f"  Patched {evidenceqc_path.relative_to(tmp_path)} (strip orphan output decls)")

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
            "Strip 15 `File? <caller>_qc_*/<caller>_variant_counts = \"NONE\"` "
            "declarations and the `qc_table = MakeQcTable.qc_table` declaration "
            "from the workflow output block. miniwdl's [file_io] input check "
            "rejects the literal \"NONE\" string. With the v1+v2 patches gating "
            "the upstream calls on run_vcf_qc, these output declarations are "
            "orphans and should be removed entirely. Also rewrites "
            "ploidy_plots to use Ploidy.ploidy_plots directly (the original "
            "conditional referenced CreateVariantCountPlots which no longer runs)."
        ),
    })
    DIVERGENCE.write_text(json.dumps(div, indent=2))
    print(f"  Updated {DIVERGENCE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
