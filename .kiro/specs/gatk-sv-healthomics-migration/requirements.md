# Requirements Document

## Introduction

This feature migrates the Broad Institute's GATK-SV (Structural Variant discovery and genotyping) pipeline from its native Terra/Cromwell on GCP environment to AWS HealthOmics, deployed in the Singapore region (ap-southeast-1). The scope is full end-to-end: all GATK-SV modules from GatherSampleEvidence through AnnotateVcf are migrated. The migration MUST preserve the scientific correctness of the upstream pipeline while adapting it for HealthOmics execution, and MUST apply cost optimization techniques across compute, storage, container distribution, and run-caching dimensions to meet a per-sample cost target of USD $7.00.

GATK-SV is a multi-stage, multi-sample WDL workflow that combines evidence from several SV callers (Manta, Wham, Scramble, GATK-gCNV) across germline short-read samples to produce a joint-called SV VCF. MELT (Mobile Element Locator Tool) is excluded from this migration; the reduced mobile-element-insertion sensitivity that results is an accepted tradeoff. The pipeline is organized as a series of sub-workflows (GatherSampleEvidence, GatherBatchEvidence, ClusterBatch, GenerateBatchMetrics, FilterBatch, MergeBatchSites, GenotypeBatch, RegenotypeCNVs, MakeCohortVcf, AnnotateVcf) with shared reference data and many Broad-maintained container images.

The deliverable is a HealthOmics-registered, production-grade set of workflows mirroring the GATK-SV module boundaries, with associated container registry mappings, parameter templates, IAM role, and operational documentation for running cohorts in ap-southeast-1 at or below USD $7.00 per sample.

## Glossary

- **GATK_SV**: The upstream GATK-SV WDL pipeline from the Broad Institute, source-of-truth at https://github.com/broadinstitute/gatk-sv.
- **Migration_System**: The adapted GATK-SV pipeline package (WDL sources, parameter templates, container registry map, IAM role, documentation) that runs on AWS HealthOmics.
- **HealthOmics**: AWS HealthOmics managed workflow service.
- **Target_Region**: AWS region ap-southeast-1 (Singapore).
- **Workflow_Registration**: The act of creating a HealthOmics workflow resource via CreateWorkflow for each GATK-SV module selected for migration.
- **Linter**: The WDL validation tool exposed by HealthOmics (LintWorkflowDefinition / LintWorkflowBundle).
- **Container_Registry_Map**: The JSON mapping that redirects upstream container references (Docker Hub, GCR, Quay.io) to private ECR repositories accessible by HealthOmics.
- **Pull_Through_Cache**: ECR pull-through cache rule configured for HealthOmics use.
- **Parameter_Template**: JSON schema provided at workflow creation time that defines each workflow input, its type, description, and optional flag.
- **Reference_Bundle**: The set of GATK-SV reference files (GRCh38 primary sequence, PAR regions, allosome regions, contig exclusions, training data for gCNV, SV allele frequency resources).
- **Run_Cache**: HealthOmics RunCache resource used to re-use completed task outputs across runs.
- **Cost_Optimizer**: The set of configuration choices and operational practices in the Migration_System that minimize AWS spend per cohort run.
- **Operator**: A human user responsible for registering, running, and maintaining the Migration_System.
- **Cohort**: A set of samples processed together by GATK-SV, typically batched in groups defined by the pipeline (batch size on the order of 100 to 500 samples).
- **Sample_Input**: Per-sample input data, typically an analysis-ready CRAM or BAM plus its index, aligned to GRCh38.
- **Cohort_VCF**: The final joint-genotyped structural variant VCF produced at the end of the MakeCohortVcf module.
- **RESTRICTED_Networking**: The default HealthOmics networking mode that blocks task egress to non-AWS endpoints.
- **VPC_Networking**: The HealthOmics networking mode that runs tasks in a customer VPC subnet.
- **Per_Sample_Cost_Target**: USD $7.00 per sample, measured end-to-end and amortized across all migrated GATK-SV modules for a production cohort run, via AWS Cost Explorer tags applied by the Migration_System.
- **SV_Caller_Set**: The set of structural variant callers in scope for the Migration_System: Manta, Wham, Scramble, and GATK-gCNV. MELT is explicitly excluded.
- **Migrated_Modules**: The set of GATK-SV modules in scope for the Migration_System: GatherSampleEvidence, GatherBatchEvidence, ClusterBatch, GenerateBatchMetrics, FilterBatch, MergeBatchSites, GenotypeBatch, RegenotypeCNVs, MakeCohortVcf, and AnnotateVcf.

