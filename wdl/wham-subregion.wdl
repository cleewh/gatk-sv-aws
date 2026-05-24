version 1.0

# Wham optimized: pre-localize CRAM, then run whamg with sub-region
# splitting on large chromosomes. Uses -r flag for region specification.
#
# Strategy:
# - chr1-chr6 (largest): split into 4 sub-regions each = 24 processes
# - chr7-chr12: split into 2 sub-regions each = 12 processes
# - chr13-chrY: run whole = 12 processes
# Total: ~48 concurrent whamg processes, each on a smaller region
# With -x 2 threads each = 96 threads on 96 CPUs
#
# This avoids the chr1 bottleneck by splitting it into 4 × 62.5 Mbp chunks.

workflow WhamSubregion {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    String wham_docker
  }

  call RunWhamSubregion {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
      docker = wham_docker
  }

  output {
    File wham_vcf = RunWhamSubregion.vcf
    File wham_vcf_idx = RunWhamSubregion.vcf_idx
  }
}

task RunWhamSubregion {
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

    # Pre-localize CRAM to local disk
    echo "Pre-localizing CRAM..."
    cp ~{cram_or_bam} /tmp/input.cram
    cp ~{cram_or_bam_idx} /tmp/input.cram.crai
    echo "Done."

    mkdir -p /tmp/wham_shards

    # Large chromosomes split into 4 sub-regions (62.5 Mbp each for chr1)
    # chr1: 248M, chr2: 242M, chr3: 198M, chr4: 190M, chr5: 182M, chr6: 170M
    echo "Launching sub-region whamg processes..."

    # chr1 (248 Mbp) - 4 parts
    whamg -r chr1:1-62000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr1_p1.vcf &
    whamg -r chr1:62000001-124000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr1_p2.vcf &
    whamg -r chr1:124000001-186000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr1_p3.vcf &
    whamg -r chr1:186000001-248956422 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr1_p4.vcf &

    # chr2 (242 Mbp) - 4 parts
    whamg -r chr2:1-61000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr2_p1.vcf &
    whamg -r chr2:61000001-122000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr2_p2.vcf &
    whamg -r chr2:122000001-183000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr2_p3.vcf &
    whamg -r chr2:183000001-242193529 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr2_p4.vcf &

    # chr3 (198 Mbp) - 2 parts
    whamg -r chr3:1-99000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr3_p1.vcf &
    whamg -r chr3:99000001-198295559 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr3_p2.vcf &

    # chr4 (190 Mbp) - 2 parts
    whamg -r chr4:1-95000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr4_p1.vcf &
    whamg -r chr4:95000001-190214555 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr4_p2.vcf &

    # chr5 (182 Mbp) - 2 parts
    whamg -r chr5:1-91000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr5_p1.vcf &
    whamg -r chr5:91000001-181538259 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr5_p2.vcf &

    # chr6 (170 Mbp) - 2 parts
    whamg -r chr6:1-85000000 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr6_p1.vcf &
    whamg -r chr6:85000001-170805979 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr6_p2.vcf &

    # Remaining chromosomes - whole (smaller, finish fast)
    whamg -c chr7 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr7.vcf &
    whamg -c chr8 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr8.vcf &
    whamg -c chr9 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr9.vcf &
    whamg -c chr10 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr10.vcf &
    whamg -c chr11 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr11.vcf &
    whamg -c chr12 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr12.vcf &
    whamg -c chr13 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr13.vcf &
    whamg -c chr14 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr14.vcf &
    whamg -c chr15 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr15.vcf &
    whamg -c chr16 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr16.vcf &
    whamg -c chr17 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr17.vcf &
    whamg -c chr18 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr18.vcf &
    whamg -c chr19 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr19.vcf &
    whamg -c chr20 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr20.vcf &
    whamg -c chr21 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr21.vcf &
    whamg -c chr22 -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chr22.vcf &
    whamg -c chrX -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chrX.vcf &
    whamg -c chrY -x 2 -a ~{ref_fasta} -f /tmp/input.cram > /tmp/wham_shards/chrY.vcf &

    # Wait for all (36 total processes)
    wait
    echo "All whamg processes complete."

    # Merge all VCFs
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
    memory: "96 GiB"
    cpu: 72
  }
}
