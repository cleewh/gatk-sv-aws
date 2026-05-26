## Wham (upstream Broad GATK-SV) for AWS HealthOmics
##
## This runs the original upstream `whamg` binary (single-threaded over the
## whole genome) instead of our custom `whamg-fast` build. Used to validate
## output equivalence against `whamg-fast`.
##
## Differences from upstream Whamg.wdl:
##   - Drops `-c <chr>` per-chromosome scattering (HealthOmics single-task,
##     so we run on the full genome at once, like upstream's WhamSingleSample
##     pattern).
##   - Symlinks .crai to a sibling of the CRAM (HealthOmics localizes
##     File inputs by hash, breaking sibling discovery; see divergence-log).
##   - Does NOT use `-x N` OpenMP — upstream whamg has no OpenMP support.

version 1.0

workflow WhamUpstream {
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
    File vcf = RunWham.vcf
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

    # Symlink CRAI sibling so /opt/wham can find it
    CRAM_DIR=$(dirname ~{cram_or_bam})
    CRAM_BASE=$(basename ~{cram_or_bam})
    ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

    # Convert CRAM to BAM
    echo "$(date) Converting CRAM to BAM..."
    samtools view -b -@ 8 -T ~{ref_fasta} -o /tmp/input.bam ~{cram_or_bam}
    samtools index -@ 8 /tmp/input.bam
    echo "$(date) BAM ready: $(du -sh /tmp/input.bam | cut -f1)"

    # Run upstream whamg (single-threaded full-genome)
    echo "$(date) Running upstream whamg..."
    whamg \
        -a ~{ref_fasta} \
        -f /tmp/input.bam \
        > ~{sample_id}.wham.vcf \
        2> /tmp/whamg.err
    EXITCODE=$?
    echo "$(date) whamg exit code: $EXITCODE"
    cat /tmp/whamg.err >&2
    if [ $EXITCODE -ne 0 ]; then exit $EXITCODE; fi

    bgzip ~{sample_id}.wham.vcf
    echo "$(date) Done."
  >>>

  output {
    File vcf = "~{sample_id}.wham.vcf.gz"
  }

  runtime {
    docker: docker
    memory: "16 GiB"
    cpu: 4
  }
}
