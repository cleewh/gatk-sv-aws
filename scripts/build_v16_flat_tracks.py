#!/usr/bin/env python3
"""Build MakeCohortVcf v16 — replace the track_bed_tarball pattern with
three separate File inputs (matching the diagnostic that succeeded on
HealthOmics).

The diagnostic (run 5601461, workflow 8667186) proved that GroupedSVCluster
runs fine on HealthOmics when track files are passed as individual File
inputs rather than a tarball that the task body extracts at runtime.

v15 → v16 changes:
  - MakeCohortVcf.wdl: drop `track_names` / `track_bed_tarball`, add six
    `File` inputs (3 tracks × .bed.gz + .tbi).
  - CombineBatches.wdl (workflow): same swap, threaded through.
  - ClusterSitesAndGroupedCluster task: take six File inputs, build
    --track-intervals / --track-name args directly with the SimpRep+SD+RM
    triple and the SR/SD/RM names hardcoded.
  - GroupedSVClusterTask (unused but kept): same swap so the file lints.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SOURCE_DIR = Path(
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/v16-build-src"
)
OUT_DIR = Path("/tmp/mcv-v16-out")
BUNDLE_PATH = Path(
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v16.zip"
)


def patch_makecohortvcf(text: str) -> str:
    # input declaration block: replace the two tarball lines
    old = "    Array[String] track_names\n    File track_bed_tarball"
    new = "    File track_simrep\n    File track_simrep_idx\n    File track_segdups\n    File track_segdups_idx\n    File track_rmsk\n    File track_rmsk_idx"
    assert old in text, "MakeCohortVcf input block not found"
    text = text.replace(old, new, 1)

    # call wiring: there is one call to CombineBatches with both lines
    old = "      track_names=track_names,\n      track_names=track_names,\n      track_bed_tarball=track_bed_tarball,\n      track_bed_tarball=track_bed_tarball,"
    new = "      track_simrep=track_simrep,\n      track_simrep_idx=track_simrep_idx,\n      track_segdups=track_segdups,\n      track_segdups_idx=track_segdups_idx,\n      track_rmsk=track_rmsk,\n      track_rmsk_idx=track_rmsk_idx,"
    if old in text:
        text = text.replace(old, new, 1)
    else:
        # Maybe no doubled lines; fall back to single-line replace
        single_old = "      track_names=track_names,\n      track_bed_tarball=track_bed_tarball,"
        assert single_old in text, "MakeCohortVcf call wiring not found"
        text = text.replace(single_old, new, 1)
        # If still has doubled-up names line, drop the extra
        text = text.replace(
            "      track_simrep=track_simrep,\n      track_simrep=track_simrep,",
            "      track_simrep=track_simrep,",
        )
    return text


# Workflow header track block
WF_OLD_INPUTS = (
    "    Array[String] track_names\n"
    "    File track_bed_tarball\n"
)
WF_NEW_INPUTS = (
    "    File track_simrep\n"
    "    File track_simrep_idx\n"
    "    File track_segdups\n"
    "    File track_segdups_idx\n"
    "    File track_rmsk\n"
    "    File track_rmsk_idx\n"
)

# Call to ClusterSitesAndGroupedCluster (in the scatter)
CALL_OLD = (
    "        track_bed_tarball=track_bed_tarball,\n"
    "        track_names=track_names,\n"
)
CALL_NEW = (
    "        track_simrep=track_simrep,\n"
    "        track_simrep_idx=track_simrep_idx,\n"
    "        track_segdups=track_segdups,\n"
    "        track_segdups_idx=track_segdups_idx,\n"
    "        track_rmsk=track_rmsk,\n"
    "        track_rmsk_idx=track_rmsk_idx,\n"
)

# ClusterSitesAndGroupedCluster task input block
TASK_OLD_INPUTS = (
    "    File track_bed_tarball\n"
    "    Array[String] track_names\n"
)
TASK_NEW_INPUTS = (
    "    File track_simrep\n"
    "    File track_simrep_idx\n"
    "    File track_segdups\n"
    "    File track_segdups_idx\n"
    "    File track_rmsk\n"
    "    File track_rmsk_idx\n"
)

# ClusterSitesAndGroupedCluster body: replace the tarball extract + dynamic
# TRACK_ARGS with explicit references to the three localized File inputs.
TASK_OLD_BODY = """    # Extract bundled track files locally
    mkdir -p track_files
    tar xzf ~{track_bed_tarball} -C track_files/
    ls -la track_files/

    # Build --track-intervals args from track_names
    TRACK_ARGS=""
    for name in ~{sep=" " track_names}; do
        TRACK_ARGS="$TRACK_ARGS --track-intervals track_files/track.${name}.bed.gz --track-name ${name}"
    done
    echo "Track arguments: $TRACK_ARGS"
