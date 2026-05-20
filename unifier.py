#!/usr/bin/env python3
"""
Yamlizr Pipeline Unifier
Merges Yamlizr-converted Azure DevOps CI/CD YAML pipelines into unified multi-stage YAML files.
"""

import argparse
import base64
import logging
import os
import sys
from pathlib import Path

import copy

import requests
import yaml

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("unifier")


# ─── Azure DevOps API Client ─────────────────────────────────────────────────

class AzureDevOpsClient:
    """Thin wrapper around the Azure DevOps REST API."""

    RELEASE_API = "https://vsrm.dev.azure.com"
    BUILD_API   = "https://dev.azure.com"

    def __init__(self, pat: str, org: str, project: str):
        # Accept either a full URL ("https://dev.azure.com/myorg") or bare org name.
        # Normalise to just the org name so _get() can build correct URLs for
        # both dev.azure.com and vsrm.dev.azure.com.
        from urllib.parse import urlparse
        org = org.rstrip("/")
        if org.startswith("http"):
            # "https://dev.azure.com/myorg" -> "myorg"
            parsed   = urlparse(org)
            org_name = parsed.path.lstrip("/")
        else:
            org_name = org
        self.org     = org_name
        self.project = project
        token        = base64.b64encode(f":{pat}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type":  "application/json",
        }

    # ── internal ──────────────────────────────────────────────────────────────

    def _get(self, base: str, path: str, params: dict | None = None) -> dict:
        url = f"{base}/{self.org}/{self.project}/_apis/{path}"
        r   = requests.get(url, headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _get_raw(self, base: str, path: str, params: dict | None = None) -> str | None:
        """Like _get but returns raw response text (for file content endpoints)."""
        url = f"{base}/{self.org}/{self.project}/_apis/{path}"
        r   = requests.get(url, headers=self.headers, params=params, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text if r.text else None

    # ── release pipelines ─────────────────────────────────────────────────────

    def list_release_definitions(self) -> list[dict]:
        """Return all release-pipeline definition summaries."""
        data = self._get(
            self.RELEASE_API,
            "release/definitions",
            params={"api-version": "7.1", "$expand": "artifacts"},
        )
        return data.get("value", [])

    def get_release_definition(self, definition_id: int) -> dict:
        """Return the full release-pipeline definition (including artifacts)."""
        return self._get(
            self.RELEASE_API,
            f"release/definitions/{definition_id}",
            params={"api-version": "7.1"},
        )

    # ── build pipelines ───────────────────────────────────────────────────────

    def get_build_definition(self, definition_id: int) -> dict:
        """Return the full build-pipeline definition."""
        return self._get(
            self.BUILD_API,
            f"build/definitions/{definition_id}",
            params={"api-version": "7.1"},
        )

    def get_yaml_pipeline_file_path(self, definition_id: int) -> str | None:
        """Return the repo-relative YAML file path for a YAML build pipeline, or None."""
        defn = self.get_build_definition(definition_id)
        return defn.get("process", {}).get("yamlFilename")

    def get_yaml_pipeline_content(self, definition_id: int) -> str | None:
        """
        Fetch the raw YAML content of a YAML build pipeline from its repository.
        Uses the repository details embedded in the build definition.
        """
        defn      = self.get_build_definition(definition_id)
        process   = defn.get("process", {})
        yaml_path = process.get("yamlFilename")
        if not yaml_path:
            log.debug("  No yamlFilename in build definition %s", definition_id)
            return None

        repo      = defn.get("repository", {})
        repo_id   = repo.get("id")
        repo_type = repo.get("type", "").lower()
        branch    = repo.get("defaultBranch", "refs/heads/main").replace("refs/heads/", "")

        log.debug("  Fetching YAML from repo %s, path %s, branch %s", repo_id, yaml_path, branch)

        if repo_type == "tfsgit":
            # $format=text makes the API return raw file bytes instead of a JSON envelope
            text = self._get_raw(
                self.BUILD_API,
                f"git/repositories/{repo_id}/items",
                params={
                    "path":                      yaml_path,
                    "versionDescriptor.version": branch,
                    "$format":                   "text",
                    "api-version":               "7.1",
                },
            )
            if text:
                return text
            # Fallback: try downloading via the download flag
            text = self._get_raw(
                self.BUILD_API,
                f"git/repositories/{repo_id}/items",
                params={
                    "path":                      yaml_path,
                    "versionDescriptor.version": branch,
                    "download":                  "true",
                    "api-version":               "7.1",
                },
            )
            return text
        log.warning("  Unsupported repo type '%s' for pipeline %s", repo_type, definition_id)
        return None

    # ── YAML pipeline detection ───────────────────────────────────────────────

    def is_yaml_pipeline(self, definition_id: int) -> bool:
        """True when the build pipeline uses a YAML process (type 2)."""
        defn = self.get_build_definition(definition_id)
        return defn.get("process", {}).get("type") == 2

    def is_yaml_release_pipeline(self, release_def: dict) -> bool:
        """
        True when the release definition is itself a YAML pipeline.
        Azure DevOps multi-stage YAML releases have no 'environments' key
        and carry a 'pipelineConfigurationId' or similar marker.
        A robust heuristic: Classic releases always have an 'environments' list.
        """
        return "environments" not in release_def or release_def.get("isYaml", False)

    def get_yaml_cd_build_id(self, release_def: dict) -> int | None:
        """
        For a YAML multi-stage CD pipeline, return its Build API definition ID.

        In Azure DevOps, a YAML multi-stage pipeline is registered as BOTH:
          - A release pipeline (visible in Releases) with a release definition ID
          - A build pipeline (visible in Pipelines) with a build definition ID

        The release definition carries the build pipeline ID in one of:
          pipelineConfigurationId   — preferred, direct build def ID
          properties.DefinitionId   — alternative location

        We also try searching the build API by pipeline name as a fallback.
        Returns None if the build definition ID cannot be determined.
        """
        # 1. Direct field
        pipeline_config_id = release_def.get("pipelineConfigurationId")
        if pipeline_config_id:
            return int(pipeline_config_id)

        # 2. Properties bag
        props = release_def.get("properties", {})
        def_id = props.get("DefinitionId") or props.get("definitionId")
        if def_id:
            return int(def_id)

        # 3. Search build definitions by name
        try:
            data = self._get(
                self.BUILD_API,
                "build/definitions",
                params={"name": release_def.get("name", ""), "api-version": "7.1"},
            )
            defs = data.get("value", [])
            if len(defs) == 1:
                return int(defs[0]["id"])
            if len(defs) > 1:
                log.debug(
                    "  Multiple build definitions found for name '%s' — cannot auto-select.",
                    release_def.get("name", ""),
                )
        except Exception as exc:
            log.debug("  Build definition search failed: %s", exc)

        return None

    # ── Classic CD enrichment ─────────────────────────────────────────────────

    def get_classic_cd_triggers(self, release_def: dict) -> list[dict]:
        """
        Return the raw triggers array from a Classic release definition.
        Each entry has a triggerType field:
          ArtifactSource  — fires when a new build artifact is available
          Schedule        — cron-based scheduled trigger
          PullRequest     — fires on PR completion
        The full release_def is passed in (already fetched) to avoid a second API call.
        """
        return release_def.get("triggers", [])

    def resolve_queue_id(self, queue_id: int) -> str | None:
        """
        Resolve a numeric agent queue ID to the pool name string.
        Uses the distributedtask/queues endpoint which returns the queue
        record including the pool name it references.
        Returns None if the queue cannot be resolved.
        """
        try:
            data = self._get(
                self.BUILD_API,
                f"distributedtask/queues/{queue_id}",
                params={"api-version": "7.1"},
            )
            # Response shape: { id, name, pool: { id, name } }
            pool_name = (
                data.get("pool", {}).get("name")
                or data.get("name")   # queue name often matches pool name
            )
            return pool_name
        except Exception as exc:
            log.debug("  Could not resolve queueId %s: %s", queue_id, exc)
            return None

    def get_classic_cd_environment_pools(self, release_def: dict) -> dict[str, object]:
        """
        Return a mapping of  environment_name → pool  for every environment
        in a Classic release definition.

        Classic CD stores the agent pool per deploy phase inside each environment:
          environments[].deployPhases[].deploymentInput.queueId  (numeric pool id)
          environments[].deployPhases[].deploymentInput.agentSpecification.identifier
          environments[].deployPhases[].phaseType  ("agentBasedDeployment" for agent jobs)

        Resolution order:
          1. agentSpecification.identifier  — pool name string, most reliable
          2. agentSpecification.name        — alternative name field
          3. queueId → API lookup           — resolve numeric ID to pool name
        Returns {} if no environments exist.
        """
        env_pools: dict[str, object] = {}
        for env in release_def.get("environments", []):
            env_name = env.get("name", "")
            for phase in env.get("deployPhases", []):
                if phase.get("phaseType") != "agentBasedDeployment":
                    continue
                di = phase.get("deploymentInput", {})

                # 1. Prefer agentSpecification — most reliable source
                agent_spec = di.get("agentSpecification") or {}
                pool_name  = agent_spec.get("identifier") or agent_spec.get("name")
                if pool_name:
                    env_pools[env_name] = {"name": pool_name}
                    log.debug("  Env '%s' pool resolved via agentSpecification: %s", env_name, pool_name)
                    break

                # 2. Fall back to queueId — resolve via API
                queue_id = di.get("queueId")
                if queue_id:
                    resolved = self.resolve_queue_id(queue_id)
                    if resolved:
                        env_pools[env_name] = {"name": resolved}
                        log.debug("  Env '%s' pool resolved via queueId %s: %s", env_name, queue_id, resolved)
                    else:
                        # API lookup failed — use placeholder but warn the user
                        env_pools[env_name] = {"name": f"pool-id-{queue_id}"}
                        log.warning(
                            "  ⚠  Env '%s': could not resolve queueId %s to a pool name. "
                            "Stored as 'pool-id-%s' — update manually.",
                            env_name, queue_id, queue_id,
                        )
                    break
        return env_pools


# ─── YAML Merge Helpers ───────────────────────────────────────────────────────

def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_yaml_str(content: str) -> dict:
    return yaml.safe_load(content) or {}


def dump_yaml(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    log.info("  ✔  Written → %s", path)


def extract_stages(pipeline: dict, default_stage_name: str) -> list[dict]:
    """
    Return a list of stage dicts from a pipeline.
    If the pipeline uses a flat jobs/steps structure, wrap it in a synthetic stage.
    """
    if "stages" in pipeline:
        return copy.deepcopy(pipeline["stages"])

    # Flat structure — wrap it
    stage: dict = {"stage": default_stage_name, "displayName": default_stage_name}
    if "jobs" in pipeline:
        stage["jobs"] = pipeline["jobs"]
    elif "steps" in pipeline:
        stage["jobs"] = [{"job": "Run", "steps": pipeline["steps"]}]
    return [stage]


# ─── Top-level key merge helpers ─────────────────────────────────────────────

def _merge_trigger(triggers: list) -> dict | str | None:
    """
    Merge trigger blocks from multiple CI pipelines into one.
    Handles both shorthand (trigger: none / trigger: [branch])
    and longhand (trigger: branches: include: [...]) forms.
    Returns a unified trigger block, or None if no CI has a trigger.
    """
    if not triggers:
        return None

    # If any pipeline disables trigger entirely, respect that only if ALL do
    none_count = sum(1 for t in triggers if t == "none" or t is None)
    if none_count == len(triggers):
        return "none"

    # Collect all branch include/exclude patterns across all CIs
    include_branches: list[str] = []
    exclude_branches: list[str] = []
    batch: bool | None = None
    paths_include: list[str] = []
    paths_exclude: list[str] = []
    tags_include: list[str] = []
    tags_exclude: list[str] = []

    for t in triggers:
        if t is None or t == "none":
            continue
        if isinstance(t, list):
            # shorthand: trigger: [main, develop]
            for b in t:
                if b not in include_branches:
                    include_branches.append(b)
            continue
        if isinstance(t, dict):
            if batch is None and "batch" in t:
                batch = t["batch"]
            for b in t.get("branches", {}).get("include", []) or []:
                if b not in include_branches:
                    include_branches.append(b)
            for b in t.get("branches", {}).get("exclude", []) or []:
                if b not in exclude_branches:
                    exclude_branches.append(b)
            for p in t.get("paths", {}).get("include", []) or []:
                if p not in paths_include:
                    paths_include.append(p)
            for p in t.get("paths", {}).get("exclude", []) or []:
                if p not in paths_exclude:
                    paths_exclude.append(p)
            for tag in t.get("tags", {}).get("include", []) or []:
                if tag not in tags_include:
                    tags_include.append(tag)
            for tag in t.get("tags", {}).get("exclude", []) or []:
                if tag not in tags_exclude:
                    tags_exclude.append(tag)

    result: dict = {}
    if batch is not None:
        result["batch"] = batch
    branches: dict = {}
    if include_branches:
        branches["include"] = include_branches
    if exclude_branches:
        branches["exclude"] = exclude_branches
    if branches:
        result["branches"] = branches
    if paths_include or paths_exclude:
        paths: dict = {}
        if paths_include:
            paths["include"] = paths_include
        if paths_exclude:
            paths["exclude"] = paths_exclude
        result["paths"] = paths
    if tags_include or tags_exclude:
        tags: dict = {}
        if tags_include:
            tags["include"] = tags_include
        if tags_exclude:
            tags["exclude"] = tags_exclude
        result["tags"] = tags

    return result if result else None


def _merge_pr(pr_blocks: list) -> dict | str | None:
    """
    Merge pr: blocks from multiple CI pipelines.
    Same structure as trigger — branches, paths, drafts flag.
    """
    if not pr_blocks:
        return None

    none_count = sum(1 for p in pr_blocks if p == "none" or p is None)
    if none_count == len(pr_blocks):
        return "none"

    include_branches: list[str] = []
    exclude_branches: list[str] = []
    paths_include: list[str] = []
    paths_exclude: list[str] = []
    drafts: bool | None = None

    for p in pr_blocks:
        if p is None or p == "none":
            continue
        if isinstance(p, list):
            for b in p:
                if b not in include_branches:
                    include_branches.append(b)
            continue
        if isinstance(p, dict):
            if drafts is None and "drafts" in p:
                drafts = p["drafts"]
            for b in p.get("branches", {}).get("include", []) or []:
                if b not in include_branches:
                    include_branches.append(b)
            for b in p.get("branches", {}).get("exclude", []) or []:
                if b not in exclude_branches:
                    exclude_branches.append(b)
            for path in p.get("paths", {}).get("include", []) or []:
                if path not in paths_include:
                    paths_include.append(path)
            for path in p.get("paths", {}).get("exclude", []) or []:
                if path not in paths_exclude:
                    paths_exclude.append(path)

    result: dict = {}
    if drafts is not None:
        result["drafts"] = drafts
    branches: dict = {}
    if include_branches:
        branches["include"] = include_branches
    if exclude_branches:
        branches["exclude"] = exclude_branches
    if branches:
        result["branches"] = branches
    if paths_include or paths_exclude:
        paths: dict = {}
        if paths_include:
            paths["include"] = paths_include
        if paths_exclude:
            paths["exclude"] = paths_exclude
        result["paths"] = paths

    return result if result else None


def _merge_schedules(schedule_lists: list[list]) -> list:
    """
    Concatenate schedules from all CI pipelines, deduplicating by cron expression.
    """
    seen_crons: set[str] = set()
    merged: list = []
    for schedules in schedule_lists:
        if not schedules:
            continue
        for entry in schedules:
            cron = entry.get("cron", "") if isinstance(entry, dict) else str(entry)
            if cron not in seen_crons:
                seen_crons.add(cron)
                merged.append(entry)
    return merged


def _merge_resources(resource_lists: list[dict]) -> dict:
    """
    Merge resources: blocks from all CI pipelines.
    Each resource type (pipelines, repositories, containers, feeds, packages)
    is a list keyed by alias — deduplicate by alias.
    """
    result: dict = {}
    resource_types = ["pipelines", "repositories", "containers", "feeds", "packages"]

    for resources in resource_lists:
        if not resources or not isinstance(resources, dict):
            continue
        for rtype in resource_types:
            items = resources.get(rtype)
            if not items:
                continue
            if rtype not in result:
                result[rtype] = []
            existing_aliases = {
                r.get("pipeline") or r.get("repository") or r.get("container")
                or r.get("feed") or r.get("package") or r.get("alias", "")
                for r in result[rtype]
            }
            for item in items:
                alias = (item.get("pipeline") or item.get("repository")
                         or item.get("container") or item.get("feed")
                         or item.get("package") or item.get("alias", ""))
                if alias not in existing_aliases:
                    result[rtype].append(item)
                    existing_aliases.add(alias)
    return result


def _merge_variables(ci_vars_list: list, cd_vars) -> list | dict | None:
    """
    Merge variables from CI pipelines and CD pipeline. CD wins on conflicts.
    Handles both list form (- name: X  value: Y) and map form (X: Y).
    """
    def to_map(v) -> dict:
        if not v:
            return {}
        if isinstance(v, dict):
            return {k: {"value": val} if not isinstance(val, dict) else val
                    for k, val in v.items()}
        if isinstance(v, list):
            out = {}
            for item in v:
                if isinstance(item, dict):
                    if "name" in item:
                        out[item["name"]] = {k: v2 for k, v2 in item.items() if k != "name"}
                    elif "group" in item:
                        out[f"__group__{item['group']}"] = item
                    elif "template" in item:
                        out[f"__template__{item['template']}"] = item
            return out
        return {}

    def to_list(m: dict) -> list:
        result = []
        for name, val in m.items():
            if name.startswith("__group__") or name.startswith("__template__"):
                result.append(val)
            elif isinstance(val, dict):
                result.append({"name": name, **val})
            else:
                result.append({"name": name, "value": val})
        return result

    combined: dict = {}
    for ci_vars in ci_vars_list:
        combined.update(to_map(ci_vars))
    # CD overrides CI on conflicts
    combined.update(to_map(cd_vars))

    if not combined:
        return None
    return to_list(combined)


def _collect_pipeline_effective_pool(pipeline: dict) -> object | None:
    """
    Walk top → stage → job levels and return the effective pool for a pipeline.

    Priority order (first non-None wins):
      1. Top-level pool
      2. Stage-level pool (from first stage that has one)
      3. Job-level pool   (from first job in first stage that has one)

    This handles YAML pipelines that define pool at stage or job level only,
    with no top-level pool key.
    """
    # 1. Top-level
    if pipeline.get("pool") is not None:
        return pipeline["pool"]

    # 2. Stage-level
    for stage in pipeline.get("stages", []):
        if not isinstance(stage, dict):
            continue
        if stage.get("pool") is not None:
            return stage["pool"]
        # 3. Job-level within this stage
        for job in stage.get("jobs", []):
            if isinstance(job, dict) and job.get("pool") is not None:
                return job["pool"]

    # Flat pipeline (no stages key) — check jobs directly
    for job in pipeline.get("jobs", []):
        if isinstance(job, dict) and job.get("pool") is not None:
            return job["pool"]

    return None


def _all_job_pools_in_stage(stage: dict) -> list:
    """
    Return a list of pool values for every job in a stage.
    Returns [] if the stage has no jobs.
    None entries mean that job defines no pool.
    """
    pools = []
    for job in stage.get("jobs", []):
        if isinstance(job, dict):
            pools.append(job.get("pool"))
    return pools


def _pool_key_local(p):
    if p is None:
        return None
    if isinstance(p, str):
        return p.strip().lower()
    if isinstance(p, dict):
        name = p.get("name", "")
        rest = {k: v for k, v in p.items() if k != "name"}
        if rest:
            return (name.strip().lower(), str(sorted(rest.items())))
        return name.strip().lower()
    return str(p).strip().lower()


def _collapse_duplicate_job_pools_to_stage(stage: dict) -> None:
    """Normalize stage/job pools: collapse duplicate job pools into a stage pool."""
    if not isinstance(stage, dict):
        return

    jobs = [j for j in stage.get("jobs", []) if isinstance(j, dict)]
    if not jobs:
        return

    stage_pool = stage.get("pool")
    if stage_pool is not None:
        stage_key = _pool_key_local(stage_pool)
        for job in jobs:
            if _pool_key_local(job.get("pool")) == stage_key:
                job.pop("pool", None)
        return

    pool_counts: dict[object, int] = {}
    pool_examples: dict[object, object] = {}
    for job in jobs:
        if "pool" not in job:
            continue
        key = _pool_key_local(job["pool"])
        pool_counts[key] = pool_counts.get(key, 0) + 1
        pool_examples[key] = job["pool"]

    if not pool_counts:
        return

    best_key, best_count = max(pool_counts.items(), key=lambda kv: kv[1])
    if best_count < 2:
        return

    selected_pool = copy.deepcopy(pool_examples[best_key])
    stage["pool"] = selected_pool
    for job in jobs:
        if _pool_key_local(job.get("pool")) == best_key:
            job.pop("pool", None)


def _inject_pool_into_stage(stage: dict, pool: object) -> None:
    """
    Inject a pool into a stage using the smartest placement:

    Rules (in order):
      1. Stage already has pool → do nothing (never overwrite)
      2. No jobs → inject at stage level
      3. No job has a pool yet → inject at stage level
      4. Some jobs have pools and some don't → inject at stage level when the
         injected pool applies to at least one job, otherwise inject at job level
      5. All jobs have pools → collapse identical pools to stage level when
         possible, otherwise do nothing.
    """
    if not isinstance(stage, dict):
        return

    if "pool" in stage:
        return

    jobs = [j for j in stage.get("jobs", []) if isinstance(j, dict)]
    if not jobs:
        stage["pool"] = copy.deepcopy(pool)
        return

    def _pool_key_local(p):
        if p is None:
            return None
        if isinstance(p, str):
            return p.strip().lower()
        if isinstance(p, dict):
            name = p.get("name", "")
            rest = {k: v for k, v in p.items() if k != "name"}
            if rest:
                return (name.strip().lower(), str(sorted(rest.items())))
            return name.strip().lower()
        return str(p).strip().lower()

    jobs_with_pool    = [j for j in jobs if "pool" in j]
    jobs_without_pool = [j for j in jobs if "pool" not in j]
    job_pool_keys     = {_pool_key_local(j.get("pool")) for j in jobs_with_pool}
    pool_key          = _pool_key_local(pool)

    if not jobs_with_pool:
        stage["pool"] = copy.deepcopy(pool)
        return

    # If the injected pool matches one or more existing jobs, apply it at
    # the stage level and collapse those matching job pools.
    if pool_key in job_pool_keys:
        stage["pool"] = copy.deepcopy(pool)
        for job in jobs_with_pool:
            if _pool_key_local(job.get("pool")) == pool_key:
                job.pop("pool", None)
        return

    if not jobs_without_pool:
        return

    # Otherwise, inject the pool only into jobs that are missing one.
    for job in jobs_without_pool:
        job["pool"] = copy.deepcopy(pool)


def merge_pipelines(ci_pipelines: list[dict], cd_pipeline: dict, artifact_names: list[str]) -> dict:
    """
    Merge one or more CI pipelines and one CD pipeline into a single
    multi-stage YAML with structure:  stages: [build…, deploy…]

    Top-level key ownership:
      trigger, pr, schedules  → merged across all CI pipelines (union of branches)
      resources               → merged across all CI pipelines (deduplicated by alias)
      variables               → CI pipelines first, CD overrides on conflicts
      pool, parameters, name  → CD wins; CI fills gaps if CD does not define them
      stages/jobs/steps       → rebuilt from scratch (CI build stages → CD deploy stages)
    """
    merged: dict = {}

    # Debug: log the CD pipeline structure to verify enrichment arrived correctly
    log.debug("  CD pipeline keys at merge time: %s", list(cd_pipeline.keys()))
    for i, s in enumerate(cd_pipeline.get("stages", [])):
        log.debug("  CD stage[%d]: name=%r  pool=%r", i,
                  s.get("stage") if isinstance(s, dict) else "?",
                  s.get("pool")  if isinstance(s, dict) else "?")

    # ── Keys never copied to the top level ──────────────────────────────────────
    # - Structural keys are rebuilt from scratch
    # - Trigger-related keys handled by dedicated merge helpers
    # - pool handled separately in step 8 (top-level vs stage-level decision)
    SKIP = {"stages", "jobs", "steps",
            "trigger", "pr", "schedules", "resources", "variables",
            "pool"}

    # ── 1. Runtime/config keys: CD wins, CI fills gaps ────────────────────────
    if ci_pipelines:
        for key, val in ci_pipelines[0].items():
            if key not in SKIP:
                merged[key] = val
    for key, val in cd_pipeline.items():
        if key not in SKIP:
            merged[key] = val     # CD always wins

    # ── Collect per-pipeline pools for the smart pool placement decision ──────
    # Each CI pipeline gets its own pool entry; CD gets one entry.
    # A None entry means that pipeline defines no pool.
    def _pool_key(pool) -> str | None:
        """
        Normalise a pool value to a canonical string key for equality comparison.

        Azure Pipelines allows pool to be written in two equivalent forms:
          pool: MyPool                    (plain string — shorthand for name only)
          pool:                           (dict form)
            name: MyPool

        Both mean the same thing, so we normalise both to just the lowercase
        pool name string. Additional dict keys (demands, vmImage, etc.) are
        included so pools that differ on those are still treated as different.
        """
        if pool is None:
            return None
        if isinstance(pool, str):
            return pool.strip().lower()
        if isinstance(pool, dict):
            name = pool.get("name", "")
            # Build a stable key from all keys, but treat missing name the same
            # as an explicit name so string "X" == {"name": "X"} compares equal
            rest = {k: v for k, v in pool.items() if k != "name"}
            if rest:
                # Dict has extra keys (demands, vmImage, etc.) — include them
                return (name.strip().lower(), str(sorted(rest.items())))
            return name.strip().lower()
        return str(pool).strip().lower()

    # ── Collect effective pool per pipeline ──────────────────────────────────
    # Walk all three levels (top → stage → job) to find the pool each pipeline
    # actually uses. This handles YAML pipelines that define pool at stage or
    # job level only, with no top-level pool key.
    ci_effective_pools = [_collect_pipeline_effective_pool(ci) for ci in ci_pipelines]
    cd_effective_pool  = _collect_pipeline_effective_pool(cd_pipeline)

    # cd_stage_pools: pools already on individual CD stages (set by enrich_classic_cd)
    # We track these separately so we know not to overwrite them.
    cd_stage_pools: list = []
    for stage in cd_pipeline.get("stages", []):
        sp = stage.get("pool") if isinstance(stage, dict) else None
        if sp is not None and sp not in cd_stage_pools:
            cd_stage_pools.append(sp)

    # Representative pool for the CD side — use top-level if available,
    # otherwise first stage pool, otherwise first job pool.
    all_pools        = [p for p in ci_effective_pools + [cd_effective_pool] if p is not None]
    unique_pool_keys = {_pool_key(p) for p in all_pools}

    log.info("  Pool detection →")
    for idx, cp in enumerate(ci_effective_pools):
        log.info("    CI[%d] effective pool : %r  (key: %r)", idx, cp, _pool_key(cp))
    log.info("    CD      effective pool : %r  (key: %r)", cd_effective_pool, _pool_key(cd_effective_pool))
    log.info("    unique keys  : %r", unique_pool_keys)
    if len(unique_pool_keys) <= 1:
        log.info("    decision     : TOP-LEVEL (all same or none)")
    else:
        log.info("    decision     : STAGE-LEVEL (pools differ across pipelines)")

    # ── 2. trigger — merge branch lists from all CI pipelines ─────────────────
    triggers = [ci.get("trigger") for ci in ci_pipelines if "trigger" in ci]
    if triggers:
        result = _merge_trigger(triggers)
        if result is not None:
            merged["trigger"] = result
    else:
        # No CI defines a trigger — disable to avoid accidental runs
        merged["trigger"] = "none"

    # ── 3. pr — merge pr blocks from all CI pipelines + CD ───────────────────
    # Include CD pr block too — enrich_classic_cd may have translated a Classic
    # CD pull request trigger and injected it into cd_pipeline["pr"].
    pr_blocks = [ci.get("pr") for ci in ci_pipelines if "pr" in ci]
    if cd_pipeline.get("pr"):
        pr_blocks.append(cd_pipeline["pr"])
    if pr_blocks:
        result = _merge_pr(pr_blocks)
        if result is not None:
            merged["pr"] = result

    # ── 4. schedules — concatenate CI + CD schedules, deduplicate by cron ──────
    # Include CD schedules too — enrich_classic_cd may have translated Classic
    # CD scheduled triggers and injected them into cd_pipeline["schedules"].
    schedule_lists = [ci.get("schedules", []) for ci in ci_pipelines
                      if ci.get("schedules")]
    if cd_pipeline.get("schedules"):
        schedule_lists.append(cd_pipeline["schedules"])
    if schedule_lists:
        merged_schedules = _merge_schedules(schedule_lists)
        if merged_schedules:
            merged["schedules"] = merged_schedules

    # ── 5. resources — merge by resource alias ────────────────────────────────
    resource_list = [ci.get("resources", {}) for ci in ci_pipelines
                     if ci.get("resources")]
    if resource_list:
        merged_resources = _merge_resources(resource_list)
        if merged_resources:
            merged["resources"] = merged_resources

    # ── 6. variables — CI baseline, CD overrides ──────────────────────────────
    ci_vars_list = [ci.get("variables") for ci in ci_pipelines if ci.get("variables")]
    cd_vars      = cd_pipeline.get("variables")
    merged_vars  = _merge_variables(ci_vars_list, cd_vars)
    if merged_vars:
        merged["variables"] = merged_vars

    # ── 7. Stages ─────────────────────────────────────────────────────────────
    all_stages: list[dict] = []

    for idx, (ci, art_name) in enumerate(zip(ci_pipelines, artifact_names)):
        stage_name = f"Build_{art_name}" if art_name else f"Build_{idx + 1}"
        ci_stages  = extract_stages(ci, stage_name)
        if ci_stages:
            ci_stages[0].setdefault("stage", stage_name)
        all_stages.extend(ci_stages)

    cd_stages = extract_stages(cd_pipeline, "Deploy")
    if cd_stages and all_stages:
        build_stage_ids = [s["stage"] for s in all_stages if "stage" in s]
        if build_stage_ids:
            cd_stages[0]["dependsOn"] = build_stage_ids
    all_stages.extend(cd_stages)

    # ── 8. Smart pool placement ──────────────────────────────────────────────
    #
    # Decision table:
    #   No pools defined anywhere         → do nothing
    #   All pipelines use the same pool   → write once at top level
    #                                       UNLESS CD stages already carry their
    #                                       own pools (enrich_classic_cd set them)
    #                                       → inject into CI stages only
    #   Any pool differs across pipelines → inject at stage level per pipeline;
    #                                       within each stage use _inject_pool_into_stage
    #                                       which places it at stage level if all jobs
    #                                       share the same pool, or at job level when
    #                                       some jobs already have different pools.
    #
    # Golden rule: never overwrite a pool that is already explicitly defined
    # on a stage or a job — that pool was intentionally set and must be kept.

    if not all_pools:
        pass  # nothing to do — no pipeline defines a pool

    elif len(unique_pool_keys) == 1:
        # ── All pipelines share the same pool ─────────────────────────────────
        shared_pool = copy.deepcopy(all_pools[0])

        # If any CI pipeline originally declared a pipeline-level `pool`,
        # move that definition into the CI stages instead of emitting a
        # top-level `pool` in the unified file. This preserves the intent
        # of pipeline-level CI pools while following the requested rule to
        # materialise them at stage level during migration.
        ci_declares_top_level_pool = any("pool" in ci for ci in ci_pipelines)

        if not cd_stage_pools and not ci_declares_top_level_pool:
            # Simple case — write once at top level; Azure Pipelines will
            # apply it to every stage and job that doesn't override it.
            log.info("  Pool placement: TOP-LEVEL (all pipelines share the same pool)")
            merged["pool"] = shared_pool
        else:
            # CD stages already carry per-environment pools from enrich_classic_cd
            # OR one or more CI pipelines declared a pipeline-level pool. In
            # both cases we should avoid a top-level pool that could hide
            # stage-specific settings — inject the shared pool into CI stages
            # using smart placement and ensure CD stages retain their effective
            # pool by injecting CD's top-level pool into its stages when needed.
            log.info("  Pool placement: STAGE-LEVEL for CI (CD stages kept if present)")
            ci_stage_cursor = 0
            for ci, ci_eff_pool in zip(ci_pipelines, ci_effective_pools):
                ci_stage_count = len(extract_stages(ci, "_count"))
                for stage in all_stages[ci_stage_cursor: ci_stage_cursor + ci_stage_count]:
                    _inject_pool_into_stage(stage, shared_pool)
                ci_stage_cursor += ci_stage_count

            # If the CD pipeline declared a top-level pool (but its stages
            # don't already carry per-stage pools), inject that pool into the
            # CD stages so the CD's pool is preserved even though we won't
            # emit a global top-level `pool`.
            cd_declares_top_level_pool = cd_pipeline.get("pool") is not None and not cd_stage_pools
            if cd_declares_top_level_pool:
                cd_pool = copy.deepcopy(cd_pipeline.get("pool"))
                for stage in all_stages[ci_stage_cursor:]:
                    _inject_pool_into_stage(stage, cd_pool)

    else:
        # ── Pools differ → stage-level injection per pipeline ─────────────────
        log.info("  Pool placement: STAGE-LEVEL (pools differ across pipelines)")

        # Map each assembled stage back to the pipeline pool it should use.
        # CI stages → pool of the CI pipeline they came from.
        # CD stages → effective CD pool (or individual stage pool if already set).
        stage_pool_map: dict[str, object] = {}

        ci_stage_cursor = 0
        for ci, ci_eff_pool in zip(ci_pipelines, ci_effective_pools):
            ci_stage_count = len(extract_stages(ci, "_count"))
            for stage in all_stages[ci_stage_cursor: ci_stage_cursor + ci_stage_count]:
                if isinstance(stage, dict) and "stage" in stage:
                    stage_pool_map[stage["stage"]] = ci_eff_pool
            ci_stage_cursor += ci_stage_count

        for stage in all_stages[ci_stage_cursor:]:
            if isinstance(stage, dict) and "stage" in stage:
                stage_pool_map[stage["stage"]] = cd_effective_pool

        # Inject using smart placement:
        #   _inject_pool_into_stage handles stage vs job level automatically.
        #   It never overwrites an existing pool on a stage or job.
        for stage in all_stages:
            if not isinstance(stage, dict):
                continue
            _collapse_duplicate_job_pools_to_stage(stage)
            pool_to_inject = stage_pool_map.get(stage.get("stage"))
            if pool_to_inject is not None:
                _inject_pool_into_stage(stage, pool_to_inject)

    # Final cleanup: collapse duplicate job pools into stage-level pool where
    # more than one job uses the same agent, while preserving distinct job-
    # level pools for jobs using different agents.
    for stage in all_stages:
        if isinstance(stage, dict):
            _collapse_duplicate_job_pools_to_stage(stage)

    merged["stages"] = all_stages
    return merged


def _materialize_pipeline_level_pool(pipeline: dict, default_stage_name: str = "Build") -> None:
    """
    If a pipeline defines a top-level `pool`, move that definition into
    its stages or jobs according to the smart-placement rules.

    - If `stages` exists: inject the pool into each stage (smartly at
      stage or job level via `_inject_pool_into_stage`).
    - If flat (jobs/steps only): wrap into a synthetic stage and inject
      the pool there, then replace `jobs`/`steps` with `stages`.

    This mutates `pipeline` in-place and removes the top-level `pool`.
    """
    if "pool" not in pipeline:
        return
    pool = copy.deepcopy(pipeline["pool"])

    if "stages" in pipeline and isinstance(pipeline["stages"], list):
        for stage in pipeline["stages"]:
            _inject_pool_into_stage(stage, pool)
        pipeline.pop("pool", None)
        return

    # Flat pipeline — convert to a stages list then inject
    new_stages = extract_stages(pipeline, default_stage_name)
    for stage in new_stages:
        _inject_pool_into_stage(stage, pool)
    # Remove flat structure keys and install stages
    pipeline.pop("jobs", None)
    pipeline.pop("steps", None)
    pipeline["stages"] = new_stages
    pipeline.pop("pool", None)


# ─── Yamlizr File Locator ─────────────────────────────────────────────────────

def _normalise_name(name: str) -> str:
    """
    Normalise a pipeline name for fuzzy matching:
    - lowercase
    - collapse whitespace
    - treat hyphens and spaces as equivalent
    This handles the Yamlizr quirk where 'CI_CD_Classic-CI' (Azure DevOps)
    is saved as 'CI_CD_Classic CI' (Yamlizr replaces hyphens with spaces).
    """
    import re
    name = name.strip().lower()
    # Replace hyphens surrounded by word chars with a space, then collapse spaces
    name = re.sub(r"[-\s]+", " ", name)
    return name


def find_yamlizr_file(directory: Path, pipeline_name: str,
                      definition_id: int | None = None) -> Path | None:
    """
    Find the Yamlizr-generated file for a pipeline by matching the
    <PipelineName> portion of <PipelineName>-<DefinitionId>.yml.

    Yamlizr naming: <PipelineName>-<DefinitionId>.yml
    The DefinitionId is always a bare integer suffix, e.g. -42.

    Matching is fuzzy: case-insensitive, hyphens == spaces, whitespace collapsed.
    When definition_id is supplied it is used to break ties between files that
    share the same normalised name (e.g. duplicates with different IDs).

    Examples (Azure DevOps name → Yamlizr filename):
      'CI_CD_Classic-CI'     → 'CI_CD_Classic CI-5.yml'
      'New release pipeline' → 'New release pipeline-1.yml'
      'My Pipeline (1)'      → 'My Pipeline (1)-12.yml'
    """
    import re
    needle = _normalise_name(pipeline_name)

    if not directory.exists():
        log.warning("  Directory does not exist: %s", directory)
        return None

    candidates = list(directory.glob("*.yml"))
    log.debug("  Searching %d file(s) in %s for pipeline '%s' (normalised: '%s', id=%s)",
              len(candidates), directory, pipeline_name, needle, definition_id)

    matches = []
    for f in candidates:
        # Extract the DefinitionId from the end of the stem
        id_match  = re.search(r"-(\d+)$", f.stem)
        file_id   = int(id_match.group(1)) if id_match else None
        stem_name = _normalise_name(re.sub(r"-\d+$", "", f.stem))
        log.debug("    %s  →  normalised='%s'  file_id=%s  needle='%s'  match=%s",
                  f.name, stem_name, file_id, needle, stem_name == needle)
        if stem_name == needle:
            matches.append((file_id, f))

    if not matches:
        log.debug("  No match found for '%s'. Files present: %s",
                  pipeline_name, [f.name for f in candidates])
        return None

    # Prefer exact DefinitionId match when available
    if definition_id is not None:
        for file_id, f in matches:
            if file_id == definition_id:
                return f

    # Fall back to first match (sorted by id for determinism)
    matches.sort(key=lambda x: x[0] if x[0] is not None else 0)
    return matches[0][1]


# ─── Classic CD Enrichment ────────────────────────────────────────────────────

def _translate_classic_cd_triggers(triggers: list[dict], rel_name: str) -> dict:
    """
    Translate the Classic CD triggers array into YAML-equivalent top-level keys.

    Returns a dict with zero or more of: schedules, pr
    (artifact triggers are intentionally NOT translated — see inline comment).

    Logs a clear message for every trigger type encountered.
    """
    result: dict = {}
    schedules: list[dict] = []
    pr_branches: list[str] = []

    for t in triggers:
        ttype = t.get("triggerType", "").lower()

        # ── Artifact / Continuous Deployment trigger ──────────────────────────
        if ttype in ("artifactsource", "artifact", "64"):
            # In a unified multi-stage YAML pipeline the build stage and deploy
            # stage live in the same file.  The deploy stage already runs
            # automatically after the build stage via `dependsOn` — so the
            # artifact trigger is IMPLICITLY HANDLED by the pipeline structure.
            # Translating it to a trigger: block would re-run the whole pipeline
            # (including the build) every time a build completes, which is wrong.
            log.info(
                "  ℹ  Artifact (continuous deployment) trigger detected on '%s' — "
                "handled implicitly by dependsOn in the unified pipeline. "
                "No explicit trigger block needed.",
                rel_name,
            )

        # ── Scheduled trigger ─────────────────────────────────────────────────
        elif ttype in ("schedule", "onschedule", "4"):
            # The Azure DevOps API returns schedule triggers in two shapes
            # depending on the API version:
            #
            # Shape A (older):  { triggerType: "schedule",
            #                     schedule: { startHours, startMinutes,
            #                                 daysToRelease, timeZoneId } }
            #
            # Shape B (newer):  { triggerType: "schedule",
            #                     schedules: [ { startHours, startMinutes,
            #                                    daysToRelease, timeZoneId } ] }
            #
            # Handle both by trying "schedules" (list) first, then "schedule" (dict).
            schedule_blocks: list[dict] = []
            if t.get("schedules"):
                raw = t["schedules"]
                schedule_blocks = raw if isinstance(raw, list) else [raw]
            elif t.get("schedule"):
                schedule_blocks = [t["schedule"]]

            if not schedule_blocks:
                log.debug("  Schedule trigger has no schedule data: %r", t)
                continue

            for schedule_block in schedule_blocks:
                cron = _classic_schedule_to_cron(schedule_block)
                tz   = schedule_block.get("timeZoneId", "UTC")
                entry: dict = {
                    "cron":        cron,
                    "displayName": f"Migrated from Classic CD schedule ({tz})",
                    "branches":    {"include": ["main"]},
                    "always":      True,
                }
                schedules.append(entry)
                log.info("  ✔  Scheduled trigger translated → cron: '%s'  tz: %s", cron, tz)

        # ── Pull Request trigger ───────────────────────────────────────────────
        elif ttype in ("pullrequest", "8"):
            # Classic PR trigger has a list of artifact branch filters
            for af in t.get("artifactFilters", []):
                branch = af.get("sourceBranch", "").lstrip("refs/heads/")
                if branch and branch not in pr_branches:
                    pr_branches.append(branch)
            if not pr_branches:
                pr_branches = ["main"]
            log.warning(
                "  ⚠  Pull Request trigger detected on '%s' — translated to pr: block. "
                "REVIEW RECOMMENDED: deploying on a PR is unusual and may be unintentional.",
                rel_name,
            )

        else:
            log.debug("  Unknown trigger type '%s' on '%s' — skipped.", ttype, rel_name)

    if schedules:
        result["schedules"] = schedules
    if pr_branches:
        result["pr"] = {"branches": {"include": pr_branches}}

    return result


def _classic_schedule_to_cron(schedule: dict) -> str:
    """
    Convert a Classic schedule block to a cron expression.

    Classic fields:
      startHours      : 0–23
      startMinutes    : 0–59
      daysToRelease   : bitmask  Monday=1, Tuesday=2, Wednesday=4, Thursday=8,
                              Friday=16, Saturday=32, Sunday=64
      daysToBuild     : same as daysToRelease for build definitions
    """
    hour    = schedule.get("startHours",   0)
    minute  = schedule.get("startMinutes", 0)
    mask    = schedule.get("daysToRelease", schedule.get("daysToBuild", 127))

    # Map bitmask to cron day-of-week (0=Sun … 6=Sat in cron)
    # Classic bitmask: Mon=1 Tue=2 Wed=4 Thu=8 Fri=16 Sat=32 Sun=64
    classic_to_cron = {1: 1, 2: 2, 4: 3, 8: 4, 16: 5, 32: 6, 64: 0}
    days = [
        str(cron_day)
        for bit, cron_day in sorted(classic_to_cron.items())
        if mask & bit
    ]

    if not days or len(days) == 7:
        dow = "*"
    else:
        dow = ",".join(days)

    return f"{minute} {hour} * * {dow}"


def _translate_classic_build_triggers(triggers: list[dict], build_def: dict, client: AzureDevOpsClient) -> dict:
    """
    Translate Classic build-definition triggers into YAML equivalents.

    Returns a dict with optional keys: trigger, pr, schedules, resources.
    """
    result: dict = {}
    trigger_blocks: list[dict] = []
    pr_branches: list[str] = []
    schedules: list[dict] = []
    pipeline_resources: list[dict] = []

    def _normalize_branch_filters(filters: list[str]) -> list[str]:
        branches: list[str] = []
        for b in filters:
            if not b:
                continue
            normalized = b
            if normalized.startswith("+") or normalized.startswith("-"):
                normalized = normalized[1:]
            if normalized.startswith("refs/heads/"):
                normalized = normalized[len("refs/heads/"):]
            if normalized and normalized not in branches:
                branches.append(normalized)
        return branches

    for t in triggers:
        ttype = t.get("triggerType", "").lower()

        if ttype in ("continuousintegration", "batchedcontinuousintegration"):
            trigger: dict = {}
            branches = _normalize_branch_filters(t.get("branchFilters", []))
            if branches:
                trigger["branches"] = {"include": branches}
            paths = [p for p in t.get("pathFilters", []) if p]
            if paths:
                trigger["paths"] = {"include": paths}
            if trigger:
                trigger_blocks.append(trigger)

        elif ttype == "schedule":
            schedule_blocks = []
            if t.get("schedules"):
                schedule_blocks = t["schedules"] if isinstance(t["schedules"], list) else [t["schedules"]]
            elif t.get("schedule"):
                schedule_blocks = [t["schedule"]]

            for schedule_block in schedule_blocks:
                cron = _classic_schedule_to_cron(schedule_block)
                entry: dict = {
                    "cron": cron,
                    "displayName": f"Migrated from Classic CI schedule ({schedule_block.get('timeZoneId', 'UTC')})",
                }
                branch_filters = _normalize_branch_filters(schedule_block.get("branchFilters", []))
                if branch_filters:
                    entry["branches"] = {"include": branch_filters}
                else:
                    entry["branches"] = {"include": ["main"]}
                if not schedule_block.get("scheduleOnlyWithChanges", False):
                    entry["always"] = True
                schedules.append(entry)

        elif ttype == "buildcompletion":
            upstream = t.get("definition", {})
            upstream_id = upstream.get("id")
            if upstream_id is None:
                continue
            upstream_def = client.get_build_definition(int(upstream_id))
            upstream_name = upstream_def.get("name", f"build_{upstream_id}")
            alias = _normalise_name(upstream_name).replace(" ", "_")
            if not alias:
                alias = f"build_{upstream_id}"
            entry: dict = {
                "pipeline": alias,
                "source": upstream_name,
            }
            branch_filters = _normalize_branch_filters(t.get("branchFilters", []))
            if branch_filters:
                entry["trigger"] = {"branches": {"include": branch_filters}}
            else:
                entry["trigger"] = True
            if upstream_def.get("project", {}).get("name"):
                entry["project"] = upstream_def["project"]["name"]
            pipeline_resources.append(entry)

        elif ttype in ("pullrequest", "pullrequesttrigger"):
            for branch in t.get("branchFilters", []):
                clean = branch.lstrip("+refs/heads/").lstrip("refs/heads/")
                if clean and clean not in pr_branches:
                    pr_branches.append(clean)
            if not pr_branches:
                pr_branches = ["main"]

    if trigger_blocks:
        trigger_merged = _merge_trigger(trigger_blocks)
        if trigger_merged is not None:
            result["trigger"] = trigger_merged

    if pr_branches:
        result["pr"] = {"branches": {"include": pr_branches}}

    if schedules:
        result["schedules"] = schedules

    if pipeline_resources:
        result["resources"] = {"pipelines": pipeline_resources}

    return result


def enrich_ci_pipeline(
    ci_pipeline: dict,
    build_def_id: int,
    client: AzureDevOpsClient,
) -> dict:
    """
    Enrich a CI pipeline dict with triggers from its build definition.

    This preserves Classic CI build triggers such as schedule and
    build-completion when the YAML itself does not contain them.
    """
    build_def = client.get_build_definition(build_def_id)
    raw_triggers = build_def.get("triggers", [])
    if not raw_triggers:
        return ci_pipeline

    translated = _translate_classic_build_triggers(raw_triggers, build_def, client)
    if not translated:
        return ci_pipeline

    log.info("  Enriching CI pipeline with build definition triggers …")
    for key, val in translated.items():
        if key == "trigger":
            if "trigger" in ci_pipeline:
                merged = _merge_trigger([ci_pipeline["trigger"], val])
                if merged is not None:
                    ci_pipeline["trigger"] = merged
            else:
                ci_pipeline["trigger"] = val
        elif key == "pr":
            if "pr" in ci_pipeline:
                merged = _merge_pr([ci_pipeline["pr"], val])
                if merged is not None:
                    ci_pipeline["pr"] = merged
            else:
                ci_pipeline["pr"] = val
        elif key == "schedules":
            existing = ci_pipeline.setdefault("schedules", [])
            seen = {entry.get("cron") for entry in existing}
            existing.extend([entry for entry in val if entry.get("cron") not in seen])
        elif key == "resources":
            resources = ci_pipeline.setdefault("resources", {})
            pipelines = resources.setdefault("pipelines", [])
            seen_aliases = {entry.get("pipeline") for entry in pipelines}
            for entry in val.get("pipelines", []):
                if entry.get("pipeline") not in seen_aliases:
                    pipelines.append(entry)
                    seen_aliases.add(entry.get("pipeline"))
        else:
            if key not in ci_pipeline:
                ci_pipeline[key] = val

    return ci_pipeline


def enrich_classic_cd(
    cd_pipeline: dict,
    release_def: dict,
    client: AzureDevOpsClient,
    emit_artifact_triggers: bool = False,
) -> dict:
    """
    Enrich a Yamlizr-converted Classic CD pipeline dict with two things
    Yamlizr misses:

    1. Triggers  — read from the release definition's `triggers` array,
                   translate to YAML equivalents, inject into the pipeline.
    2. Agent pools — read from each environment's deployPhases, inject the
                     correct pool into the matching stage in the YAML.

    Returns the enriched pipeline dict (modified in place, also returned).
    """
    rel_name = release_def.get("name", "")
    log.info("  Enriching Classic CD with triggers and agent pools …")

    # ── Triggers ──────────────────────────────────────────────────────────────
    raw_triggers = client.get_classic_cd_triggers(release_def)
    if raw_triggers:
        log.debug("  Raw triggers from API (%d): %s", len(raw_triggers), raw_triggers)
        translated = _translate_classic_cd_triggers(raw_triggers, rel_name)
        log.debug("  Translated trigger keys: %s", list(translated.keys()))
        for key, val in translated.items():
            if key not in cd_pipeline:
                cd_pipeline[key] = val
            elif key == "schedules":
                # Merge rather than overwrite
                existing = cd_pipeline[key] if isinstance(cd_pipeline[key], list) else []
                seen = {e.get("cron") for e in existing}
                cd_pipeline[key] = existing + [e for e in val if e.get("cron") not in seen]
    else:
        log.info("  No Classic CD triggers found for '%s'.", rel_name)

    # ── Optional: emit explicit pipeline resources for build-completion triggers
    # When enabled, convert Classic CD artifact (build completion) triggers
    # into `resources.pipelines` entries with `trigger: true` so the unified
    # YAML can be triggered directly by the CI build pipeline.
    if emit_artifact_triggers:
        pipelines_list = cd_pipeline.setdefault("resources", {}).setdefault("pipelines", [])
        existing_aliases = {p.get("pipeline") or p.get("alias") for p in pipelines_list}
        for art in release_def.get("artifacts", []):
            if art.get("type", "").lower() != "build":
                continue
            alias = art.get("alias") or art.get("definitionReference", {}).get("definition", {}).get("name")
            source = art.get("definitionReference", {}).get("definition", {}).get("name") or alias
            if not alias:
                continue
            if alias in existing_aliases:
                continue
            entry = {"pipeline": alias, "source": source, "trigger": True}
            pipelines_list.append(entry)
            existing_aliases.add(alias)
            log.info("  ✔  Injected pipeline resource for artifact '%s' (build-completion trigger)", alias)

    # ── Agent pools per environment / stage ───────────────────────────────────
    env_pools = client.get_classic_cd_environment_pools(release_def)
    if not env_pools:
        log.info("  No agent pool information found in Classic CD environments.")
        return cd_pipeline

    log.info("  Agent pools detected per environment:")
    for env, pool in env_pools.items():
        log.info("    %-30s → %s", env, pool)

    # Yamlizr can produce two structures:
    #   A) Multi-stage YAML  →  cd_pipeline has a "stages" list
    #   B) Flat YAML         →  cd_pipeline has only "jobs" or "steps" (no "stages")
    #
    # For (A): inject pool into each stage that matches an environment name.
    # For (B): the whole file represents one environment — inject the pool at
    #          the top level of the cd_pipeline dict (pool: {name: ...}).
    #          merge_pipelines will then pick it up as cd_pipeline.get("pool").

    stages = cd_pipeline.get("stages", [])

    if not stages:
        # ── Flat structure (no stages key) ─────────────────────────────────────
        if "pool" not in cd_pipeline:
            # Use the first environment pool as the top-level pool
            first_pool = next(iter(env_pools.values()), None)
            if first_pool:
                cd_pipeline["pool"] = copy.deepcopy(first_pool)
                log.info("  Flat CD structure — injected pool at top level: %s", first_pool)
        return cd_pipeline

    # ── Multi-stage structure ──────────────────────────────────────────────────
    # Inject the pool into each stage whose name matches an environment name.
    # Matching is fuzzy (normalised) because Yamlizr may sanitise stage names.
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        if "pool" in stage:
            continue   # stage already has a pool — don't overwrite
        stage_name = stage.get("stage", stage.get("displayName", ""))
        # Try exact match first, then fuzzy
        pool = env_pools.get(stage_name)
        if pool is None:
            norm_stage = _normalise_name(stage_name)
            for env_name, env_pool in env_pools.items():
                if _normalise_name(env_name) == norm_stage:
                    pool = env_pool
                    break
        if pool is not None:
            stage["pool"] = copy.deepcopy(pool)
            log.info("    Injected pool '%s' into stage '%s'", pool, stage_name)
        else:
            log.debug("    No pool match found for stage '%s'", stage_name)

    return cd_pipeline


# ─── Scenario Handlers ────────────────────────────────────────────────────────

def handle_scenario_1(
    release_def: dict,
    artifacts: list[dict],
    unified_dir: Path,
    client: AzureDevOpsClient,
) -> None:
    """YAML CI + YAML CD.

    A YAML multi-stage CD pipeline is registered in Azure DevOps as both a
    release pipeline (release definition ID) AND a build pipeline (build
    definition ID).  All content fetching must use the BUILD definition ID
    because the YAML file and its path live in the build pipeline record.
    """
    rel_name = release_def["name"]
    out_path  = unified_dir / f"{rel_name}.yml"
    log.info("  Scenario 1 — YAML CI + YAML CD")

    # ── Resolve the CD build definition ID ────────────────────────────────────
    cd_build_id = client.get_yaml_cd_build_id(release_def)
    if cd_build_id is None:
        log.warning(
            "  ⚠  Could not resolve build definition ID for YAML CD pipeline '%s'. "
            "Try checking pipelineConfigurationId in the release definition. Skipping.",
            rel_name,
        )
        return
    log.debug("  CD build definition ID: %d", cd_build_id)

    # ── Fetch the CD YAML file path and content ────────────────────────────────
    cd_yaml_path = client.get_yaml_pipeline_file_path(cd_build_id)
    cd_content   = client.get_yaml_pipeline_content(cd_build_id)
    if not cd_content:
        log.warning("  ⚠  Could not fetch CD YAML content for '%s'. Skipping.", rel_name)
        return

    cd_pipeline = load_yaml_str(cd_content)

    # ── 0 artifacts — write CD as-is ──────────────────────────────────────────
    if not artifacts:
        log.info("  No artifacts — writing CD YAML as-is.")
        dump_yaml(cd_pipeline, out_path)
        return

    # ── Collect CI pipelines and check if already unified ─────────────────────
    # "Already unified" means every CI pipeline points to the same YAML file
    # as the CD pipeline — i.e. they are all defined in one file already.
    all_same: bool          = True
    ci_pipelines: list[dict] = []
    art_names: list[str]     = []

    for art in artifacts:
        ci_def_id    = int(art["definitionReference"]["definition"]["id"])
        ci_yaml_path = client.get_yaml_pipeline_file_path(ci_def_id)

        if ci_yaml_path != cd_yaml_path:
            all_same = False

        ci_content = client.get_yaml_pipeline_content(ci_def_id)
        if not ci_content:
            log.warning(
                "  ⚠  Could not fetch CI YAML for artifact '%s'. Skipping.",
                art.get("alias"),
            )
            return
        ci = load_yaml_str(ci_content)
        ci = enrich_ci_pipeline(ci, ci_def_id, client)
        ci_pipelines.append(ci)
        art_names.append(art.get("alias", f"Artifact{len(art_names)+1}"))

    if all_same:
        log.info("  Already unified — no action required.")
        return

    # ── Merge and write ────────────────────────────────────────────────────────
    merged = merge_pipelines(ci_pipelines, cd_pipeline, art_names)
    dump_yaml(merged, out_path)


def handle_scenario_2(
    release_def: dict,
    artifacts: list[dict],
    unified_dir: Path,
    releases_dir: Path,
    client: AzureDevOpsClient,
    emit_artifact_triggers: bool = False,
) -> None:
    """YAML CI + Classic CD."""
    rel_name  = release_def["name"]
    out_path  = unified_dir / f"{rel_name}.yml"
    log.info("  Scenario 2 — YAML CI + Classic CD")

    cd_file = find_yamlizr_file(releases_dir, rel_name, definition_id=release_def["id"])
    if not cd_file:
        log.error("  ✖  No Yamlizr CD file found for '%s' in %s", rel_name, releases_dir)
        return
    cd_pipeline = load_yaml(cd_file)

    # Enrich Yamlizr output with triggers and agent pools from the API
    cd_pipeline = enrich_classic_cd(cd_pipeline, release_def, client, emit_artifact_triggers)

    if not artifacts:
        log.info("  No artifacts — writing enriched Yamlizr CD YAML as-is.")
        dump_yaml(cd_pipeline, out_path)
        return

    ci_pipelines: list[dict] = []
    art_names: list[str]     = []

    for art in artifacts:
        ci_def_id  = int(art["definitionReference"]["definition"]["id"])
        ci_content = client.get_yaml_pipeline_content(ci_def_id)
        if not ci_content:
            log.warning("  ⚠  Could not fetch YAML CI for artifact '%s'. Skipping.", art.get("alias"))
            return
        ci = load_yaml_str(ci_content)
        ci = enrich_ci_pipeline(ci, ci_def_id, client)
        # If CI pipeline declares a top-level pool, materialise it at stage/job level
        _materialize_pipeline_level_pool(ci)
        ci_pipelines.append(ci)
        art_names.append(art.get("alias", f"Artifact{len(art_names)+1}"))

    merged = merge_pipelines(ci_pipelines, cd_pipeline, art_names)
    dump_yaml(merged, out_path)


def handle_scenario_3(
    release_def: dict,
    artifacts: list[dict],
    unified_dir: Path,
    releases_dir: Path,
    builds_dir: Path,
    client: AzureDevOpsClient,
    emit_artifact_triggers: bool = False,
) -> None:
    """Classic CI + Classic CD."""
    rel_name  = release_def["name"]
    out_path  = unified_dir / f"{rel_name}.yml"
    log.info("  Scenario 3 — Classic CI + Classic CD")

    cd_file = find_yamlizr_file(releases_dir, rel_name, definition_id=release_def["id"])
    if not cd_file:
        log.error("  ✖  No Yamlizr CD file found for '%s' in %s", rel_name, releases_dir)
        return
    cd_pipeline = load_yaml(cd_file)

    # Enrich Yamlizr output with triggers and agent pools from the API
    cd_pipeline = enrich_classic_cd(cd_pipeline, release_def, client, emit_artifact_triggers)

    if not artifacts:
        log.info("  No artifacts — writing enriched Yamlizr CD YAML as-is.")
        dump_yaml(cd_pipeline, out_path)
        return

    ci_pipelines: list[dict] = []
    art_names: list[str]     = []

    for art in artifacts:
        ci_name   = art["definitionReference"]["definition"].get("name", "")
        ci_def_id = int(art["definitionReference"]["definition"].get("id", 0))
        ci_file   = find_yamlizr_file(builds_dir, ci_name, definition_id=ci_def_id)
        if not ci_file:
            log.error(
                "  ✖  No Yamlizr CI file found for artifact pipeline '%s' in %s",
                ci_name, builds_dir,
            )
            return
        ci = load_yaml(ci_file)
        ci = enrich_ci_pipeline(ci, ci_def_id, client)
        _materialize_pipeline_level_pool(ci)
        ci_pipelines.append(ci)
        art_names.append(art.get("alias", ci_name))

    merged = merge_pipelines(ci_pipelines, cd_pipeline, art_names)
    dump_yaml(merged, out_path)


# ─── Main Orchestrator ────────────────────────────────────────────────────────

def _resolve_yamlizr_root(out: Path, project: str) -> Path:
    """
    Yamlizr sometimes nests its output under a project-named subfolder:
      <out>/<project>/AzureDevOpsBuilds/
      <out>/<project>/AzureDevOpsReleases/
    This function detects which layout was used and returns the correct root.
    """
    # Check direct layout first
    if (out / "AzureDevOpsBuilds").exists() or (out / "AzureDevOpsReleases").exists():
        return out
    # Check project-named subfolder (Yamlizr default on some versions)
    nested = out / project
    if (nested / "AzureDevOpsBuilds").exists() or (nested / "AzureDevOpsReleases").exists():
        log.info("Detected Yamlizr project subfolder: %s", nested)
        return nested
    # Fall back to direct — will surface a clear error later if files are missing
    return out


def run(
    pat: str,
    org: str,
    project: str,
    output_dir: str,
    emit_artifact_triggers: bool = False,
) -> None:
    out          = Path(output_dir)
    yamlizr_root = _resolve_yamlizr_root(out, project)
    builds_dir   = yamlizr_root / "AzureDevOpsBuilds"
    releases_dir = yamlizr_root / "AzureDevOpsReleases"
    unified_dir  = out / "Unified"
    unified_dir.mkdir(parents=True, exist_ok=True)
    log.info("Yamlizr root : %s", yamlizr_root)
    log.info("Builds dir   : %s", builds_dir)
    log.info("Releases dir : %s", releases_dir)
    log.info("Unified dir  : %s", unified_dir)

    client = AzureDevOpsClient(pat, org, project)

    log.info("Fetching release pipeline definitions …")
    release_summaries = client.list_release_definitions()
    log.info("Found %d release pipeline(s).", len(release_summaries))

    for summary in release_summaries:
        rel_id   = summary["id"]
        rel_name = summary["name"]
        log.info("")
        log.info("━━ Processing: %s (id=%s)", rel_name, rel_id)

        # Fetch full definition (includes artifacts)
        release_def = client.get_release_definition(rel_id)
        artifacts   = release_def.get("artifacts", [])
        # Filter to only build-type artifacts (ignore repo/container/etc.)
        build_artifacts = [
            a for a in artifacts
            if a.get("type", "").lower() == "build"
        ]
        log.info("  Artifacts (build type): %d", len(build_artifacts))

        # ── Determine scenario ─────────────────────────────────────────────
        is_yaml_cd = client.is_yaml_release_pipeline(release_def)

        if is_yaml_cd:
            # Scenario 1: YAML CD → all CIs must be YAML
            # The handler resolves the CD build definition ID internally.
            handle_scenario_1(release_def, build_artifacts, unified_dir, client)
        else:
            # Classic CD — check the CI type(s)
            if not build_artifacts:
                # No artifacts → Classic CD, no CI → Scenario 3 (0-artifact path)
                handle_scenario_3(release_def, [], unified_dir, releases_dir, builds_dir, client)
                continue

            # Check the first artifact's build pipeline type
            first_ci_id  = int(build_artifacts[0]["definitionReference"]["definition"]["id"])
            first_is_yaml = client.is_yaml_pipeline(first_ci_id)

            if first_is_yaml:
                # Scenario 2: YAML CI + Classic CD
                handle_scenario_2(release_def, build_artifacts, unified_dir, releases_dir, client, emit_artifact_triggers)
            else:
                # Scenario 3: Classic CI + Classic CD
                handle_scenario_3(
                    release_def, build_artifacts, unified_dir, releases_dir, builds_dir, client, emit_artifact_triggers
                )

    log.info("")
    log.info("✅  Unification complete. Output: %s", unified_dir)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge Yamlizr-converted Azure DevOps pipelines into unified YAML files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python unifier.py \\
      -pat  myPersonalAccessToken \\
      -org  https://dev.azure.com/myorg \\
      -proj MyProject \\
      -out  ./output

  python unifier.py -pat TOKEN -org https://dev.azure.com/acme -proj WebApp -out /tmp/pipelines
        """,
    )
    parser.add_argument("-pat",  required=True, metavar="TOKEN",  help="Azure DevOps Personal Access Token")
    parser.add_argument("-org",  required=True, metavar="URL",    help="Organisation URL (https://dev.azure.com/<org>)")
    parser.add_argument("-proj", required=True, metavar="NAME",   help="Azure DevOps project name")
    parser.add_argument("-out",  required=True, metavar="DIR",    help="Output directory (same as Yamlizr -out)")
    parser.add_argument("-v", "--verbose", action="store_true",   help="Enable DEBUG logging")
    parser.add_argument(
        "--emit-artifact-triggers",
        action="store_true",
        help="Emit explicit resources.pipelines entries for Classic CD artifact (build-completion) triggers",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        run(
            pat=args.pat,
            org=args.org,
            project=args.proj,
            output_dir=args.out,
            emit_artifact_triggers=args.emit_artifact_triggers,
        )
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(1)
    except requests.HTTPError as exc:
        log.error("Azure DevOps API error: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
