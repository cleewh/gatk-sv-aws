version 1.0

workflow WhamLeanChr22 {
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

        # Pipe: samtools decodes CRAM chr22 → BAM stream → whamg-lean reads from stdin
        # No BAM file on disk needed! samtools handles CRAM decoding, whamg-lean reads BAM stream.
        echo "$(date) Starting pipe: samtools view chr22 | whamg-lean --lean"

        /opt/samtools/bin/samtools view -b -h -@ 4 -T ~{ref_fasta} ~{cram_or_bam} chr22 \
            | whamg-lean --lean \
                -x 4 \
                -c chr22 \
                -a ~{ref_fasta} \
                -f /dev/stdin \
                > /tmp/wham_chr22.vcf \
                2> /tmp/whamg.err

        EXITCODE=$?
        cat /tmp/whamg.err >&2

        if [ $EXITCODE -ne 0 ]; then
            echo "FATAL: whamg-lean exited with code $EXITCODE" >&2
            exit $EXITCODE
        fi

        echo "$(date) whamg-lean chr22 done!"

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
        memory: "16 GiB"
        cpu: 8
    }
}
