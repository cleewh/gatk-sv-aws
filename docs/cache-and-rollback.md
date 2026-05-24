# Run Cache Invalidation and Workflow Version Rollback

Satisfies Requirement 10.5 and 17.6.

## Run Cache lifecycle

HealthOmics Run Caches allow completed task outputs to be re-used across runs,
eliminating re-cost on re-runs after partial failure (Req 10.1, 10.2, 10.4).

### Default configuration

- **One Run Cache per AWS account** (HealthOmics limit).
- **Behavior**: `CACHE_ALWAYS` — every successful task result is cached and available
  for re-use on any subsequent run of the same workflow.
- **Location**: An S3 bucket in `ap-southeast-1` (tagged with `gatk-sv:environment =
  prod`). The Orchestrator reuses the same cache across cohorts; the cache's own
  TTL rules determine when stale entries expire.

### When to invalidate the cache

Invalidate in exactly these situations:

1. **Upstream GATK-SV commit changed**: the WDL task signatures, container images, or
   command lines have changed. Cached outputs may no longer be bit-equivalent to the
   current definition.
2. **Container registry map changed non-trivially**: if a container image digest
   changed (not a no-op alias swap), cached outputs from the old image are stale.
3. **Reference bundle changed**: a reference file was re-staged with different
   content. Cached outputs computed against the old reference are stale.
4. **Regulatory / audit**: your compliance regime requires a clean slate.

### Invalidation procedure

**Recommended (safe)**:

```bash
.venv/bin/python scripts/invalidate_cache.py --create-new
# Creates a new Run Cache, switches the Orchestrator's cache_id pointer.
# Old cache remains read-only until confirmed unused, then deleted with --delete-old.
```

**Direct (AWS CLI)**:

```bash
# List existing caches
aws omics list-run-caches --region ap-southeast-1

# Create a new one
NEW_CACHE_ID=$(aws omics create-run-cache \
    --region ap-southeast-1 \
    --name gatk-sv-run-cache-v2 \
    --cache-s3-location s3://<cache-bucket>/v2/ \
    --cache-behavior CACHE_ALWAYS \
    --tags '{"gatk-sv:environment":"prod"}' \
    --query id --output text)
echo "New cache: $NEW_CACHE_ID"

# Update the orchestrator's configuration to point at the new cache id.
# (The orchestrator keeps its cache-id pointer in a config file; the
# invalidate_cache.py script wraps the swap atomically.)
```

After invalidation, the first cohort run against the new cache re-computes every task
from scratch. Cost accounting will show a spike for that run; subsequent runs benefit
from the newly-populated cache.

### What the cache does not invalidate

- **S3 output artifacts from prior runs** — those remain in your output bucket.
  Operators who want to delete past outputs must do so explicitly; HealthOmics does
  not garbage-collect them.
- **Cost Explorer history** — the tag-based cost attribution survives a cache swap.
- **Registered workflows** — cache invalidation is orthogonal to workflow versioning.

## Workflow version rollback

Every published change to the WDL sources, parameter templates, or container registry
map produces a new HealthOmics workflow version (Req 16.1). Rollback reverts production
traffic to an earlier version.

### Procedure

**Recommended**:

```bash
.venv/bin/python scripts/rollback.py --module GatherSampleEvidence --to 1.2.2
# Re-registers version 1.2.2 as prod, preserving cache and history.
```

**Direct (AWS CLI)**:

```bash
# List versions for a specific workflow
aws omics list-workflow-versions \
    --region ap-southeast-1 \
    --workflow-id <workflow-id>

# Tag an earlier version as `prod`
aws omics update-workflow \
    --region ap-southeast-1 \
    --id <workflow-id> \
    --version-name 1.2.2 \
    --tags '{"gatk-sv:environment":"prod"}'
```

### Invariants preserved across rollback

- **Run Cache persists** — cached outputs from the older version are still valid
  and reused.
- **Cost Explorer history persists** — rolled-back runs tag with the rollback
  version string (`gatk-sv:workflow-version = 1.2.2`).
- **Divergence log persists** — the `workflow-versions.json` record for 1.2.2 remains
  unchanged.

### When rollback is NOT enough

If the bug being rolled back corrupted outputs, cached entries from the buggy version
are also bad. You must combine rollback with cache invalidation:

```bash
.venv/bin/python scripts/rollback.py --module GatherSampleEvidence --to 1.2.2
.venv/bin/python scripts/invalidate_cache.py --create-new
```

Cost impact: one production cohort re-computes from scratch at the rolled-back
version. Subsequent cohorts benefit from the newly-populated cache.

## Audit trail

Every cache change and every workflow version rollback is recorded in:

- `workflow-versions.json` — cumulative log of workflow versions, their upstream
  commits, and their divergence entries.
- Cost Explorer tag history — searchable by `gatk-sv:cohort-id`,
  `gatk-sv:workflow-version`.
- CloudWatch Logs under `/aws/omics/*` — run-level events including orchestrator
  `run-started` / `run-finished` events.

These three together let operators reconstruct exactly which workflow version each
cohort ran against, with which cache, at what cost.
