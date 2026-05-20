# Yamlizr Pipeline Unifier

Merges Yamlizr-converted Azure DevOps CI/CD YAML pipelines into a single
unified multi-stage YAML file per release pipeline.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11 + |
| .NET SDK | 8.0 + (for Yamlizr) |
| `requests` | ≥ 2.31 |
| `PyYAML` | ≥ 6.0 |

---

## Step 1 — Run Yamlizr first (Classic pipelines only)

```bash
dotnet tool update --global yamlizr

yamlizr generate \
  -pat  <Azure-DevOps-PAT> \
  -org  https://dev.azure.com/<organization> \
  -proj <project-name> \
  -out  <output-directory>
```

Yamlizr creates two folders inside `<output-directory>`:

```
<output-directory>/
  AzureDevOpsBuilds/      ← Converted Classic CI pipelines
  AzureDevOpsReleases/    ← Converted Classic CD pipelines
```

File naming: `<PipelineName>-<DefinitionId>.yml`

> Yamlizr only touches **Classic** pipelines.  
> YAML pipelines are fetched directly from Azure DevOps by the unifier.

---

## Step 2 — Install the unifier dependencies

```bash
pip install -r requirements.txt
```

---

## Step 3 — Run the unifier

```bash
python unifier.py \
  -pat  <Azure-DevOps-PAT> \
  -org  https://dev.azure.com/<organization> \
  -proj <project-name> \
  -out  <output-directory>        # same path you passed to Yamlizr
```

Or use the all-in-one runner (does everything automatically):

```bash
python run.py \
  -pat  <Azure-DevOps-PAT> \
  -org  https://dev.azure.com/<organization> \
  -proj <project-name> \
  -out  <output-directory>
```

Add `-v` for verbose/debug output.

Output is written to:

```
<output-directory>/Unified/<ReleasePipelineName>.yml
```

---

## Scenarios

The unifier automatically detects which scenario applies to each release
pipeline and handles it accordingly.

### Scenario 1 — YAML CI + YAML CD

Both pipelines already live in YAML in a repository.

- **0 artifacts** → CD YAML written as-is.
- **All CIs point to the same file as CD** → logged as *already unified*, skipped.
- **Otherwise** → CI stage(s) prepended to CD stages; saved to `Unified/`.

### Scenario 2 — YAML CI + Classic CD

The release pipeline is Classic; all linked build pipelines are YAML.
Yamlizr must have been run before this step.

- **0 artifacts** → Enriched Yamlizr CD YAML written as-is.
- **Otherwise** → YAML CI(s) fetched from Azure DevOps, merged with the
  enriched Yamlizr CD YAML; saved to `Unified/`.

### Scenario 3 — Classic CI + Classic CD

Both the release pipeline and all linked build pipelines are Classic.
Yamlizr must have been run before this step.

- **0 artifacts** → Enriched Yamlizr CD YAML written as-is.
- **Otherwise** → Yamlizr CI file(s) and enriched Yamlizr CD file merged;
  saved to `Unified/`.

> **Classic CI + YAML CD** is an impossible combination and is never handled.

---

## Merged YAML structure

```yaml
trigger:          # from CI pipeline(s) — merged across all CIs
  branches:
    include:
      - main

schedules:        # from CI pipeline(s) + Classic CD scheduled triggers
  - cron: "0 2 * * *"
    ...

pr:               # from CI pipeline(s) + Classic CD PR triggers (if any)
  branches:
    include:
      - main

variables:        # CI baseline, CD overrides on conflicts
  - name: env
    value: prod

pool:             # top-level ONLY when all stages share the same pool
  name: Default   # omitted here if stages use different pools

stages:
  - stage: Build_<ArtifactAlias>   # one stage per CI artifact, in artifact order
    pool:                          # stage-level pool when pools differ
      name: CI-Pool
    ...
  - stage: Deploy
    pool:
      name: CD-Pool
    dependsOn:
      - Build_<ArtifactAlias>
    ...
```

---

## Classic CD enrichment

Yamlizr converts the structure of Classic release pipelines but misses two
things. The unifier fills both gaps automatically by reading the release
definition from the Azure DevOps API.

### Triggers

Classic CD pipelines store triggers separately from the pipeline body.
The unifier reads the `triggers` array from the API and handles each type:

| Classic trigger type | YAML translation | Notes |
|---|---|---|
| **Artifact / Continuous Deployment** | None (intentional) | The unified pipeline's `dependsOn` between the build stage and deploy stage already serves this purpose. Adding a `trigger:` block would incorrectly re-run the whole pipeline on every build. Logged as *handled implicitly*. |
| **Scheduled** | `schedules:` block | Converted from Classic day/time fields to a cron expression. Merged with any existing schedules from CI pipelines. |
| **Pull Request** | `pr:` block | Translated directly. A **warning is logged** because deploying on a PR is unusual — review before committing. |

### Agent pools per environment

Classic CD sets the agent pool **per environment** (e.g. Dev, Staging, Prod),
not at the pipeline level. Yamlizr loses this information. The unifier reads
each environment's `deployPhases → deploymentInput → agentSpecification` from
the API and injects the correct `pool:` into the matching stage.

If the pool name is not directly available (only a numeric `queueId` is
present), the pool is stored as `pool-id-<N>` and a note is logged — update
it manually to the correct pool name.

---

## Smart pool placement

Pool placement is decided automatically based on whether all pipelines share
the same agent pool:

| Situation | Result |
|---|---|
| All stages use the same pool | `pool:` written **once at the top level** |
| Any stage uses a different pool | `pool:` written **on each stage individually** |
| No pool defined anywhere | Nothing written |
| A stage already defines its own pool | Never overwritten |

Both the shorthand form (`pool: Default`) and the dict form
(`pool: {name: Default}`) are recognised as equivalent when comparing.

---

## Running the tests

```bash
python tests.py
```

Or with pytest if installed:

```bash
python -m pytest tests.py -v
```

---

## Azure DevOps PAT permissions required

| Scope | Access |
|---|---|
| **Build** | Read |
| **Release** | Read |
| **Code** | Read (to fetch YAML file content) |
