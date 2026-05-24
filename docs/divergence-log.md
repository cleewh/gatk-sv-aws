# Divergence Log — Edits Applied to Upstream GATK-SV

This log enumerates every divergence from the upstream GATK-SV WDL sources introduced
by the Migration System, with rationale. It satisfies Requirement 17.2 and consolidates
the machine-readable `divergence.json` files produced per-module by
`package_module` (Task 3.1.5) into a single human-readable record.

## Policy divergences (apply to every module)

### 1. MELT caller excluded

**Requirement**: 2a.3, 2a.4, 2a.5
**Change kind**: `remove_caller`
**Applies to**: Every module that references MELT — in practice
`GatherSampleEvidence`, `GatherBatchEvidence`, `ClusterBatch`, `MakeCohortVcf`.

MELT (Mobile Element Locator Tool) requires a per-user academic/commercial license
from the Scott Devine lab. The upstream GATK-SV pipeline integrates MELT as a
per-sample caller and propagates MELT outputs through the evidence, clustering,
and cohort-VCF assembly stages.

**Accepted tradeoff**: Mobile-element-insertion (MEI) sensitivity is reduced relative
to the upstream pipeline. Deletion, duplication, insertion (non-MEI), and inversion
calling are unaffected.

**Edits applied per MELT-referencing WDL**:

- Remove `task RunMELT { ... }` and any `task *MELT*` tasks.
- Remove `call MELT { ... }` and `call melt.MELT { ... }` statements.
- Remove `runtime { docker: "*/melt*" }` lines.
- Remove `import "MELT.wdl"` statements and exclude `MELT.wdl` from the ZIP.
- Remove `if (run_melt) { ... }` conditional blocks.
- Remove `File? melt_*` input declarations.
- Remove `File? melt_* = MELT.*` output declarations.

Every removal is recorded as a `DivergenceEntry(change_kind="remove_caller", reason="MELT excluded per Req 2a.3 (...)")` in the per-module `divergence.json`.

### 2. GCS URIs rejected at packaging time

**Requirement**: 2.6
**Change kind**: `rewrite_construct`
**Applies to**: Every module whose upstream WDL embeds `gs://` literals.

HealthOmics cannot read `gs://` URIs. The packager fails fast with a
`PackagingError` listing every `gs://` literal found in any of the module's WDLs;
operators must rewrite each to an equivalent `s3://` URI in `ap-southeast-1` (staged
via the Reference Bundle Stager, Task 3.4) before packaging succeeds.

The upstream pipeline pins several reference files (e.g. GRCh38 FASTA, gCNV training
model) to Broad-hosted `gs://` URIs. The Reference Bundle Stager copies those to
regional S3 and returns the resolved `s3://` URIs; operators substitute them into
the WDL before re-running `package_module`.

### 3. Floating container tags rejected

**Requirement**: 3.5
**Change kind**: `swap_container`
**Applies to**: Every module whose upstream WDL references `:latest` or bare image
names.

The Container Registry Map Builder rejects any `runtime.docker` reference ending in
`:latest` or lacking an explicit tag (`canonicalize_image` in `registry/__init__.py`).
Upstream references are pinned to immutable tags or `@sha256:<digest>` references.

## Per-module divergences

Produced automatically by `package_module` with AST-driven MELT surgery. Each
module's `divergence.json` lives alongside its `<module>-bundle.zip` under
`gatk-sv-healthomics/wdl/bundles/<module>/`.

Results from packaging against the current `broadinstitute/gatk-sv` HEAD (commit
`7eb2af1feea9`), verified with HealthOmics `LintAHOWorkflowBundle`:

| Module | Divergences | Bundle size | Lint status |
|---|---|---|---|
| `GatherSampleEvidence` | 37 | 17 KB | ✓ success |
| `GatherBatchEvidence` | 4 | 37 KB | ✓ success |
| `ClusterBatch` | 12 | 24 KB | ✓ success |
| `GenerateBatchMetrics` | 1 | 16 KB | ✓ success |
| `FilterBatch` | 9 | 22 KB | ✓ success |
| `MergeBatchSites` | 0 | 12 KB | ✓ success |
| `GenotypeBatch` | 0 | 12 KB | ✓ success |
| `RegenotypeCNVs` | 0 | 29 KB | ✓ success |
| `MakeCohortVcf` | 0 | 65 KB | ✓ success |
| `AnnotateVcf` | 0 | 17 KB | ✓ success |
| **Total** | **63** | **251 KB** | **10/10 clean** |

