version 1.0

# SCRAMble optimized: STATIC storage for guaranteed disk space,
# pre-localize CRAM once, then 24 parallel cluster_identifier processes
# reading from local disk.

workflow ScrambleParallelStatic {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File mei_bed
    File primary_contigs_list
    String scramble_docker
  }

  call RunScrambleParallel {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
      mei_bed = mei_bed,
      primary_contigs_list = primary_contigs_list,
      docker = scramble_docker
  }

  output {
    File scramble_vcf = RunScrambleParallel.vcf
  }
}

task RunScrambleParallel {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File mei_bed
    File primary_contigs_list
    String docker
  }

  command <<<
    set -eo pipefail

    # Pre-localize CRAM to local disk (STATIC storage guarantees space)
    echo "Pre-localizing CRAM to local disk..."
    cp ~{cram_or_bam} /tmp/input.cram
    cp ~{cram_or_bam_idx} /tmp/input.cram.crai
    echo "Pre-localization complete."

    # Read contigs
    mapfile -t CONTIGS < ~{primary_contigs_list}
    echo "Running cluster_identifier on ${#CONTIGS[@]} contigs in parallel..."

    # Run all 24 cluster_identifier processes in parallel from local disk
    mkdir -p /tmp/clusters
    for contig in "${CONTIGS[@]}"; do
      (
        /app/scramble-gatk-sv/cluster_identifier/src/build/cluster_identifier \
          -r "$contig" \
          /tmp/input.cram \
          > "/tmp/clusters/${contig}.clusters.txt"
      ) &
    done

    # Wait for all parallel jobs
    wait
    echo "All cluster_identifier processes complete."

    # Merge all cluster files
    cat /tmp/clusters/*.clusters.txt > ~{sample_id}.clusters.txt
    echo "Merged clusters: $(wc -l < ~{sample_id}.clusters.txt) lines"

    # Run SCRAMble.R on merged clusters
    echo "Running SCRAMble.R..."
    Rscript --vanilla /app/scramble-gatk-sv/cluster_analysis/bin/SCRAMble.R \
      --out-name ~{sample_id}.scramble \
      --cluster-file ~{sample_id}.clusters.txt \
      --install-dir /app/scramble-gatk-sv/cluster_identifier/src \
      --mei-refs ~{mei_bed} \
      --ref ~{ref_fasta} \
      --eval-meis \
      --no-vcf

    # Produce output VCF
    if [ -f "~{sample_id}.scramble_MEIs.txt" ]; then
      bgzip -c ~{sample_id}.scramble_MEIs.txt > ~{sample_id}.scramble.vcf.gz
    else
      echo "##fileformat=VCFv4.2" > ~{sample_id}.scramble.vcf
      printf "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t~{sample_id}\n" >> ~{sample_id}.scramble.vcf
      bgzip ~{sample_id}.scramble.vcf
    fi
    echo "Done."
  >>>

  output {
    File vcf = "~{sample_id}.scramble.vcf.gz"
  }

  runtime {
    docker: docker
    memory: "64 GiB"
    cpu: 24
  }
}
