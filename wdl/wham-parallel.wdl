version 1.0

# Wham optimized: single large instance, pre-localize CRAM once,
# then run 24 whamg processes in parallel (one per chromosome).
# Avoids 24 redundant CRAM copies that the sharded approach requires.

workflow WhamParallel {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File primary_contigs_list
    String wham_docker
  }

  call RunWhamParallel {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
      primary_contigs_list = primary_contigs_list,
      docker = wham_docker
  }

  output {
    File wham_vcf = RunWhamParallel.vcf
    File wham_vcf_idx = RunWhamParallel.vcf_idx
  }
}

task RunWhamParallel {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File primary_contigs_list
    String docker
  }

  command <<<
    set -eo pipefail

    # Pre-localize CRAM to local disk (one copy, used by all 24 processes)
    echo "Pre-localizing CRAM to local disk..."
    cp ~{cram_or_bam} /tmp/input.cram
    cp ~{cram_or_bam_idx} /tmp/input.cram.crai
    echo "Pre-localization complete."

    # Read contigs list
    mapfile -t CONTIGS < ~{primary_contigs_list}
    echo "Running whamg on ${#CONTIGS[@]} contigs in parallel..."

    # Run whamg in parallel for each contig (background jobs)
    mkdir -p /tmp/wham_shards
    for contig in "${CONTIGS[@]}"; do
      (
        whamg \
          -c "$contig" \
          -x 4 \
          -a ~{ref_fasta} \
          -f /tmp/input.cram \
          > "/tmp/wham_shards/${contig}.vcf"
      ) &
    done

    # Wait for all background jobs
    wait
    echo "All whamg processes complete."

    # Merge per-chromosome VCFs
    echo "Merging VCFs..."
    for contig in "${CONTIGS[@]}"; do
      bgzip "/tmp/wham_shards/${contig}.vcf"
      echo "/tmp/wham_shards/${contig}.vcf.gz" >> /tmp/vcf_list.txt
    done

    bcftools concat \
      --file-list /tmp/vcf_list.txt \
      --allow-overlaps \
      --output-type z \
      --output ~{sample_id}.wham.vcf.gz

    tabix -p vcf ~{sample_id}.wham.vcf.gz
    echo "Done."
  >>>

  output {
    File vcf = "~{sample_id}.wham.vcf.gz"
    File vcf_idx = "~{sample_id}.wham.vcf.gz.tbi"
  }

  runtime {
    docker: docker
    memory: "192 GiB"
    cpu: 96
  }
}
