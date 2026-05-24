#!/usr/bin/env python3
"""Build v14 that does `sleep` before GATK to test if HealthOmics has a 47s kill timer."""
import shutil
import subprocess
from pathlib import Path

TMP_DIR = Path("/tmp/makecohortvcf-v14")
SOURCE_DIR = Path("/tmp/makecohortvcf-v13")
BUNDLE_PATH = Path("gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v14.zip")


def main():
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    shutil.copytree(SOURCE_DIR, TMP_DIR)

    cb_path = TMP_DIR / "wdl" / "CombineBatches.wdl"
    cb = cb_path.read_text()

    # Add a sleep loop with echo every 10 seconds for 90 seconds
    # before GATK runs. If the task dies at 47s with no logs, the issue
    # is HealthOmics killing tasks regardless of what they do.
    
    old = '    echo "Track arguments: $TRACK_ARGS"\n    echo "DIAG: pre-gatk: $(date -u +%H:%M:%S.%N)"\n'
    new = '''    echo "Track arguments: $TRACK_ARGS"
    echo "DIAG: pre-sleep: $(date -u +%H:%M:%S.%N)"

    # SLEEP TEST: print progress every 10s for 90 seconds total
    # If HealthOmics kills tasks at ~47s, we'll know the issue is platform-side
    for i in 1 2 3 4 5 6 7 8 9; do
        echo "DIAG: sleep iteration $i at $(date -u +%H:%M:%S)"
        sleep 10
    done

    echo "DIAG: post-sleep, pre-gatk: $(date -u +%H:%M:%S.%N)"
'''
    cb_new = cb.replace(old, new)
    assert cb_new != cb, "Sleep insertion failed"
    cb_path.write_text(cb_new)
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
