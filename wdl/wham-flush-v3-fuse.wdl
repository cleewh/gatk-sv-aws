version 1.0

workflow WhamFlushV3Fuse {
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

        # Create symlink so CRAI is co-located with CRAM (required by samtools)
        CRAM_DIR=$(dirname ~{cram_or_bam})
        CRAM_BASE=$(basename ~{cram_or_bam})
        ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

        # Convert CRAM to BAM via streaming (no full pre-localization needed)
        # Write BAM to /tmp which is local disk
        echo "$(date) Converting CRAM to BAM (streaming from FUSE)..."
        /opt/samtools/bin/samtools view -b -@ 4 -T ~{ref_fasta} \
            -o /tmp/input.bam ~{cram_or_bam}
        /opt/samtools/bin/samtools index -@ 4 /tmp/input.bam
        echo "$(date) BAM ready: $(du -sh /tmp/input.bam | cut -f1)"

        # Run whamg-flush on whole genome with --flush-per-chr
        # 10 Mbp batches, flushes graph between batches to bound memory
        # globalPairStore preserved for cross-boundary SV detection
        echo "$(date) Running whamg-flush --flush-per-chr on whole genome..."
        whamg-flush --flush-per-chr \
            -x 4 \
            -a ~{ref_fasta} \
            -f /tmp/input.bam \
            > /tmp/wham_raw.vcf \
            2> /tmp/whamg.err

        echo "$(date) whamg-flush done!"
        cat /tmp/whamg.err >&2

        # Sort VCF (flush mode outputs per-batch, may not be globally sorted)
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
        memory: "16 GiB"
        cpu: 4
    }
}
