# GATK-SV on AWS HealthOmics — Collaborator Review Guide

This guide is for collaborators reviewing a **port of the Broad Institute
[GATK-SV](https://github.com/broadinstitute/gatk-sv) structural-variant pipeline**
from Terra/Cromwell-on-GCP to **AWS HealthOmics** (region `ap-southeast-1`). It
explains exactly what was run, on what inputs, and where every output (intermediate
and final) lives so you can check correctness against an upstream/Terra run.

- **Code repository:** https://github.com/cleewh/gatk-sv-aws
- **Cohort run reviewed here:** `gatk-sv-156-smoke-test` — a **10-sample** end-to-end run
  (the "156" in the name is the parent staged cohort; this run is only the 10 samples below)
- **Reference build:** GRCh38
- **Pipeline scope:** all 19 GATK-SV v1.0 modules, `GatherSampleEvidence` → `MainVcfQC`,
  including the GQ_Recalibrator chain
- **Caller set:** Manta, Wham, Scramble, GATK-gCNV (**MELT excluded** — see caveat below)

> **One thing to know up front:** GATK-SV produces a **joint, multi-sample cohort VCF** —
> the final SV calls for all 10 samples are in a single VCF (with one genotype column per
> sample), not ten separate per-sample VCFs. The per-sample folders contain the Phase A
> *evidence* (per-caller intermediates); the cohort-level VCFs are where you'll check the
> actual variant calls and genotypes.

---

## 1. Input samples (10)

All ten are 1000 Genomes Project samples (GRCh38 CRAMs), so you can cross-reference calls
against published 1000G SV callsets.

| # | Sample ID | Sex (1000G panel) |
|---|---|---|
| 1 | HG00096 | male |
| 2 | HG00129 | male |
| 3 | HG00140 | male |
| 4 | HG00150 | male |
| 5 | HG00187 | male |
| 6 | HG00239 | female |
| 7 | HG00277 | female |
| 8 | HG00288 | female |
| 9 | HG00337 | female |
| 10 | HG00349 | female |

(5 male + 5 female; sex is used by the cn.MOPS / gCNV allosome handling.)

---

## 2. What was run — all 19 modules, four phases

Execution engine per module: **HO** = native AWS HealthOmics; **EC2** = EC2 + miniwdl
hybrid (same WDL, same Docker image, same engine miniwdl — run on a plain EC2 VM because
those modules trip a HealthOmics scheduler issue; the algorithm/output is unchanged).

### Phase A — per-sample evidence (once per sample)

| Step | Module | Engine | What it produces |
|---|---|---|---|
| A.1 | GatherSampleEvidence — CollectCounts (`cc`) | HO | read-depth counts (`.counts.tsv.gz`) |
| A.2 | GatherSampleEvidence — CollectSVEvidence (`cse`) | HO | PE / SR / SD evidence (`.pe.txt.gz`, `.sr.txt.gz`, `.sd.txt.gz`) |
| A.3 | GatherSampleEvidence — Manta | HO | per-sample Manta SV VCF |
| A.4 | GatherSampleEvidence — Wham | HO | per-sample Wham SV VCF (upstream `whamg`) |
| A.5 | GatherSampleEvidence — Scramble | EC2 | per-sample MEI VCF (`INS:ME:ALU/L1/SVA`) |
| A.6 | EvidenceQC | HO | bincov matrix, median coverage, ploidy, WGD scores |

### Phase B — cohort modules (once per cohort)

| Step | Module | Engine | What it produces |
|---|---|---|---|
| B.1 | GatherBatchEvidence (incl. TrainGCNV gCNV cohort-mode) | HO | merged PE/SR/RD/BAF evidence, gCNV calls |
| B.2 | ClusterBatch | HO | clustered depth/manta/wham/scramble VCFs |
| B.3 | GenerateBatchMetrics | HO | per-batch QC metrics |
| B.4 | FilterBatch | HO | frequency-filtered sites + outlier-sample list |
| B.5 | MergeBatchSites | HO | cross-batch combined sites VCF |
| B.6 | GenotypeBatch | HO | genotyped PESR + depth VCFs |
| — | RegenotypeCNVs | (skipped) | only activates for cohorts ≥ 100 samples (Req 19.6) |

### Phase C — post-processing (once per cohort)

| Step | Module | Engine | What it produces |
|---|---|---|---|
| C.0 | MakeCohortVcf (CombineBatches → ResolveCPX → GenotypeCPX → CleanVcf) | EC2 | the cleaned joint cohort VCF |
| C.1 | RefineComplexVariants | HO | complex-SV-refined cohort VCF |
| C.2 | JoinRawCalls (GQ chain 1/4) | EC2 | joined raw-call VCF + ploidy table |
| C.3 | SVConcordance (GQ chain 2/4) | EC2 | concordance-annotated VCF (adds `CONC_ST`) |
| C.4 | ScoreGenotypes (GQ chain 3/4) | EC2 | GQ-recalibrated VCF (adds `SL`, `OGQ`) |
| C.5 | FilterGenotypes (GQ chain 4/4) | EC2 | genotype-filtered + header-sanitized cohort VCF |

### Phase D — delivery (once per cohort)

| Step | Module | Engine | What it produces |
|---|---|---|---|
| D.1 | AnnotateVcf | EC2 | functionally annotated cohort VCF (PREDICTED_* + AF/AC/AN) |
| D.2 | MainVcfQC | EC2 | cohort QC plot tarball (86 plots) + duplicate TSVs + vcf2bed |
| D.3 | VisualizeCnvs | (optional) | per-CNV PNGs (opt-in; not run here) |

Full per-module porting detail (workflow IDs, divergences, memory tuning) is in the repo:
[`README.md`](README.md), [`docs/scope-inventory.md`](docs/scope-inventory.md),
[`docs/wdl-audit.md`](docs/wdl-audit.md), [`docs/divergence-log.md`](docs/divergence-log.md).

---

## 3. The cohort VCF as it flows through post-processing (correctness checkpoints)

These are the numbers we measured; they should be in the right ballpark vs an upstream run.
The **FORMAT field evolving correctly at each step** is the strongest sign each module did
its real work (not just pass data through):

| Stage | File (in `cohort.zip`) | Records | FORMAT keys (what each step adds) |
|---|---|---:|---|
| MakeCohortVcf cleaned | `make-cohort-vcf-ec2/cleaned/…cleaned.vcf.gz` | 13,514 | `GT:ECN:EV:GQ:PE_GQ:PE_GT:SR_GQ:SR_GT` |
| JoinRawCalls (raw, pre-merge) | `join-raw-calls/…join_raw_calls.vcf.gz` | 46,848 | `GT:ECN` (raw) |
| SVConcordance | `sv-concordance/…sv_concordance.vcf.gz` | 13,514 | **+`CONC_ST`** |
| ScoreGenotypes | `score-genotypes/…gq_recalibrated.vcf.gz` | 13,480 | **+`SL` +`OGQ`** |
| FilterGenotypes | `filter-genotypes/…sanitized.vcf.gz` | 12,041 | **+`HIGH_NCR` filter** |
| AnnotateVcf (final) | `annotate-vcf/…annotated.vcf.gz` | 12,041 | adds INFO `PREDICTED_*`, `AF`/`AC`/`AN`, `AF_MALE`/`AF_FEMALE` |

Final SV-type distribution (FilterGenotypes / AnnotateVcf, 12,041 records):
DEL=5,500 · INS=3,172 · DUP=2,085 · BND=1,199 · CPX=63 · CNV=22.

Sanity checks we'd suggest: `AN=20` for a 10-sample diploid cohort; `AC`/`AN`/`AF` internally
consistent; DEL > INS > DUP > BND ordering; the `JoinRawCalls` count being larger than the
cohort count (it's raw/unclustered, pre-merge — expected).

---

## 4. Downloading the outputs (intermediate + final)

The full output tree (≈7.7 GB, 511 files, no input CRAMs) is packaged as **12 zip files**:

| Zip | Contents | Approx size |
|---|---|---|
| `cohort.zip` | All Phase B / C / D **joint cohort** outputs (the variant calls + QC) | ~1.9 GB |
| `HG00096.zip` … `HG00349.zip` (×10) | Each sample's **Phase A** per-caller evidence (cc/cse/manta/wham/scramble + EvidenceQC inputs) | ~510–560 MB each |
| `MANIFEST.txt` | Full file listing (every object, with byte sizes) inside each zip | small |

**Access:** download links (or AWS access) are sent to you **separately** (not in this
public repo, since they grant read access to the data). Start with `MANIFEST.txt` to see
the exact file layout, then pull `cohort.zip` for the variant calls and any per-sample zip
for that sample's evidence.

Each zip unpacks to a folder mirroring the S3 layout, e.g.:

```
cohort/
  make-cohort-vcf-ec2/cleaned/gatk-sv-156-smoke-test.cleaned.vcf.gz
  refine-complex-variants/.../gatk-sv-156-smoke-test.refine_complex.cpx_refined.vcf.gz
  join-raw-calls/gatk-sv-156-smoke-test.join_raw_calls.vcf.gz
  sv-concordance/gatk-sv-156-smoke-test.sv_concordance.vcf.gz
  score-genotypes/gatk-sv-156-smoke-test.score_genotypes.gq_recalibrated.vcf.gz
  filter-genotypes/gatk-sv-156-smoke-test.filter_genotypes.sanitized.vcf.gz
  annotate-vcf/gatk-sv-156-smoke-test.annotated.vcf.gz
  main-vcf-qc/gatk-sv-156-smoke-test.main_vcf_qc_SV_VCF_QC_output.tar.gz   (86 plots)
HG00096/
  gse/cc/...   gse/cse/...   gse/manta/...   gse/wham/...   scramble-real-ec2/...
```

All VCFs are bgzip-compressed with a `.tbi` index; inspect with `bcftools`/`tabix`.

---

## 5. Important caveat — MELT excluded

MELT (Mobile Element Locator Tool) requires a per-user license and is **not** in this port.
Mobile-element-insertion (MEI) sensitivity is therefore reduced vs the full upstream
pipeline. Deletion, duplication, non-MEI insertion, and inversion calling are unaffected.
Scramble (Alu/LINE1/SVA) **is** included and runs on the EC2 hybrid. See
[`docs/divergence-log.md`](docs/divergence-log.md) for the complete list of divergences
from upstream (the only deliberate algorithmic change is the MELT exclusion; everything
else is HealthOmics/miniwdl engine-compatibility patching, documented per-module).

---

## 6. What "porting correctness" means here

The goal of this review is to confirm the AWS HealthOmics port produces results equivalent
to the upstream Terra/Cromwell pipeline. Cross-engine equivalence was already spot-checked
(Manta on NA12878 produced a **bit-identical** body-MD5 between HealthOmics and miniwdl).
Your review of the full cohort VCF against a Terra reference run for these same 10 samples
would close the loop on end-to-end concordance (target gates: ≥99% DEL/DUP, ≥95% INS/INV).

Questions or discrepancies: please open an issue on the GitHub repo or reply on the thread
the links were shared on.
