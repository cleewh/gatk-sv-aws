# CONTEXT TRANSFER: GATK-SV Pipeline — Session 6 (PIPELINE COMPLETE + VALIDATION)

## Account & Region
- Account: __ACCOUNT_ID__
- Region: ap-southeast-1
- Role: `arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-healthomics-run-role`
- Output bucket: `healthomics-outputs-__ACCOUNT_ID__-apse1`
- Reference bucket: `omics-ref-ap-southeast-1-__ACCOUNT_ID__`

## Cohort
- Cohort id: `gatk-sv-validation-2026q2`
- Batch: `batch_01`
- 10 samples: NA12878, HG00096, HG00097, HG00099, HG00100, HG00101, HG00102, HG00513, NA19238, NA19239

## Pipeline Status (End of Session 6)

| # | Module | Workflow ID | Run ID | Engine | Status |
|---|--------|-------------|--------|--------|--------|
| 1 | GatherSampleEvidence | various | various | HealthOmics | COMPLETE |
| 2 | GatherBatchEvidence | 1575165 (v5) | 6129002 | HealthOmics | COMPLETE |
| 3 | ClusterBatch | 2641017 (v3) | 2870194 | HealthOmics | COMPLETE |
| 4 | GenerateBatchMetrics | 5339393 | 2916467 | HealthOmics | COMPLETE |
| 5 | FilterBatch | 3328339 (v3) | 5070716 | HealthOmics | COMPLETE |
| 6 | MergeBatchSites | 3326995 (v2) | 7287325 | HealthOmics | COMPLETE |
| 7 | GenotypeBatch | 9542089 | 3154916 | HealthOmics | COMPLETE |
| 8 | RegenotypeCNVs | 8299455 | --- | (skipped) | --- |
| 9a | MakeCohortVcf.CombineBatches | --- | --- | EC2 bash | COMPLETE |
| 9b | MakeCohortVcf.{Resolve,Genotype,Clean,Qc} | --- | --- | EC2 + miniwdl | COMPLETE |
| 10 | AnnotateVcf | 6832584 | 9839171 | HealthOmics | COMPLETE |

**Final annotated cohort VCF:**
`s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/annotate-vcf/9839171/out/annotated_vcf/gatk-sv-validation-2026q2.annotated.annotated.vcf.gz`

- 18,703 SV variants (DEL=9635, INS=4485, DUP=2682, BND=1728, CPX=145, CNV=28)
- 10 samples present
- Full GATK-SV functional annotation set (PREDICTED_LOF, PREDICTED_INTRAGENIC_EXON_DUP, PREDICTED_NEAREST_TSS, gnomAD-SV AFs across 11 populations, etc.)

## The HealthOmics 47-Second Kill Issue (Definitive Findings)

After 17 bundle versions (v2 through v17 of MakeCohortVcf), 5 failed runs of various RemainingSteps variants, and a controlled diagnostic, we proved:

**Reproducible**: HealthOmics terminates `gatk GroupedSVCluster` and `svtk resolve` (separate Docker images, separate tools) at exactly **47.0 +/- 1.0 s** of in-container execution time. No CloudWatch logs delivered. Status: `RUN_TASK_FAILED`.

**Tested without effect** -- all still kill at 47s:
- Memory: 3.75 -> 16 -> 30 GiB
- CPU: 1 -> 2 -> 4 vCPUs
- Instance: omics.c.large, m.large, r.large, c.xlarge, m.xlarge, r.xlarge
- Storage: DYNAMIC and STATIC (2400 GiB)
- Concurrency: 24-way scatter down to single sequential task
- Bundle shape: tarball'd track files, flat track files, single combined task
- WDL version: 1.0
- logLevel=ALL

**Critical pattern**: A single-task **diagnostic WDL** running the exact same `gatk GroupedSVCluster` command (workflow `8667186`, run `5601461`) **completed cleanly in 44s**. Same image, same arguments. The kill only triggers when the failing task is invoked from inside a multi-import workflow with deep sub-workflow chains (specifically `MakeCohortVcf` and `MakeCohortVcfRemainingSteps`).

