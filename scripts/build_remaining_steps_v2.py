#!/usr/bin/env python3
"""Build MakeCohortVcf-RemainingSteps v2.

v1 failed at ResolveComplexSv with miniwdl InputError because the upstream
WDL constructs sibling index file paths via string concat (vcf + ".tbi")
inside the workflow body. Cromwell tolerates this; miniwdl strict mode
rejects it because the constructed path isn't an explicit workflow input.

Fix: rewrite every workflow-body String-concat sibling pattern to use
explicit `File ..._idx` inputs threaded through from the parent workflow.

Affected workflow files (workflow-body sibling concat):
  - ResolveCpxSv.wdl   : vcf_idx, pe_exclude_list_idx, cytobands_idx,
                          disc_files_idx (scatter)
  - ResolveComplexVariants.wdl : cluster_vcf_indexes (Array, threaded
                          to FilterVcf.vcf_index and ResolveCpxSv.vcf_idx)
  - GenotypeCpxCnvsPerBatch.wdl: coverage_file_idx
  - ReshardVcf.wdl     : vcfs[i] + ".tbi" (scatter — replace with input)
  - MainVcfQc.wdl      : vcf + ".tbi" (replace with explicit idx input)
  - CollectQcVcfWide.wdl: vcf + ".tbi" (idem)

Task-output sibling concat is left intact (filename = produced-file +
".tbi" inside task output{} blocks works on miniwdl because the file
genuinely exists in the task's working dir).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

SRC_DIR = Path("/tmp/mcv-remaining-steps")  # v1 source
OUT_DIR = Path("/tmp/mcv-remaining-steps-v2")
BUNDLE = Path(
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/"
    "MakeCohortVcf-RemainingSteps-bundle-v2.zip"
)


# -------- ResolveCpxSv.wdl --------
# Replace lines 42-47:
#   File vcf_idx = vcf + ".tbi"
#   File pe_exclude_list_idx = pe_exclude_list + ".tbi"
#   File cytobands_idx = cytobands + ".tbi"
#   scatter (i in range(length(disc_files))) {
#     File disc_files_idx = disc_files[i] + ".tbi"
#   }
RESOLVECPX_OLD = """  File vcf_idx = vcf + ".tbi"
  File pe_exclude_list_idx = pe_exclude_list + ".tbi"
  File cytobands_idx = cytobands + ".tbi"
  scatter (i in range(length(disc_files))) {
    File disc_files_idx = disc_files[i] + ".tbi"
  }
"""
RESOLVECPX_NEW = ""  # remove entirely; use the new explicit File inputs

# Add new explicit File inputs to ResolveCpxSv workflow input block:
# Position: insert before the closing `}` of the inputs block
# Marker: the line right before the existing string-concat block.
RESOLVECPX_INPUT_OLD = """    RuntimeAttr? runtime_override_preconcat
    RuntimeAttr? runtime_override_fix_header
  }
"""
RESOLVECPX_INPUT_NEW = """    RuntimeAttr? runtime_override_preconcat
    RuntimeAttr? runtime_override_fix_header

    # Explicit sibling-index inputs (replaces string-concat pattern;
    # miniwdl strict mode requires them as actual File inputs)
    File vcf_idx
    File? pe_exclude_list_idx
    File? cytobands_idx
    Array[File] disc_files_idx
  }
"""

# -------- ResolveComplexVariants.wdl --------
# Add cluster_vcf_indexes Array[File] input; thread to FilterVcf.vcf_index
# (replacing cluster_vcfs[i] + ".tbi") and to ResolveCpxSv.vcf_idx + the
# new index inputs.

RCV_INPUT_OLD = """    Array[File] cluster_vcfs
    Array[File] cluster_bothside_pass_lists
    Array[File] cluster_background_fail_lists
"""
RCV_INPUT_NEW = """    Array[File] cluster_vcfs
    Array[File] cluster_vcf_indexes
    Array[File] cluster_bothside_pass_lists
    Array[File] cluster_background_fail_lists
    File? cytobands_idx
    File? pe_exclude_list_idx
    Array[File] disc_files_idx
