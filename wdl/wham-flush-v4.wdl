version 1.0

workflow WhamFlushV4 {
    input {
        File cram_or_bam
        File cram_or_bam_idx
        String sample_id
        File ref_fasta
        File ref_fasta_fai
        String wham_docker
    }

    call ConvertCram {
        input:
            cram_or_bam = cram_or_bam,
            cram_or_bam_idx = cram_or_bam_idx,
            ref_fasta = ref_fasta,
            ref_fasta_fai = ref_fasta_fai,
            sample_id = sample_id,
            docker = wham_docker
    }

    call RunWhamFlush {
        input:
            bam = ConvertCram.bam,
            bam_idx = ConvertCram.bam_idx,
            sample_id = sample_id,
            ref_fasta = ref_fasta,
            ref_fasta_fai = ref_fasta_fai,
            docker = wham_docker
    }

    output {
        File vcf = RunWhamFlush.vcf
    }
}

task ConvertCram {
    input {
        File cram_or_bam
        File cram_or_bam_idx
        File ref_fasta
        File ref_fasta_fai
        String sample_id
        String docker
    }

    command <<<
        set -eo pipefail

        # Symlink CRAI next to CRAM
        CRAM_DIR=$(dirname ~{cram_or_bam})
        CRAM_BASE=$(basename ~{cram_or_bam})
        ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

        echo "$(date) Converting CRAM to BAM (single-threaded to minimize memory)..."
        /opt/samtools/bin/samtools view -b -@ 1 -T ~{ref_fasta} \
            -o ~{sample_id}.bam ~{cram_or_bam}
        echo "$(date) Indexing BAM..."
        /opt/samtools/bin/samtools index ~{sample_id}.bam
        echo "$(date) BAM conversion done: $(du -sh ~{sample_id}.bam | cut -f1)"
    >>>

    output {
        File bam = "~{sample_id}.bam"
        File bam_idx = "~{sample_id}.bam.bai"
    }

    runtime {
        docker: docker
        memory: "8 GiB"
        cpu: 2
    }
}

task RunWhamFlush {
    input {
        File bam
        File bam_idx
        String sample_id
        File ref_fasta
        File ref_fasta_fai
        String docker
    }

    command <<<
        set -eo pipefail

        # Symlink BAI next to BAM (whamg expects .bam.bai or .bai)
        BAM_DIR=$(dirname ~{bam})
        BAM_BASE=$(basename ~{bam})
        ln -sf ~{bam_idx} "${BAM_DIR}/${BAM_BASE}.bai" || true

        CHROMS="chr1 chr2 chr3 chr4 chr5 chr6 chr7 chr8 chr9 chr10 chr11 chr12 chr13 chr14 chr15 chr16 chr17 chr18 chr19 chr20 chr21 chr22 chrX chrY"

        echo "$(date) Running whamg-flush per chromosome (10 Mbp batches)..."
        FIRST=true
        for CHR in $CHROMS; do
            echo "$(date) Processing $CHR..."
            whamg-flush --flush-per-chr \
                -x 4 \
                -c $CHR \
                -a ~{ref_fasta} \
                -f ~{bam} \
                > /tmp/wham_${CHR}.vcf \
                2>> /tmp/whamg_${CHR}.err

            cat /tmp/whamg_${CHR}.err >&2

            if [ "$FIRST" = true ]; then
                grep '^#' /tmp/wham_${CHR}.vcf > /tmp/wham_combined.vcf
                FIRST=false
            fi
            grep -v '^#' /tmp/wham_${CHR}.vcf >> /tmp/wham_combined.vcf || true
            rm -f /tmp/wham_${CHR}.vcf /tmp/whamg_${CHR}.err
            echo "$(date) $CHR done."
        done

        echo "$(date) All chromosomes done. Sorting..."
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
        memory: "16 GiB"
        cpu: 4
    }
}
