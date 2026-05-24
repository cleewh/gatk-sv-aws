#!/usr/bin/env python3
"""Build v13 with extensive diagnostics to figure out why GroupedSVCluster fails on HealthOmics."""
import shutil
import subprocess
from pathlib import Path

TMP_DIR = Path("/tmp/makecohortvcf-v13")
SOURCE_DIR = Path("/tmp/makecohortvcf-v12")
BUNDLE_PATH = Path("gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v13.zip")


def main():
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    shutil.copytree(SOURCE_DIR, TMP_DIR)

    cb_path = TMP_DIR / "wdl" / "CombineBatches.wdl"
    cb = cb_path.read_text()

    # Insert diagnostic section right after `JVM_MAX_MEM=...` line in GroupedSVClusterTask
    # Find the section and replace using regex-like approach
    diag = """
    # ========== DIAGNOSTIC ==========
    echo "DIAG: container start: $(date -u +%H:%M:%S.%N)"
    echo "DIAG: hostname: $(hostname), uname: $(uname -a)"
    echo "DIAG: free -m:"; free -m
    echo "DIAG: df -h:"; df -h
    echo "DIAG: ls inputs:"
    ls -la ~{vcf} ~{ploidy_table} ~{reference_fasta} ~{clustering_config} ~{stratification_config} ~{track_bed_tarball} 2>&1 || echo "DIAG: ls failed"
    echo "DIAG: head clustering_config:"; head -3 ~{clustering_config}
    echo "DIAG: head stratification_config:"; head -3 ~{stratification_config}
    echo "DIAG: vcf size:"; wc -c ~{vcf}
    echo "DIAG: java version:"; java -version 2>&1
    echo "DIAG: pre-extract: $(date -u +%H:%M:%S.%N)"
    # ========== END DIAGNOSTIC ==========

    # Extract bundled track files (HealthOmics workaround for Array[File] localization issue)"""

    # Find the spot: just before "# Extract bundled track files"
    marker = "    # Extract bundled track files (HealthOmics workaround for Array[File] localization issue)"
    cb_new = cb.replace(marker, diag.lstrip("\n").replace("    # Extract bundled track files (HealthOmics workaround for Array[File] localization issue)\n", "") + "\n" + marker)
    
    # Simpler: just replace marker once with diag (which already includes the marker at end)
    cb_new = cb.replace(marker, diag.lstrip("\n"))
    assert cb_new != cb, "Diagnostic insertion failed"
    cb = cb_new

    # Add timing echo right before gatk command
    cb_new = cb.replace(
        '    echo "Track arguments: $TRACK_ARGS"\n',
        '    echo "Track arguments: $TRACK_ARGS"\n    echo "DIAG: pre-gatk: $(date -u +%H:%M:%S.%N)"\n'
    )
    assert cb_new != cb, "Pre-gatk timing failed"
    cb = cb_new
    
    cb_path.write_text(cb)
    print(f"✓ Modified {cb_path}")
    
    if BUNDLE_PATH.exists():
        BUNDLE_PATH.unlink()
    subprocess.run(
        ["zip", "-q", "-r", str(BUNDLE_PATH.resolve()), "wdl/"],
        cwd=TMP_DIR, check=True,
    )
    print(f"✓ Bundle created: {BUNDLE_PATH} ({BUNDLE_PATH.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
