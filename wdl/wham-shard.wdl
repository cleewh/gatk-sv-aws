version 1.0

# Single-chromosome Wham shard. The orchestrator launches one of these
# per contig in primary_contigs.list (24 parallel runs), then merges.

workflow WhamShard {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    String contig
    File ref_fasta
    File ref_fasta_fai
    String wham_docker
  }

  call RunWhamShard {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      contig = contig,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
      docker = wham_docker
  }

  output {
    File wham_vcf = RunWhamShard.vcf
  }
}

task RunWhamShard {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    String contig
    File ref_fasta
    File ref_fasta_fai
    String docker
  }

  command <<<
    set -eo pipefail

    # Pre-localize CRAM to local disk for faster random access
    echo "Pre-localizing CRAM to local disk..."
    cp ~{cram_or_bam} /tmp/input.cram
    cp ~{cram_or_bam_idx} /tmp/input.cram.crai

    # Run Wham on a single chromosome
    whamg \
      -c ~{contig} \
      -x 4 \
      -a ~{ref_fasta} \
      -f /tmp/input.cram \
      > ~{sample_id}.~{contig}.wham.vcf

    bgzip ~{sample_id}.~{contig}.wham.vcf
  >>>

  output {
    File vcf = "~{sample_id}.~{contig}.wham.vcf.gz"
  }

  runtime {
    docker: docker
    memory: "8 GiB"
    cpu: 4
  }
}
