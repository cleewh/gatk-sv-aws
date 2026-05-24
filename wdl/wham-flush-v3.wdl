version 1.0

workflow WhamFlushV3 {
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

        echo "$(date) Pre-localizing CRAM to local disk..."
        cp ~{cram_or_bam} /tmp/input.cram
        cp ~{cram_or_bam_idx} /tmp/input.cram.crai
        echo "$(date) CRAM pre-localized ($(du -sh /tmp/input.cram | cut -f1))"

        # Convert CRAM to BAM for whamg (whamg needs BAM format)
        echo "$(date) Converting CRAM to BAM..."
        /opt/samtools/bin/samtools view -b -@ 6 -T ~{ref_fasta} -o /tmp/input.bam /tmp/input.cram
        /opt/samtools/bin/samtools index -@ 6 /tmp/input.bam
        echo "$(date) BAM conversion done ($(du -sh /tmp/input.bam | cut -f1))"

        # Remove CRAM to free disk space
        rm -f /tmp/input.cram /tmp/input.cram.crai

        # Run whamg-flush on whole genome with --flush-per-chr
        # This processes in 10 Mbp batches, flushing graph memory between batches
        # Preserves cross-chromosome SV detection via globalPairStore
        echo "$(date) Running whamg-flush --flush-per-chr on whole genome..."
        whamg-flush --flush-per-chr \
            -x 8 \
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
        memory: "32 GiB"
        cpu: 8
    }
}
