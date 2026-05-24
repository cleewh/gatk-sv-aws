version 1.0

# SCRAMble test: 4 parallel cluster_identifier on smallest chromosomes
# to verify if limited parallelism works with STATIC storage + pre-localize.

workflow ScrambleTest4Par {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File mei_bed
    String scramble_docker
  }

  call RunTest {
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
    File scramble_vcf = RunTest.vcf
  }
}

task RunTest {
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

    # Pre-localize CRAM to local disk
    echo "Pre-localizing CRAM..."
    cp ~{cram_or_bam} /tmp/input.cram
    cp ~{cram_or_bam_idx} /tmp/input.cram.crai
    echo "Done. Running 4 smallest chromosomes in parallel..."

    # 4 smallest: chr21 (46M), chr22 (50M), chrY (57M), chr19 (58M)
    mkdir -p /tmp/clusters
    /app/scramble-gatk-sv/cluster_identifier/src/build/cluster_identifier -r chr21 /tmp/input.cram > /tmp/clusters/chr21.txt &
    /app/scramble-gatk-sv/cluster_identifier/src/build/cluster_identifier -r chr22 /tmp/input.cram > /tmp/clusters/chr22.txt &
    /app/scramble-gatk-sv/cluster_identifier/src/build/cluster_identifier -r chrY /tmp/input.cram > /tmp/clusters/chrY.txt &
    /app/scramble-gatk-sv/cluster_identifier/src/build/cluster_identifier -r chr19 /tmp/input.cram > /tmp/clusters/chr19.txt &
    wait
    echo "4 parallel processes complete."

    # Merge and produce minimal output
    cat /tmp/clusters/*.txt > ~{sample_id}.clusters.txt
    echo "Clusters: $(wc -l < ~{sample_id}.clusters.txt) lines"

    # Minimal VCF output
    echo "##fileformat=VCFv4.2" > ~{sample_id}.scramble.vcf
    printf "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t~{sample_id}\n" >> ~{sample_id}.scramble.vcf
    bgzip ~{sample_id}.scramble.vcf
    echo "Done."
  >>>

  output {
    File vcf = "~{sample_id}.scramble.vcf.gz"
  }

  runtime {
    docker: docker
    memory: "32 GiB"
    cpu: 4
  }
}
