version 1.0

# Merges per-chromosome Wham VCFs into a single sample-level VCF.
# Run after all 24 WhamShard runs complete.

workflow WhamMerge {
  input {
    Array[File] shard_vcfs
    String sample_id
    String wham_docker
  }

  call MergeShards {
    input:
      shard_vcfs = shard_vcfs,
      sample_id = sample_id,
      docker = wham_docker
  }

  output {
    File wham_vcf = MergeShards.merged_vcf
    File wham_vcf_idx = MergeShards.merged_vcf_idx
  }
}

task MergeShards {
  input {
    Array[File] shard_vcfs
    String sample_id
    String docker
  }

  command <<<
    set -eo pipefail

    # Create file list for bcftools
    for vcf in ~{sep=' ' shard_vcfs}; do
      echo "$vcf" >> vcf_list.txt
    done

    # Sort and concatenate all chromosome VCFs
    bcftools concat \
      --file-list vcf_list.txt \
      --allow-overlaps \
      --output-type z \
      --output ~{sample_id}.wham.vcf.gz

    tabix -p vcf ~{sample_id}.wham.vcf.gz
  >>>

  output {
    File merged_vcf = "~{sample_id}.wham.vcf.gz"
    File merged_vcf_idx = "~{sample_id}.wham.vcf.gz.tbi"
  }

  runtime {
    docker: docker
    memory: "4 GiB"
    cpu: 2
  }
}
