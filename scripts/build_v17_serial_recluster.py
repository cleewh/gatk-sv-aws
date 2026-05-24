#!/usr/bin/env python3
"""Build MakeCohortVcf v17 — serialize the failing recluster step.

Diagnosis from v16 run 1041904:
  - 24 ClusterSitesAndGroupedCluster tasks scattered concurrently all
    got "Terminated" after ~3 min into GATK execution.
  - The single-task diagnostic (run 5601461) ran the same GATK call to
    completion on HealthOmics in 44s.
  - Conclusion: HealthOmics terminates the scatter under concurrent
    pressure (FUSE / preemption / autoscaling — root cause unclear, but
    serializing avoids it).

v17 keeps the 24-way JoinVcfs scatter (works fine), then collapses the
ClusterSitesAndGroupedCluster scatter into one task that loops over all
contigs sequentially. Downstream GatkToSvtkVcf and ExtractSRVariantLists
still scatter (small/fast tasks).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SOURCE_DIR = Path("/tmp/mcv-v16-out")  # v16-patched (flat tracks)
OUT_DIR = Path("/tmp/mcv-v17-out")
BUNDLE_PATH = Path(
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v17.zip"
)


# Replace the workflow scatter body. The old shape is:
#   scatter (contig in contigs) {
#     call SVCluster as JoinVcfs { ... contig=contig ... }
#     call ClusterSitesAndGroupedCluster { vcf=JoinVcfs.out, contig=contig, ... }
#     call GatkToSvtkVcf { vcf=ClusterSitesAndGroupedCluster.out, ... }
#     call ExtractSRVariantLists { vcf=ClusterSitesAndGroupedCluster.out, ... }
#   }
#
# New shape:
#   scatter (contig in contigs) {
#     call SVCluster as JoinVcfs { ... contig=contig ... }
#   }
#   call AllContigsClusterAndGroup {
#     join_vcfs=JoinVcfs.out, contigs=contigs, ...
#   }
#   scatter (i in range(length(contigs))) {
#     String contig = contigs[i]
#     call GatkToSvtkVcf { vcf=AllContigsClusterAndGroup.reclustered_vcfs[i], ... }
#     call ExtractSRVariantLists { vcf=AllContigsClusterAndGroup.reclustered_vcfs[i], ... }
#   }


OLD_SCATTER = """  #Scatter per chromosome
  Array[String] contigs = transpose(read_tsv(contig_list))[0]
  scatter ( contig in contigs ) {

    # Naively join across batches
    call ClusterTasks.SVCluster as JoinVcfs {
      input:
        vcfs=flatten([SetSRVariantFlags.out, depth_vcfs]),
        ploidy_table=CreatePloidyTableFromPed.out,
        output_prefix="~{cohort_name}.combine_batches.~{contig}.join_vcfs",
        contig=contig,
        fast_mode=false,
        pesr_sample_overlap=0,
        pesr_interval_overlap=1,
        pesr_breakend_window=0,
        depth_sample_overlap=0,
        depth_interval_overlap=1,
        depth_breakend_window=0,
        mixed_sample_overlap=0,
        mixed_interval_overlap=1,
        mixed_breakend_window=0,
        reference_fasta=reference_fasta,
        reference_fasta_fai=reference_fasta_fai,
        reference_dict=reference_dict,
        java_mem_fraction=java_mem_fraction,
        gatk_docker=gatk_docker,
        runtime_attr_override=runtime_attr_join_vcfs
    }

    # Combined: ClusterSites + GroupedSVClusterPart1 + GroupedSVClusterPart2
    # (Combined into single task to avoid HealthOmics FUSE I/O issues with intermediate VCFs)
    call ClusterSitesAndGroupedCluster {
      input:
        vcf=JoinVcfs.out,
        ploidy_table=CreatePloidyTableFromPed.out,
        output_prefix="~{cohort_name}.combine_batches.~{contig}",
        contig=contig,
        cohort_name=cohort_name,
        reference_fasta=reference_fasta,
        reference_fasta_fai=reference_fasta_fai,
        reference_dict=reference_dict,
        clustering_config_part1=clustering_config_part1,
        stratification_config_part1=stratification_config_part1,
        clustering_config_part2=clustering_config_part2,
        stratification_config_part2=stratification_config_part2,
        track_simrep=track_simrep,
        track_simrep_idx=track_simrep_idx,
        track_segdups=track_segdups,
        track_segdups_idx=track_segdups_idx,
        track_rmsk=track_rmsk,
        track_rmsk_idx=track_rmsk_idx,
        java_mem_fraction=java_mem_fraction,
        gatk_docker=gatk_docker,
        runtime_attr_override=runtime_attr_recluster_part2
    }

    # Use \"depth\" as source to match legacy headers
    # AC/AF cause errors due to being lists instead of single values
    call ClusterTasks.GatkToSvtkVcf {
      input:
        vcf=ClusterSitesAndGroupedCluster.out,
        output_prefix="~{cohort_name}.combine_batches.~{contig}.svtk_formatted",
        source="depth",
        contig_list=contig_list,
        remove_formats="CN,RD_MCR",
        remove_infos="AC,AF,AN,HIGH_SR_BACKGROUND,BOTHSIDES_SUPPORT,SR1POS,SR2POS",
        set_pass=true,
        sv_pipeline_docker=sv_pipeline_docker,
        runtime_attr_override=runtime_attr_gatk_to_svtk_vcf
    }

    call ExtractSRVariantLists {
      input:
        vcf=ClusterSitesAndGroupedCluster.out,
        vcf_index=ClusterSitesAndGroupedCluster.out_index,
        output_prefix="~{cohort_name}.combine_batches.~{contig}",
        sv_base_mini_docker=sv_base_mini_docker,
        runtime_attr_override=runtime_attr_extract_vids_2
    }
  }
