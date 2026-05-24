#!/usr/bin/env python3
"""Build a minimal diagnostic WDL bundle that runs *exactly* the same
GroupedSVCluster command that succeeds on EC2 (~13s).

Goal: isolate whether HealthOmics is mangling argument passing, file
localization, or the runtime itself. The task:
  1. Computes checksums of every localized input
  2. Dumps the resolved argument list to stderr and an S3 sentinel
  3. Runs the EXACT same docker / gatk command line as the EC2 script
  4. Streams both stdout and stderr to a CloudWatch-friendly sink AND
     to S3 every 5 seconds via a background tee, so we still see
     output even if HealthOmics kills the task at ~47s

Inputs are positional file URIs; the WDL deliberately has no scatter,
no sub-workflow, no localization_optional, no Array[File] — the
simplest possible shape that still mirrors the failing call.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

OUTDIR = Path("/tmp/groupedsvcluster-diag")
BUNDLE_PATH = Path(
    "gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/"
    "GroupedSVClusterDiag-bundle.zip"
)

WDL = r"""version 1.0

# Diagnostic workflow: runs the same GroupedSVCluster command that
# succeeds on EC2 in ~13s. If this fails on HealthOmics, the failure
# is in HealthOmics itself, not in our pipeline.

workflow GroupedSVClusterDiag {
  input {
    File cluster_sites_vcf
    File cluster_sites_vcf_index
    File ploidy_table
    File reference_fasta
    File reference_fasta_fai
    File reference_dict
    File clustering_config
    File stratification_config
    File track_simrep
    File track_simrep_idx
    File track_segdups
    File track_segdups_idx
    File track_rmsk
    File track_rmsk_idx

    String s3_diag_bucket
    String s3_diag_key_prefix
    String gatk_docker
    String output_prefix = "diag"
  }

  call RunGroupedSVCluster {
    input:
      cluster_sites_vcf            = cluster_sites_vcf,
      cluster_sites_vcf_index      = cluster_sites_vcf_index,
      ploidy_table                 = ploidy_table,
      reference_fasta              = reference_fasta,
      reference_fasta_fai          = reference_fasta_fai,
      reference_dict               = reference_dict,
      clustering_config            = clustering_config,
      stratification_config        = stratification_config,
      track_simrep                 = track_simrep,
      track_simrep_idx             = track_simrep_idx,
      track_segdups                = track_segdups,
      track_segdups_idx            = track_segdups_idx,
      track_rmsk                   = track_rmsk,
      track_rmsk_idx               = track_rmsk_idx,
      s3_diag_bucket               = s3_diag_bucket,
      s3_diag_key_prefix           = s3_diag_key_prefix,
      gatk_docker                  = gatk_docker,
      output_prefix                = output_prefix
  }

  output {
    File reclustered_vcf       = RunGroupedSVCluster.out
    File reclustered_vcf_index = RunGroupedSVCluster.out_index
    File diag_log              = RunGroupedSVCluster.diag_log
    File arg_dump              = RunGroupedSVCluster.arg_dump
    File checksums             = RunGroupedSVCluster.checksums
  }
}

