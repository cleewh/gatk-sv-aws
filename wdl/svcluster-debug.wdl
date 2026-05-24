version 1.0

workflow SVClusterDebug {
    input {
        Array[File] vcfs
        File reference_fasta
        File reference_fasta_fai
        File reference_dict
        File? ploidy_table
        String contig
        String gatk_docker
    }

    call RunSVClusterDebug {
        input:
            vcfs = vcfs,
            reference_fasta = reference_fasta,
            reference_fasta_fai = reference_fasta_fai,
            reference_dict = reference_dict,
            ploidy_table = ploidy_table,
            contig = contig,
            docker = gatk_docker
    }

    output {
        File debug_log = RunSVClusterDebug.debug_log
        File? output_vcf = RunSVClusterDebug.output_vcf
    }
}

task RunSVClusterDebug {
    input {
        Array[File] vcfs
        File reference_fasta
        File reference_fasta_fai
        File reference_dict
        File? ploidy_table
        String contig
        String docker
    }

    command <<<
        set -x

        echo "=== DEBUG: SVCluster Depth VCF Test ==="
        echo "Date: $(date)"
        echo "Memory: $(cat /proc/meminfo | head -3)"
        echo ""

        # Check GATK version
        echo "=== GATK Version ==="
        gatk --version 2>&1 || echo "gatk --version failed"
        echo ""

        # List input VCFs
        echo "=== Input VCFs ==="
        VCF_LIST=~{write_lines(vcfs)}
        cat "$VCF_LIST"
        echo ""
        echo "Number of VCFs: $(wc -l < $VCF_LIST)"
        echo ""

        # Check first VCF header
        echo "=== First VCF Header ==="
        FIRST_VCF=$(head -1 "$VCF_LIST")
        echo "File: $FIRST_VCF"
        echo "Size: $(ls -la "$FIRST_VCF" 2>/dev/null || echo 'not found')"
        zcat "$FIRST_VCF" 2>/dev/null | head -50 || echo "Failed to read VCF"
        echo ""

        # Build arguments file
        awk '{print "-V "$0}' "$VCF_LIST" > arguments.txt
        echo "=== Arguments file ==="
        cat arguments.txt
        echo ""

        # Run SVCluster with full error output
        echo "=== Running GATK SVCluster ==="
        gatk --java-options "-Xmx6g" SVCluster \
            --arguments_file arguments.txt \
            --output debug_output.vcf.gz \
            --reference ~{reference_fasta} \
            ~{"--ploidy-table " + ploidy_table} \
            -L ~{contig} \
            --fast-mode \
            --depth-sample-overlap 0 \
            --depth-interval-overlap 0.8 \
            --depth-breakend-window 10000000 \
            --variant-prefix "debug_depth_~{contig}_" \
            --verbosity DEBUG \
            2>&1 | tee /tmp/svcluster_output.log || true

        echo ""
        echo "=== Exit code: $? ==="
        echo "=== End of debug ==="

        # Save all output
        cat /tmp/svcluster_output.log > debug_log.txt 2>/dev/null || echo "no log" > debug_log.txt
    >>>

    output {
        File debug_log = "debug_log.txt"
        File? output_vcf = "debug_output.vcf.gz"
    }

    runtime {
        docker: docker
        memory: "8 GiB"
        cpu: 2
    }
}
