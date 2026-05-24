version 1.0

workflow CollectCountsOptimized {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File ref_fasta_dict
    File gatk_jar
    File intervals
    String docker
  }

  call T {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
      ref_fasta_dict = ref_fasta_dict,
      gatk_jar = gatk_jar,
      intervals = intervals,
      docker = docker
  }

  output {
    File counts = T.counts
  }
}

task T {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File ref_fasta_dict
    File gatk_jar
    File intervals
    String docker
  }

  command <<<
    set -eo pipefail

    # CRAM index symlink for FUSE access
    CRAM_DIR=$(dirname ~{cram_or_bam})
    CRAM_BASE=$(basename ~{cram_or_bam})
    ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

    echo "Running CollectReadCounts (streaming via FUSE)..."
    java -Xmx6g -jar ~{gatk_jar} CollectReadCounts \
      -I ~{cram_or_bam} \
      -L ~{intervals} \
      -R ~{ref_fasta} \
      --format TSV \
      --interval-merging-rule OVERLAPPING_ONLY \
      -O ~{sample_id}.counts.tsv

    # Compress output
    bgzip ~{sample_id}.counts.tsv
  >>>

  output {
    File counts = "~{sample_id}.counts.tsv.gz"
  }

  runtime {
    docker: docker
    memory: "8 GiB"
    cpu: 2
  }
}