## Requirements

### Requirement 1: Target Region Availability

**User Story:** As an Operator, I want the Migration_System to be deployable in ap-southeast-1, so that data residency requirements for Singapore-based cohorts are satisfied.

#### Acceptance Criteria

1. THE Migration_System SHALL verify that HealthOmics is available in Target_Region before any deployment step executes.
2. IF HealthOmics is not available in Target_Region, THEN THE Migration_System SHALL fail the deployment with a message that identifies the missing service and the region.
3. THE Migration_System SHALL create all AWS resources (ECR repositories, HealthOmics workflows, HealthOmics run caches, IAM role, S3 buckets referenced by outputs) in Target_Region.
4. WHEN an input S3 object is referenced from outside Target_Region, THE Migration_System SHALL emit a warning identifying the cross-region object and its bucket region.

### Requirement 2: WDL Compatibility with HealthOmics

**User Story:** As an Operator, I want the GATK-SV WDL sources to run on HealthOmics, so that I do not need a Terra or Cromwell environment.

#### Acceptance Criteria

1. THE Migration_System SHALL use WDL version 1.0 or 1.1 for every workflow registered with HealthOmics.
2. WHEN a GATK_SV WDL source uses a construct unsupported by HealthOmics, THE Migration_System SHALL replace the construct with a HealthOmics-compatible equivalent and document the change.
3. THE Migration_System SHALL pass the Linter with zero errors for every workflow bundle submitted to Workflow_Registration.
4. THE Migration_System SHALL preserve the scientific logic of each migrated GATK_SV task such that, for a fixed seed and fixed inputs, outputs match the upstream reference outputs within the tolerances declared in the upstream test suite.
5. WHERE a GATK_SV task depends on the Cromwell call-caching hash, THE Migration_System SHALL replace that dependency with a HealthOmics Run_Cache configuration.
6. IF a workflow references a GCS (gs://) URI directly, THEN THE Migration_System SHALL reject the workflow during packaging with a message identifying the offending URI.

### Requirement 2a: Module and Caller Scope

**User Story:** As an Operator, I want the module and caller scope of the Migration_System to be explicit, so that I know which parts of the upstream GATK_SV pipeline are supported and which are not.

#### Acceptance Criteria

1. THE Migration_System SHALL migrate every module in Migrated_Modules (GatherSampleEvidence, GatherBatchEvidence, ClusterBatch, GenerateBatchMetrics, FilterBatch, MergeBatchSites, GenotypeBatch, RegenotypeCNVs, MakeCohortVcf, AnnotateVcf) end-to-end.
2. THE Migration_System SHALL integrate the callers in SV_Caller_Set (Manta, Wham, Scramble, GATK-gCNV) into the migrated modules.
3. THE Migration_System SHALL exclude MELT (Mobile Element Locator Tool) from every migrated module.
4. IF an upstream WDL task references the MELT caller, THEN THE Migration_System SHALL remove or bypass the task and SHALL document the removal in the divergence log.
5. THE Migration_System SHALL document in the README that mobile-element-insertion sensitivity is reduced relative to the upstream GATK_SV pipeline as a consequence of excluding MELT, and that this tradeoff is accepted.

### Requirement 3: Container Image Availability in ECR

**User Story:** As an Operator, I want every container image used by GATK_SV to be available in an ECR repository accessible by HealthOmics, so that workflow tasks can pull images without leaving AWS.

#### Acceptance Criteria

1. THE Migration_System SHALL produce a Container_Registry_Map covering every distinct container image reference used by any migrated GATK_SV task.
2. FOR every container image referenced in the Container_Registry_Map, THE Migration_System SHALL verify that the image is present in ECR in Target_Region and that the ECR repository policy grants HealthOmics the ecr:BatchGetImage and ecr:GetDownloadUrlForLayer actions.
3. WHERE an upstream image originates from Docker Hub, GCR, or Quay.io, THE Migration_System SHALL route the image through a Pull_Through_Cache configured for HealthOmics use rather than hard-coding an ECR copy.
4. IF an upstream image cannot be resolved through a Pull_Through_Cache, THEN THE Migration_System SHALL clone the image to a private ECR repository and grant HealthOmics access to that repository.
5. THE Migration_System SHALL pin every container image to an immutable tag or digest; floating tags such as "latest" SHALL NOT appear in the Container_Registry_Map.

### Requirement 4: Parameter Templates

**User Story:** As an Operator, I want each migrated workflow to expose a parameter template, so that I can supply inputs without reading the WDL sources.

#### Acceptance Criteria

1. THE Migration_System SHALL supply a Parameter_Template for every registered workflow.
2. THE Parameter_Template SHALL declare each input with a description, a type, and an optional flag that matches the WDL declaration.
3. WHEN a workflow input refers to the Reference_Bundle, THE Parameter_Template SHALL describe the expected S3 URI and the referenced file type.
4. WHEN a Parameter_Template input is of type File, THE Parameter_Template SHALL require an S3 URI in Target_Region and SHALL reject HTTP, HTTPS, and GCS URIs.
5. FOR ALL Parameter_Template entries marked optional, THE Migration_System SHALL define a default value either in the WDL or in documentation.

### Requirement 5: Reference Bundle Provisioning

**User Story:** As an Operator, I want the Reference_Bundle staged in S3 in Target_Region, so that workflow tasks read references with low latency and no egress cost.

#### Acceptance Criteria

1. THE Migration_System SHALL document the complete list of Reference_Bundle files required by each migrated GATK_SV module.
2. THE Migration_System SHALL provide a reproducible procedure that copies the Reference_Bundle from the upstream Broad storage location to an S3 bucket in Target_Region.
3. WHEN the Reference_Bundle copy procedure completes, THE Migration_System SHALL verify each copied file against the upstream checksum.
4. IF a Reference_Bundle file fails checksum verification, THEN THE Migration_System SHALL report the file name, the expected checksum, and the observed checksum, and SHALL mark the provisioning run as failed.
5. THE Migration_System SHALL support GRCh38 as the reference genome build.
6. WHERE GRCh37 support is requested, THE Migration_System SHALL document the GRCh37 reference file set separately and SHALL treat GRCh37 as an optional configuration.

### Requirement 6: Sample Input Handling

**User Story:** As an Operator, I want to supply a batch of Sample_Input data and have the Migration_System process the batch end-to-end, so that I can produce a Cohort_VCF from aligned reads.

#### Acceptance Criteria

1. THE Migration_System SHALL accept Sample_Input data as CRAM files with companion .crai index files, aligned to the GRCh38 primary assembly.
2. WHERE Sample_Input data is supplied as BAM with BAI, THE Migration_System SHALL accept the BAM/BAI inputs.
3. THE Migration_System SHALL accept a sample manifest that lists, for each sample, a sample identifier, the reads URI, the index URI, and the sex assignment.
4. WHEN a Cohort batch size is between 100 and 500 samples, THE Migration_System SHALL run GATK_SV batch-scope modules without manual re-batching.
5. IF a Sample_Input reads file is not indexed, THEN THE Migration_System SHALL reject the sample manifest with a message identifying the sample and the missing index.
6. WHEN multiple samples share the same identifier in a sample manifest, THE Migration_System SHALL reject the sample manifest and SHALL report each duplicated identifier.

### Requirement 7: Cohort VCF Output

**User Story:** As an Operator, I want the Migration_System to write a Cohort_VCF and supporting artifacts to a caller-specified S3 location, so that downstream analysis tooling can consume the results.

#### Acceptance Criteria

1. WHEN a cohort run completes successfully, THE Migration_System SHALL write the Cohort_VCF and its tabix index to the caller-specified output S3 URI.
2. THE Migration_System SHALL write per-sample evidence artifacts (PE, SR, RD, BAF) to the caller-specified output S3 URI.
3. THE Migration_System SHALL write run-level quality metrics for each GATK_SV module to the caller-specified output S3 URI.
4. THE Migration_System SHALL confirm that each declared output file exists in S3 before reporting the run as COMPLETED.
5. IF a declared output file is missing after a run reports COMPLETED, THEN THE Migration_System SHALL relabel the run as FAILED with a message identifying each missing output.

### Requirement 8: Cost Optimization — Storage and Per-Sample Cost Target

**User Story:** As an Operator, I want storage costs minimized and a measurable per-sample cost ceiling enforced for each run, so that cohort processing stays within budget.

#### Acceptance Criteria

1. THE Cost_Optimizer SHALL default each HealthOmics run to DYNAMIC storage.
2. WHERE the total Sample_Input size for a cohort exceeds 1 TiB, THE Cost_Optimizer SHALL recommend STATIC storage with a capacity computed from the measured peak working set of the upstream reference run, plus a 20 percent headroom.
3. THE Cost_Optimizer SHALL record the measured peak working set per module after each run and SHALL update the STATIC capacity recommendation on the next run.
4. THE Migration_System SHALL write workflow outputs to S3 using the S3 Intelligent-Tiering storage class when the caller-supplied output bucket has Intelligent-Tiering configured as its default.
5. THE Cost_Optimizer SHALL target a measured end-to-end cost at or below Per_Sample_Cost_Target for a production cohort run, measured via AWS Cost Explorer tags applied by the Migration_System.
6. WHEN a completed production cohort run exceeds Per_Sample_Cost_Target, THE Cost_Optimizer SHALL surface cost-optimization recommendations to the Operator that identify the modules, tasks, and cost dimensions (compute, storage, data transfer, container pulls) contributing most to the overage.
7. THE Migration_System SHALL tag every run-associated AWS resource with a cohort identifier and a workflow version string that allows AWS Cost Explorer to compute the per-sample cost for the run.

### Requirement 9: Cost Optimization — Compute Right-Sizing

**User Story:** As an Operator, I want each task sized to its actual resource needs, so that I do not pay for unused CPU, memory, or GPU.

#### Acceptance Criteria

1. THE Migration_System SHALL declare CPU and memory requirements for every WDL task.
2. WHEN a task has been executed at least three times at cohort scale, THE Cost_Optimizer SHALL recommend revised CPU and memory values using the AnalyzeRunPerformance output with a 20 percent headroom.
3. WHERE a Cost_Optimizer recommendation reduces CPU or memory by at least 25 percent, THE Migration_System SHALL record the recommendation in the optimization log and SHALL surface it to the Operator.
4. THE Migration_System SHALL NOT apply a Cost_Optimizer recommendation automatically; the Operator SHALL approve each recommendation before the corresponding workflow version is published.
5. WHERE a GATK_SV task does not require a GPU, THE Migration_System SHALL declare zero GPUs for that task.

### Requirement 10: Cost Optimization — Run Caching

**User Story:** As an Operator, I want completed task outputs re-used across runs, so that re-running a cohort after a partial failure does not re-incur cost for the already-completed tasks.

#### Acceptance Criteria

1. THE Migration_System SHALL create and reference a Run_Cache in Target_Region for every production cohort run.
2. THE Migration_System SHALL default the Run_Cache behavior to CACHE_ALWAYS.
3. WHERE the Operator requires caching only on failure, THE Migration_System SHALL accept a CACHE_ON_FAILURE override.
4. WHEN a run is re-submitted after a failure, THE Migration_System SHALL reference the same Run_Cache as the failed run.
5. THE Migration_System SHALL document how to invalidate the Run_Cache.

### Requirement 11: Cost Optimization — Data Locality

**User Story:** As an Operator, I want all data accesses to stay within Target_Region, so that cross-region and internet egress charges do not occur.

#### Acceptance Criteria

1. WHEN a workflow run references Sample_Input, Reference_Bundle, or container images that reside outside Target_Region, THE Migration_System SHALL abort the run before StartRun with a message identifying each cross-region artifact.
2. THE Migration_System SHALL default new runs to RESTRICTED_Networking.
3. WHERE the Operator requires private connectivity, THE Migration_System SHALL accept VPC_Networking with a HealthOmics configuration that pins subnets and security groups to Target_Region.

### Requirement 12: IAM Role and Least Privilege

**User Story:** As an Operator, I want a dedicated IAM role that grants the Migration_System exactly the permissions it needs, so that the blast radius of a credential compromise is bounded.

#### Acceptance Criteria

1. THE Migration_System SHALL define a dedicated HealthOmics run role scoped to Target_Region.
2. THE Migration_System SHALL grant the run role read access only to the S3 prefixes that contain the Reference_Bundle, Sample_Input data, and workflow definition ZIPs referenced by the run.
3. THE Migration_System SHALL grant the run role write access only to the S3 output prefixes declared for the run.
4. THE Migration_System SHALL grant the run role ECR pull access only to the repositories listed in the Container_Registry_Map.
5. THE Migration_System SHALL grant the run role CloudWatch Logs write access limited to the /aws/omics log group prefix.
6. IF a requested permission is broader than the prefixes or repositories listed above, THEN THE Migration_System SHALL reject the IAM policy with a message identifying the overly broad statement.

### Requirement 13: Validation Run at Small Scale

**User Story:** As an Operator, I want a small-cohort validation procedure, so that I can confirm correctness and cost before processing a production cohort.

#### Acceptance Criteria

1. THE Migration_System SHALL provide a validation cohort of no more than 10 samples whose expected Cohort_VCF is documented.
2. WHEN the validation cohort is run, THE Migration_System SHALL compare the produced Cohort_VCF against the expected Cohort_VCF on SV site concordance.
3. IF site concordance is below 99 percent for deletions and duplications or below 95 percent for insertions and inversions, THEN THE Migration_System SHALL report the validation run as failed and SHALL list the discordant sites.
4. THE Migration_System SHALL report the measured dollar cost of the validation run using AWS Cost Explorer tags applied by the Migration_System, and SHALL report the measured per-sample cost computed as total run cost divided by the number of samples in the validation cohort.
5. WHEN the validation run per-sample cost exceeds Per_Sample_Cost_Target, THE Migration_System SHALL flag the overage in the validation report and SHALL include the Cost_Optimizer recommendations described in Requirement 8 that would close the gap.

### Requirement 14: Monitoring, Diagnostics, and Observability

**User Story:** As an Operator, I want visibility into run progress, failures, and resource utilization, so that I can intervene early and improve future runs.

#### Acceptance Criteria

1. WHEN a run reaches RUNNING, THE Migration_System SHALL emit a run-started event that includes the run identifier, the cohort identifier, and the submitted parameters.
2. WHEN a run reaches COMPLETED or FAILED, THE Migration_System SHALL emit a run-finished event that includes the run identifier, the final status, the wall-clock duration, and the measured dollar cost.
3. IF a run reaches FAILED, THEN THE Migration_System SHALL capture the DiagnoseRunFailure output and attach it to the run-finished event.
4. THE Migration_System SHALL generate a run timeline (SVG) for every COMPLETED or FAILED run longer than 30 minutes.
5. THE Migration_System SHALL generate an AnalyzeRunPerformance report for every COMPLETED run.

### Requirement 15: Error Handling and Retries

**User Story:** As an Operator, I want transient failures retried automatically and permanent failures reported with actionable messages, so that I do not re-run an entire cohort for a flaky task.

#### Acceptance Criteria

1. WHEN a task fails with a retryable error code declared by HealthOmics, THE Migration_System SHALL retry the task up to three times with exponential backoff.
2. IF a task fails three consecutive times with a retryable error code, THEN THE Migration_System SHALL fail the run and SHALL report the task identifier, the error code, and the last log excerpt.
3. WHEN a task fails with a non-retryable error code, THE Migration_System SHALL fail the run without retry and SHALL report the task identifier and the error code.
4. THE Migration_System SHALL record each retry attempt in the run-level log.

### Requirement 16: Workflow Versioning

**User Story:** As an Operator, I want each published change to the Migration_System to be addressable as a workflow version, so that I can reproduce past cohort results and roll back if needed.

#### Acceptance Criteria

1. THE Migration_System SHALL publish a new HealthOmics workflow version for every change to WDL sources, container references, or parameter templates.
2. THE Migration_System SHALL label each workflow version with a semantic version string.
3. THE Migration_System SHALL record, for each published workflow version, the upstream GATK_SV commit hash and the list of changes applied during migration.
4. WHEN a cohort run is started, THE Migration_System SHALL record the workflow version string in the run metadata.

### Requirement 17: Documentation

**User Story:** As an Operator new to the Migration_System, I want clear documentation, so that I can deploy, run, and troubleshoot without reading the WDL sources.

#### Acceptance Criteria

1. THE Migration_System SHALL provide a README that describes how to provision the Reference_Bundle, register each workflow, and start a cohort run.
2. THE Migration_System SHALL document each divergence from the upstream GATK_SV sources and the reason for each divergence.
3. THE Migration_System SHALL document Per_Sample_Cost_Target (USD $7.00 per sample), the AWS Cost Explorer tags used to measure it, the tag values emitted for each run, and the procedure an Operator follows to verify the measured per-sample cost for a completed cohort run.
4. THE Migration_System SHALL document the expected runtime for a 100-sample GRCh38 cohort in Target_Region and SHALL document the expected per-sample cost for that cohort relative to Per_Sample_Cost_Target.
5. THE Migration_System SHALL document the supported input file formats, the supported reference build, the migrated modules (Migrated_Modules), the in-scope callers (SV_Caller_Set), the exclusion of MELT, and the expected output files.
6. THE Migration_System SHALL document the procedure for invalidating the Run_Cache and for rolling back to an earlier workflow version.

### Requirement 18: Parser Round-Trip for Parameter Template

**User Story:** As an Operator, I want the Parameter_Template generator to round-trip cleanly with the WDL sources, so that manually edited templates remain valid.

#### Acceptance Criteria

1. THE Migration_System SHALL include a Parameter_Template generator that reads a WDL workflow and writes a Parameter_Template JSON document.
2. THE Migration_System SHALL include a Parameter_Template validator that reads a Parameter_Template JSON document and a WDL workflow and reports whether the template matches the workflow inputs.
3. FOR ALL WDL workflows in the Migration_System, generating a Parameter_Template and then validating the generated template against the same WDL workflow SHALL report a match.
4. WHEN a Parameter_Template is manually edited to remove a required input, THE Parameter_Template validator SHALL report the missing input.
5. WHEN a Parameter_Template is manually edited to add an input not declared in the WDL, THE Parameter_Template validator SHALL report the extra input.
