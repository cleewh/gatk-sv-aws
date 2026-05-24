version 1.0

workflow WhamLeanChr22V3 {
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

        # Symlink CRAI next to CRAM
        CRAM_DIR=$(dirname ~{cram_or_bam})
        CRAM_BASE=$(basename ~{cram_or_bam})
        ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

        # Convert full CRAM to BAM sequentially (no region query, no CRAI needed)
        # This is reliable on FUSE - sequential read only
        echo "$(date) Converting full CRAM to BAM (sequential)..."
        /opt/samtools/bin/samtools view -b -@ 1 -T ~{ref_fasta} \
            -o /tmp/input.bam ~{cram_or_bam}
        /opt/samtools/bin/samtools index /tmp/input.bam
        echo "$(date) Full BAM ready: $(du -sh /tmp/input.bam | cut -f1)"

        # Run whamg-lean on chr22 only (lean mode skips other chromosomes efficiently)
        echo "$(date) Running whamg-lean --lean -c chr22..."
        whamg-lean --lean \
            -x 8 \
            -c chr22 \
            -a ~{ref_fasta} \
            -f /tmp/input.bam \
            > /tmp/wham_chr22.vcf \
            2> /tmp/whamg.err

        EXITCODE=$?
        echo "$(date) whamg-lean exit code: $EXITCODE"
        cat /tmp/whamg.err >&2

        if [ $EXITCODE -ne 0 ]; then
            echo "FATAL: whamg-lean failed" >&2
            exit $EXITCODE
        fi

        # Sort and compress
        grep '^#' /tmp/wham_chr22.vcf > /tmp/sorted.vcf
        grep -v '^#' /tmp/wham_chr22.vcf | sort -k1,1V -k2,2n >> /tmp/sorted.vcf || true
        bgzip /tmp/sorted.vcf
        cp /tmp/sorted.vcf.gz ~{sample_id}.wham.chr22.vcf.gz
        echo "$(date) Done."
    >>>

    output {
        File vcf = "~{sample_id}.wham.chr22.vcf.gz"
    }

    runtime {
        docker: docker
        memory: "32 GiB"
        cpu: 8
    }
}
