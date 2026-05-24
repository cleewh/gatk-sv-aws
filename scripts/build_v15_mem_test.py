#!/usr/bin/env python3
"""Build v15 - 16 GiB memory + java_mem_fraction=0.5 to test JVM heap vs container memory hypothesis."""
import shutil
import subprocess
from pathlib import Path

TMP_DIR = Path("/tmp/makecohortvcf-v15")
SOURCE_DIR = Path("/tmp/makecohortvcf-v12")  # Start from v12 (no sleep test, no diagnostic)
BUNDLE_PATH = Path("gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v15.zip")


def main():
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    shutil.copytree(SOURCE_DIR, TMP_DIR)

    cb_path = TMP_DIR / "wdl" / "CombineBatches.wdl"
    cb = cb_path.read_text()

    # Bump GroupedSVClusterTask memory to 16 GiB and explicitly set lower java_mem_fraction
    # We modified v12 to have mem_gb: 8, cpu_cores: 1 - bump to 16
    cb_new = cb.replace(
        '''  RuntimeAttr default_attr = object {
                               cpu_cores: 1,
                               mem_gb: 8,
                               disk_gb: ceil(10 + size(vcf, "GB") * 2),''',
        '''  RuntimeAttr default_attr = object {
                               cpu_cores: 4,
                               mem_gb: 16,
                               disk_gb: 100,'''
    )
    assert cb_new != cb, "Memory bump failed"
    cb = cb_new

    # Override default java_mem_fraction in the GATK command
    # Find the JVM_MAX_MEM line and force lower fraction
    # The line has: ~{default="0.85" java_mem_fraction}
    # Change to default 0.40 (40% of container memory for JVM heap)
    cb_new = cb.replace(
        'printf "%dM", f[MEM_FIELD] * ~{default="0.85" java_mem_fraction} / 1024',
        'printf "%dM", f[MEM_FIELD] * ~{default="0.40" java_mem_fraction} / 1024'
    )
    if cb_new == cb:
        print("⚠ java_mem_fraction default not found in expected location")
    else:
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
