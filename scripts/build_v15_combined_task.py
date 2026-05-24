#!/usr/bin/env python3
"""Build v15 - combine ClusterSites + GroupedSVClusterPart1 + GroupedSVClusterPart2 into a single task.

Hypothesis: HealthOmics is killing GroupedSVCluster tasks due to FUSE I/O on intermediate VCFs.
By running ClusterSites + Part1 + Part2 in a SINGLE container, all intermediate VCFs stay on local
disk, never going through FUSE.
"""
import shutil
import subprocess
from pathlib import Path

TMP_DIR = Path("/tmp/makecohortvcf-v15")
SOURCE_DIR = Path("/tmp/makecohortvcf-v12")  # Start from v12 (has tarball workaround)
BUNDLE_PATH = Path("gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v15.zip")


COMBINED_TASK = '''task ClusterSitesAndGroupedCluster {
  input {
    File vcf
    File ploidy_table
    String output_prefix
    String? contig
    String cohort_name

    File reference_fasta
    File reference_fasta_fai
    File reference_dict

    File clustering_config_part1
    File stratification_config_part1
    File clustering_config_part2
    File stratification_config_part2
    File track_bed_tarball
    Array[String] track_names

    Float? java_mem_fraction
    String gatk_docker
    RuntimeAttr? runtime_attr_override
  }

  RuntimeAttr default_attr = object {
                               cpu_cores: 4,
                               mem_gb: 16,
                               disk_gb: 100,
                               boot_disk_gb: 10,
                               preemptible_tries: 3,
                               max_retries: 1
                             }
  RuntimeAttr runtime_attr = select_first([runtime_attr_override, default_attr])

  output {
    File out = "~{output_prefix}.recluster_part_2.vcf.gz"
    File out_index = "~{output_prefix}.recluster_part_2.vcf.gz.tbi"
  }

  command <<<
    set -euxo pipefail

    function getJavaMem() {
      cat /proc/meminfo \\
        | awk -v MEM_FIELD="$1" '{
            f[substr($1, 1, length($1)-1)] = $2
          } END {
            printf "%dM", f[MEM_FIELD] * ~{default="0.50" java_mem_fraction} / 1024
          }'
    }
    JVM_MAX_MEM=$(getJavaMem MemTotal)
    echo "JVM memory: $JVM_MAX_MEM"
    echo "DIAG start: $(date -u +%H:%M:%S.%N)"

    # Extract bundled track files locally
    mkdir -p track_files
    tar xzf ~{track_bed_tarball} -C track_files/
    ls -la track_files/

    # Build --track-intervals args from track_names
    TRACK_ARGS=""
    for name in ~{sep=" " track_names}; do
        TRACK_ARGS="$TRACK_ARGS --track-intervals track_files/track.${name}.bed.gz --track-name ${name}"
    done
    echo "Track arguments: $TRACK_ARGS"

    # ===== Step 1: ClusterSites (SVCluster, first round of clustering) =====
    # NOTE: Upstream does NOT pass -L to ClusterSites; the input VCF is already contig-restricted.
    echo "DIAG ClusterSites start: $(date -u +%H:%M:%S.%N)"
    awk '{print "-V "$0}' <<EOF > cluster_sites.args
~{vcf}
EOF
    gatk --java-options "-Xmx${JVM_MAX_MEM}" SVCluster \\
      --arguments_file cluster_sites.args \\
      --output ~{output_prefix}.cluster_sites.vcf.gz \\
      --reference ~{reference_fasta} \\
      --ploidy-table ~{ploidy_table} \\
      --breakpoint-summary-strategy REPRESENTATIVE \\
      --variant-prefix "~{cohort_name}_~{contig}_" \\
      --pesr-sample-overlap 0.5 \\
      --pesr-interval-overlap 0.1 \\
      --pesr-breakend-window 300 \\
      --depth-sample-overlap 0.5 \\
      --depth-interval-overlap 0.5 \\
      --depth-breakend-window 500000 \\
      --mixed-sample-overlap 0.5 \\
      --mixed-interval-overlap 0.5 \\
      --mixed-breakend-window 1000000

    ls -la ~{output_prefix}.cluster_sites.vcf.gz*

    # ===== Step 2: GroupedSVClusterPart1 =====
    # NOTE: Upstream does NOT pass contig (-L) to GroupedSVClusterPart1.
    echo "DIAG GroupedSVClusterPart1 start: $(date -u +%H:%M:%S.%N)"
    gatk --java-options "-Xmx${JVM_MAX_MEM}" GroupedSVCluster \\
      --reference ~{reference_fasta} \\
      --ploidy-table ~{ploidy_table} \\
      -V ~{output_prefix}.cluster_sites.vcf.gz \\
      -O ~{output_prefix}.recluster_part_1.vcf.gz \\
      --clustering-config ~{clustering_config_part1} \\
      --stratify-config ~{stratification_config_part1} \\
      $TRACK_ARGS \\
      --stratify-overlap-fraction 0 \\
      --stratify-num-breakpoint-overlaps 1 \\
      --stratify-num-breakpoint-overlaps-interchromosomal 1 \\
      --breakpoint-summary-strategy REPRESENTATIVE

    ls -la ~{output_prefix}.recluster_part_1.vcf.gz*

    # ===== Step 3: GroupedSVClusterPart2 =====
    # NOTE: Upstream does NOT pass contig (-L) to GroupedSVClusterPart2.
    echo "DIAG GroupedSVClusterPart2 start: $(date -u +%H:%M:%S.%N)"
    gatk --java-options "-Xmx${JVM_MAX_MEM}" GroupedSVCluster \\
      --reference ~{reference_fasta} \\
      --ploidy-table ~{ploidy_table} \\
      -V ~{output_prefix}.recluster_part_1.vcf.gz \\
      -O ~{output_prefix}.recluster_part_2.vcf.gz \\
      --clustering-config ~{clustering_config_part2} \\
      --stratify-config ~{stratification_config_part2} \\
      $TRACK_ARGS \\
      --stratify-overlap-fraction 0 \\
      --stratify-num-breakpoint-overlaps 1 \\
      --stratify-num-breakpoint-overlaps-interchromosomal 1 \\
      --breakpoint-summary-strategy REPRESENTATIVE

    ls -la ~{output_prefix}.recluster_part_2.vcf.gz*
    echo "DIAG done: $(date -u +%H:%M:%S.%N)"
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
'''


