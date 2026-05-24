#!/usr/bin/env python3
"""Build MakeCohortVcf-RemainingSteps bundle.

Skips the CombineBatches sub-workflow (which fails on HealthOmics due to
the 47-s GroupedSVCluster kill) and runs every step downstream of it on
HealthOmics, taking the EC2-produced CombineBatches outputs as inputs.

Sub-workflows still run on HealthOmics:
  ResolveComplexVariants
  GenotypeComplexVariants
  CleanVcf
  MainVcfQc

Inputs:
  Array[File] combined_vcfs                   <- EC2 svtk_formatted VCFs (24 contigs)
  Array[File] combined_vcf_indexes            <- .tbi
  Array[File] cluster_bothside_pass_lists     <- EC2 bothsides_sr_support.txt (24 contigs)
  Array[File] cluster_background_fail_lists   <- EC2 high_sr_background.txt (24 contigs)

CombineBatches-only inputs are dropped:
  - clustering_config_part1/2, stratification_config_part1/2
  - track_simrep* / track_segdups* / track_rmsk*
  - min_sr_background_fail_batches
  - track-related runtime overrides
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

SRC_DIR = Path("/tmp/mcv-v16-out")  # the v16-patched WDL (flat tracks)
OUT_DIR = Path("/tmp/mcv-remaining-steps")
BUNDLE = Path(
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/"
    "MakeCohortVcf-RemainingSteps-bundle.zip"
)


REMAINING_STEPS_WDL = r'''version 1.0

import "ResolveComplexVariants.wdl" as ComplexResolve
import "GenotypeComplexVariants.wdl" as ComplexGenotype
import "CleanVcf.wdl" as Clean
import "MainVcfQc.wdl" as VcfQc

# Skips CombineBatches; takes its outputs (produced on EC2 because
# HealthOmics terminates GroupedSVCluster at 47 s) as inputs.
workflow MakeCohortVcfRemainingSteps {
  input {
    String cohort_name
    Array[String] batches
    File ped_file

    # ---- Pre-computed CombineBatches outputs (from EC2) ----
    Array[File] combined_vcfs
    Array[File] combined_vcf_indexes
    Array[File] cluster_bothside_pass_lists
    Array[File] cluster_background_fail_lists

    # ---- Standard per-batch inputs ----
    Array[File] depth_vcfs
    Array[File] disc_files
    Array[File] bincov_files
    Array[File] genotyping_rd_tables
    Array[File] median_coverage_files
    Array[File] rf_cutoff_files

    # ---- References ----
    File reference_dict
    File bin_exclude
    File contig_list
    File allosome_fai
    File cytobands
    File mei_bed
    File pe_exclude_list
    File HERVK_reference
    File LINE1_reference
    File intron_reference

    Int max_shard_size_resolve
    Int? max_samples_per_shard_clean_vcf_step3

    String chr_x
    String chr_y

    File? outlier_samples_list
    Int? random_seed
    Int? max_gq

    Boolean merge_complex_resolve_vcfs = false
    Boolean merge_complex_genotype_vcfs = false

    Array[Array[String]]? site_level_comparison_datasets
    Array[Array[String]]? sample_level_comparison_datasets
    File? sample_renaming_tsv

    Boolean? run_module_metrics
    File? primary_contigs_list
    File? baseline_complex_resolve_vcf
    File? baseline_complex_genotype_vcf
    File? baseline_cleaned_vcf

    String linux_docker
    String gatk_docker
    String sv_base_mini_docker
    String sv_pipeline_docker
    String sv_pipeline_qc_docker

    # Runtime overrides we still expose
    RuntimeAttr? runtime_attr_create_ploidy
    RuntimeAttr? runtime_override_integrate_resolved_vcfs
    RuntimeAttr? runtime_override_rename_variants
    RuntimeAttr? runtime_override_breakpoint_overlap_filter
    RuntimeAttr? runtime_override_subset_inversions
    RuntimeAttr? runtime_override_update_sr_list_pass
    RuntimeAttr? runtime_override_update_sr_list_fail
    RuntimeAttr? runtime_override_concat_resolve
    RuntimeAttr? runtime_override_concat_bothside_pass
    RuntimeAttr? runtime_override_concat_background_fail
    RuntimeAttr? runtime_override_get_se_cutoff
    RuntimeAttr? runtime_override_shard_vcf_cpx
    RuntimeAttr? runtime_override_shard_vids_resolve
    RuntimeAttr? runtime_override_resolve_prep
    RuntimeAttr? runtime_override_resolve_cpx_per_shard
    RuntimeAttr? runtime_override_restore_unresolved_cnv_per_shard
    RuntimeAttr? runtime_override_concat_resolved_per_shard
    RuntimeAttr? runtime_override_pull_vcf_shard
    RuntimeAttr? runtime_override_preconcat_resolve
    RuntimeAttr? runtime_override_fix_header_resolve
    RuntimeAttr? runtime_override_get_se_cutoff_inv
    RuntimeAttr? runtime_override_shard_vcf_cpx_inv
    RuntimeAttr? runtime_override_shard_vids_resolve_inv
    RuntimeAttr? runtime_override_resolve_prep_inv
    RuntimeAttr? runtime_override_resolve_cpx_per_shard_inv
    RuntimeAttr? runtime_override_restore_unresolved_cnv_per_shard_inv
    RuntimeAttr? runtime_override_concat_resolved_per_shard_inv
    RuntimeAttr? runtime_override_pull_vcf_shard_inv
    RuntimeAttr? runtime_override_preconcat_resolve_inv
    RuntimeAttr? runtime_override_fix_header_resolve_inv
    RuntimeAttr? runtime_override_reshard
    RuntimeAttr? runtime_override_ids_from_median
    RuntimeAttr? runtime_override_split_vcf_to_genotype
    RuntimeAttr? runtime_override_concat_cpx_cnv_vcfs
    RuntimeAttr? runtime_override_get_cpx_cnv_intervals
    RuntimeAttr? runtime_override_parse_genotypes
    RuntimeAttr? runtime_override_merge_melted_gts
    RuntimeAttr? runtime_override_split_bed_by_size
    RuntimeAttr? runtime_override_rd_genotype
    RuntimeAttr? runtime_override_concat_melted_genotypes
    RuntimeAttr? runtime_attr_ids_from_vcf_regeno
    RuntimeAttr? runtime_attr_subset_ped_regeno
    RuntimeAttr? runtime_override_preconcat_regeno
    RuntimeAttr? runtime_override_fix_header_regeno
    RuntimeAttr? runtime_attr_format_to_clean_create_ploidy
    RuntimeAttr? runtime_attr_format_to_clean_scatter
    RuntimeAttr? runtime_attr_format_to_clean_format
    RuntimeAttr? runtime_attr_format_to_clean_concat
    RuntimeAttr? runtime_attr_scatter_preprocess
    RuntimeAttr? runtime_attr_preprocess
    RuntimeAttr? runtime_attr_concat_preprocess
    RuntimeAttr? runtime_attr_revise_overlapping_cnvs
    RuntimeAttr? runtime_attr_revise_large_cnvs
    RuntimeAttr? runtime_attr_revise_multiallelics
    RuntimeAttr? runtime_attr_scatter_postprocess
    RuntimeAttr? runtime_attr_postprocess
    RuntimeAttr? runtime_attr_concat_postprocess
    RuntimeAttr? runtime_override_drop_redundant_cnvs
    RuntimeAttr? runtime_override_sort_drop_redundant_cnvs
    RuntimeAttr? runtime_override_stitch_fragmented_cnvs
    RuntimeAttr? runtime_override_rescue_me_dels
    RuntimeAttr? runtime_attr_add_high_fp_rate_filters
    RuntimeAttr? runtime_attr_add_retro_del_filters
    RuntimeAttr? runtime_override_final_cleanup
    RuntimeAttr? runtime_attr_format_to_output_create_ploidy
    RuntimeAttr? runtime_attr_format_to_output_scatter
    RuntimeAttr? runtime_attr_format_to_output_format
    RuntimeAttr? runtime_attr_format_to_output_concat
    RuntimeAttr? runtime_override_concat_cleaned_vcfs
    RuntimeAttr? runtime_override_site_level_benchmark_plot
    RuntimeAttr? runtime_override_per_sample_benchmark_plot
    RuntimeAttr? runtime_override_subset_vcf
    RuntimeAttr? runtime_override_preprocess_vcf
    RuntimeAttr? runtime_override_site_level_benchmark
    RuntimeAttr? runtime_override_merge_site_level_benchmark
    RuntimeAttr? runtime_override_merge_sharded_per_sample_vid_lists
    RuntimeAttr? runtime_override_plot_qc_vcf_wide
    RuntimeAttr? runtime_override_plot_qc_per_sample
    RuntimeAttr? runtime_override_plot_qc_per_family
    RuntimeAttr? runtime_override_sanitize_outputs
    RuntimeAttr? runtime_override_merge_vcfwide_stat_shards
    RuntimeAttr? runtime_override_merge_vcf_2_bed
    RuntimeAttr? runtime_override_collect_sharded_vcf_stats
    RuntimeAttr? runtime_override_svtk_vcf_2_bed
    RuntimeAttr? runtime_override_scatter_vcf
    RuntimeAttr? runtime_override_merge_subvcf_stat_shards
    RuntimeAttr? runtime_override_collect_vids_per_sample
    RuntimeAttr? runtime_override_split_samples_list
    RuntimeAttr? runtime_override_tar_shard_vid_lists
    RuntimeAttr? runtime_override_benchmark_samples
    RuntimeAttr? runtime_override_split_shuffled_list
    RuntimeAttr? runtime_override_merge_and_tar_shard_benchmarks
  }

  call ComplexResolve.ResolveComplexVariants {
    input:
      cohort_name=cohort_name,
      merge_vcfs=merge_complex_resolve_vcfs,
      cluster_vcfs=combined_vcfs,
      cluster_bothside_pass_lists=cluster_bothside_pass_lists,
      cluster_background_fail_lists=cluster_background_fail_lists,
      disc_files=disc_files,
      rf_cutoff_files=rf_cutoff_files,
      contig_list=contig_list,
      cytobands=cytobands,
      mei_bed=mei_bed,
      pe_exclude_list=pe_exclude_list,
      ref_dict=reference_dict,
      max_shard_size=max_shard_size_resolve,
      sv_base_mini_docker=sv_base_mini_docker,
      sv_pipeline_docker=sv_pipeline_docker,
      runtime_override_update_sr_list_pass=runtime_override_update_sr_list_pass,
      runtime_override_update_sr_list_fail=runtime_override_update_sr_list_fail,
      runtime_override_integrate_resolved_vcfs=runtime_override_integrate_resolved_vcfs,
      runtime_override_rename_variants=runtime_override_rename_variants,
      runtime_override_breakpoint_overlap_filter=runtime_override_breakpoint_overlap_filter,
      runtime_override_subset_inversions=runtime_override_subset_inversions,
      runtime_override_concat=runtime_override_concat_resolve,
      runtime_override_concat_bothside_pass=runtime_override_concat_bothside_pass,
      runtime_override_concat_background_fail=runtime_override_concat_background_fail,
      runtime_override_get_se_cutoff=runtime_override_get_se_cutoff,
      runtime_override_shard_vcf_cpx=runtime_override_shard_vcf_cpx,
      runtime_override_shard_vids=runtime_override_shard_vids_resolve,
      runtime_override_resolve_prep=runtime_override_resolve_prep,
      runtime_override_resolve_cpx_per_shard=runtime_override_resolve_cpx_per_shard,
      runtime_override_restore_unresolved_cnv_per_shard=runtime_override_restore_unresolved_cnv_per_shard,
      runtime_override_concat_resolved_per_shard=runtime_override_concat_resolved_per_shard,
      runtime_override_pull_vcf_shard=runtime_override_pull_vcf_shard,
      runtime_override_preconcat=runtime_override_preconcat_resolve,
      runtime_override_fix_header=runtime_override_fix_header_resolve,
      runtime_override_get_se_cutoff_inv=runtime_override_get_se_cutoff_inv,
      runtime_override_shard_vcf_cpx_inv=runtime_override_shard_vcf_cpx_inv,
      runtime_override_shard_vids_inv=runtime_override_shard_vids_resolve_inv,
      runtime_override_resolve_prep_inv=runtime_override_resolve_prep_inv,
      runtime_override_resolve_cpx_per_shard_inv=runtime_override_resolve_cpx_per_shard_inv,
      runtime_override_restore_unresolved_cnv_per_shard_inv=runtime_override_restore_unresolved_cnv_per_shard_inv,
      runtime_override_concat_resolved_per_shard_inv=runtime_override_concat_resolved_per_shard_inv,
      runtime_override_pull_vcf_shard_inv=runtime_override_pull_vcf_shard_inv,
      runtime_override_preconcat_inv=runtime_override_preconcat_resolve_inv,
      runtime_override_fix_header_inv=runtime_override_fix_header_resolve_inv
  }

  call ComplexGenotype.GenotypeComplexVariants {
    input:
      cohort_name=cohort_name,
      batches=batches,
      merge_vcfs=merge_complex_genotype_vcfs,
      complex_resolve_vcfs=ResolveComplexVariants.complex_resolve_vcfs,
      complex_resolve_vcf_indexes=ResolveComplexVariants.complex_resolve_vcf_indexes,
      depth_vcfs=depth_vcfs,
      ped_file=ped_file,
      bincov_files=bincov_files,
      genotyping_rd_tables=genotyping_rd_tables,
      median_coverage_files=median_coverage_files,
      bin_exclude=bin_exclude,
      contig_list=contig_list,
      ref_dict=reference_dict,
      linux_docker=linux_docker,
      sv_base_mini_docker=sv_base_mini_docker,
      sv_pipeline_docker=sv_pipeline_docker,
      runtime_override_ids_from_median=runtime_override_ids_from_median,
      runtime_override_split_vcf_to_genotype=runtime_override_split_vcf_to_genotype,
      runtime_override_concat_cpx_cnv_vcfs=runtime_override_concat_cpx_cnv_vcfs,
      runtime_override_get_cpx_cnv_intervals=runtime_override_get_cpx_cnv_intervals,
      runtime_override_parse_genotypes=runtime_override_parse_genotypes,
      runtime_override_merge_melted_gts=runtime_override_merge_melted_gts,
      runtime_override_split_bed_by_size=runtime_override_split_bed_by_size,
      runtime_override_rd_genotype=runtime_override_rd_genotype,
      runtime_override_concat_melted_genotypes=runtime_override_concat_melted_genotypes,
      runtime_attr_ids_from_vcf=runtime_attr_ids_from_vcf_regeno,
      runtime_attr_subset_ped=runtime_attr_subset_ped_regeno,
      runtime_override_preconcat=runtime_override_preconcat_regeno,
      runtime_override_fix_header=runtime_override_fix_header_regeno
  }

  call Clean.CleanVcf {
    input:
      cohort_name=cohort_name,
      complex_genotype_vcfs=GenotypeComplexVariants.complex_genotype_vcfs,
      complex_resolve_bothside_pass_list=ResolveComplexVariants.complex_resolve_bothside_pass_list,
      complex_resolve_background_fail_list=ResolveComplexVariants.complex_resolve_background_fail_list,
      ped_file=ped_file,
      contig_list=contig_list,
      allosome_fai=allosome_fai,
      chr_x=chr_x,
      chr_y=chr_y,
      HERVK_reference=HERVK_reference,
      LINE1_reference=LINE1_reference,
      intron_reference=intron_reference,
      outlier_samples_list=outlier_samples_list,
      baseline_complex_resolve_vcf=baseline_complex_resolve_vcf,
      baseline_complex_genotype_vcf=baseline_complex_genotype_vcf,
      baseline_cleaned_vcf=baseline_cleaned_vcf,
      primary_contigs_list=primary_contigs_list,
      run_module_metrics=run_module_metrics,
      gatk_docker=gatk_docker,
      linux_docker=linux_docker,
      sv_base_mini_docker=sv_base_mini_docker,
      sv_pipeline_docker=sv_pipeline_docker,
      runtime_attr_create_ploidy=runtime_attr_create_ploidy,
      runtime_attr_format_to_clean_create_ploidy=runtime_attr_format_to_clean_create_ploidy,
      runtime_attr_format_to_clean_scatter=runtime_attr_format_to_clean_scatter,
      runtime_attr_format_to_clean_format=runtime_attr_format_to_clean_format,
      runtime_attr_format_to_clean_concat=runtime_attr_format_to_clean_concat,
      runtime_attr_scatter_preprocess=runtime_attr_scatter_preprocess,
      runtime_attr_preprocess=runtime_attr_preprocess,
      runtime_attr_concat_preprocess=runtime_attr_concat_preprocess,
      runtime_attr_revise_overlapping_cnvs=runtime_attr_revise_overlapping_cnvs,
      runtime_attr_revise_large_cnvs=runtime_attr_revise_large_cnvs,
      runtime_attr_revise_multiallelics=runtime_attr_revise_multiallelics,
      runtime_attr_scatter_postprocess=runtime_attr_scatter_postprocess,
      runtime_attr_postprocess=runtime_attr_postprocess,
      runtime_attr_concat_postprocess=runtime_attr_concat_postprocess,
      runtime_override_drop_redundant_cnvs=runtime_override_drop_redundant_cnvs,
      runtime_override_sort_drop_redundant_cnvs=runtime_override_sort_drop_redundant_cnvs,
      runtime_override_stitch_fragmented_cnvs=runtime_override_stitch_fragmented_cnvs,
      runtime_override_rescue_me_dels=runtime_override_rescue_me_dels,
      runtime_attr_add_high_fp_rate_filters=runtime_attr_add_high_fp_rate_filters,
      runtime_attr_add_retro_del_filters=runtime_attr_add_retro_del_filters,
      runtime_override_final_cleanup=runtime_override_final_cleanup,
      runtime_attr_format_to_output_create_ploidy=runtime_attr_format_to_output_create_ploidy,
      runtime_attr_format_to_output_scatter=runtime_attr_format_to_output_scatter,
      runtime_attr_format_to_output_format=runtime_attr_format_to_output_format,
      runtime_attr_format_to_output_concat=runtime_attr_format_to_output_concat,
      runtime_override_concat_cleaned_vcfs=runtime_override_concat_cleaned_vcfs
  }

  call VcfQc.MainVcfQc {
    input:
      vcfs=[CleanVcf.cleaned_vcf],
      ped_file=ped_file,
      prefix="~{cohort_name}.cleaned",
      sv_per_shard=2500,
      samples_per_shard=600,
      site_level_comparison_datasets=site_level_comparison_datasets,
      sample_level_comparison_datasets=sample_level_comparison_datasets,
      sample_renaming_tsv=sample_renaming_tsv,
      primary_contigs_fai=contig_list,
      random_seed=random_seed,
      sv_pipeline_qc_docker=sv_pipeline_qc_docker,
      sv_base_mini_docker=sv_base_mini_docker,
      sv_pipeline_docker=sv_pipeline_docker,
      runtime_override_site_level_benchmark_plot=runtime_override_site_level_benchmark_plot,
      runtime_override_per_sample_benchmark_plot=runtime_override_per_sample_benchmark_plot,
      runtime_override_subset_vcf=runtime_override_subset_vcf,
      runtime_override_preprocess_vcf=runtime_override_preprocess_vcf,
      runtime_override_site_level_benchmark=runtime_override_site_level_benchmark,
      runtime_override_merge_site_level_benchmark=runtime_override_merge_site_level_benchmark,
      runtime_override_merge_sharded_per_sample_vid_lists=runtime_override_merge_sharded_per_sample_vid_lists,
      runtime_override_plot_qc_vcf_wide=runtime_override_plot_qc_vcf_wide,
      runtime_override_plot_qc_per_sample=runtime_override_plot_qc_per_sample,
      runtime_override_plot_qc_per_family=runtime_override_plot_qc_per_family,
      runtime_override_sanitize_outputs=runtime_override_sanitize_outputs,
      runtime_override_merge_vcfwide_stat_shards=runtime_override_merge_vcfwide_stat_shards,
      runtime_override_merge_vcf_2_bed=runtime_override_merge_vcf_2_bed,
      runtime_override_collect_sharded_vcf_stats=runtime_override_collect_sharded_vcf_stats,
      runtime_override_svtk_vcf_2_bed=runtime_override_svtk_vcf_2_bed,
      runtime_override_scatter_vcf=runtime_override_scatter_vcf,
      runtime_override_merge_subvcf_stat_shards=runtime_override_merge_subvcf_stat_shards,
      runtime_override_collect_vids_per_sample=runtime_override_collect_vids_per_sample,
      runtime_override_split_samples_list=runtime_override_split_samples_list,
      runtime_override_tar_shard_vid_lists=runtime_override_tar_shard_vid_lists,
      runtime_override_benchmark_samples=runtime_override_benchmark_samples,
      runtime_override_split_shuffled_list=runtime_override_split_shuffled_list,
      runtime_override_merge_and_tar_shard_benchmarks=runtime_override_merge_and_tar_shard_benchmarks
  }

  output {
    File vcf = CleanVcf.cleaned_vcf
    File vcf_index = CleanVcf.cleaned_vcf_index
    File vcf_qc = MainVcfQc.sv_vcf_qc_output

    Array[File] complex_resolve_vcfs = ResolveComplexVariants.complex_resolve_vcfs
    Array[File] complex_resolve_vcf_indexes = ResolveComplexVariants.complex_resolve_vcf_indexes
    File complex_resolve_bothside_pass_list = ResolveComplexVariants.complex_resolve_bothside_pass_list
    File complex_resolve_background_fail_list = ResolveComplexVariants.complex_resolve_background_fail_list
    Array[File] breakpoint_overlap_dropped_record_vcfs = ResolveComplexVariants.breakpoint_overlap_dropped_record_vcfs
    Array[File] breakpoint_overlap_dropped_record_vcf_indexes = ResolveComplexVariants.breakpoint_overlap_dropped_record_vcf_indexes

    Array[File] complex_genotype_vcfs = GenotypeComplexVariants.complex_genotype_vcfs
    Array[File] complex_genotype_vcf_indexes = GenotypeComplexVariants.complex_genotype_vcfs

    File? metrics_file_makecohortvcf = CleanVcf.metrics_file_makecohortvcf
  }
}
'''


def main() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    # Copy every WDL except MakeCohortVcf.wdl and CombineBatches.wdl
    src_wdl = SRC_DIR / "wdl"
    dst_wdl = OUT_DIR / "wdl"
    dst_wdl.mkdir()
    skip = {"MakeCohortVcf.wdl", "CombineBatches.wdl"}
    for p in src_wdl.iterdir():
        if p.name in skip:
            continue
        shutil.copy2(p, dst_wdl / p.name)

    # Write the new top-level workflow
    (dst_wdl / "MakeCohortVcfRemainingSteps.wdl").write_text(REMAINING_STEPS_WDL)

    # Sanity-check the imports we still rely on
    must_have = {
        "ResolveComplexVariants.wdl",
        "GenotypeComplexVariants.wdl",
        "CleanVcf.wdl",
        "MainVcfQc.wdl",
        "Structs.wdl",
    }
    have = {p.name for p in dst_wdl.iterdir()}
    missing = must_have - have
    assert not missing, f"missing required WDLs: {missing}"
    print(f"  wrote {len(have)} WDL files into {dst_wdl}")

    # Build bundle
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
