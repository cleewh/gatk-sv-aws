version 1.0

workflow WhamStreaming {
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

        # Symlink CRAI next to CRAM for samtools
        CRAM_DIR=$(dirname ~{cram_or_bam})
        CRAM_BASE=$(basename ~{cram_or_bam})
        ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

        # Convert CRAM to BAM (single-threaded, minimal memory)
        echo "$(date) Converting CRAM to BAM..."
        /opt/samtools/bin/samtools view -b -@ 1 -T ~{ref_fasta} \
            -o /tmp/input.bam ~{cram_or_bam}
        /opt/samtools/bin/samtools index /tmp/input.bam
        echo "$(date) BAM ready: $(du -sh /tmp/input.bam | cut -f1)"

        # Run whamg-streaming: sequential read, 10 Mbp flush, pairstore eviction
        # No random access = no FUSE seek penalty
        # Memory bounded by flush + eviction
        echo "$(date) Running whamg-streaming --streaming..."
        whamg-streaming --streaming \
            -x 8 \
            -a ~{ref_fasta} \
            -f /tmp/input.bam \
            > /tmp/wham_raw.vcf \
            2> /tmp/whamg.err

        echo "$(date) whamg-streaming done!"
        cat /tmp/whamg.err >&2

        # Sort VCF (streaming outputs per-flush-batch)
        echo "$(date) Sorting VCF..."
        grep '^#' /tmp/wham_raw.vcf > /tmp/wham_sorted.vcf
        grep -v '^#' /tmp/wham_raw.vcf | sort -k1,1V -k2,2n >> /tmp/wham_sorted.vcf

        bgzip /tmp/wham_sorted.vcf
        cp /tmp/wham_sorted.vcf.gz ~{sample_id}.wham.vcf.gz
        echo "$(date) Done."
    >>>

    output {
        File vcf = "~{sample_id}.wham.vcf.gz"
    }

    runtime {
        docker: docker
        memory: "64 GiB"
        cpu: 8
    }
}
