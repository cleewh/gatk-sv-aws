version 1.0

# Wham final optimized: split CRAM into per-chromosome BAMs, then run
# whamg on each small BAM in parallel. This avoids whamg reading the
# full 14.7 GB CRAM for each chromosome.
#
# Key insight: whamg reads the ENTIRE input file regardless of -r flag.
# By giving it small per-chromosome BAMs (~600 MB for chr1), the read
# phase is 15x faster and the graph analysis is proportionally smaller.
#
# Expected: ~45 min total for 30x WGS.

workflow WhamSplitBam {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File primary_contigs_list
    String wham_docker
  }

  call RunWham {
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
    File wham_vcf = RunWham.vcf
    File wham_vcf_idx = RunWham.vcf_idx
  }
}

task RunWham {
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

    # Step 1: Pre-localize CRAM to local disk
    echo "$(date) Pre-localizing CRAM..."
    cp ~{cram_or_bam} /tmp/input.cram
    cp ~{cram_or_bam_idx} /tmp/input.cram.crai
    echo "$(date) Pre-localization complete."

    # Read contigs
    mapfile -t CONTIGS < ~{primary_contigs_list}
    echo "$(date) Splitting into ${#CONTIGS[@]} per-chromosome BAMs..."

    # Step 2: Split CRAM into per-chromosome BAMs (SEQUENTIAL to avoid termination)
    mkdir -p /tmp/chr_bams /tmp/wham_out
    for contig in "${CONTIGS[@]}"; do
      /opt/samtools/bin/samtools view -b -@ 4 -o "/tmp/chr_bams/${contig}.bam" /tmp/input.cram "$contig"
      /opt/samtools/bin/samtools index "/tmp/chr_bams/${contig}.bam"
    done
    echo "$(date) Split complete. Per-chromosome BAMs created."

    # Step 3: Run whamg on each per-chromosome BAM (parallel, 12 concurrent)
    echo "$(date) Running whamg on per-chromosome BAMs..."
    PARALLEL=12
    running=0
    for contig in "${CONTIGS[@]}"; do
      (
        whamg -x 2 -c "$contig" -a ~{ref_fasta} -f "/tmp/chr_bams/${contig}.bam" \
          > "/tmp/wham_out/${contig}.vcf" 2> "/tmp/wham_out/${contig}.err"
      ) &
      running=$((running + 1))
      if [ $running -ge $PARALLEL ]; then
        wait -n
        running=$((running - 1))
      fi
    done
    wait
    echo "$(date) All whamg processes complete."

    # Step 4: Merge VCFs
    echo "$(date) Merging VCFs..."
    for contig in "${CONTIGS[@]}"; do
      bgzip "/tmp/wham_out/${contig}.vcf"
      echo "/tmp/wham_out/${contig}.vcf.gz" >> /tmp/vcf_list.txt
    done

    bcftools concat \
      --file-list /tmp/vcf_list.txt \
      --allow-overlaps \
      --output-type z \
      --output ~{sample_id}.wham.vcf.gz

    tabix -p vcf ~{sample_id}.wham.vcf.gz
    echo "$(date) Done."
  >>>

  output {
    File vcf = "~{sample_id}.wham.vcf.gz"
    File vcf_idx = "~{sample_id}.wham.vcf.gz.tbi"
  }

  runtime {
    docker: docker
    memory: "64 GiB"
    cpu: 24
  }
}