"""

NEW_SCATTER = """  #Scatter JoinVcfs per chromosome (works fine on HealthOmics, 24-way parallel)
  Array[String] contigs = transpose(read_tsv(contig_list))[0]
  scatter ( contig in contigs ) {

    # Naively join across batches
    call ClusterTasks.SVCluster as JoinVcfs {
      input:
        vcfs=flatten([SetSRVariantFlags.out, depth_vcfs]),
        ploidy_table=CreatePloidyTableFromPed.out,
        output_prefix="~{cohort_name}.combine_batches.~{contig}.join_vcfs",
        contig=contig,
        fast_mode=false,
        pesr_sample_overlap=0,
        pesr_interval_overlap=1,
        pesr_breakend_window=0,
        depth_sample_overlap=0,
        depth_interval_overlap=1,
        depth_breakend_window=0,
        mixed_sample_overlap=0,
        mixed_interval_overlap=1,
        mixed_breakend_window=0,
        reference_fasta=reference_fasta,
        reference_fasta_fai=reference_fasta_fai,
        reference_dict=reference_dict,
        java_mem_fraction=java_mem_fraction,
        gatk_docker=gatk_docker,
        runtime_attr_override=runtime_attr_join_vcfs
    }
  }

  # SERIAL recluster: 24-way scatter of GroupedSVCluster gets killed by
  # HealthOmics under concurrent FUSE pressure. Run all contigs in one
  # task, sequentially. GATK is deterministic so results are bit-identical.
  call AllContigsClusterAndGroup {
    input:
      join_vcfs=JoinVcfs.out,
      contigs=contigs,
      ploidy_table=CreatePloidyTableFromPed.out,
      cohort_name=cohort_name,
      reference_fasta=reference_fasta,
      reference_fasta_fai=reference_fasta_fai,
      reference_dict=reference_dict,
      clustering_config_part1=clustering_config_part1,
      stratification_config_part1=stratification_config_part1,
      clustering_config_part2=clustering_config_part2,
      stratification_config_part2=stratification_config_part2,
      track_simrep=track_simrep,
      track_simrep_idx=track_simrep_idx,
      track_segdups=track_segdups,
      track_segdups_idx=track_segdups_idx,
      track_rmsk=track_rmsk,
      track_rmsk_idx=track_rmsk_idx,
      java_mem_fraction=java_mem_fraction,
      gatk_docker=gatk_docker,
      runtime_attr_override=runtime_attr_recluster_part2
  }

  # Downstream small/fast tasks scatter again (worked fine on prior runs)
  scatter ( i in range(length(contigs)) ) {
    String contig_i = contigs[i]

    call ClusterTasks.GatkToSvtkVcf {
      input:
        vcf=AllContigsClusterAndGroup.reclustered_vcfs[i],
        output_prefix="~{cohort_name}.combine_batches.~{contig_i}.svtk_formatted",
        source="depth",
        contig_list=contig_list,
        remove_formats="CN,RD_MCR",
        remove_infos="AC,AF,AN,HIGH_SR_BACKGROUND,BOTHSIDES_SUPPORT,SR1POS,SR2POS",
        set_pass=true,
        sv_pipeline_docker=sv_pipeline_docker,
        runtime_attr_override=runtime_attr_gatk_to_svtk_vcf
    }

    call ExtractSRVariantLists {
      input:
        vcf=AllContigsClusterAndGroup.reclustered_vcfs[i],
        vcf_index=AllContigsClusterAndGroup.reclustered_vcf_indexes[i],
        output_prefix="~{cohort_name}.combine_batches.~{contig_i}",
        sv_base_mini_docker=sv_base_mini_docker,
        runtime_attr_override=runtime_attr_extract_vids_2
    }
  }
