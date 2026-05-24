version 1.0

workflow WhamLeanChr22Debug {
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
        File memlog = RunWham.memlog
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

        # Start memory monitor in background (writes every 10 sec)
        (while true; do
            echo "$(date +%H:%M:%S) $(free -m | grep Mem | awk '{print "used="$3"M free="$4"M avail="$7"M"}')" >> /tmp/memlog.txt
            echo "$(date +%H:%M:%S) $(free -m | grep Mem | awk '{print "used="$3"M free="$4"M avail="$7"M"}')" >&2
            sleep 10
        done) &
        MONITOR_PID=$!

        # Symlink CRAI
        CRAM_DIR=$(dirname ~{cram_or_bam})
        CRAM_BASE=$(basename ~{cram_or_bam})
        ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

        # Step 1: Convert CRAM to BAM
        echo "$(date) Step 1: Converting CRAM to BAM..." >&2
        /opt/samtools/bin/samtools view -b -@ 1 -T ~{ref_fasta} \
            -o /tmp/input.bam ~{cram_or_bam}
        /opt/samtools/bin/samtools index -@ 4 /tmp/input.bam
        echo "$(date) Full BAM: $(du -sh /tmp/input.bam | cut -f1)" >&2

        # Step 2: Extract chr22
        echo "$(date) Step 2: Extracting chr22..." >&2
        /opt/samtools/bin/samtools view -b -@ 8 /tmp/input.bam chr22 > /tmp/chr22.bam
        /opt/samtools/bin/samtools index /tmp/chr22.bam
        echo "$(date) chr22 BAM: $(du -sh /tmp/chr22.bam | cut -f1)" >&2

        # Remove full BAM
        rm -f /tmp/input.bam /tmp/input.bam.bai
        echo "$(date) Full BAM removed. Disk: $(df -h /tmp | tail -1)" >&2

        # Step 3: Run whamg-lean
        echo "$(date) Step 3: Running whamg-lean --lean -c chr22..." >&2
        whamg-lean --lean \
            -x 4 \
            -c chr22 \
            -a ~{ref_fasta} \
            -f /tmp/chr22.bam \
            > /tmp/wham_chr22.vcf \
            2>> /tmp/whamg.err

        EXITCODE=$?
        echo "$(date) whamg-lean exit code: $EXITCODE" >&2
        cat /tmp/whamg.err >&2

        # Stop monitor
        kill $MONITOR_PID 2>/dev/null || true

        if [ $EXITCODE -ne 0 ]; then
            # Still copy memlog even on failure
            cp /tmp/memlog.txt ~{sample_id}.memlog.txt
            exit $EXITCODE
        fi

        # Sort and compress
        grep '^#' /tmp/wham_chr22.vcf > /tmp/sorted.vcf
        grep -v '^#' /tmp/wham_chr22.vcf | sort -k1,1V -k2,2n >> /tmp/sorted.vcf || true
        bgzip /tmp/sorted.vcf
        cp /tmp/sorted.vcf.gz ~{sample_id}.wham.chr22.vcf.gz
        cp /tmp/memlog.txt ~{sample_id}.memlog.txt
        echo "$(date) Done." >&2
    >>>

    output {
        File vcf = "~{sample_id}.wham.chr22.vcf.gz"
        File memlog = "~{sample_id}.memlog.txt"
    }

    runtime {
        docker: docker
        memory: "32 GiB"
        cpu: 8
    }
}