"""
TASK_NEW_BODY = """    # Each --track-intervals expects a sibling .tbi index already present
    # next to the .bed.gz; miniwdl colocates them when both are passed as
    # File inputs. Names ("SR", "SD", "RM") match the stratify-config keys.
    TRACK_ARGS="--track-intervals ~{track_simrep} --track-name SR \
--track-intervals ~{track_segdups} --track-name SD \
--track-intervals ~{track_rmsk} --track-name RM"
    echo "Track arguments: $TRACK_ARGS"
"""

# After replacing TRACK_ARGS construction, also replace the GroupedSVCluster
# command lines that use $TRACK_ARGS to use the inline form (so the task is
# trivially correct even if the variable expansion ever fails).
GATK_OLD = (
    "      --stratify-config ~{stratification_config_part1} \\\n"
    "      $TRACK_ARGS \\\n"
)
GATK_NEW = (
    "      --stratify-config ~{stratification_config_part1} \\\n"
    "      $TRACK_ARGS \\\n"
)
# (Same — keep $TRACK_ARGS interpolation; only the construction changed.)

# GroupedSVClusterTask (unused but bundled): swap inputs + body the same way.
TASK2_OLD_INPUTS = (
    "    File track_bed_tarball\n"
    "    Array[String] track_names\n"
)
TASK2_OLD_BODY = """    # Extract bundled track files (HealthOmics workaround for Array[File] localization issue)
    mkdir -p track_files
    tar xzf ~{track_bed_tarball} -C track_files/
    ls -la track_files/

    # Build --track-intervals args from track_names
    TRACK_ARGS=""
    for name in ~{sep=" " track_names}; do
        TRACK_ARGS="$TRACK_ARGS --track-intervals track_files/track.${name}.bed.gz --track-name ${name}"
    done
    echo "Track arguments: $TRACK_ARGS"
"""


def patch_combinebatches(text: str) -> str:
    # Workflow inputs
    assert WF_OLD_INPUTS in text, "CombineBatches workflow inputs not found"
    text = text.replace(WF_OLD_INPUTS, WF_NEW_INPUTS, 1)

    # Workflow call to ClusterSitesAndGroupedCluster
    assert CALL_OLD in text, "ClusterSitesAndGroupedCluster call wiring not found"
    text = text.replace(CALL_OLD, CALL_NEW, 1)

    # ClusterSitesAndGroupedCluster task inputs (first occurrence)
    idx = text.find("task ClusterSitesAndGroupedCluster")
    assert idx >= 0, "ClusterSitesAndGroupedCluster task not found"
    rel = text[idx:].replace(TASK_OLD_INPUTS, TASK_NEW_INPUTS, 1)
    text = text[:idx] + rel

    # Task body
    assert TASK_OLD_BODY in text, "ClusterSitesAndGroupedCluster body not found"
    text = text.replace(TASK_OLD_BODY, TASK_NEW_BODY, 1)

    # GroupedSVClusterTask (unused, but keep it lintable)
    idx2 = text.find("task GroupedSVClusterTask")
    if idx2 >= 0:
        rel2 = text[idx2:]
        if TASK2_OLD_INPUTS in rel2:
            rel2 = rel2.replace(TASK2_OLD_INPUTS, TASK_NEW_INPUTS, 1)
        if TASK2_OLD_BODY in rel2:
            rel2 = rel2.replace(TASK2_OLD_BODY, TASK_NEW_BODY, 1)
        text = text[:idx2] + rel2

    return text


def main() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    shutil.copytree(SOURCE_DIR, OUT_DIR)

    mcv = OUT_DIR / "wdl" / "MakeCohortVcf.wdl"
    mcv.write_text(patch_makecohortvcf(mcv.read_text()))
    print(f"  patched {mcv.name}")

    cb = OUT_DIR / "wdl" / "CombineBatches.wdl"
    cb.write_text(patch_combinebatches(cb.read_text()))
    print(f"  patched {cb.name}")

    if BUNDLE_PATH.exists():
        BUNDLE_PATH.unlink()
    subprocess.run(
        ["zip", "-q", "-r", str(BUNDLE_PATH.resolve()), "wdl/"],
        cwd=OUT_DIR,
        check=True,
    )
    size = BUNDLE_PATH.stat().st_size
    print(f"\n✓ Bundle: {BUNDLE_PATH} ({size:,} bytes)")


if __name__ == "__main__":
    main()
