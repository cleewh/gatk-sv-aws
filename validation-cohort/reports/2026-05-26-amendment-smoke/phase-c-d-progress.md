# 10-sample Phase 8 Amendment Smoke Test — Phase C + D progress

**Cohort:** `gatk-sv-156-smoke-test`
**Samples (10):** HG00096, HG00129, HG00140, HG00150, HG00187, HG00239, HG00277, HG00288, HG00337, HG00349
**Account/region:** __ACCOUNT_ID__ / ap-southeast-1
**Execution model:** EC2/miniwdl hybrid (instance `i-02c67bb34211a85ed`) for all of Phase C/D — native HealthOmics is not viable for the scatter-heavy post-processing modules (JoinRawCalls' 24-way SVCluster scatter trips the HealthOmics multi-task kill; same class of issue forced MakeCohortVcf onto the hybrid).

## Phase C — post-processing ✅ COMPLETE

| Step | Module | Engine | Output records | Notes |
|---|---|---|---|---|
| C.0 | MakeCohortVcf | EC2 miniwdl | 13,514 | CombineBatches (24 contigs) + RemainingSteps. Cleaned cohort VCF. |
| C.1 | RefineComplexVariants | HealthOmics (run 7401347) | — | 32 min. First Phase 8 module validated on real cohort VCF. → `cpx_refined.vcf.gz` |
| C.2 | JoinRawCalls | EC2 miniwdl | 46,848 | Patched `TasksClusterBatch.wdl` SVCluster to tabix-index inputs (miniwdl sibling-index). |
| C.3 | SVConcordance | EC2 miniwdl | — | Patched `SVConcordance.wdl` SVConcordanceTask to tabix-index eval/truth. eval=cpx_refined, truth=JoinRawCalls. |
| C.4 | ScoreGenotypes (GqRecalibrator) | EC2 miniwdl | 13,480 | See "GQ recalibrator root-cause" below. |
| C.5 | FilterGenotypes | EC2 miniwdl | 12,041 | FilterVcf → ConcatVcfs → SanitizeHeader. `run_qc=False` (QC handled in Phase D). 13,480 → 12,041 after SL/NCR genotype filtering. |

### GQ recalibrator root-cause (C.4) — the one real investigation of Phase C/D

The AoU phase-1 v1 public GQ model (`gatk-sv-recalibrator.aou_phase_1.v1.model`) initially failed
`XGBoostMinGqVariantFilter` with `PropertiesTable contains inconsistent numbers of rows` on the
first variant. This was **not** a GATK-build-vs-model schema mismatch. The model JSON declares
exactly **46 properties**; indices 35–45 are 11 genome-context features
(`hg38-RepeatMasker`, `hg38-Segmental-Dups`, `hg38-Simple-Repeats`, `hg38_umap_s100`, `hg38_umap_s24`)
that the recalibrator computes on-the-fly from genome-track BED files. Our ScoreGenotypes input
passed **no `genome_tracks`**, so those 11 properties produced 0 rows while the FORMAT-derived
properties produced N rows → the cardinality mismatch.

**Fixes (all committed):**
1. Staged the 5 canonical genome-track BEDs (+ `.tbi`) from
   `gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/ucsc-genome-tracks/` to
   `s3://omics-ref-.../gatk-sv/reference/GRCh38/ucsc-genome-tracks/` and wired them into
   `ScoreGenotypes.genome_tracks`.
2. Patched `RecalibrateGqTask` (RecalibrateGq.wdl) to tabix-index each localized genome track
   before the GATK call (same miniwdl sibling-index antipattern as JoinRawCalls/SVConcordance);
   repackaged the ScoreGenotypes bundle.
3. After (1)+(2) the recalibrator advanced and then errored
   `VCF does not have AF annotated or enough samples to estimate it
   (min-samples-to-estimate-allele-frequency=100 but there are 10 samples)` — the AoU default only
   estimates AF from genotypes for ≥100-sample cohorts. Set
   `recalibrate_gq_args = ["--min-samples-to-estimate-allele-frequency", "1"]` for the small
   smoke cohort. Re-ran → RecalibrateGqTask exit 0, ConcatVcfs exit 0, ScoreGenotypes done.

## Phase D — delivery ✅ COMPLETE

| Step | Module | Engine | Output | Notes |
|---|---|---|---|---|
| D.1 | AnnotateVcf | EC2 miniwdl | 12,041-record annotated VCF | Per-contig scatter (chr1–chr22, chrX, chrY) all exit 0 on EC2 miniwdl. protein_coding_gtf=gencode v47, noncoding_bed=gs_noncoding. |
| D.2 | MainVcfQC | EC2 miniwdl | **86 QC plots** + duplicate TSVs + vcf2bed | Patched `PlotQcPerFamily` `rm ~{ped_file} ~{samples_list}` → `rm -f … \|\| true` (miniwdl localizes inputs read-only, so the unconditional rm aborted the task — the documented IdentifyDuplicates-stage kill). `sv_pipeline_qc_docker` points at the `sv-pipeline` image (no separate `-qc` repo). |

### Phase C/D defects found & fixed
1. **GQ model genome_tracks gap + small-cohort AF** (C.4) — see root-cause above. The two-part fix (`genome_tracks` + `--min-samples-to-estimate-allele-frequency 1`) is now in `scripts/run_gq_chain_ec2.py` and the `.gq-sg-inputs.json` template.
2. **RecalibrateGqTask genome-track sibling index** (C.4) — miniwdl doesn't co-localize sibling `.tbi` for `Array[File] genome_tracks`; patched the task to tabix-index each track. Committed in `wdl/bundles/ScoreGenotypes/v1-build-src` + repackaged bundle.
3. **MainVcfQC `rm` of localized read-only input** (D.2) — `PlotQcPerFamily` unconditionally `rm`'d the localized ped/samples files; made non-fatal. Committed in `wdl/bundles/MainVcfQC/v1-build-src` + repackaged bundle.
4. **MainVcfQC workflow-name casing** (D.2) — bundle/file is `MainVcfQC` but the WDL `workflow` is `MainVcfQc`; inputs must use the `MainVcfQc.` key prefix.

## Output artifacts (S3, `s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/gatk-sv-156-smoke-test/batch/`)
- `make-cohort-vcf-ec2/cleaned/…cleaned.vcf.gz` (13,514)
- `refine-complex-variants/…cpx_refined.vcf.gz`
- `join-raw-calls/…join_raw_calls.vcf.gz` (46,848) + `…ploidy.tsv`
- `sv-concordance/…sv_concordance.vcf.gz`
- `score-genotypes/…score_genotypes.gq_recalibrated.vcf.gz` (13,480)
- `filter-genotypes/…filter_genotypes.sanitized.vcf.gz` (12,041)
- `annotate-vcf/…annotated.vcf.gz` (12,041)
- `main-vcf-qc/…main_vcf_qc_SV_VCF_QC_output.tar.gz` (86 plots) + duplicate TSVs + vcf2bed

## GQ reference artifacts staged (were missing before this run)
- `gatk-sv-recalibrator.aou_phase_1.v1.model` (AoU phase-1 v1, from gs://gatk-sv-resources-public)
- `aou_sl_cutoff_table.tsv` (published DEL/DUP/INS/INV SL cutoffs)
- `ucsc-genome-tracks/` — the 5 genome-context BEDs (+ .tbi) the GQ model needs for properties 35–45

## Status: PHASE_C_D_COMPLETE — full A→B→C→D pipeline validated end-to-end on the real 10-sample cohort.
All 19 modules exercised. GQ_Recalibrator chain (JoinRawCalls → SVConcordance → ScoreGenotypes →
FilterGenotypes) validated. MainVcfQC produced 86 plots. **Full 156-sample cohort NOT run — gated on
the 8.11 user checkpoint.**
