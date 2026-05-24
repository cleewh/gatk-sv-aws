version 1.0

workflow WhamFlushV3PerChr {
    input {
        File cram_or_bam
        File cram_or_bam_idx
        String sample_id
        File ref_fasta
        File ref_fasta_fai
        String wham_docker
    }

    # Process all standard chromosomes sequentially with flush mode
    # Each chromosome uses bounded memory via 10 Mbp batch flushing
    call RunWhamAllChrs {
        input:
            cram_or_bam = cram_or_bam,
            cram_or_bam_idx = cram_or_bam_idx,
            sample_id = sample_id,
            ref_fasta = ref_fasta,
            ref_fasta_fai = ref_fasta_fai,
            docker = wham_docker
    }

    output {
        File vcf = RunWhamAllChrs.vcf
    }
}

task RunWhamAllChrs {
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

        # Create symlink so CRAI is co-located with CRAM
        CRAM_DIR=$(dirname ~{cram_or_bam})
        CRAM_BASE=$(basename ~{cram_or_bam})
        ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

        # Convert CRAM to BAM via FUSE streaming (sequential read, memory-safe)
        echo "$(date) Converting CRAM to BAM (streaming via FUSE)..."
        /opt/samtools/bin/samtools view -b -@ 2 -T ~{ref_fasta} \
            -o /tmp/input.bam ~{cram_or_bam}
        /opt/samtools/bin/samtools index -@ 2 /tmp/input.bam
        echo "$(date) BAM ready: $(du -sh /tmp/input.bam | cut -f1)"

        CHROMS="chr1,chr2,chr3,chr4,chr5,chr6,chr7,chr8,chr9,chr10,chr11,chr12,chr13,chr14,chr15,chr16,chr17,chr18,chr19,chr20,chr21,chr22,chrX,chrY"

        # Run whamg-flush per chromosome with --flush-per-chr
        # Each chromosome processed independently with 10 Mbp batch flushing
        # This bounds both graph memory AND pairstore size
        echo "$(date) Running whamg-flush --flush-per-chr per chromosome..."
        FIRST=true
        for CHR in $(echo $CHROMS | tr ',' ' '); do
            echo "$(date) Processing $CHR..."
            whamg-flush --flush-per-chr \
                -x 4 \
                -c $CHR \
                -a ~{ref_fasta} \
                -f /tmp/input.bam \
                > /tmp/wham_${CHR}.vcf \
                2>> /tmp/whamg.err

            # Collect results: header from first, data from all
            if [ "$FIRST" = true ]; then
                grep '^#' /tmp/wham_${CHR}.vcf > /tmp/wham_combined.vcf
                FIRST=false
            fi
            grep -v '^#' /tmp/wham_${CHR}.vcf >> /tmp/wham_combined.vcf || true
            rm -f /tmp/wham_${CHR}.vcf
            echo "$(date) $CHR done."
        done

        cat /tmp/whamg.err >&2
        echo "$(date) All chromosomes done. Sorting..."

        # Sort combined VCF
        grep '^#' /tmp/wham_combined.vcf > /tmp/wham_sorted.vcf
        grep -v '^#' /tmp/wham_combined.vcf | sort -k1,1V -k2,2n >> /tmp/wham_sorted.vcf

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
        cpu: 4
    }
}
