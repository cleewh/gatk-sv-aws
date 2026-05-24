version 1.0

workflow MantaFlat {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File manta_region_bed
    File manta_region_bed_index
    String manta_docker
  }

  call RunManta {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
      manta_region_bed = manta_region_bed,
      manta_region_bed_index = manta_region_bed_index,
      docker = manta_docker
  }

  output {
    File manta_vcf = RunManta.vcf
    File manta_vcf_idx = RunManta.vcf_idx
  }
}

task RunManta {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File manta_region_bed
    File manta_region_bed_index
    String docker
  }

  command <<<
    set -eo pipefail

    # Manta requires the index co-located with the CRAM/BAM.
    # HealthOmics FUSE mounts inputs at different paths, so we
    # symlink the index next to the CRAM.
    ln -sf ~{cram_or_bam_idx} ~{cram_or_bam}.crai

    /usr/local/bin/manta/bin/configManta.py \
      --bam ~{cram_or_bam} \
      --referenceFasta ~{ref_fasta} \
      --callRegions ~{manta_region_bed} \
      --runDir manta_run

    manta_run/runWorkflow.py -j $(nproc)

    mv manta_run/results/variants/diploidSV.vcf.gz ~{sample_id}.manta.vcf.gz
    mv manta_run/results/variants/diploidSV.vcf.gz.tbi ~{sample_id}.manta.vcf.gz.tbi
  >>>

  output {
    File vcf = "~{sample_id}.manta.vcf.gz"
    File vcf_idx = "~{sample_id}.manta.vcf.gz.tbi"
  }

  runtime {
    docker: docker
    memory: "16 GiB"
    cpu: 4
  }
}
