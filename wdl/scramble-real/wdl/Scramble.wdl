## Scramble (real, 2-task) for AWS HealthOmics
##
## Part1+Part2 fused into one task (scramble image): cluster_identifier in
## 12-way parallel + SCRAMble.R MEI evaluation.
##
## MakeScrambleVcf is a separate task using the sv_pipeline image (where
## make_scramble_vcf.py + bcftools live).
##
## A 3-task version (matching upstream Scramble.wdl exactly) hits the
## HealthOmics 47-second kill, same pattern as gatk GroupedSVCluster /
## svtk resolve.  This 2-task structure has been observed not to trigger
## the kill (confirmed via the existing GBE / ClusterBatch / FilterBatch
## v3+ workflows which have many tasks but stay within the kill envelope).

version 1.0

workflow Scramble {
  input {
    File bam_or_cram_file
    File bam_or_cram_index
    File counts_file
    File input_vcf
    String sample_name
    File reference_fasta
    File reference_index
    File regions_list
    File mei_bed
    Int alignment_score_cutoff = 90
    Float min_clipped_reads_fraction = 0.22
    Int percent_align_cutoff = 70
    String scramble_docker
    String sv_pipeline_docker
  }

  call ScrambleClusterAndEval {
    input:
      bam_or_cram_file = bam_or_cram_file,
      bam_or_cram_index = bam_or_cram_index,
      counts_file = counts_file,
      sample_name = sample_name,
      reference_fasta = reference_fasta,
      reference_index = reference_index,
      regions_list = regions_list,
      alignment_score_cutoff = alignment_score_cutoff,
      min_clipped_reads_fraction = min_clipped_reads_fraction,
      percent_align_cutoff = percent_align_cutoff,
      scramble_docker = scramble_docker,
  }

  call MakeScrambleVcf {
    input:
      scramble_table = ScrambleClusterAndEval.table,
      bam_or_cram_file = bam_or_cram_file,
      bam_or_cram_index = bam_or_cram_index,
      input_vcf = input_vcf,
      sample_name = sample_name,
      reference_fasta = reference_fasta,
      reference_index = reference_index,
      mei_bed = mei_bed,
      sv_pipeline_docker = sv_pipeline_docker,
  }

  output {
    File vcf = MakeScrambleVcf.vcf
    File vcf_index = MakeScrambleVcf.vcf_index
    File table = ScrambleClusterAndEval.table
    File clusters = ScrambleClusterAndEval.clusters
  }
}