def main():
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    shutil.copytree(SOURCE_DIR, TMP_DIR)

    cb_path = TMP_DIR / "wdl" / "CombineBatches.wdl"
    cb = cb_path.read_text()

    # Replace ClusterSites + GroupedSVClusterPart1 + GroupedSVClusterPart2 calls with the combined task
    # Find from "# First round of clustering" through end of GroupedSVClusterPart2 call
    # Then update GatkToSvtkVcf to use new output

    # Find ClusterSites start
    cluster_sites_start = cb.find("    # First round of clustering")
    assert cluster_sites_start != -1, "Could not find ClusterSites comment"

    # Find end of GroupedSVClusterPart2 (closing brace before next call)
    # The end is right before "# Use \"depth\" as source"
    part2_end_marker = '        runtime_attr_override=runtime_attr_recluster_part2\n    }\n\n    # Use "depth" as source to match legacy headers'
    part2_end = cb.find(part2_end_marker)
    assert part2_end != -1, "Could not find GroupedSVClusterPart2 end"
    # We want to keep the "# Use 'depth' as source" comment, so end the replacement just at the "}\n\n"
    part2_end_pos = part2_end + len('        runtime_attr_override=runtime_attr_recluster_part2\n    }\n\n')

    # Replace this block with our combined task call
    new_combined_call = '''    # Combined: ClusterSites + GroupedSVClusterPart1 + GroupedSVClusterPart2
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
        track_bed_tarball=track_bed_tarball,
        track_names=track_names,
        java_mem_fraction=java_mem_fraction,
        gatk_docker=gatk_docker,
        runtime_attr_override=runtime_attr_recluster_part2
    }

'''

    cb_new = cb[:cluster_sites_start] + new_combined_call + cb[part2_end_pos:]
    cb = cb_new

    # Update GatkToSvtkVcf input: vcf=GroupedSVClusterPart2.out -> vcf=ClusterSitesAndGroupedCluster.out
    # Note: there are TWO usages of vcf=GroupedSVClusterPart2.out (one in GatkToSvtkVcf, one in ExtractSRVariantLists)
    cb_new = cb.replace(
        "vcf=GroupedSVClusterPart2.out,",
        "vcf=ClusterSitesAndGroupedCluster.out,"
    )
    # Should replace BOTH usages
    assert cb_new.count("ClusterSitesAndGroupedCluster.out,") >= 2, f"Expected 2+ replacements, got {cb_new.count('ClusterSitesAndGroupedCluster.out,')}"
    cb = cb_new

    # Update ExtractSRVariantLists vcf_index input
    cb_new = cb.replace(
        "vcf_index=GroupedSVClusterPart2.out_index,",
        "vcf_index=ClusterSitesAndGroupedCluster.out_index,"
    )
    assert cb_new != cb, "ExtractSRVariantLists vcf_index update failed"
    cb = cb_new

    # Append the combined task definition at the end of the file (before the closing })
    # Find the LAST } that closes the workflow
    # Actually, just append before the GroupedSVClusterTask definition (which we still need - or do we?)
    # Since we no longer call GroupedSVClusterTask, we can leave it in place (unused). 
    # Add ClusterSitesAndGroupedCluster task at end of file.
    cb = cb.rstrip() + "\n\n" + COMBINED_TASK + "\n"

    cb_path.write_text(cb)
    print(f"✓ Modified {cb_path}")
    print(f"  File size: {len(cb):,} chars")

    if BUNDLE_PATH.exists():
        BUNDLE_PATH.unlink()
    subprocess.run(
        ["zip", "-q", "-r", str(BUNDLE_PATH.resolve()), "wdl/"],
        cwd=TMP_DIR, check=True,
    )
    print(f"✓ Bundle created: {BUNDLE_PATH} ({BUNDLE_PATH.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