"""

# New task body to append to CombineBatches.wdl
ALL_CONTIGS_TASK = r"""

# Sequentially run ClusterSites + GroupedSVClusterPart1 + Part2 for every
# contig inside a single container. Avoids HealthOmics terminating
# parallel GroupedSVCluster scatters.
task AllContigsClusterAndGroup {
  input {
    Array[File] join_vcfs
    Array[String] contigs
    File ploidy_table
    String cohort_name

    File reference_fasta
    File reference_fasta_fai
    File reference_dict

    File clustering_config_part1
    File stratification_config_part1
    File clustering_config_part2
    File stratification_config_part2
    File track_simrep
    File track_simrep_idx
    File track_segdups
    File track_segdups_idx
    File track_rmsk
    File track_rmsk_idx

    Float? java_mem_fraction
    String gatk_docker
    RuntimeAttr? runtime_attr_override
  }

  RuntimeAttr default_attr = object {
                               cpu_cores: 4,
                               mem_gb: 16,
                               disk_gb: 200,
                               boot_disk_gb: 10,
                               preemptible_tries: 3,
                               max_retries: 1
                             }
  RuntimeAttr runtime_attr = select_first([runtime_attr_override, default_attr])

  output {
    Array[File] reclustered_vcfs = read_lines("manifest.vcfs.txt")
    Array[File] reclustered_vcf_indexes = read_lines("manifest.tbis.txt")
  }

  command <<<
    set -euxo pipefail

    function getJavaMem() {
      cat /proc/meminfo \
        | awk -v MEM_FIELD="$1" '{
            f[substr($1, 1, length($1)-1)] = $2
          } END {
            printf "%dM", f[MEM_FIELD] * ~{default="0.50" java_mem_fraction} / 1024
          }'
    }
    JVM_MAX_MEM=$(getJavaMem MemTotal)
    echo "JVM memory: $JVM_MAX_MEM"

    TRACK_ARGS="--track-intervals ~{track_simrep} --track-name SR \
--track-intervals ~{track_segdups} --track-name SD \
--track-intervals ~{track_rmsk} --track-name RM"
    echo "Track arguments: $TRACK_ARGS"

    JOIN_VCFS=(~{sep=" " join_vcfs})
    CONTIGS=(~{sep=" " contigs})

    : > manifest.vcfs.txt
    : > manifest.tbis.txt

    for i in "${!CONTIGS[@]}"; do
      CONTIG="${CONTIGS[$i]}"
      VCF="${JOIN_VCFS[$i]}"
      PREFIX="~{cohort_name}.combine_batches.${CONTIG}"

      echo "================================================================"
      echo "[${CONTIG}] $(date -u +%H:%M:%S) ClusterSites"
      echo "================================================================"
      gatk --java-options "-Xmx${JVM_MAX_MEM}" SVCluster \
        -V "${VCF}" \
        --output "${PREFIX}.cluster_sites.vcf.gz" \
        --reference ~{reference_fasta} \
        --ploidy-table ~{ploidy_table} \
        --breakpoint-summary-strategy REPRESENTATIVE \
        --variant-prefix "~{cohort_name}_${CONTIG}_" \
        --pesr-sample-overlap 0.5 \
        --pesr-interval-overlap 0.1 \
        --pesr-breakend-window 300 \
        --depth-sample-overlap 0.5 \
        --depth-interval-overlap 0.5 \
        --depth-breakend-window 500000 \
        --mixed-sample-overlap 0.5 \
        --mixed-interval-overlap 0.5 \
        --mixed-breakend-window 1000000

      echo "[${CONTIG}] $(date -u +%H:%M:%S) GroupedSVClusterPart1"
      gatk --java-options "-Xmx${JVM_MAX_MEM}" GroupedSVCluster \
        --reference ~{reference_fasta} \
        --ploidy-table ~{ploidy_table} \
        -V "${PREFIX}.cluster_sites.vcf.gz" \
        -O "${PREFIX}.recluster_part_1.vcf.gz" \
        --clustering-config ~{clustering_config_part1} \
        --stratify-config ~{stratification_config_part1} \
        $TRACK_ARGS \
        --stratify-overlap-fraction 0 \
        --stratify-num-breakpoint-overlaps 1 \
        --stratify-num-breakpoint-overlaps-interchromosomal 1 \
        --breakpoint-summary-strategy REPRESENTATIVE

      echo "[${CONTIG}] $(date -u +%H:%M:%S) GroupedSVClusterPart2"
      gatk --java-options "-Xmx${JVM_MAX_MEM}" GroupedSVCluster \
        --reference ~{reference_fasta} \
        --ploidy-table ~{ploidy_table} \
        -V "${PREFIX}.recluster_part_1.vcf.gz" \
        -O "${PREFIX}.recluster_part_2.vcf.gz" \
        --clustering-config ~{clustering_config_part2} \
        --stratify-config ~{stratification_config_part2} \
        $TRACK_ARGS \
        --stratify-overlap-fraction 0 \
        --stratify-num-breakpoint-overlaps 1 \
        --stratify-num-breakpoint-overlaps-interchromosomal 1 \
        --breakpoint-summary-strategy REPRESENTATIVE

      # Free intermediate VCFs to save disk
      rm -f "${PREFIX}.cluster_sites.vcf.gz" "${PREFIX}.cluster_sites.vcf.gz.tbi"
      rm -f "${PREFIX}.recluster_part_1.vcf.gz" "${PREFIX}.recluster_part_1.vcf.gz.tbi"

      readlink -f "${PREFIX}.recluster_part_2.vcf.gz" >> manifest.vcfs.txt
      readlink -f "${PREFIX}.recluster_part_2.vcf.gz.tbi" >> manifest.tbis.txt
      echo "[${CONTIG}] $(date -u +%H:%M:%S) done"
    done

    echo "All ${#CONTIGS[@]} contigs processed."
    cat manifest.vcfs.txt
  >>>

  runtime {
    cpu: select_first([runtime_attr.cpu_cores, default_attr.cpu_cores])
    memory: select_first([runtime_attr.mem_gb, default_attr.mem_gb]) + " GiB"
    disks: "local-disk " + select_first([runtime_attr.disk_gb, default_attr.disk_gb]) + " HDD"
    bootDiskSizeGb: select_first([runtime_attr.boot_disk_gb, default_attr.boot_disk_gb])
    docker: gatk_docker
    preemptible: select_first([runtime_attr.preemptible_tries, default_attr.preemptible_tries])
    maxRetries: select_first([runtime_attr.max_retries, default_attr.max_retries])
  }
}
"""


def main() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    shutil.copytree(SOURCE_DIR, OUT_DIR)

    cb_path = OUT_DIR / "wdl" / "CombineBatches.wdl"
    cb = cb_path.read_text()

    # Sanity check
    assert OLD_SCATTER in cb, (
        "OLD_SCATTER block not found verbatim. The CombineBatches.wdl shape "
        "differs from what the v17 builder expects."
    )
    cb = cb.replace(OLD_SCATTER, NEW_SCATTER, 1)

    # Append the new sequential-recluster task at the end of the file
    cb = cb.rstrip() + ALL_CONTIGS_TASK + "\n"
    cb_path.write_text(cb)
    print(f"  patched {cb_path.name}")

    # Build bundle
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
