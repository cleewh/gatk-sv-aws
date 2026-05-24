version 1.0

workflow WhamLeanChr22V4 {
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

        # Step 1: Convert full CRAM to BAM (sequential FUSE read, single-threaded)
        echo "$(date) Converting CRAM to BAM..."
        /opt/samtools/bin/samtools view -b -@ 1 -T ~{ref_fasta} \
            -o /tmp/input.bam ~{cram_or_bam}
        /opt/samtools/bin/samtools index -@ 4 /tmp/input.bam
        echo "$(date) Full BAM ready: $(du -sh /tmp/input.bam | cut -f1)"

        # Step 2: Extract chr22 using BAM index (htslib multi-threaded, instant)
        echo "$(date) Extracting chr22 from local BAM (indexed, fast)..."
        /opt/samtools/bin/samtools view -b -@ 8 /tmp/input.bam chr22 > /tmp/chr22.bam
        /opt/samtools/bin/samtools index /tmp/chr22.bam
        echo "$(date) chr22 BAM: $(du -sh /tmp/chr22.bam | cut -f1)"

        # Remove full BAM to free disk
        rm -f /tmp/input.bam /tmp/input.bam.bai

        # Step 3: Run whamg-lean on small chr22 BAM (~2 GB, fast scan)
        echo "$(date) Running whamg-lean --lean on chr22 BAM..."
        whamg-lean --lean \
            -x 8 \
            -c chr22 \
            -a ~{ref_fasta} \
            -f /tmp/chr22.bam \
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
        memory: "96 GiB"
        cpu: 8
    }
}