"""

# Add the four matching workflow inputs to ResolveComplexVariants
# (cytobands/pe_exclude_list already exist; we just add their _idx siblings)

# Replace `vcf_index=cluster_vcfs[i] + ".tbi"` (two places) with
# `vcf_index=cluster_vcf_indexes[i]`
RCV_VI_OLD = 'vcf_index=cluster_vcfs[i] + ".tbi"'
RCV_VI_NEW = "vcf_index=cluster_vcf_indexes[i]"

# Add new fields to BOTH ResolveCpxSv calls (ResolveCpxAll, ResolveCpxInv)
# Inserted right before the closing `}` of each call. Easiest: find and
# inject. We'll rely on the line `runtime_override_fix_header=...` then add
# the index inputs after that with a comma.
# But the simpler way: after `ref_dict=ref_dict,` add the new lines.
RCV_RESOLVECPX_OLD = """        ref_dict=ref_dict,
"""
# Note: there are 2 calls (ResolveCpxAll and ResolveCpxInv). Both have ref_dict.
# Replace BOTH with augmented version that adds the new explicit indexes.
RCV_RESOLVECPX_NEW = """        ref_dict=ref_dict,
        cytobands_idx=cytobands_idx,
        pe_exclude_list_idx=pe_exclude_list_idx,
        disc_files_idx=disc_files_idx,