task ScrambleClusterAndEval {
  input {
    File bam_or_cram_file
    File bam_or_cram_index
    File counts_file
    String sample_name
    File reference_fasta
    File reference_index
    File regions_list
    Int alignment_score_cutoff
    Float min_clipped_reads_fraction
    Int percent_align_cutoff
    String scramble_docker
  }

  output {
    File clusters = "~{sample_name}.scramble_clusters.tsv.gz"
    File table = "~{sample_name}.scramble.tsv.gz"
  }

  command <<<
    set -eo pipefail
    echo "=== Part1+Part2 fused: cluster_identifier + SCRAMble.R ==="

    # Calibrate cutoff from counts
    zcat ~{counts_file} \
      | awk '$0!~"@"' \
      | sed 1d \
      | awk 'NR % 100 == 0' \
      | cut -f4 \
      | Rscript -e "cat(round(~{min_clipped_reads_fraction}*median(data.matrix(read.csv(file(\"stdin\"))))))" \
      > cutoff.txt
    MIN_CLIPPED_READS=$(cat cutoff.txt)
    echo "$(date) MIN_CLIPPED_READS: ${MIN_CLIPPED_READS}"

    # Symlink CRAI alongside CRAM
    CRAM_DIR=$(dirname ~{bam_or_cram_file})
    CRAM_BASE=$(basename ~{bam_or_cram_file})
    ln -sf ~{bam_or_cram_index} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

    # Part 1: 12-parallel cluster_identifier
    echo "$(date) cluster_identifier 12-parallel..."
    mkdir -p /tmp/clusters
    PIDS=()
    while read region; do
        /app/scramble-gatk-sv/cluster_identifier/src/build/cluster_identifier \
            -l \
            -s ${MIN_CLIPPED_READS} \
            -r "${region}" \
            -t ~{reference_fasta} \
            ~{bam_or_cram_file} > "/tmp/clusters/${region}.txt" &
        PIDS+=($!)
        if (( ${#PIDS[@]} >= 12 )); then
            wait "${PIDS[@]}"
            PIDS=()
        fi
    done < ~{regions_list}
    if (( ${#PIDS[@]} > 0 )); then
        wait "${PIDS[@]}"
    fi

    while read region; do
        cat "/tmp/clusters/${region}.txt"
    done < ~{regions_list} | gzip > ~{sample_name}.scramble_clusters.tsv.gz
    echo "$(date) clusters: $(zcat ~{sample_name}.scramble_clusters.tsv.gz | wc -l) lines"

    # Part 2: SCRAMble.R --eval-meis
    echo "$(date) Building BLAST DB..."
    cat ~{reference_fasta} | makeblastdb -in - -parse_seqids -title ref -dbtype nucl -out ref

    clusterFile=$PWD/clusters
    scrambleDir="/app/scramble-gatk-sv"
    meiRef=$scrambleDir/cluster_analysis/resources/MEI_consensus_seqs.fa

    gunzip -c ~{sample_name}.scramble_clusters.tsv.gz > $clusterFile

    echo "$(date) SCRAMble.R..."
    Rscript --vanilla $scrambleDir/cluster_analysis/bin/SCRAMble.R \
        --out-name $clusterFile \
        --cluster-file $clusterFile \
        --install-dir $scrambleDir/cluster_analysis/bin \
        --mei-refs $meiRef \
        --ref $PWD/ref \
        --no-vcf \
        --eval-meis \
        --cores 7 \
        --pct-align ~{percent_align_cutoff} \
        -n ${MIN_CLIPPED_READS} \
        --mei-score ~{alignment_score_cutoff}

    mv ${clusterFile}_MEIs.txt ~{sample_name}.scramble.tsv
    gzip ~{sample_name}.scramble.tsv
    echo "$(date) Done. MEI candidates: $(zcat ~{sample_name}.scramble.tsv.gz | wc -l)"
  >>>

  runtime {
    docker: scramble_docker
    memory: "32 GiB"
    cpu: 12
  }
}

task MakeScrambleVcf {
  input {
    File scramble_table
    File bam_or_cram_file
    File bam_or_cram_index
    File input_vcf
    String sample_name
    File reference_fasta
    File reference_index
    File mei_bed
    String sv_pipeline_docker
  }

  output {
    File vcf = "~{sample_name}.scramble.vcf.gz"
    File vcf_index = "~{sample_name}.scramble.vcf.gz.tbi"
  }

  command <<<
    set -euxo pipefail

    # Symlink CRAI alongside CRAM
    CRAM_DIR=$(dirname ~{bam_or_cram_file})
    CRAM_BASE=$(basename ~{bam_or_cram_file})
    ln -sf ~{bam_or_cram_index} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

    # make_scramble_vcf.py is shipped at /opt/sv-pipeline/scripts/ in the
    # sv_pipeline image, per upstream Scramble.wdl conventions.
    python /opt/sv-pipeline/scripts/make_scramble_vcf.py \
        --table ~{scramble_table} \
        --input-vcf ~{input_vcf} \
        --alignments-file ~{bam_or_cram_file} \
        --sample ~{sample_name} \
        --reference ~{reference_fasta} \
        --mei-bed ~{mei_bed} \
        --out unsorted.vcf.gz

    bcftools sort unsorted.vcf.gz -Oz -o ~{sample_name}.scramble.vcf.gz
    tabix ~{sample_name}.scramble.vcf.gz
    echo "$(date) Done. Records: $(bcftools view -H ~{sample_name}.scramble.vcf.gz | wc -l)"
  >>>

  runtime {
    docker: sv_pipeline_docker
    memory: "8 GiB"
    cpu: 2
  }
}
