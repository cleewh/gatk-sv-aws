version 1.0

# Wham lowest-cost: 50 Mbp chunks, 48 CPU instance, concurrency limiter.
# ~62 regions processed 24 at a time with wait -n.
# Expected: ~45 min total, ~$2.50 cost.

workflow WhamLowCost {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    String wham_docker
  }

  call RunWham {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
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
    String docker
  }

  command <<<
    set -eo pipefail

    # Pre-localize CRAM
    echo "Pre-localizing CRAM..."
    cp ~{cram_or_bam} /tmp/input.cram
    cp ~{cram_or_bam_idx} /tmp/input.cram.crai
    echo "Done."

    mkdir -p /tmp/wham_shards

    # Generate 50 Mbp regions for all chromosomes
    # GRCh38 chromosome sizes
    declare -A SIZES=(
      [chr1]=248956422 [chr2]=242193529 [chr3]=198295559 [chr4]=190214555
      [chr5]=181538259 [chr6]=170805979 [chr7]=159345973 [chr8]=145138636
      [chr9]=138394717 [chr10]=133797422 [chr11]=135086622 [chr12]=133275309
      [chr13]=114364328 [chr14]=107043718 [chr15]=101991189 [chr16]=90338345
      [chr17]=83257441 [chr18]=80373285 [chr19]=58617616 [chr20]=64444167
      [chr21]=46709983 [chr22]=50818468 [chrX]=156040895 [chrY]=57227415
    )

    CHUNK=50000000
    PARALLEL=24
    running=0
    idx=0

    for chr in chr1 chr2 chr3 chr4 chr5 chr6 chr7 chr8 chr9 chr10 chr11 chr12 chr13 chr14 chr15 chr16 chr17 chr18 chr19 chr20 chr21 chr22 chrX chrY; do
      size=${SIZES[$chr]}
      start=1
      part=1
      while [ $start -le $size ]; do
        end=$((start + CHUNK - 1))
        if [ $end -gt $size ]; then
          end=$size
        fi
        region="${chr}:${start}-${end}"
        outfile="/tmp/wham_shards/${chr}_p${part}.vcf"
        (
          whamg -r "$region" -x 2 -a ~{ref_fasta} -f /tmp/input.cram > "$outfile"
        ) &
        running=$((running + 1))
        if [ $running -ge $PARALLEL ]; then
          wait -n
          running=$((running - 1))
        fi
        start=$((end + 1))
        part=$((part + 1))
        idx=$((idx + 1))
      done
    done

    wait
    echo "All $idx whamg processes complete."

    # Merge
    echo "Merging VCFs..."
    for f in /tmp/wham_shards/*.vcf; do
      bgzip "$f"
      echo "${f}.gz" >> /tmp/vcf_list.txt
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
    cpu: 48
  }
}