"""
# vcf_idx is per-call (different VCFs), needs separate handling

# For ResolveCpxAll: input is `vcf=cluster_vcfs[i]`, so we add
# `vcf_idx=cluster_vcf_indexes[i]`
# For ResolveCpxInv: input is `vcf=SubsetInversions.filtered_vcf`, so we add
# `vcf_idx=SubsetInversions.filtered_vcf_idx`

RCV_CPXALL_OLD = """    call ResolveComplexContig.ResolveComplexSv as ResolveCpxAll {
      input:
        vcf=BreakpointOverlap.out,
"""
RCV_CPXALL_NEW = """    call ResolveComplexContig.ResolveComplexSv as ResolveCpxAll {
      input:
        vcf=BreakpointOverlap.out,
        vcf_idx=BreakpointOverlap.out_index,
"""

RCV_CPXINV_OLD = """    call ResolveComplexContig.ResolveComplexSv as ResolveCpxInv {
      input:
        vcf=SubsetInversions.filtered_vcf,
"""
RCV_CPXINV_NEW = """    call ResolveComplexContig.ResolveComplexSv as ResolveCpxInv {
      input:
        vcf=SubsetInversions.filtered_vcf,
        vcf_idx=SubsetInversions.filtered_vcf_idx,
"""

# -------- ReshardVcf.wdl --------
# Workflow ReshardVcf has `Array[File] vcfs` as input then constructs
# `File vcf_indexes = vcfs[i] + ".tbi"` inside a scatter. Replace with an
# Array[File] vcf_indexes input on the workflow only (the inner task
# already has Array[File] vcf_indexes).
RESHARD_INPUT_OLD = """workflow ReshardVcf {
  input {
    Array[File] vcfs  # Order does not matter but must be sorted and indexed
"""
RESHARD_INPUT_NEW = """workflow ReshardVcf {
  input {
    Array[File] vcfs  # Order does not matter but must be sorted and indexed
    Array[File] vcf_indexes
"""
# Then remove the scatter that constructs vcf_indexes
RESHARD_SCATTER_OLD = """  scatter (i in range(length(vcfs))) {
    File vcf_indexes = vcfs[i] + ".tbi"
  }
"""
RESHARD_SCATTER_NEW = ""

# -------- GenotypeCpxCnvsPerBatch.wdl --------
GCNV_OLD = """  File coverage_file_idx = coverage_file + ".tbi"
"""
GCNV_NEW = ""
# Add as input (find inputs block and add before `}`)
GCNV_INPUT_OLD = """    File coverage_file
"""
GCNV_INPUT_NEW = """    File coverage_file
    File coverage_file_idx
"""

# -------- MainVcfQc.wdl --------
# Line 85: `vcf_idx=vcf + ".tbi"` inside a scatter
MAINQC_OLD = '''          vcf_idx=vcf + ".tbi",'''
MAINQC_NEW = """          vcf_idx=vcf_indexes[i],"""

# Need to add Array[File] vcf_indexes as input
MAINQC_INPUT_OLD = """    Array[File] vcfs
"""
MAINQC_INPUT_NEW = """    Array[File] vcfs
    Array[File] vcf_indexes
"""
# And the scatter uses `String vcf` — let me check.

# -------- CollectQcVcfWide.wdl --------
COLLECT_OLD = '''        vcf_index=vcf + ".tbi",'''
COLLECT_NEW = """        vcf_index=vcf_idx,"""
COLLECT_INPUT_OLD = """    File vcf
"""
COLLECT_INPUT_NEW = """    File vcf
    File vcf_idx
"""


PATCHES = {
    "ResolveComplexVariants.wdl": [
        (RCV_INPUT_OLD, RCV_INPUT_NEW),
        (RCV_VI_OLD, RCV_VI_NEW),                         # 1st use
        (RCV_VI_OLD, RCV_VI_NEW),                         # 2nd use
        (RCV_CPXALL_OLD, RCV_CPXALL_NEW),
        (RCV_CPXINV_OLD, RCV_CPXINV_NEW),
        # The two ref_dict lines need separate context to disambiguate;
        # use the surrounding workflow-name comment as anchor instead.
        (
            """    call ResolveComplexContig.ResolveComplexSv as ResolveCpxAll {
      input:
        vcf=BreakpointOverlap.out,
        vcf_idx=BreakpointOverlap.out_index,
""",
            """    call ResolveComplexContig.ResolveComplexSv as ResolveCpxAll {
      input:
        vcf=BreakpointOverlap.out,
        vcf_idx=BreakpointOverlap.out_index,
        cytobands_idx=cytobands_idx,
        pe_exclude_list_idx=pe_exclude_list_idx,
        disc_files_idx=disc_files_idx,
""",
        ),
        (
            """    call ResolveComplexContig.ResolveComplexSv as ResolveCpxInv {
      input:
        vcf=SubsetInversions.filtered_vcf,
        vcf_idx=SubsetInversions.filtered_vcf_idx,
""",
            """    call ResolveComplexContig.ResolveComplexSv as ResolveCpxInv {
      input:
        vcf=SubsetInversions.filtered_vcf,
        vcf_idx=SubsetInversions.filtered_vcf_idx,
        cytobands_idx=cytobands_idx,
        pe_exclude_list_idx=pe_exclude_list_idx,
        disc_files_idx=disc_files_idx,
""",
        ),
        # Add vcf_indexes to ReshardVcf call
        (
            """  call Reshard.ReshardVcf {
    input:
      vcfs=RenameVariants.renamed_vcf,
""",
            """  call Reshard.ReshardVcf {
    input:
      vcfs=RenameVariants.renamed_vcf,
      vcf_indexes=RenameVariants.renamed_vcf_index,
""",
        ),
    ],
    "ReshardVcf.wdl": [
        (RESHARD_INPUT_OLD, RESHARD_INPUT_NEW),
        (RESHARD_SCATTER_OLD, RESHARD_SCATTER_NEW),
    ],
    "GenotypeCpxCnvsPerBatch.wdl": [
        (GCNV_INPUT_OLD, GCNV_INPUT_NEW),
        (GCNV_OLD, GCNV_NEW),
    ],
    "ResolveCpxSv.wdl": [
        (RESOLVECPX_INPUT_OLD, RESOLVECPX_INPUT_NEW),
        (RESOLVECPX_OLD, RESOLVECPX_NEW),
        # Inner SvtkResolve task: also make cytobands_idx / pe_exclude_list_idx optional
        (
            """    File cytobands
    File cytobands_idx
    File mei_bed
    File pe_exclude_list
    File pe_exclude_list_idx
""",
            """    File cytobands
    File? cytobands_idx
    File mei_bed
    File pe_exclude_list
    File? pe_exclude_list_idx
""",
        ),
        # RestoreUnresolvedCnv: `mv` of input also fails on bind mount.
        # Copy first, then use the local copy. (rm is handled by global pass.)
        (
            """    mv ~{resolved_vcf} ~{resolved_plus_cnv}.tmp.gz""",
            """    cp ~{resolved_vcf} ~{resolved_plus_cnv}.tmp.gz""",
        ),
    ],
    "GenotypeCpxCnvs.wdl": [
        (
            "    Array[File] coverage_files\n",
            "    Array[File] coverage_files\n    Array[File] coverage_files_idx\n",
        ),
        (
            "        coverage_file=coverage_files[i],\n",
            "        coverage_file=coverage_files[i],\n        coverage_file_idx=coverage_files_idx[i],\n",
        ),
    ],
    "ScatterCpxGenotyping.wdl": [
        (
            "    Array[File] coverage_files\n",
            "    Array[File] coverage_files\n    Array[File] coverage_files_idx\n",
        ),
        (
            "        coverage_files=coverage_files,\n",
            "        coverage_files=coverage_files,\n        coverage_files_idx=coverage_files_idx,\n",
        ),
    ],
    "GenotypeComplexVariants.wdl": [
        (
            "    Array[File] bincov_files\n",
            "    Array[File] bincov_files\n    Array[File] bincov_files_idx\n",
        ),
        (
            "        coverage_files=bincov_files,\n",
            "        coverage_files=bincov_files,\n        coverage_files_idx=bincov_files_idx,\n",
        ),
    ],
    "MainVcfQc.wdl": [
        (
            """    Array[File] vcfs  # Option to provide a single GATK-SV VCF or an array of position-sharded SV VCFs. Must be indexed
""",
            """    Array[File] vcfs  # Option to provide a single GATK-SV VCF or an array of position-sharded SV VCFs. Must be indexed
    Array[File] vcf_indexes
""",
        ),
        (
            """    scatter ( vcf in vcfs ) {
      call Utils.SubsetVcfBySamplesList {
        input:
          vcf=vcf,
          vcf_idx=vcf + \".tbi\",
""",
            """    scatter ( i_subset in range(length(vcfs)) ) {
      File subset_vcf = vcfs[i_subset]
      call Utils.SubsetVcfBySamplesList {
        input:
          vcf=subset_vcf,
          vcf_idx=vcf_indexes[i_subset],
""",
        ),
        # vcfs_for_qc is the post-subset array; we need a parallel
        # vcf_indexes_for_qc derived the same way (subset adds its own
        # .vcf_subset_idx output).
        (
            """  Array[File] vcfs_for_qc = select_first([SubsetVcfBySamplesList.vcf_subset, vcfs])
""",
            """  Array[File] vcfs_for_qc = select_first([SubsetVcfBySamplesList.vcf_subset, vcfs])
  Array[File] vcf_indexes_for_qc = select_first([SubsetVcfBySamplesList.vcf_subset_index, vcf_indexes])
""",
        ),
        # Pass vcf_indexes_for_qc to CollectQcVcfWide
        (
            """    call vcfwideqc.CollectQcVcfWide {
      input:
        vcfs=vcfs_for_qc,
""",
            """    call vcfwideqc.CollectQcVcfWide {
      input:
        vcfs=vcfs_for_qc,
        vcf_indexes=vcf_indexes_for_qc,
""",
        ),
    ],
    "CollectQcVcfWide.wdl": [
        (
            """    Array[File] vcfs
""",
            """    Array[File] vcfs
    Array[File] vcf_indexes
""",
        ),
        (
            """  scatter ( vcf in vcfs ) {
    call MiniTasks.ScatterVcf {
      input:
        vcf=vcf,
        vcf_index=vcf + \".tbi\",
""",
            """  scatter ( i_collect in range(length(vcfs)) ) {
    File collect_vcf = vcfs[i_collect]
    call MiniTasks.ScatterVcf {
      input:
        vcf=collect_vcf,
        vcf_index=vcf_indexes[i_collect],
""",
        ),
    ],
}


def apply_patches(text: str, patches: list[tuple[str, str]], path: Path) -> str:
    """Apply patches in order. Each tuple is (old, new). Replace exactly once."""
    for i, (old, new) in enumerate(patches):
        if old not in text:
            raise SystemExit(
                f"PATCH {i} for {path.name} not found:\n--- expected ---\n{old}"
            )
        # Replace the FIRST occurrence (so duplicate patches work)
        idx = text.find(old)
        text = text[:idx] + new + text[idx + len(old):]
    return text


# Generic post-pass: every task body that does
#   rm <some-path>
#   rm "<some-path>"
#   rm -f <some-path>
# where the path could be a WDL-localised input gets the safer
# `rm -f <path> 2>/dev/null || true` so miniwdl's read-only bind
# mounts don't fail the task.
RM_PATTERNS = [
    (re.compile(r'^(\s*)rm "(~\{[^}]+\})"$', re.MULTILINE),
     r'\1rm -f "\2" 2>/dev/null || true'),
    (re.compile(r'^(\s*)rm (~\{[^}]+\})$', re.MULTILINE),
     r'\1rm -f \2 2>/dev/null || true'),
    (re.compile(r'^(\s*)rm -f (~\{[^}]+\})$', re.MULTILINE),
     r'\1rm -f \2 2>/dev/null || true'),
    # Hardcoded `rm -f /mnt/miniwdl_task_container/work/_miniwdl_inputs/...`
    # appears in some scripts that already partially-resolved paths; harden.
    (re.compile(r'^(\s*)rm -f /mnt/miniwdl_task_container/work/_miniwdl_inputs/0/([^\s]+)$', re.MULTILINE),
     r'\1rm -f /mnt/miniwdl_task_container/work/_miniwdl_inputs/0/\2 2>/dev/null || true'),
]


def soften_rm_of_inputs(text: str) -> str:
    for pat, repl in RM_PATTERNS:
        text = pat.sub(repl, text)
    return text


def patch_top_level_workflow(remaining_steps_wdl: Path) -> None:
    """Update MakeCohortVcfRemainingSteps.wdl to thread the new index args."""
    text = remaining_steps_wdl.read_text()

    # Need to declare the new indexes-related params, and pass them through.
    # Add to inputs (after cluster_background_fail_lists):
    inp_old = "    Array[File] cluster_background_fail_lists\n"
    inp_new = (
        "    Array[File] cluster_background_fail_lists\n"
        "    File? cytobands_idx\n"
        "    File? pe_exclude_list_idx\n"
        "    Array[File] disc_files_idx\n"
    )
    if inp_old not in text:
        raise SystemExit("MakeCohortVcfRemainingSteps inputs block not found")
    text = text.replace(inp_old, inp_new, 1)

    # Add cluster_vcf_indexes -> ResolveComplexVariants
    call_old = (
        "      cluster_vcfs=combined_vcfs,\n"
        "      cluster_bothside_pass_lists=cluster_bothside_pass_lists,\n"
    )
    call_new = (
        "      cluster_vcfs=combined_vcfs,\n"
        "      cluster_vcf_indexes=combined_vcf_indexes,\n"
        "      cytobands_idx=cytobands_idx,\n"
        "      pe_exclude_list_idx=pe_exclude_list_idx,\n"
        "      disc_files_idx=disc_files_idx,\n"
        "      cluster_bothside_pass_lists=cluster_bothside_pass_lists,\n"
    )
    if call_old not in text:
        raise SystemExit("MakeCohortVcfRemainingSteps RC call wiring not found")
    text = text.replace(call_old, call_new, 1)

    # Add bincov_files_idx to GenotypeComplexVariants call
    gcv_old = "      bincov_files=bincov_files,\n"
    gcv_new = (
        "      bincov_files=bincov_files,\n"
        "      bincov_files_idx=bincov_files_idx,\n"
    )
    if gcv_old not in text:
        raise SystemExit("MakeCohortVcfRemainingSteps GenotypeComplexVariants call not found")
    text = text.replace(gcv_old, gcv_new, 1)

    # Add bincov_files_idx as top-level input
    inp2_old = "    Array[File] bincov_files\n"
    inp2_new = (
        "    Array[File] bincov_files\n"
        "    Array[File] bincov_files_idx\n"
    )
    if inp2_old not in text:
        raise SystemExit("MakeCohortVcfRemainingSteps bincov_files input not found")
    text = text.replace(inp2_old, inp2_new, 1)

    # Pass vcf_indexes to MainVcfQc
    qc_old = "      vcfs=[CleanVcf.cleaned_vcf],\n"
    qc_new = (
        "      vcfs=[CleanVcf.cleaned_vcf],\n"
        "      vcf_indexes=[CleanVcf.cleaned_vcf_index],\n"
    )
    if qc_old not in text:
        raise SystemExit("MakeCohortVcfRemainingSteps MainVcfQc call not found")
    text = text.replace(qc_old, qc_new, 1)

    remaining_steps_wdl.write_text(text)


def main() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    shutil.copytree(SRC_DIR, OUT_DIR)

    wdl_dir = OUT_DIR / "wdl"

    for filename, patches in PATCHES.items():
        path = wdl_dir / filename
        text = path.read_text()
        text = apply_patches(text, patches, path)
        path.write_text(text)
        print(f"  patched {filename}")

    # Apply the `rm` softening pass to every WDL file. miniwdl's bind
    # mounts of input files reject rm with EBUSY; Cromwell tolerates it
    # by copying inputs. We make every `rm` of a WDL placeholder safe.
    for path in sorted(wdl_dir.iterdir()):
        if path.suffix != ".wdl":
            continue
        original = path.read_text()
        softened = soften_rm_of_inputs(original)
        if softened != original:
            path.write_text(softened)
            n_changes = sum(
                1 for _ in re.finditer(
                    r"2>/dev/null \|\| true", softened
                )
            ) - sum(
                1 for _ in re.finditer(
                    r"2>/dev/null \|\| true", original
                )
            )
            print(f"  softened {n_changes} rm(s) in {path.name}")

    patch_top_level_workflow(wdl_dir / "MakeCohortVcfRemainingSteps.wdl")
    print("  patched MakeCohortVcfRemainingSteps.wdl")

    if BUNDLE.exists():
        BUNDLE.unlink()
    subprocess.run(
        ["zip", "-q", "-r", str(BUNDLE.resolve()), "wdl/"],
        cwd=OUT_DIR,
        check=True,
    )
    print(f"\n✓ Bundle: {BUNDLE} ({BUNDLE.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
