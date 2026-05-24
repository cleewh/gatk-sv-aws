# Requirements Document

## Introduction

The GATK-SV pipeline runs wham (a structural variant caller) on per-sample CRAMs via AWS HealthOmics. The current orchestrator (`run_gse_cohort.py`) uses a single hardcoded wham workflow with 16 GiB memory. This works for CRAMs up to ~20 GiB but causes out-of-memory failures on larger files. Since HealthOmics does not support dynamic memory allocation via WDL parameters, two separate workflows with different memory configurations have been deployed. This feature adds tiered memory provisioning logic to the orchestrator so it automatically selects the appropriate workflow based on CRAM file size.

## Glossary

- **Orchestrator**: The `run_gse_cohort.py` Python script that launches HealthOmics workflow runs for a cohort of samples.
- **CRAM**: A compressed alignment file format; the primary input to the wham workflow.
- **Tier**: A memory configuration level mapped to a specific deployed HealthOmics workflow.
- **Standard_Tier**: The 16 GiB memory workflow (ID `2723477`) used for CRAMs ≤ 20 GiB.
- **High_Memory_Tier**: The 30 GiB memory workflow (ID `6217382`) used for CRAMs > 20 GiB.
- **Size_Threshold**: The CRAM file size boundary (20 GiB) that determines tier selection.
- **S3_Size_Query**: A HEAD object request to Amazon S3 that returns the file size without downloading the file.

## Requirements

### Requirement 1: Query CRAM File Size from S3

**User Story:** As a pipeline operator, I want the orchestrator to determine each sample's CRAM file size before launching wham, so that the correct memory tier can be selected.

#### Acceptance Criteria

1. WHEN the Orchestrator prepares to launch a wham run for a sample, THE Orchestrator SHALL issue an S3_Size_Query for that sample's CRAM file to determine its size in bytes.
2. THE Orchestrator SHALL convert the returned size from bytes to gibibytes (GiB) using the formula: size_gib = size_bytes / (1024³).
3. IF the S3_Size_Query fails for a sample, THEN THE Orchestrator SHALL log an error message containing the sample ID and the failure reason, and skip that sample's wham run without terminating the cohort batch.

### Requirement 2: Select Wham Workflow Based on CRAM Size

**User Story:** As a pipeline operator, I want the orchestrator to automatically choose the appropriate wham workflow based on CRAM size, so that large CRAMs use more memory and small CRAMs use the cheaper workflow.

#### Acceptance Criteria

1. WHEN the CRAM size is less than or equal to the Size_Threshold (20 GiB), THE Orchestrator SHALL select the Standard_Tier workflow (ID `2723477`).
2. WHEN the CRAM size is greater than the Size_Threshold (20 GiB), THE Orchestrator SHALL select the High_Memory_Tier workflow (ID `6217382`).
3. THE Orchestrator SHALL use the same workflow parameter interface for both tiers, differing only in the workflow ID passed to HealthOmics.
4. THE Orchestrator SHALL define the Size_Threshold as a configurable constant at the module level, defaulting to 20 GiB (21474836480 bytes).

### Requirement 3: Log Tier Selection

**User Story:** As a pipeline operator, I want the orchestrator to log which memory tier was selected for each sample, so that I can audit decisions and troubleshoot failures.

#### Acceptance Criteria

1. WHEN a tier is selected for a sample, THE Orchestrator SHALL log a message containing the sample ID, the CRAM size in GiB (rounded to one decimal place), and the selected tier name (Standard_Tier or High_Memory_Tier).
2. THE Orchestrator SHALL include the selected workflow ID in the run manifest JSON output for each wham run.

### Requirement 4: Preserve Existing Behavior for Non-Wham Modules

**User Story:** As a pipeline operator, I want the tiered selection logic to apply only to the wham module, so that other modules (manta, cc, scramble, cse) continue to work unchanged.

#### Acceptance Criteria

1. WHEN the module is not wham, THE Orchestrator SHALL select the workflow using the existing single-workflow-ID lookup without performing an S3_Size_Query.
2. THE Orchestrator SHALL not modify the parameter-building logic, output URIs, or storage configuration for non-wham modules.

### Requirement 5: Batch Size Query Efficiency

**User Story:** As a pipeline operator running 100K+ samples, I want CRAM size lookups to be efficient, so that the orchestrator does not add excessive latency before launching runs.

#### Acceptance Criteria

1. THE Orchestrator SHALL query CRAM sizes using S3 HEAD requests (one request per sample), which complete without downloading file content.
2. WHILE processing a cohort, THE Orchestrator SHALL query each sample's CRAM size at most once, caching the result for reuse if needed.
