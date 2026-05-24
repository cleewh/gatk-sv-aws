version 1.0

# CollectReadCounts parallelized: split intervals into 4 shards,
# run 4 concurrent GATK processes, concatenate in order.
# Expected runtime: ~25-30 min (vs 75-90 min sequential).

workflow CollectCountsParallel {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File ref_fasta_dict
    File gatk_jar
    File intervals
    String docker
  }

  call T {
    input:
      cram_or_bam = cram_or_bam,
      cram_or_bam_idx = cram_or_bam_idx,
      sample_id = sample_id,
      ref_fasta = ref_fasta,
      ref_fasta_fai = ref_fasta_fai,
      ref_fasta_dict = ref_fasta_dict,
      gatk_jar = gatk_jar,
      intervals = intervals,
      docker = docker
  }

  output {
    File counts = T.counts
  }
}

task T {
  input {
    File cram_or_bam
    File cram_or_bam_idx
    String sample_id
    File ref_fasta
    File ref_fasta_fai
    File ref_fasta_dict
    File gatk_jar
    File intervals
    String docker
  }

  command <<<
    set -eo pipefail

    # CRAM index symlink for FUSE access
    CRAM_DIR=$(dirname ~{cram_or_bam})
    CRAM_BASE=$(basename ~{cram_or_bam})
    ln -sf ~{cram_or_bam_idx} "${CRAM_DIR}/${CRAM_BASE}.crai" || true

    # Split intervals into 4 shards using awk
    echo "Splitting intervals into 4 shards..."
    HEADER_FILE=/tmp/header.txt
    BODY_FILE=/tmp/body.txt
    grep '^@' ~{intervals} > "$HEADER_FILE"
    grep -v '^@' ~{intervals} > "$BODY_FILE"
    TOTAL=$(wc -l < "$BODY_FILE")
    CHUNK=$(( (TOTAL + 3) / 4 ))

    awk -v chunk="$CHUNK" -v hdr="$HEADER_FILE" '
      BEGIN { shard=0; count=0 }
      {
        if (count == 0) {
          outfile = "/tmp/shard_" shard ".interval_list"
          while ((getline line < hdr) > 0) print line > outfile
          close(hdr)
        }
        print > ("/tmp/shard_" shard ".interval_list")
        count++
        if (count >= chunk) { shard++; count=0 }
      }
    ' "$BODY_FILE"

    echo "Running 4 parallel CollectReadCounts..."
    mkdir -p /tmp/counts

    java -Xmx1500m -jar ~{gatk_jar} CollectReadCounts \
      -I ~{cram_or_bam} -L /tmp/shard_0.interval_list -R ~{ref_fasta} \
      --format TSV --interval-merging-rule OVERLAPPING_ONLY \
      -O /tmp/counts/s0.tsv 2>/tmp/counts/s0.log &

    java -Xmx1500m -jar ~{gatk_jar} CollectReadCounts \
      -I ~{cram_or_bam} -L /tmp/shard_1.interval_list -R ~{ref_fasta} \
      --format TSV --interval-merging-rule OVERLAPPING_ONLY \
      -O /tmp/counts/s1.tsv 2>/tmp/counts/s1.log &

    java -Xmx1500m -jar ~{gatk_jar} CollectReadCounts \
      -I ~{cram_or_bam} -L /tmp/shard_2.interval_list -R ~{ref_fasta} \
      --format TSV --interval-merging-rule OVERLAPPING_ONLY \
      -O /tmp/counts/s2.tsv 2>/tmp/counts/s2.log &

    java -Xmx1500m -jar ~{gatk_jar} CollectReadCounts \
      -I ~{cram_or_bam} -L /tmp/shard_3.interval_list -R ~{ref_fasta} \
      --format TSV --interval-merging-rule OVERLAPPING_ONLY \
      -O /tmp/counts/s3.tsv 2>/tmp/counts/s3.log &

    wait
    echo "All shards complete."

    # Concatenate: header from shard 0, data from all in order
    grep '^#\|^CONTIG' /tmp/counts/s0.tsv > ~{sample_id}.counts.tsv
    grep -v '^#\|^CONTIG' /tmp/counts/s0.tsv >> ~{sample_id}.counts.tsv
    grep -v '^#\|^CONTIG' /tmp/counts/s1.tsv >> ~{sample_id}.counts.tsv
    grep -v '^#\|^CONTIG' /tmp/counts/s2.tsv >> ~{sample_id}.counts.tsv
    grep -v '^#\|^CONTIG' /tmp/counts/s3.tsv >> ~{sample_id}.counts.tsv

    bgzip ~{sample_id}.counts.tsv
    echo "Done."
  >>>

  output {
    File counts = "~{sample_id}.counts.tsv.gz"
  }

  runtime {
    docker: docker
    memory: "8 GiB"
    cpu: 4
  }
}
