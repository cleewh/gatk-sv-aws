version 1.0

workflow WhamStreamingChr22V2 {
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

        # Convert entire CRAM to BAM sequentially (no region query, no index needed)
        echo "$(date) Converting full CRAM to BAM (sequential, single-threaded)..."
        /opt/samtools/bin/samtools view -b -@ 1 -T ~{ref_fasta} \
            -o /tmp/input.bam ~{cram_or_bam}
        /opt/samtools/bin/samtools index /tmp/input.bam
        echo "$(date) BAM ready: $(du -sh /tmp/input.bam | cut -f1)"

        # Run whamg-streaming on chr22 only (streaming skips other chromosomes)
        echo "$(date) Running whamg-streaming --streaming -c chr22..."
        whamg-streaming --streaming \
            -x 4 \
            -c chr22 \
            -a ~{ref_fasta} \
            -f /tmp/input.bam \
            > /tmp/wham_chr22.vcf \
            2> /tmp/whamg.err

        echo "$(date) whamg-streaming chr22 done!"
        cat /tmp/whamg.err >&2

        bgzip /tmp/wham_chr22.vcf
        cp /tmp/wham_chr22.vcf.gz ~{sample_id}.wham.chr22.vcf.gz
        echo "$(date) Done."
    >>>

    output {
        File vcf = "~{sample_id}.wham.chr22.vcf.gz"
    }

    runtime {
        docker: docker
        memory: "32 GiB"
        cpu: 4
    }
}