Of 63 total MELT divergences, the largest share comes from `GatherSampleEvidence`
(37) because it's the entry point for per-sample SV calling and threads MELT through
every caller-integration path. The four zero-divergence modules don't reference MELT
upstream at all. The `MakeCohortVcf` module has 0 divergences because its
"MELT" references are actually `melted genotypes` strings (unrelated English word).

For full per-module divergence details, inspect each module's `divergence.json`:

```bash
for d in gatk-sv-healthomics/wdl/bundles/*/; do
    echo "=== $(basename $d) ==="
    jq -r '.[] | .reason' "$d/divergence.json" 2>/dev/null | head -5
done
```

## How to regenerate this log

```python
from pathlib import Path
import json

bundles = Path("gatk-sv-healthomics/wdl/bundles")
for module_dir in sorted(bundles.iterdir()):
    div = module_dir / "divergence.json"
    if div.exists():
        entries = json.loads(div.read_text())
        print(f"## {module_dir.name}")
        for e in entries:
            print(f"- {e['change_kind']}: {e['reason']}")
```

## Non-divergences (things we did NOT change)

- Scientific logic of every non-MELT task is preserved (Req 2.4).
- Caller set remains Manta, Wham, Scramble, GATK-gCNV.
- Evidence types (PE, SR, RD, BAF) remain unchanged.
- Cohort VCF schema is unchanged except for the absence of MELT-emitted sites.
- Reference inputs (GRCh38 FASTA, PAR bed, allosome/autosome contigs, gCNV training
  model, gnomAD-SV, GENCODE) remain the same set, only their locations change from
  `gs://` to regional `s3://`.

## Traceability

Every published HealthOmics workflow version is tagged with:

- `upstream_commit`: pinned SHA of the upstream GATK-SV repository at which the
  module was packaged.
- `divergences`: human-readable summary of this log, attached to the
  `WorkflowVersionRecord` persisted in `workflow-versions.json`.

This lets operators reproduce any past cohort result by checking out the recorded
upstream commit and the Migration System at the recorded Python-package version.

## Engine-level deviations from the upstream Cromwell-on-GCP pipeline

These are **runtime behavior differences** between HealthOmics and the upstream
Cromwell engine; they don't affect WDL sources but they shaped the deployment
pattern the Migration System uses.

### A. The 47-second kill on long-running tasks

**Discovered**: 2026-05-23 (session 5), reproduced 2026-05-24 (session 6)
**Symptoms**: For specific tasks, HealthOmics terminates the container at exactly
47.0 ± 1.0 seconds of in-container execution time, with no logs delivered to
CloudWatch and an opaque `RUN_TASK_FAILED` status.

**Tasks confirmed affected**:
- `gatk GroupedSVCluster` (in `MakeCohortVcf.CombineBatches`)
- `svtk resolve` (in `MakeCohortVcf.ResolveComplexVariants` → `ResolveCpxSv`)

**Tasks confirmed NOT affected** (same Docker image, same workflow context):
- A standalone diagnostic WDL running the exact same `gatk GroupedSVCluster`
  command line as a sub-task within MakeCohortVcf — runs to completion in 44 s.
- Manta (1.7 hours of in-container execution) — runs to completion.
- Every per-batch task in the prior pipeline stages (GSE, GBE, ClusterBatch,
  GenerateBatchMetrics, FilterBatch, MergeBatchSites, GenotypeBatch).

**Conditions tested without effect** (still fails at 47 s):
- Memory: 3.75 GiB to 16 GiB
- CPU: 1 to 4 vCPUs
- Instance: `omics.c.large` to `omics.r.xlarge`
- Storage: DYNAMIC and STATIC (2400 GiB)
- Concurrency: from 24-way scatter down to a single sequential task
- Networking: RESTRICTED only (VPC mode not tried)
- Bundle shape: tarball'd track files, flat track files, single combined task
- WDL version: `1.0`

