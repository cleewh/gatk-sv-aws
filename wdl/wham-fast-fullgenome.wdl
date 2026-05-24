version 1.0

workflow WhamFast {
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

        # Symlink CRAI
        CRAM_DIR=$(dirname ~{cram_or_bam})
        CRAM_BASE=$(basename ~{cram_or_bam})
        ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

        # Convert CRAM to BAM (multi-threaded samtools via FUSE)
        echo "$(date) Converting CRAM to BAM..."
        /opt/samtools/bin/samtools view -b -@ 8 -T ~{ref_fasta} \
            -o /tmp/input.bam ~{cram_or_bam}
        /opt/samtools/bin/samtools index -@ 8 /tmp/input.bam
        echo "$(date) BAM ready: $(du -sh /tmp/input.bam | cut -f1)"

        # Run whamg-fast (original region-based architecture + getRefBases fix)
        # Uses OpenMP parallel region processing — keeps pairstore bounded
        echo "$(date) Running whamg-fast -x 16 on full genome..."
        whamg-fast \
            -x 16 \
            -a ~{ref_fasta} \
            -f /tmp/input.bam \
            > ~{sample_id}.wham.vcf \
            2> /tmp/whamg.err

        EXITCODE=$?
        echo "$(date) whamg-fast exit code: $EXITCODE"
        cat /tmp/whamg.err >&2

        if [ $EXITCODE -ne 0 ]; then
            exit $EXITCODE
        fi

        bgzip ~{sample_id}.wham.vcf
        echo "$(date) Done."
    >>>

    output {
        File vcf = "~{sample_id}.wham.vcf.gz"
    }

    runtime {
        docker: docker
        memory: "16 GiB"
        cpu: 16
    }
}
