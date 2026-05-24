version 1.0

workflow CollectSVEvidenceOptimized {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File ref_fasta_dict
    File gatk_jar
    File preprocessed_intervals
    File primary_contigs_list
    File sd_locs_vcf
    String docker
  }

  call T {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
      ref_fasta_dict = ref_fasta_dict,
      gatk_jar = gatk_jar,
      preprocessed_intervals = preprocessed_intervals,
      primary_contigs_list = primary_contigs_list,
      sd_locs_vcf = sd_locs_vcf,
      docker = docker
  }

  output {
    File pe_file = T.pe_file
    File sr_file = T.sr_file
    File sd_file = T.sd_file
  }
}

task T {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File ref_fasta_dict
    File gatk_jar
    File preprocessed_intervals
    File primary_contigs_list
    File sd_locs_vcf
    String docker
  }

  command <<<
    set -eo pipefail

    # Pre-localize CRAM to local disk for faster I/O
    echo "Pre-localizing CRAM to local disk..."
    cp ~{cram_or_bam} /tmp/input.cram
    cp ~{cram_or_bam_idx} /tmp/input.cram.crai

    # Regenerate index on local copy
    samtools index /tmp/input.cram

    echo "Running CollectSVEvidence..."
    java -Xmx12g -jar ~{gatk_jar} CollectSVEvidence \
      -I /tmp/input.cram \
      -R ~{ref_fasta} \
      --sample-name ~{sample_id} \
      --pe-file ~{sample_id}.pe.txt.gz \
      --sr-file ~{sample_id}.sr.txt.gz \
      --sd-file ~{sample_id}.sd.txt.gz \
      --allele-count-file ~{sample_id}.ac.txt.gz \
      -L ~{preprocessed_intervals} \
      --sd-locs-vcf ~{sd_locs_vcf}
  >>>

  output {
    File pe_file = "~{sample_id}.pe.txt.gz"
    File sr_file = "~{sample_id}.sr.txt.gz"
    File sd_file = "~{sample_id}.sd.txt.gz"
  }

  runtime {
    docker: docker
    memory: "30 GiB"
    cpu: 4
  }
}
