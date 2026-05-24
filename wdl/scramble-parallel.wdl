version 1.0

# SCRAMble optimized: single large instance, pre-localize CRAM once,
# then run cluster_identifier in parallel per chromosome using -r flag.
# Merges cluster files and runs SCRAMble.R once on the combined output.

workflow ScrambleParallel {
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

    # Symlink index next to CRAM (required for BAM/CRAM index lookup)
    ln -sf ~{cram_or_bam_idx} ~{cram_or_bam}.crai

    # Read contigs
    mapfile -t CONTIGS < ~{primary_contigs_list}
    TOTAL=${#CONTIGS[@]}
    PARALLEL=4
    echo "Running cluster_identifier on $TOTAL contigs ($PARALLEL at a time)..."

    # Run cluster_identifier with limited parallelism (4 at a time)
    mkdir -p /tmp/clusters
    running=0
    for contig in "${CONTIGS[@]}"; do
      (
        /app/scramble-gatk-sv/cluster_identifier/src/build/cluster_identifier \
          -r "$contig" \
          ~{cram_or_bam} \
          > "/tmp/clusters/${contig}.clusters.txt"
      ) &
      running=$((running + 1))
      if [ $running -ge $PARALLEL ]; then
        wait -n
        running=$((running - 1))
      fi
    done
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
    memory: "16 GiB"
    cpu: 4
  }
}