**Behavior pattern**: The kill triggers reliably when the failing task is invoked
from inside `MakeCohortVcf` (a multi-import workflow with a deep sub-workflow
chain). The same task in a single-import diagnostic workflow runs to completion.
This suggests the kill is conditional on workflow-context, not on the task itself.

**Workaround applied**: Hybrid HealthOmics + EC2/miniwdl execution for
`MakeCohortVcf` only. Every other module runs natively on HealthOmics. Concretely:

- `MakeCohortVcf.CombineBatches` (which calls `GroupedSVCluster`) runs on EC2 via
  `gatk-sv-healthomics/scripts/run_combinebatches_ec2.sh`.
- `MakeCohortVcf.{ResolveComplexVariants,GenotypeComplexVariants,CleanVcf,MainVcfQc}`
  runs on EC2 via `gatk-sv-healthomics/scripts/run_remaining_steps_ec2.py` against
  miniwdl (the same WDL engine HealthOmics uses).

**Open follow-up**: AWS Support case to identify the root cause.

### B. Cromwell vs miniwdl behavior differences

These are pre-existing behaviors in the upstream Broad WDL that work under Cromwell
but fail under miniwdl (the engine HealthOmics ships). The Migration System patches
each:

| Pattern | Cromwell behavior | miniwdl behavior | Patch |
|---|---|---|---|
| `vcf + ".tbi"` in workflow body | Auto-localizes sibling `.tbi` next to `.vcf` | Treats as plain string, rejects with `InputError` | Pass `vcf_indexes` as explicit `Array[File]`/`File` inputs through `MakeCohortVcfRemainingSteps`, `ResolveComplexVariants`, `ReshardVcf`, `MainVcfQc`, `CollectQcVcfWide`. |
| `rm <localized-input-path>` in task body | Removes the file (Cromwell stages by copy) | Fails with `Device or resource busy` (miniwdl mounts inputs read-only via bind mount) | Globally soften `rm` of WDL-input placeholders to `rm -f … 2>/dev/null \|\| true` via the post-pass in `build_remaining_steps_v2.py`. |
| `mv <localized-input-path>` in task body | Renames the file | Same EBUSY as `rm` | Surgical patch in `RestoreUnresolvedCnv`: `cp` the input first, then operate on the local copy. |
| Reference file format mismatches | Cromwell-on-GCP relies on Broad's pre-staged GCS files | We previously staged placeholder `LINE1_reference.fa` / `HERVK_reference.fa` (FASTA, not BED) | Replaced with the actual Broad references: `LINE1.sorted.bed.gz`, `HERVK.sorted.bed.gz`, `gencode.v39.CDS.intron.tsv.gz`, `cytobands_hg38.bed.gz` (with `.tbi`). |

All five patches are mechanical edits applied by the
`gatk-sv-healthomics/scripts/build_remaining_steps_v2.py` builder against the
v15 bundle source tree, then the resulting bundle ships unchanged.

## Engine-equivalence evidence (cross-engine divergence test)

To validate that HealthOmics and miniwdl produce identical results for the same
WDL + Docker + inputs (Req 2.4), the Migration System runs a cross-engine
divergence test on a single sample's Manta output:

| Sample | Run config | Result |
|---|---|---|
| NA12878 | HealthOmics native run (workflow `4091926`, run `8688241`) | 13,988 SV records, body MD5 `3c3d300b2dfc514babdf7f6ab0e757d3` |
| NA12878 | EC2 + miniwdl, same Docker image (`gatk-sv/manta:2023-09-14-v0.28.3-beta-3f22f94d`), same CRAM, same arguments | 13,988 SV records, body MD5 `3c3d300b2dfc514babdf7f6ab0e757d3` |

The body MD5 is computed after stripping `##` metadata header lines (which contain
engine-specific timestamps and run IDs) and sorting records. The result is
**bit-identical** between the two engines. Test:

```
RUN_ACCEPTANCE_TESTS=1 pytest \
  kiro-life-sciences/tests/gatk_sv_healthomics/acceptance/test_engine_divergence.py \
  -k NA12878 -v
```

Implementation in
`kiro_life_sciences.gatk_sv_healthomics.validation.divergence.diff_artifact`.
