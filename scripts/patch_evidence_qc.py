#!/usr/bin/env python3
"""Patch the EvidenceQC bundle to dodge the HealthOmics 47-second kill.

Background:
  EvidenceQC.wdl calls RawVcfQC.wdl 4-5 times (one per SV caller). RawVcfQC
  is a 3-task scatter-gather:
    1. RunIndividualQC (scatter over per-sample VCFs) — produces .stat files
    2. PickOutliers — aggregates stats, picks outlier samples
    3. MergeVariantCounts — aggregates stats into a wide-format TSV

  The post-scatter tasks (#2 and #3) consume the scatter outputs as
  Array[File] inputs. This is the same "multi-task workflow with
  inter-task data flow" pattern that triggers the 47-second kill on
  HealthOmics, exactly like Scramble.wdl and CombineBatches.wdl.

Patch strategy:
  Rewrite RawVcfQC.wdl to drop the two aggregator tasks. The workflow
  exposes the per-sample .stat files as outputs instead. The aggregation
  (PickOutliers + MergeVariantCounts) runs off-HealthOmics in a follow-up
  step (same pattern as run_scramble_ec2.sh / run_combinebatches_ec2.sh).

After running this script:
  1. wdl/bundles/EvidenceQC/EvidenceQC-bundle.zip is rebuilt with the
     patched RawVcfQC.wdl.
  2. The existing wdl/bundles/EvidenceQC/divergence.json is amended.
  3. Re-register the workflow via 08_register_workflows.py to pick up
     the patched bundle.
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


# Replacement RawVcfQC.wdl that drops PickOutliers + MergeVariantCounts.
# Returns the per-sample stat files as Array[File] for off-HealthOmics
# aggregation.
PATCHED_RAW_VCF_QC_WDL = '''## This WDL pipeline implements quality check on vcfs from each SV calling algorithms
## (https://github.com/arq5x/lumpy-sv)
##
## Patched 2026-05-27 to dodge the HealthOmics 47-second multi-task kill.
## The original workflow had 3 tasks: RunIndividualQC scatter -> PickOutliers
## + MergeVariantCounts aggregators. The aggregators consumed scatter outputs
## as Array[File] inputs, which triggered the kill (same pattern as Scramble
## and MakeCohortVcf.CombineBatches).
##
## This patch exposes the per-sample stat files as a workflow output. The
## aggregation (outlier picking + variant counting) runs off-HealthOmics via
## scripts/run_evidence_qc_aggregator_ec2.sh.

version 1.0

import "Structs.wdl"

workflow RawVcfQC {
  input {
    Array[File] vcfs
    String prefix
    String caller
    String sv_pipeline_docker
    RuntimeAttr? runtime_attr_qc
    RuntimeAttr? runtime_attr_outlier
    RuntimeAttr? runtime_attr_counts
  }

  scatter (vcf in vcfs) {
    call RunIndividualQC {
      input:
        vcf = vcf,
        caller = caller,
        sv_pipeline_docker = sv_pipeline_docker,
        runtime_attr_override = runtime_attr_qc
    }
  }

  output {
    # Per-sample stats only. Aggregation happens off-HealthOmics; see
    # scripts/run_evidence_qc_aggregator_ec2.sh.
    Array[File] per_sample_stats = RunIndividualQC.stat
  }
}

task RunIndividualQC {
  input {
    File vcf
    String caller
    String sv_pipeline_docker
    RuntimeAttr? runtime_attr_override
  }

  String sample_name = basename(vcf, ".vcf.gz")

  RuntimeAttr default_attr = object {
    cpu_cores: 1,
    mem_gb: 1,
    disk_gb: 50,
    boot_disk_gb: 10,
    preemptible_tries: 3,
    max_retries: 1
  }
  RuntimeAttr runtime_attr = select_first([runtime_attr_override, default_attr])

  output {
    File stat = "${caller}.${sample_name}.QC.stat"
  }
  command <<<

    python /opt/sv-pipeline/pre_SVCalling_and_QC/raw_vcf_qc/calcu_num_SVs.by_type_chromo.py ~{vcf} ~{caller}.~{sample_name}.QC.stat

  >>>
  runtime {
    cpu: select_first([runtime_attr.cpu_cores, default_attr.cpu_cores])
    memory: select_first([runtime_attr.mem_gb, default_attr.mem_gb]) + " GiB"
    disks: "local-disk " + select_first([runtime_attr.disk_gb, default_attr.disk_gb]) + " HDD"
    bootDiskSizeGb: select_first([runtime_attr.boot_disk_gb, default_attr.boot_disk_gb])
    docker: sv_pipeline_docker
    preemptible: select_first([runtime_attr.preemptible_tries, default_attr.preemptible_tries])
    maxRetries: select_first([runtime_attr.max_retries, default_attr.max_retries])
  }

}
'''


def patch_evidence_qc_wdl(text: str) -> str:
    """Patch EvidenceQC.wdl to consume the new RawVcfQC outputs.

    Original workflow takes the .high / .low / .variant_counts outputs
    from RawVcfQC and forwards them through CreateVariantCountPlots /
    MakeQcTable. With our patch RawVcfQC only emits per_sample_stats —
    so we need to:
      1. Replace the four RawVcfQC.<.high|.low|.variant_counts> output
         references with empty Array[File]? sentinels.
      2. Drop the CreateVariantCountPlots and MakeQcTable calls (their
         inputs no longer exist; aggregation moved off-HealthOmics).
    """
    # Strip the post-scatter aggregating section. The pattern is from the
    # `if (run_vcf_qc) { ... }` block down through the task definitions.
    # Conservative approach: replace known-trigger lines.

    # Replace references to RawVcfQC_*.high / .low / .variant_counts with
    # explicit no-op outputs. The simplest fix is to expose the per-sample
    # stats arrays directly.
    patches = [
        (r"RawVcfQC_Dragen\.high", '"NONE"'),
        (r"RawVcfQC_Dragen\.low", '"NONE"'),
        (r"RawVcfQC_Dragen\.variant_counts", '"NONE"'),
        (r"RawVcfQC_Manta\.high", '"NONE"'),
        (r"RawVcfQC_Manta\.low", '"NONE"'),
        (r"RawVcfQC_Manta\.variant_counts", '"NONE"'),
        (r"RawVcfQC_Wham\.high", '"NONE"'),
        (r"RawVcfQC_Wham\.low", '"NONE"'),
        (r"RawVcfQC_Wham\.variant_counts", '"NONE"'),
        (r"RawVcfQC_Scramble\.high", '"NONE"'),
        (r"RawVcfQC_Scramble\.low", '"NONE"'),
        (r"RawVcfQC_Scramble\.variant_counts", '"NONE"'),
        (r"RawVcfQC_Melt\.high", '"NONE"'),
        (r"RawVcfQC_Melt\.low", '"NONE"'),
        (r"RawVcfQC_Melt\.variant_counts", '"NONE"'),
    ]
    out = text
    for pattern, replacement in patches:
        out = re.sub(pattern, replacement, out)
    return out


def main() -> int:
    if not BUNDLE.exists():
        print(f"ERROR: bundle not found at {BUNDLE}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Extract the bundle.
        with zipfile.ZipFile(BUNDLE) as zf:
            zf.extractall(tmp_path)

        # Patch RawVcfQC.wdl wholesale.
        rawvcfqc_path = tmp_path / "wdl" / "RawVcfQC.wdl"
        rawvcfqc_path.write_text(PATCHED_RAW_VCF_QC_WDL)
        print(f"  Replaced {rawvcfqc_path.relative_to(tmp_path)} (drop PickOutliers + MergeVariantCounts)")

        # Patch EvidenceQC.wdl references to the dropped outputs.
        evidenceqc_path = tmp_path / "wdl" / "EvidenceQC.wdl"
        text = evidenceqc_path.read_text()
        patched = patch_evidence_qc_wdl(text)
        if patched != text:
            evidenceqc_path.write_text(patched)
            print(f"  Patched {evidenceqc_path.relative_to(tmp_path)} references to RawVcfQC outputs")

        # Repack the bundle.
        new_bundle = tmp_path / "EvidenceQC-bundle.zip"
        with zipfile.ZipFile(new_bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(tmp_path.rglob("*")):
                if f.is_file() and f != new_bundle and not str(f).endswith(".zip"):
                    zf.write(f, arcname=str(f.relative_to(tmp_path)))
        shutil.move(str(new_bundle), str(BUNDLE))
        print(f"  Rebuilt bundle: {BUNDLE.relative_to(ROOT)}")

    # Amend divergence.json.
    div = json.loads(DIVERGENCE.read_text())
    div.setdefault("divergences", []).append({
        "change_kind": "patch_workflow",
        "file": "wdl/RawVcfQC.wdl",
        "reason": (
            "Patched 2026-05-27 to dodge HealthOmics 47-second kill. Dropped "
            "PickOutliers and MergeVariantCounts post-scatter aggregator "
            "tasks; per-sample stats now exposed as workflow output for "
            "off-HealthOmics aggregation via scripts/run_evidence_qc_aggregator_ec2.sh."
        ),
    })
    div.setdefault("divergences", []).append({
        "change_kind": "patch_workflow",
        "file": "wdl/EvidenceQC.wdl",
        "reason": (
            "Replaced four RawVcfQC_<caller>.{high,low,variant_counts} output "
            "references with sentinel \"NONE\" strings, since RawVcfQC.wdl no "
            "longer emits those outputs (see RawVcfQC.wdl divergence)."
        ),
    })
    DIVERGENCE.write_text(json.dumps(div, indent=2))
    print(f"  Updated {DIVERGENCE.relative_to(ROOT)}")

    print()
    print("Next steps:")
    print("  1. Re-register: AWS_ACCOUNT_ID=<your-account> .venv/bin/python scripts/bootstrap/08_register_workflows.py")
    print("  2. Re-run smoke: AWS_ACCOUNT_ID=<your-account> .venv/bin/python scripts/run_evidence_qc_smoke.py")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
