version 1.0

# SCRAMble optimized v2: DYNAMIC storage, no CRAM pre-localization,
# 12 concurrent cluster_identifier processes reading via FUSE.
# 12 CPU / 12 GiB — matches actual parallelism.
# Expected runtime: ~22-25 min for 30x WGS.

workflow ScrambleDynamic {
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

    # CRAM index symlink for FUSE access (no pre-localization)
    CRAM_DIR=$(dirname ~{cram_or_bam})
    CRAM_BASE=$(basename ~{cram_or_bam})
    ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

    # Read contigs
    mapfile -t CONTIGS < ~{primary_contigs_list}
    TOTAL=${#CONTIGS[@]}
    PARALLEL=12
    echo "$(date) Running cluster_identifier on $TOTAL contigs ($PARALLEL at a time) via FUSE..."

    # Run cluster_identifier with concurrency limiter (12 at a time)
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
    echo "$(date) All cluster_identifier processes complete."

    # Merge all cluster files
    cat /tmp/clusters/*.clusters.txt > ~{sample_id}.clusters.txt
    echo "Merged clusters: $(wc -l < ~{sample_id}.clusters.txt) lines"

    # Run SCRAMble.R on merged clusters
    echo "$(date) Running SCRAMble.R..."
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
    echo "$(date) Done."
  >>>

  output {
    File vcf = "~{sample_id}.scramble.vcf.gz"
  }

  runtime {
    docker: docker
    memory: "12 GiB"
    cpu: 12
  }
}