task RunGroupedSVCluster {
  input {
    File cluster_sites_vcf
    File cluster_sites_vcf_index
    File ploidy_table
    File reference_fasta
    File reference_fasta_fai
    File reference_dict
    File clustering_config
    File stratification_config
    File track_simrep
    File track_simrep_idx
    File track_segdups
    File track_segdups_idx
    File track_rmsk
    File track_rmsk_idx

    String s3_diag_bucket
    String s3_diag_key_prefix
    String gatk_docker
    String output_prefix
  }

  command <<<
    set -euxo pipefail

    DIAG=diag.log
    ARGS=args.txt
    SUMS=checksums.txt

    S3_DIAG="s3://~{s3_diag_bucket}/~{s3_diag_key_prefix}"

    {
      echo "== HealthOmics task diagnostic =="
      echo "Started at:    $(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"
      echo "Hostname:      $(hostname)"
      echo "Container:     ~{gatk_docker}"
      echo "Working dir:   $(pwd)"
      echo "Disk free:"
      df -h .
      echo
      echo "== Java / GATK identity =="
      java -version 2>&1 || true
      gatk --version 2>&1 | head -5 || true
      echo
      echo "== gatk GroupedSVCluster --help (first 80 lines) =="
      gatk GroupedSVCluster --help 2>&1 | head -80 || true
      echo
    } > "$DIAG" 2>&1

    # Dump every resolved file path & size — this proves what HO localised.
    {
      echo "== Resolved input paths =="
      for f in \
        "~{cluster_sites_vcf}" \
        "~{cluster_sites_vcf_index}" \
        "~{ploidy_table}" \
        "~{reference_fasta}" \
        "~{reference_fasta_fai}" \
        "~{reference_dict}" \
        "~{clustering_config}" \
        "~{stratification_config}" \
        "~{track_simrep}" \
        "~{track_simrep_idx}" \
        "~{track_segdups}" \
        "~{track_segdups_idx}" \
        "~{track_rmsk}" \
        "~{track_rmsk_idx}" \
      ; do
        if [ -e "$f" ]; then
          ls -la "$f"
        else
          echo "MISSING: $f"
        fi
      done
    } > "$ARGS"

    # Compute md5 of every localised input — compare against EC2-known-good.
    {
      echo "== md5 sums =="
      for f in \
        "~{cluster_sites_vcf}" \
        "~{ploidy_table}" \
        "~{reference_fasta}" \
        "~{clustering_config}" \
        "~{stratification_config}" \
        "~{track_simrep}" \
        "~{track_segdups}" \
        "~{track_rmsk}" \
      ; do
        if [ -f "$f" ]; then
          md5sum "$f"
        else
          echo "MISSING: $f"
        fi
      done
    } > "$SUMS"

    # Best-effort: ship checksums + arg dump up front so we have them
    # even if GATK exits before producing any other output.
    aws s3 cp "$DIAG" "$S3_DIAG/00_diag_pre.log" --quiet || true
    aws s3 cp "$ARGS" "$S3_DIAG/00_args.txt" --quiet || true
    aws s3 cp "$SUMS" "$S3_DIAG/00_checksums.txt" --quiet || true

    # Background streamer: every 5s, push the running gatk log to S3.
    GATK_LOG=gatk.stderr.log
    : > "$GATK_LOG"
    (
      while true; do
        aws s3 cp "$GATK_LOG" "$S3_DIAG/01_gatk.stderr.live.log" --quiet || true
        sleep 5
      done
    ) &
    STREAMER_PID=$!

    # Run EXACTLY the command that succeeds on EC2 in ~13s.
    set +e
    gatk --java-options "-Xmx12g" GroupedSVCluster \
      -V ~{cluster_sites_vcf} \
      -O ~{output_prefix}.recluster_part_1.vcf.gz \
      --reference ~{reference_fasta} \
      --ploidy-table ~{ploidy_table} \
      --clustering-config ~{clustering_config} \
      --stratify-config ~{stratification_config} \
      --track-intervals ~{track_simrep} \
      --track-intervals ~{track_segdups} \
      --track-intervals ~{track_rmsk} \
      --track-name SR \
      --track-name SD \
      --track-name RM \
      --stratify-overlap-fraction 0 \
      --stratify-num-breakpoint-overlaps 1 \
      --stratify-num-breakpoint-overlaps-interchromosomal 1 \
      --breakpoint-summary-strategy REPRESENTATIVE \
      --verbosity DEBUG \
      2>&1 | tee -a "$GATK_LOG"
    GATK_RC=${PIPESTATUS[0]}
    set -e

    kill $STREAMER_PID 2>/dev/null || true

    {
      echo "== gatk exit code: $GATK_RC =="
      echo "Finished at:   $(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"
    } >> "$DIAG"

    # Always push final log + final gatk stderr regardless of rc
    aws s3 cp "$DIAG"      "$S3_DIAG/02_diag_post.log" --quiet || true
    aws s3 cp "$GATK_LOG"  "$S3_DIAG/02_gatk.stderr.final.log" --quiet || true

    if [ $GATK_RC -ne 0 ]; then
      echo "GATK GroupedSVCluster failed with rc=$GATK_RC"
      exit $GATK_RC
    fi
  >>>

  output {
    File out          = "~{output_prefix}.recluster_part_1.vcf.gz"
    File out_index    = "~{output_prefix}.recluster_part_1.vcf.gz.tbi"
    File diag_log     = "diag.log"
    File arg_dump     = "args.txt"
    File checksums    = "checksums.txt"
  }

  runtime {
    docker: gatk_docker
    cpu: 4
    memory: "16 GiB"
    disks: "local-disk 50 SSD"
    maxRetries: 0
  }
}
"""


def main() -> None:
    if OUTDIR.exists():
        shutil.rmtree(OUTDIR)
    wdl_dir = OUTDIR / "wdl"
    wdl_dir.mkdir(parents=True)
    main_wdl = wdl_dir / "GroupedSVClusterDiag.wdl"
    main_wdl.write_text(WDL)
    print(f"✓ Wrote {main_wdl} ({main_wdl.stat().st_size:,} bytes)")

    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if BUNDLE_PATH.exists():
        BUNDLE_PATH.unlink()

    subprocess.run(
        ["zip", "-q", "-r", str(BUNDLE_PATH.resolve()), "wdl/"],
        cwd=OUTDIR,
        check=True,
    )
    size = BUNDLE_PATH.stat().st_size
    print(f"✓ Bundle: {BUNDLE_PATH} ({size:,} bytes)")


if __name__ == "__main__":
    main()