**Workaround applied**: Hybrid HealthOmics + EC2/miniwdl execution for `MakeCohortVcf` only. Every other module runs natively on HealthOmics.

**Open**: AWS Support case is the recommended next step for root cause.

## Bug Fixes Applied This Session

### 1. Reference data corrected for `MakeCohortVcfRemainingSteps`

The `LINE1_reference.fa` and `HERVK_reference.fa` files in S3 were FASTA placeholders, but `RescueMobileElementDeletions` in `CleanVcf` calls `bedtools coverage` which requires BED format. Replaced with proper Broad files:

| Old (wrong) | New (Broad upstream) |
|---|---|
| `LINE1_reference.fa` (FASTA placeholder) | `LINE1.sorted.bed.gz` + `.tbi` |
| `HERVK_reference.fa` (FASTA placeholder) | `HERVK.sorted.bed.gz` + `.tbi` |
| `gs_gencode.v47.protein_coding.canonical.gtf` | `gencode.v39.CDS.intron.tsv.gz` |
| `cytoBand_hg38.txt` (plain text, no index) | `cytobands_hg38.bed.gz` + `.tbi` |

Source: `gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/` (mirrored via HTTPS at storage.googleapis.com).

### 2. Stratification config + Simple Repeats track (carried from session 5)

Already in session 5 transfer doc; mentioned for completeness.

### 3. Cromwell-vs-miniwdl WDL antipatterns

The upstream Broad WDL has several patterns that work under Cromwell but fail under miniwdl (the engine HealthOmics uses, and the engine we ran on EC2 for the hybrid). Patches applied by `gatk-sv-healthomics/scripts/build_remaining_steps_v2.py`:

| Pattern | Cromwell | miniwdl | Patch |
|---|---|---|---|
| `vcf + ".tbi"` in workflow body | Auto-localizes sibling | Rejects with `InputError` | Add explicit `vcf_indexes` Array[File] inputs through `MakeCohortVcfRemainingSteps`, `ResolveComplexVariants`, `ReshardVcf`, `MainVcfQc`, `CollectQcVcfWide` |
| `rm <localized-input>` in task body | OK (Cromwell stages by copy) | EBUSY (miniwdl uses bind mounts) | Globally soften to `rm -f ... 2>/dev/null \|\| true` via post-pass in builder |
| `mv <localized-input>` in task body | OK | EBUSY | Surgical patch in `RestoreUnresolvedCnv` to `cp` first |

## Validation Work Done This Session

### Cross-engine divergence test (HealthOmics vs miniwdl)

To validate Req 2.4 ("results match upstream within tolerance"), I ran the same Manta task on both engines for NA12878:

- **HealthOmics**: production GSE Manta run `8688241` (workflow `4091926`)
- **EC2 + miniwdl**: same Docker image, same CRAM, same arguments, run via `gatk-sv-healthomics/scripts/run_manta_one_sample_ec2.py`

Result:

| | HealthOmics | EC2/miniwdl |
|---|---|---|
| Records | 13,988 | 13,988 |
| Body MD5 (no `##` headers, sorted) | `3c3d300b2dfc514babdf7f6ab0e757d3` | `3c3d300b2dfc514babdf7f6ab0e757d3` |
| Body diff | **0** | identical |

**Test passes**. This is the strongest possible engine-equivalence evidence at the WDL level.

### Validation harness scaffolded

New code in `kiro-life-sciences/src/kiro_life_sciences/gatk_sv_healthomics/validation/`:

