#!/usr/bin/env python3
"""Build v12 MakeCohortVcf bundle with track_bed_tarball workaround.

Replaces Array[File] track_bed_files with File track_bed_tarball to bypass
HealthOmics issue where Array[File] localization for GroupedSVClusterTask
silently kills the container at ~46 seconds with no logs.
"""
import re
import shutil
import subprocess
from pathlib import Path

TMP_DIR = Path("/tmp/makecohortvcf-v12")
SOURCE_DIR = Path("/tmp/makecohortvcf-v2")  # The v11 baseline
BUNDLE_PATH = Path("gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v12.zip")


def main():
    # Fresh copy
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    shutil.copytree(SOURCE_DIR, TMP_DIR)

    cb_path = TMP_DIR / "wdl" / "CombineBatches.wdl"
    mc_path = TMP_DIR / "wdl" / "MakeCohortVcf.wdl"

    # ----- 1. CombineBatches.wdl: workflow-level input changes -----
    cb = cb_path.read_text()

    # Replace workflow input: Array[File] track_bed_files -> File track_bed_tarball
    # In workflow input section (around line 30-31)
    cb_new = cb.replace(
        "    Array[String] track_names\n    Array[File] track_bed_files\n",
        "    Array[String] track_names\n    File track_bed_tarball\n"
    )
    assert cb_new != cb, "Workflow input replacement failed"
    cb = cb_new

    # Replace task-level input: Array[File] track_bed_files -> File track_bed_tarball
    cb_new = cb.replace(
        "    File clustering_config\n    File stratification_config\n    Array[File] track_bed_files\n    Array[String] track_names\n",
        "    File clustering_config\n    File stratification_config\n    File track_bed_tarball\n    Array[String] track_names\n"
    )
    assert cb_new != cb, "Task input replacement failed"
    cb = cb_new

    # Replace GroupedSVClusterTask call sites - both Part1 and Part2
    cb_new = cb.replace(
        "        track_bed_files=track_bed_files,",
        "        track_bed_tarball=track_bed_tarball,"
    )
    assert cb_new != cb, "Call-site replacement failed"
    cb = cb_new

    # Replace the GATK command: extract tarball and build paths from track_names
    old_cmd = """    gatk --java-options "-Xmx${JVM_MAX_MEM}" GroupedSVCluster \\
      ~{"-L " + contig} \\
      --reference ~{reference_fasta} \\
      --ploidy-table ~{ploidy_table} \\
      -V ~{vcf} \\
      -O ~{output_prefix}.vcf.gz \\
      --clustering-config ~{clustering_config} \\
      --stratify-config ~{stratification_config} \\
      --track-intervals ~{sep=" --track-intervals " track_bed_files} \\
      --track-name ~{sep=" --track-name " track_names} \\"""

    new_cmd = """    # Extract bundled track files (HealthOmics workaround for Array[File] localization issue)
    mkdir -p track_files
    tar xzf ~{track_bed_tarball} -C track_files/
    ls -la track_files/

    # Build --track-intervals args from track_names
    TRACK_ARGS=""
    for name in ~{sep=" " track_names}; do
        TRACK_ARGS="$TRACK_ARGS --track-intervals track_files/track.${name}.bed.gz --track-name ${name}"
    done
    echo "Track arguments: $TRACK_ARGS"

    gatk --java-options "-Xmx${JVM_MAX_MEM}" GroupedSVCluster \\
      ~{"-L " + contig} \\
      --reference ~{reference_fasta} \\
      --ploidy-table ~{ploidy_table} \\
      -V ~{vcf} \\
      -O ~{output_prefix}.vcf.gz \\
      --clustering-config ~{clustering_config} \\
      --stratify-config ~{stratification_config} \\
      $TRACK_ARGS \\"""

    cb_new = cb.replace(old_cmd, new_cmd)
    assert cb_new != cb, "Command block replacement failed"
    cb = cb_new

    cb_path.write_text(cb)
    print(f"✓ Modified {cb_path}")

    # ----- 2. MakeCohortVcf.wdl: pass through track_bed_tarball -----
    mc = mc_path.read_text()

    # Find existing track_bed_files declarations and replace
    # Workflow input
    mc_new = mc.replace(
        "Array[File] track_bed_files",
        "File track_bed_tarball"
    )
    if mc_new == mc:
        print("⚠ No track_bed_files in MakeCohortVcf.wdl input")
    else:
        mc = mc_new

    # Pass-through to CombineBatches call
    mc_new = mc.replace(
        "track_bed_files=track_bed_files",
        "track_bed_tarball=track_bed_tarball"
    )
    if mc_new == mc:
        print("⚠ No track_bed_files=... pass-through in MakeCohortVcf.wdl")
    else:
        mc = mc_new

    mc_path.write_text(mc)
    print(f"✓ Modified {mc_path}")

    # ----- 3. Build the bundle -----
    if BUNDLE_PATH.exists():
        BUNDLE_PATH.unlink()
    subprocess.run(
        ["zip", "-q", "-r", str(BUNDLE_PATH.resolve()), "wdl/"],
        cwd=TMP_DIR, check=True,
    )
    print(f"✓ Bundle created: {BUNDLE_PATH} ({BUNDLE_PATH.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
