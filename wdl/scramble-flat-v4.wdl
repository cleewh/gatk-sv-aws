version 1.0

workflow ScrambleFlat {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File mei_bed
    String scramble_docker
  }

  call RunScramble {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
      mei_bed = mei_bed,
      docker = scramble_docker
  }

  output {
    File scramble_vcf = RunScramble.vcf
  }
}

task RunScramble {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File mei_bed
    String docker
  }

  command <<<
    set -eo pipefail

    # Pre-localize CRAM to local disk for faster random access
    echo "Pre-localizing CRAM to local disk..."
    cp ~{cram_or_bam} /tmp/input.cram
    cp ~{cram_or_bam_idx} /tmp/input.cram.crai

    /app/scramble-gatk-sv/cluster_identifier/src/build/cluster_identifier \
      /tmp/input.cram \
      > ~{sample_id}.clusters.txt

    Rscript --vanilla /app/scramble-gatk-sv/cluster_analysis/bin/SCRAMble.R \
      --out-name ~{sample_id}.scramble \
      --cluster-file ~{sample_id}.clusters.txt \
      --install-dir /app/scramble-gatk-sv/cluster_identifier/src \
      --mei-refs ~{mei_bed} \
      --ref ~{ref_fasta} \
      --eval-meis \
      --no-vcf

    if [ -f "~{sample_id}.scramble_MEIs.txt" ]; then
      bgzip -c ~{sample_id}.scramble_MEIs.txt > ~{sample_id}.scramble.vcf.gz
    else
      echo "##fileformat=VCFv4.2" > ~{sample_id}.scramble.vcf
      printf "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t~{sample_id}\n" >> ~{sample_id}.scramble.vcf
      bgzip ~{sample_id}.scramble.vcf
    fi
  >>>

  output {
    File vcf = "~{sample_id}.scramble.vcf.gz"
  }

  runtime {
    docker: docker
    memory: "16 GiB"
    cpu: 2
  }
}