1. **`fuzzy.py`** -- `compare_cohort_vcf_fuzzy(produced, expected, pos_fuzz_bp=50)` for +/-50bp tolerance (Broad's standard concordance metric). 10 unit tests.
2. **`divergence.py`** -- `diff_artifact()` with VCF body normalization (skip `##`, sort records, hash) plus tsv.gz / txt comparators. 7 unit tests.

New tests in `kiro-life-sciences/tests/gatk_sv_healthomics/`:

- `unit/test_fuzzy_concordance.py` -- 10 tests, all pass
- `unit/test_divergence.py` -- 7 tests, all pass
- `acceptance/test_engine_divergence.py` -- Manta NA12878 divergence test, **passing**
- `acceptance/test_validation_concordance.py` -- strict + fuzzy concordance, skips until Broad reference VCF available

### Validation runbook

`gatk-sv-healthomics/docs/validation-runbook.md` documents the three-track validation:
1. Strict + fuzzy cohort concordance vs Broad reference (blocked on Terra run)
2. Cross-engine divergence (DONE for Manta NA12878)
3. End-to-end pipeline run (DONE)

## Workspace housekeeping at end of session

- Deleted 4 Wham source-tree forks (`wham-fork`, `wham-htslib`, `wham-lean`, `wham-streaming`) totaling 2.4 GB. Production-pinned `wham:fast-v5` Docker image in ECR is unaffected.
- Preserved `wham-patch/` (28 KB) which holds `whamg-flush.patch` (the diff against upstream that produces `fast-v5`) plus four Dockerfiles. Reproducible from upstream + patch.
- Moved `gatk-sv-healthomics/` from `/Users/cleewh/Desktop/KiroLS/gatk-sv-healthomics/` to `/Users/cleewh/Desktop/Kiro Projects/Gatk-sv-aws/`.
- Copied 4 GATK-SV-related specs to `/Users/cleewh/Desktop/Kiro Projects/Gatk-sv-aws/.kiro/specs/`:
  - `gatk-sv-healthomics-migration` (main spec)
  - `gbe-pipeline-fix` (bug fix spec from session 3)
  - `step-functions-orchestrator` (orchestration spec)
  - `tiered-wham-memory` (Wham memory tiering spec)

## Critical Constants / Config (unchanged)

**Docker images:**
- gatk: `__ACCOUNT_ID__.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85`
- sv_pipeline: `__ACCOUNT_ID__.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604`
- sv_base_mini: `__ACCOUNT_ID__.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52`
- manta: `__ACCOUNT_ID__.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/manta:2023-09-14-v0.28.3-beta-3f22f94d`
- wham (production): `gatk-sv/wham:fast-v5` -- built from upstream + `wham-patch/whamg-flush.patch`

**EC2:** `i-02c67bb34211a85ed` (m5.2xlarge, currently STOPPED). `/tmp` wiped on reboot, so re-pull Docker images and re-download refs each session.

**SSO**: tokens expire ~1h. When `TokenRetrievalError` occurs, run `aws sso login`.

**User instruction recap**:
- Results MUST be bit-identical to Broad's reference implementation
- Use exact same Docker images as Broad
- DO NOT run anything compute-heavy on user's local laptop -- everything goes to EC2 via SSM
- Concise status updates over lengthy explanations

## Next Logical Steps

1. **GitHub repo setup** -- Create `cleewh/gatk-sv-aws` on GitHub, init git, push.
2. **README update** -- Write a top-level README documenting how each module is ported, which AWS service runs it, and the hybrid pattern for MakeCohortVcf.
3. **AWS Support case** -- Open a ticket for the 47-second kill issue with the diagnostic evidence (single-task vs nested-workflow).
4. **Broad reference VCF** -- When user has a Terra workspace, run upstream pipeline on the same 10-sample cohort to enable strict concordance testing.
5. **Extend divergence test** -- Currently only Manta NA12878 is verified bit-identical. Run the same comparison for at least one more sample and one other tool (Wham? Scramble?) to broaden engine-equivalence evidence.

## Workflow IDs (for reference / cleanup)

The following MakeCohortVcf workflow versions were created during the 47s investigation; all FAILED. Can be deleted to clean up:

5146527, 3584634, 3275497, 7609471, 4112340, 5902498, 5190636, 7294659, 6720771,
3229664, 5502288, 5052070, 7601894, 5124440, 1392917, 3622749, 3866341, 5511763,
2680064, 6507774, 7393610

The diagnostic workflow `8667186` (run `5601461`) is worth keeping as evidence.
