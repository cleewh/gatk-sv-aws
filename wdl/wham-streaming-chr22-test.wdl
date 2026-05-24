version 1.0

workflow WhamStreamingChr22 {
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

        # Extract chr22 only as BAM (much smaller, ~2 GB)
        echo "$(date) Extracting chr22 from CRAM..."
        /opt/samtools/bin/samtools view -b -@ 2 -T ~{ref_fasta} \
            -o /tmp/chr22.bam ~{cram_or_bam} chr22
        /opt/samtools/bin/samtools index /tmp/chr22.bam
        echo "$(date) chr22 BAM ready: $(du -sh /tmp/chr22.bam | cut -f1)"

        # Run whamg-streaming on chr22 only
        echo "$(date) Running whamg-streaming --streaming on chr22..."
        whamg-streaming --streaming \
            -x 4 \
            -c chr22 \
            -a ~{ref_fasta} \
            -f /tmp/chr22.bam \
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
        memory: "16 GiB"
        cpu: 4
    }
}
