"""
Tests for the Yamlizr Pipeline Unifier.
Uses stdlib unittest only — no external test framework required.

Run with:  python tests.py
       or:  python -m pytest tests.py -v   (if pytest is installed)
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml

import unifier as u

# ─── Shared fixtures ──────────────────────────────────────────────────────────

SIMPLE_CI = {
    "trigger": ["main"],
    "stages": [
        {
            "stage": "Build",
            "jobs": [{"job": "compile", "steps": [{"script": "dotnet build"}]}],
        }
    ],
}

SIMPLE_CD = {
    "stages": [
        {
            "stage": "Deploy",
            "jobs": [{"job": "deploy", "steps": [{"script": "echo deploy"}]}],
        }
    ],
}


def tmp_dir() -> Path:
    return Path(tempfile.mkdtemp())


def make_release_def(name="MyRelease", rel_id=1, has_environments=False):
    defn = {"id": rel_id, "name": name, "artifacts": []}
    if has_environments:
        defn["environments"] = [{"name": "Prod"}]
    return defn


# ─── find_yamlizr_file ────────────────────────────────────────────────────────

class TestFindYamlizrFile(unittest.TestCase):

    def setUp(self):
        self.d = tmp_dir()

    def test_found(self):
        (self.d / "MyPipeline-42.yml").touch()
        result = u.find_yamlizr_file(self.d, "MyPipeline")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "MyPipeline-42.yml")

    def test_case_insensitive(self):
        (self.d / "mypipeline-7.yml").touch()
        self.assertIsNotNone(u.find_yamlizr_file(self.d, "MyPipeline"))

    def test_not_found(self):
        (self.d / "Other-1.yml").touch()
        self.assertIsNone(u.find_yamlizr_file(self.d, "MyPipeline"))

    def test_name_with_dashes(self):
        (self.d / "My-Fancy-Pipeline-99.yml").touch()
        self.assertIsNotNone(u.find_yamlizr_file(self.d, "My-Fancy-Pipeline"))


# ─── extract_stages ───────────────────────────────────────────────────────────

class TestExtractStages(unittest.TestCase):

    def test_stages_key(self):
        pipeline = {"stages": [{"stage": "A"}, {"stage": "B"}]}
        stages = u.extract_stages(pipeline, "Fallback")
        self.assertEqual(len(stages), 2)
        self.assertEqual(stages[0]["stage"], "A")

    def test_flat_jobs(self):
        pipeline = {"jobs": [{"job": "build", "steps": []}]}
        stages = u.extract_stages(pipeline, "MyBuild")
        self.assertEqual(len(stages), 1)
        self.assertIn("jobs", stages[0])

    def test_flat_steps(self):
        pipeline = {"steps": [{"script": "echo hi"}]}
        stages = u.extract_stages(pipeline, "MyBuild")
        self.assertEqual(stages[0]["jobs"][0]["steps"][0]["script"], "echo hi")



# ─── Tests: top-level merge helpers ──────────────────────────────────────────

class TestMergeTrigger(unittest.TestCase):

    def test_single_longhand(self):
        t = {"branches": {"include": ["main", "develop"]}}
        self.assertEqual(u._merge_trigger([t])["branches"]["include"], ["main", "develop"])

    def test_two_pipelines_union(self):
        t1 = {"branches": {"include": ["main"]}}
        t2 = {"branches": {"include": ["release/*"]}}
        result = u._merge_trigger([t1, t2])
        self.assertIn("main",      result["branches"]["include"])
        self.assertIn("release/*", result["branches"]["include"])

    def test_deduplication(self):
        t1 = {"branches": {"include": ["main"]}}
        t2 = {"branches": {"include": ["main", "develop"]}}
        self.assertEqual(u._merge_trigger([t1, t2])["branches"]["include"].count("main"), 1)

    def test_all_none(self):
        self.assertEqual(u._merge_trigger(["none", "none"]), "none")

    def test_one_none_one_real(self):
        result = u._merge_trigger(["none", {"branches": {"include": ["main"]}}])
        self.assertIn("main", result["branches"]["include"])

    def test_paths_merged(self):
        t1 = {"paths": {"include": ["src/"]}}
        t2 = {"paths": {"include": ["lib/"]}}
        result = u._merge_trigger([t1, t2])
        self.assertIn("src/", result["paths"]["include"])
        self.assertIn("lib/", result["paths"]["include"])

    def test_shorthand_list(self):
        result = u._merge_trigger([["main"], ["develop"]])
        self.assertIn("main",    result["branches"]["include"])
        self.assertIn("develop", result["branches"]["include"])

    def test_batch_preserved(self):
        t = {"batch": True, "branches": {"include": ["main"]}}
        self.assertTrue(u._merge_trigger([t])["batch"])


class TestMergePr(unittest.TestCase):

    def test_union(self):
        p1 = {"branches": {"include": ["main"]}}
        p2 = {"branches": {"include": ["feature/*"]}}
        result = u._merge_pr([p1, p2])
        self.assertIn("main",      result["branches"]["include"])
        self.assertIn("feature/*", result["branches"]["include"])

    def test_all_none(self):
        self.assertEqual(u._merge_pr(["none", "none"]), "none")

    def test_drafts_preserved(self):
        self.assertFalse(u._merge_pr([{"drafts": False, "branches": {"include": ["main"]}}])["drafts"])


class TestMergeSchedules(unittest.TestCase):

    def test_concatenates(self):
        s1 = [{"cron": "0 0 * * *"}]
        s2 = [{"cron": "0 6 * * 1"}]
        self.assertEqual(len(u._merge_schedules([s1, s2])), 2)

    def test_deduplicates_by_cron(self):
        s = [{"cron": "0 0 * * *"}]
        self.assertEqual(len(u._merge_schedules([s, s])), 1)


class TestMergeResources(unittest.TestCase):

    def test_merges_pipelines(self):
        r1 = {"pipelines": [{"pipeline": "BuildA", "source": "BuildA"}]}
        r2 = {"pipelines": [{"pipeline": "BuildB", "source": "BuildB"}]}
        aliases = [p["pipeline"] for p in u._merge_resources([r1, r2])["pipelines"]]
        self.assertIn("BuildA", aliases)
        self.assertIn("BuildB", aliases)

    def test_deduplicates(self):
        r = {"pipelines": [{"pipeline": "BuildA", "source": "BuildA"}]}
        self.assertEqual(len(u._merge_resources([r, r])["pipelines"]), 1)


class TestMergeVariables(unittest.TestCase):

    def test_cd_wins_on_conflict(self):
        ci_vars = [{"name": "env", "value": "dev"}]
        result  = u._merge_variables([ci_vars], {"env": "prod"})
        entry   = next(v for v in result if v.get("name") == "env")
        self.assertEqual(entry.get("value"), "prod")

    def test_ci_keys_survive(self):
        result = u._merge_variables([[{"name": "ciOnly", "value": "yes"}]], None)
        self.assertIn("ciOnly", [v.get("name") for v in result])

    def test_multiple_ci_merged(self):
        result = u._merge_variables([[{"name": "a", "value": "1"}],
                                     [{"name": "b", "value": "2"}]], None)
        names = [v.get("name") for v in result]
        self.assertIn("a", names)
        self.assertIn("b", names)


# ─── merge_pipelines ──────────────────────────────────────────────────────────

class TestMergePipelines(unittest.TestCase):

    def test_single_ci_cd_stages(self):
        merged = u.merge_pipelines([SIMPLE_CI], SIMPLE_CD, ["MyApp"])
        self.assertEqual(merged["stages"][0]["stage"], "Build")
        self.assertEqual(merged["stages"][1]["stage"], "Deploy")

    def test_multi_ci(self):
        ci2    = {"stages": [{"stage": "Build2", "jobs": []}]}
        merged = u.merge_pipelines([SIMPLE_CI, ci2], SIMPLE_CD, ["AppA", "AppB"])
        self.assertEqual(len(merged["stages"]), 3)
        self.assertEqual(merged["stages"][-1]["stage"], "Deploy")

    def test_deploy_depends_on_builds(self):
        deploy = u.merge_pipelines([SIMPLE_CI], SIMPLE_CD, ["App"])["stages"][-1]
        self.assertIn("Build", deploy["dependsOn"])

    def test_no_ci(self):
        merged = u.merge_pipelines([], SIMPLE_CD, [])
        self.assertEqual(merged["stages"][0]["stage"], "Deploy")
        self.assertNotIn("dependsOn", merged["stages"][0])

    def test_cd_stage_pools_respected_no_top_level(self):
        # When CD stages already have pools (injected by enrich_classic_cd),
        # the top-level pool must NOT be written even if CI and CD pools match.
        ci = {"pool": {"name": "Default"}, **SIMPLE_CI}
        cd = {
            "stages": [{"stage": "Deploy", "pool": {"name": "Default"}, "jobs": []}]
        }
        merged = u.merge_pipelines([ci], cd, ["App"])
        self.assertNotIn("pool", merged)   # no top-level pool
        deploy = next(s for s in merged["stages"] if s["stage"] == "Deploy")
        self.assertEqual(deploy["pool"], {"name": "Default"})  # stage pool kept

    def test_cd_stage_pools_trigger_stage_level_when_diff(self):
        # CI uses PoolA; CD stage has PoolB injected by enrichment → stage level
        ci = {"pool": {"name": "PoolA"}, **SIMPLE_CI}
        cd = {
            "stages": [{"stage": "Deploy", "pool": {"name": "PoolB"}, "jobs": []}]
        }
        merged = u.merge_pipelines([ci], cd, ["App"])
        self.assertNotIn("pool", merged)   # no top-level pool
        build  = next(s for s in merged["stages"] if "Build"  in s["stage"])
        deploy = next(s for s in merged["stages"] if "Deploy" in s["stage"])
        self.assertEqual(build["pool"],  {"name": "PoolA"})
        self.assertEqual(deploy["pool"], {"name": "PoolB"})

    def test_yaml_ci_stage_level_pool_detected(self):
        # YAML CI defines pool at stage level (no top-level pool key)
        ci = {"stages": [{"stage": "Build", "pool": {"name": "StagePool"}, "jobs": []}]}
        cd = {"pool": {"name": "StagePool"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([ci], cd, ["App"])
        # All same pool → top level
        self.assertIn("pool", merged)
        self.assertEqual(merged["pool"], {"name": "StagePool"})

    def test_yaml_ci_job_level_pool_detected(self):
        # YAML CI defines pool only at job level
        ci = {"stages": [{"stage": "Build", "jobs": [
            {"job": "Compile", "pool": {"name": "JobPool"}, "steps": []}
        ]}]}
        cd = {"pool": {"name": "CDPool"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([ci], cd, ["App"])
        # Different pools → stage level
        self.assertNotIn("pool", merged)

    def test_jobs_with_same_pool_get_stage_level_injection(self):
        # All jobs in a stage share the same pool → inject at stage level
        stage = {
            "stage": "Build",
            "jobs": [
                {"job": "A", "steps": []},
                {"job": "B", "steps": []},
            ]
        }
        u._inject_pool_into_stage(stage, {"name": "SharedPool"})
        self.assertIn("pool", stage)
        self.assertEqual(stage["pool"], {"name": "SharedPool"})
        # Jobs should NOT have pool (it's at stage level)
        for job in stage["jobs"]:
            self.assertNotIn("pool", job)

    def test_mixed_jobs_get_job_level_injection(self):
        # Some jobs already have a pool, some don't → inject at job level only
        stage = {
            "stage": "Build",
            "jobs": [
                {"job": "A", "pool": {"name": "ExistingPool"}, "steps": []},
                {"job": "B", "steps": []},   # no pool — needs injection
            ]
        }
        u._inject_pool_into_stage(stage, {"name": "DefaultPool"})
        # Stage level must NOT have pool
        self.assertNotIn("pool", stage)
        # Job A keeps its own pool
        job_a = next(j for j in stage["jobs"] if j["job"] == "A")
        self.assertEqual(job_a["pool"], {"name": "ExistingPool"})
        # Job B gets the injected pool
        job_b = next(j for j in stage["jobs"] if j["job"] == "B")
        self.assertEqual(job_b["pool"], {"name": "DefaultPool"})

    def test_stage_level_pool_with_mixed_job_pools(self):
        # Pipeline-level pool should be moved to the stage when some jobs
        # share it and others use a different pool.
        stage = {
            "stage": "Build",
            "jobs": [
                {"job": "BuildJob", "pool": {"name": "Default"}, "steps": []},
                {"job": "TestJob", "pool": {"name": "Azure-pipeline"}, "steps": []},
                {"job": "TestJob3", "pool": {"name": "Default"}, "steps": []},
            ]
        }
        u._inject_pool_into_stage(stage, {"name": "Default"})
        self.assertEqual(stage["pool"], {"name": "Default"})
        self.assertNotIn("pool", next(j for j in stage["jobs"] if j["job"] == "BuildJob"))
        self.assertNotIn("pool", next(j for j in stage["jobs"] if j["job"] == "TestJob3"))
        self.assertEqual(next(j for j in stage["jobs"] if j["job"] == "TestJob")["pool"], {"name": "Azure-pipeline"})

    def test_collapse_duplicate_job_pools_to_stage(self):
        stage = {
            "stage": "Build",
            "jobs": [
                {"job": "BuildJob", "pool": {"name": "Default"}, "steps": []},
                {"job": "TestJob", "pool": {"name": "Default"}, "steps": []},
                {"job": "TestJob3", "pool": {"name": "Azure-pipeline"}, "steps": []},
            ]
        }
        u._collapse_duplicate_job_pools_to_stage(stage)
        self.assertEqual(stage["pool"], {"name": "Default"})
        self.assertNotIn("pool", next(j for j in stage["jobs"] if j["job"] == "BuildJob"))
        self.assertNotIn("pool", next(j for j in stage["jobs"] if j["job"] == "TestJob"))
        self.assertEqual(next(j for j in stage["jobs"] if j["job"] == "TestJob3")["pool"], {"name": "Azure-pipeline"})

    def test_stage_pool_cleans_redundant_job_pool_declarations(self):
        stage = {
            "stage": "Deploy",
            "pool": {"name": "Default"},
            "jobs": [
                {"job": "DeployJob", "pool": {"name": "Default"}, "steps": []},
                {"job": "OtherJob", "pool": {"name": "Azure-pipeline"}, "steps": []},
            ]
        }
        u._collapse_duplicate_job_pools_to_stage(stage)
        self.assertEqual(stage["pool"], {"name": "Default"})
        self.assertNotIn("pool", next(j for j in stage["jobs"] if j["job"] == "DeployJob"))
        self.assertEqual(next(j for j in stage["jobs"] if j["job"] == "OtherJob")["pool"], {"name": "Azure-pipeline"})

    def test_all_jobs_have_pool_nothing_injected(self):
        # All jobs already have their own pool → nothing should be added
        stage = {
            "stage": "Build",
            "jobs": [
                {"job": "A", "pool": {"name": "PoolA"}, "steps": []},
                {"job": "B", "pool": {"name": "PoolB"}, "steps": []},
            ]
        }
        u._inject_pool_into_stage(stage, {"name": "WouldOverwrite"})
        self.assertNotIn("pool", stage)
        job_a = next(j for j in stage["jobs"] if j["job"] == "A")
        job_b = next(j for j in stage["jobs"] if j["job"] == "B")
        self.assertEqual(job_a["pool"], {"name": "PoolA"})
        self.assertEqual(job_b["pool"], {"name": "PoolB"})

    def test_stage_with_existing_pool_never_touched(self):
        stage = {"stage": "Build", "pool": {"name": "AlreadySet"}, "jobs": [
            {"job": "A", "steps": []}
        ]}
        u._inject_pool_into_stage(stage, {"name": "WouldOverwrite"})
        self.assertEqual(stage["pool"], {"name": "AlreadySet"})
        self.assertNotIn("pool", stage["jobs"][0])

    def test_collect_effective_pool_top_level(self):
        p = {"pool": {"name": "TopPool"}, "stages": []}
        self.assertEqual(u._collect_pipeline_effective_pool(p), {"name": "TopPool"})

    def test_collect_effective_pool_stage_level(self):
        p = {"stages": [{"stage": "Build", "pool": {"name": "StagePool"}, "jobs": []}]}
        self.assertEqual(u._collect_pipeline_effective_pool(p), {"name": "StagePool"})

    def test_collect_effective_pool_job_level(self):
        p = {"stages": [{"stage": "Build", "jobs": [
            {"job": "Compile", "pool": {"name": "JobPool"}, "steps": []}
        ]}]}
        self.assertEqual(u._collect_pipeline_effective_pool(p), {"name": "JobPool"})

    def test_collect_effective_pool_none(self):
        self.assertIsNone(u._collect_pipeline_effective_pool({"stages": [{"stage": "B", "jobs": []}]}))

    def test_trigger_from_ci(self):
        ci     = {"trigger": {"branches": {"include": ["main"]}}, **SIMPLE_CI}
        merged = u.merge_pipelines([ci], SIMPLE_CD, ["App"])
        self.assertIn("main", merged["trigger"]["branches"]["include"])

    def test_trigger_none_when_ci_has_none(self):
        # A CI pipeline with no trigger key → merged output should set trigger: none
        ci_no_trigger = {"stages": [{"stage": "Build", "jobs": []}]}
        merged = u.merge_pipelines([ci_no_trigger], SIMPLE_CD, ["App"])
        self.assertEqual(merged.get("trigger"), "none")

    def test_multi_ci_triggers_merged(self):
        ci1 = {"trigger": {"branches": {"include": ["main"]}},    "stages": [{"stage": "B1", "jobs": []}]}
        ci2 = {"trigger": {"branches": {"include": ["release/*"]}}, "stages": [{"stage": "B2", "jobs": []}]}
        branches = u.merge_pipelines([ci1, ci2], SIMPLE_CD, ["A", "B"])["trigger"]["branches"]["include"]
        self.assertIn("main",      branches)
        self.assertIn("release/*", branches)

    def test_schedules_included(self):
        ci     = {"schedules": [{"cron": "0 0 * * *"}], **SIMPLE_CI}
        merged = u.merge_pipelines([ci], SIMPLE_CD, ["App"])
        self.assertEqual(len(merged.get("schedules", [])), 1)

    def test_multi_ci_schedules_merged(self):
        ci1 = {"schedules": [{"cron": "0 0 * * *"}], "stages": [{"stage": "B1", "jobs": []}]}
        ci2 = {"schedules": [{"cron": "0 6 * * 1"}], "stages": [{"stage": "B2", "jobs": []}]}
        self.assertEqual(len(u.merge_pipelines([ci1, ci2], SIMPLE_CD, ["A", "B"])["schedules"]), 2)

    def test_resources_included(self):
        ci     = {"resources": {"pipelines": [{"pipeline": "Up", "source": "Up"}]}, **SIMPLE_CI}
        merged = u.merge_pipelines([ci], SIMPLE_CD, ["App"])
        self.assertIn("pipelines", merged.get("resources", {}))

    def test_same_pool_goes_to_top_level(self):
        # When CI and CD share the same pool but CI declared it at pipeline level,
        # the pool should be materialised at stage level during migration.
        ci = {"pool": {"name": "Default"}, **SIMPLE_CI}
        cd = {"pool": {"name": "Default"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([ci], cd, ["App"])
        self.assertNotIn("pool", merged)
        build_stage  = next(s for s in merged["stages"] if "Build" in s["stage"])
        deploy_stage = next(s for s in merged["stages"] if "Deploy" in s["stage"])
        self.assertEqual(build_stage["pool"], {"name": "Default"})
        self.assertEqual(deploy_stage["pool"], {"name": "Default"})

    def test_diff_pool_goes_to_stage_level(self):
        # When CI and CD use different pools, pool must NOT be at top level
        # and each stage gets its own correct pool
        ci = {"pool": {"name": "CI-Pool"}, **SIMPLE_CI}
        cd = {"pool": {"name": "CD-Pool"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([ci], cd, ["App"])
        self.assertNotIn("pool", merged)
        build_stage  = next(s for s in merged["stages"] if "Build"  in s["stage"])
        deploy_stage = next(s for s in merged["stages"] if "Deploy" in s["stage"])
        self.assertEqual(build_stage["pool"],  {"name": "CI-Pool"})
        self.assertEqual(deploy_stage["pool"], {"name": "CD-Pool"})

    def test_multi_ci_all_same_pool_top_level(self):
        # Two CIs + CD all using same pool — pipeline-level CI pools should
        # be materialised at stage level during migration. The merged file
        # should not contain a global top-level pool; each stage carries
        # the shared pool explicitly.
        ci1 = {"pool": {"name": "Shared"}, "stages": [{"stage": "Build1", "jobs": []}]}
        ci2 = {"pool": {"name": "Shared"}, "stages": [{"stage": "Build2", "jobs": []}]}
        cd  = {"pool": {"name": "Shared"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([ci1, ci2], cd, ["A", "B"])
        self.assertNotIn("pool", merged)
        s = {st["stage"]: st for st in merged["stages"]}
        self.assertEqual(s["Build1"]["pool"], {"name": "Shared"})
        self.assertEqual(s["Build2"]["pool"], {"name": "Shared"})
        self.assertEqual(s["Deploy"]["pool"], {"name": "Shared"})

    def test_multi_ci_diff_pool_stage_level(self):
        # CI-1 uses PoolA, CI-2 uses PoolB, CD uses PoolC → all at stage level
        ci1 = {"pool": {"name": "PoolA"}, "stages": [{"stage": "Build1", "jobs": []}]}
        ci2 = {"pool": {"name": "PoolB"}, "stages": [{"stage": "Build2", "jobs": []}]}
        cd  = {"pool": {"name": "PoolC"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([ci1, ci2], cd, ["A", "B"])
        self.assertNotIn("pool", merged)
        s = {st["stage"]: st for st in merged["stages"]}
        self.assertEqual(s["Build1"]["pool"], {"name": "PoolA"})
        self.assertEqual(s["Build2"]["pool"], {"name": "PoolB"})
        self.assertEqual(s["Deploy"]["pool"], {"name": "PoolC"})

    def test_multi_ci_ci_same_cd_diff_stage_level(self):
        # CI-1 and CI-2 share a pool but CD uses different → stage level
        ci1 = {"pool": {"name": "BuildPool"}, "stages": [{"stage": "Build1", "jobs": []}]}
        ci2 = {"pool": {"name": "BuildPool"}, "stages": [{"stage": "Build2", "jobs": []}]}
        cd  = {"pool": {"name": "DeployPool"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([ci1, ci2], cd, ["A", "B"])
        self.assertNotIn("pool", merged)
        s = {st["stage"]: st for st in merged["stages"]}
        self.assertEqual(s["Build1"]["pool"], {"name": "BuildPool"})
        self.assertEqual(s["Build2"]["pool"], {"name": "BuildPool"})
        self.assertEqual(s["Deploy"]["pool"], {"name": "DeployPool"})

    def test_no_pool_defined(self):
        # No pipeline defines a pool → merged output has no pool anywhere
        merged = u.merge_pipelines([SIMPLE_CI], SIMPLE_CD, ["App"])
        self.assertNotIn("pool", merged)
        for stage in merged["stages"]:
            self.assertNotIn("pool", stage)

    def test_stage_own_pool_never_overwritten(self):
        # A stage that already declares its own pool must keep it regardless
        ci = {
            "pool": {"name": "CI-Pool"},
            "stages": [{"stage": "Build", "pool": {"name": "StageOwn"}, "jobs": []}]
        }
        cd = {"pool": {"name": "CD-Pool"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([ci], cd, ["App"])
        build = next(s for s in merged["stages"] if s["stage"] == "Build")
        self.assertEqual(build["pool"], {"name": "StageOwn"})

    def test_only_ci_defines_pool_top_level(self):
        # Only CI defines a pool, CD does not → top level (single pool)
        ci     = {"pool": {"name": "CI-Pool"}, **SIMPLE_CI}
        merged = u.merge_pipelines([ci], SIMPLE_CD, ["App"])
        # Pipeline-level CI pool should be moved to the stage level during migration
        self.assertNotIn("pool", merged)
        build_stage = next(s for s in merged["stages"] if "Build" in s["stage"])
        self.assertEqual(build_stage["pool"], {"name": "CI-Pool"})

    def test_only_cd_defines_pool_top_level(self):
        # Only CD defines a pool, CI does not → top level (single pool)
        cd     = {"pool": {"name": "CD-Pool"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([SIMPLE_CI], cd, ["App"])
        self.assertIn("pool", merged)
        self.assertEqual(merged["pool"], {"name": "CD-Pool"})

    def test_cd_variables_win_on_conflict(self):
        ci     = {"variables": {"env": "dev"}, **SIMPLE_CI}
        cd     = {"variables": {"env": "prod"}, "stages": [{"stage": "Deploy", "jobs": []}]}
        merged = u.merge_pipelines([ci], cd, ["App"])
        entry  = next(v for v in merged["variables"] if v.get("name") == "env")
        self.assertEqual(entry["value"], "prod")


# ─── Tests: Classic CD enrichment ────────────────────────────────────────────

class TestClassicScheduleToCron(unittest.TestCase):

    def test_all_days(self):
        # mask 127 = all 7 days → *
        cron = u._classic_schedule_to_cron({"startHours": 2, "startMinutes": 30, "daysToRelease": 127})
        self.assertEqual(cron, "30 2 * * *")

    def test_weekdays_only(self):
        # Mon=1 Tue=2 Wed=4 Thu=8 Fri=16 → mask 31
        cron = u._classic_schedule_to_cron({"startHours": 0, "startMinutes": 0, "daysToRelease": 31})
        self.assertIn("1,2,3,4,5", cron)

    def test_monday_only(self):
        cron = u._classic_schedule_to_cron({"startHours": 6, "startMinutes": 0, "daysToRelease": 1})
        self.assertEqual(cron, "0 6 * * 1")

    def test_sunday_only(self):
        # Sun=64 → cron day 0
        cron = u._classic_schedule_to_cron({"startHours": 12, "startMinutes": 0, "daysToRelease": 64})
        self.assertEqual(cron, "0 12 * * 0")

    def test_defaults_when_empty(self):
        cron = u._classic_schedule_to_cron({})
        self.assertRegex(cron, r"^\d+ \d+ \* \* \*$")


class TestTranslateClassicCdTriggers(unittest.TestCase):

    def test_artifact_trigger_produces_no_output(self):
        triggers = [{"triggerType": "artifactSource"}]
        result   = u._translate_classic_cd_triggers(triggers, "MyRelease")
        self.assertNotIn("trigger",   result)
        self.assertNotIn("schedules", result)
        self.assertNotIn("pr",        result)

    def test_schedule_trigger_translated(self):
        triggers = [{"triggerType": "schedule",
                     "schedule": {"startHours": 3, "startMinutes": 0, "daysToRelease": 127}}]
        result   = u._translate_classic_cd_triggers(triggers, "MyRelease")
        self.assertIn("schedules", result)
        self.assertEqual(len(result["schedules"]), 1)
        self.assertIn("3", result["schedules"][0]["cron"])

    def test_pr_trigger_translated(self):
        triggers = [{"triggerType": "pullRequest",
                     "artifactFilters": [{"sourceBranch": "refs/heads/main"}]}]
        result   = u._translate_classic_cd_triggers(triggers, "MyRelease")
        self.assertIn("pr", result)
        self.assertIn("main", result["pr"]["branches"]["include"])

    def test_pr_trigger_defaults_to_main_when_no_filters(self):
        triggers = [{"triggerType": "pullRequest", "artifactFilters": []}]
        result   = u._translate_classic_cd_triggers(triggers, "MyRelease")
        self.assertIn("main", result["pr"]["branches"]["include"])

    def test_multiple_trigger_types(self):
        triggers = [
            {"triggerType": "artifactSource"},
            {"triggerType": "schedule",
             "schedule": {"startHours": 1, "startMinutes": 0, "daysToRelease": 127}},
            {"triggerType": "pullRequest",
             "artifactFilters": [{"sourceBranch": "refs/heads/develop"}]},
        ]
        result = u._translate_classic_cd_triggers(triggers, "MyRelease")
        self.assertIn("schedules", result)
        self.assertIn("pr", result)
        self.assertNotIn("trigger", result)

    def test_empty_triggers(self):
        self.assertEqual(u._translate_classic_cd_triggers([], "MyRelease"), {})

    def test_unknown_trigger_type_ignored(self):
        triggers = [{"triggerType": "unknownFutureTrigger"}]
        result   = u._translate_classic_cd_triggers(triggers, "MyRelease")
        self.assertEqual(result, {})


class TestGetClassicCdEnvironmentPools(unittest.TestCase):

    def _make_release_def_with_envs(self, envs):
        return {"name": "Test", "environments": envs}

    def _env(self, name, pool_name=None, queue_id=None, phase_type="agentBasedDeployment"):
        agent_spec = {}
        if pool_name:
            agent_spec = {"identifier": pool_name}
        di = {"agentSpecification": agent_spec}
        if queue_id:
            di["queueId"] = queue_id
        return {
            "name": name,
            "deployPhases": [{"phaseType": phase_type, "deploymentInput": di}]
        }

    def test_pool_name_from_identifier(self):
        client = MagicMock()
        rd     = self._make_release_def_with_envs([self._env("Prod", pool_name="MyPool")])
        pools  = client.get_classic_cd_environment_pools(rd)
        # call the real method
        import unifier as u
        pools = u.AzureDevOpsClient.__dict__["get_classic_cd_environment_pools"](
            MagicMock(), rd
        )
        self.assertEqual(pools.get("Prod"), {"name": "MyPool"})

    def test_resolves_queue_id_via_api(self):
        import unifier as u
        client = MagicMock()
        client.resolve_queue_id.return_value = "ResolvedPool"
        rd     = self._make_release_def_with_envs([self._env("Dev", queue_id=42)])
        pools  = u.AzureDevOpsClient.__dict__["get_classic_cd_environment_pools"](
            client, rd
        )
        self.assertEqual(pools.get("Dev"), {"name": "ResolvedPool"})
        client.resolve_queue_id.assert_called_once_with(42)

    def test_fallback_placeholder_when_api_fails(self):
        import unifier as u
        client = MagicMock()
        client.resolve_queue_id.return_value = None   # API lookup failed
        rd     = self._make_release_def_with_envs([self._env("Dev", queue_id=99)])
        pools  = u.AzureDevOpsClient.__dict__["get_classic_cd_environment_pools"](
            client, rd
        )
        self.assertIn("Dev", pools)
        self.assertIn("99", str(pools["Dev"]))

    def test_non_agent_phase_ignored(self):
        import unifier as u
        rd    = self._make_release_def_with_envs([
            self._env("Prod", pool_name="MyPool", phase_type="machineGroupBasedDeployment")
        ])
        pools = u.AzureDevOpsClient.__dict__["get_classic_cd_environment_pools"](
            MagicMock(), rd
        )
        self.assertNotIn("Prod", pools)

    def test_no_environments(self):
        import unifier as u
        rd    = {"name": "Test"}
        pools = u.AzureDevOpsClient.__dict__["get_classic_cd_environment_pools"](
            MagicMock(), rd
        )
        self.assertEqual(pools, {})


class TestEnrichClassicCd(unittest.TestCase):

    def _client(self, triggers=None, env_pools=None):
        client = MagicMock()
        client.get_classic_cd_triggers.return_value            = triggers or []
        client.get_classic_cd_environment_pools.return_value   = env_pools or {}
        return client

    def test_schedule_injected_into_cd(self):
        cd = {"stages": [{"stage": "Deploy", "jobs": []}]}
        rd = {"name": "MyRelease"}
        triggers = [{"triggerType": "schedule",
                     "schedule": {"startHours": 2, "startMinutes": 0, "daysToRelease": 127}}]
        enriched = u.enrich_classic_cd(cd, rd, self._client(triggers=triggers))
        self.assertIn("schedules", enriched)
        self.assertEqual(len(enriched["schedules"]), 1)

    def test_pr_injected_into_cd(self):
        cd = {"stages": [{"stage": "Deploy", "jobs": []}]}
        rd = {"name": "MyRelease"}
        triggers = [{"triggerType": "pullRequest",
                     "artifactFilters": [{"sourceBranch": "refs/heads/main"}]}]
        enriched = u.enrich_classic_cd(cd, rd, self._client(triggers=triggers))
        self.assertIn("pr", enriched)

    def test_artifact_trigger_not_injected(self):
        cd = {"stages": [{"stage": "Deploy", "jobs": []}]}
        rd = {"name": "MyRelease"}
        triggers = [{"triggerType": "artifactSource"}]
        enriched = u.enrich_classic_cd(cd, rd, self._client(triggers=triggers))
        self.assertNotIn("trigger",   enriched)
        self.assertNotIn("schedules", enriched)
        self.assertNotIn("pr",        enriched)

    def test_pool_injected_into_matching_stage(self):
        cd = {"stages": [{"stage": "Prod", "jobs": []}]}
        rd = {"name": "MyRelease"}
        enriched = u.enrich_classic_cd(
            cd, rd,
            self._client(env_pools={"Prod": {"name": "DeployPool"}})
        )
        self.assertEqual(enriched["stages"][0]["pool"], {"name": "DeployPool"})

    def test_existing_stage_pool_not_overwritten(self):
        cd = {"stages": [{"stage": "Prod", "pool": {"name": "AlreadySet"}, "jobs": []}]}
        rd = {"name": "MyRelease"}
        enriched = u.enrich_classic_cd(
            cd, rd,
            self._client(env_pools={"Prod": {"name": "WouldOverwrite"}})
        )
        self.assertEqual(enriched["stages"][0]["pool"], {"name": "AlreadySet"})

    def test_fuzzy_stage_name_matching(self):
        # Stage name sanitised by Yamlizr may differ slightly from env name
        cd = {"stages": [{"stage": "Production Environment", "jobs": []}]}
        rd = {"name": "MyRelease"}
        enriched = u.enrich_classic_cd(
            cd, rd,
            self._client(env_pools={"Production-Environment": {"name": "ProdPool"}})
        )
        self.assertEqual(enriched["stages"][0]["pool"], {"name": "ProdPool"})

    def test_no_triggers_no_crash(self):
        cd = {"stages": [{"stage": "Deploy", "jobs": []}]}
        rd = {"name": "MyRelease"}
        enriched = u.enrich_classic_cd(cd, rd, self._client())
        self.assertNotIn("schedules", enriched)
        self.assertNotIn("pr",        enriched)

    def test_flat_cd_structure_pool_injected_at_top_level(self):
        # Yamlizr sometimes writes flat jobs: structure (no stages key).
        # Pool must be injected at top-level so merge_pipelines can see it.
        cd = {"jobs": [{"job": "Agent_job", "steps": []}]}
        rd = {"name": "MyRelease"}
        enriched = u.enrich_classic_cd(
            cd, rd,
            self._client(env_pools={"Stage 1": {"name": "HostedPool"}})
        )
        self.assertIn("pool", enriched)
        self.assertEqual(enriched["pool"], {"name": "HostedPool"})
        self.assertNotIn("stages", enriched)   # flat structure preserved

    def test_flat_cd_existing_pool_not_overwritten(self):
        # If flat CD already has a pool key, don't overwrite it
        cd = {"pool": {"name": "AlreadySet"}, "jobs": [{"job": "j", "steps": []}]}
        rd = {"name": "MyRelease"}
        enriched = u.enrich_classic_cd(
            cd, rd,
            self._client(env_pools={"Stage 1": {"name": "WouldOverwrite"}})
        )
        self.assertEqual(enriched["pool"], {"name": "AlreadySet"})

    def test_existing_schedule_not_duplicated(self):
        existing_cron = "0 2 * * *"
        cd = {"schedules": [{"cron": existing_cron}],
              "stages": [{"stage": "Deploy", "jobs": []}]}
        rd = {"name": "MyRelease"}
        triggers = [{"triggerType": "schedule",
                     "schedule": {"startHours": 2, "startMinutes": 0, "daysToRelease": 127}}]
        enriched = u.enrich_classic_cd(cd, rd, self._client(triggers=triggers))
        crons = [s["cron"] for s in enriched["schedules"]]
        self.assertEqual(crons.count(existing_cron), 1)


# ─── Scenario 1 ───────────────────────────────────────────────────────────────

class TestScenario1(unittest.TestCase):
    """
    handle_scenario_1 resolves the CD build definition ID internally via
    get_yaml_cd_build_id(), then fetches the YAML file path and content
    using that build def ID.

    Mock layout:
      client.get_yaml_cd_build_id()      → returns a build def ID (e.g. 99)
      client.get_yaml_pipeline_file_path(99) → CD yaml path
      client.get_yaml_pipeline_content(99)   → CD yaml content
      client.get_yaml_pipeline_file_path(10) → CI yaml path  (artifact def id)
      client.get_yaml_pipeline_content(10)   → CI yaml content
    """

    def _client(self, cd_content, ci_content=None, cd_path="cd.yml", ci_path="ci.yml",
                cd_build_id=99):
        client = MagicMock()
        client.get_yaml_cd_build_id.return_value = cd_build_id

        def file_path(def_id):
            return cd_path if def_id == cd_build_id else ci_path
        client.get_yaml_pipeline_file_path.side_effect = file_path

        def content(def_id):
            return cd_content if def_id == cd_build_id else ci_content
        client.get_yaml_pipeline_content.side_effect = content

        return client

    def test_no_artifacts(self):
        d      = tmp_dir()
        client = self._client(yaml.dump(SIMPLE_CD))
        u.handle_scenario_1(make_release_def(), [], d / "Unified", client)
        self.assertTrue((d / "Unified" / "MyRelease.yml").exists())

    def test_already_unified(self):
        d       = tmp_dir()
        unified = d / "Unified"; unified.mkdir()
        # CI path == CD path → already unified
        artifacts = [{"alias": "Drop", "definitionReference": {"definition": {"id": "10"}}}]
        client = self._client(yaml.dump(SIMPLE_CD), yaml.dump(SIMPLE_CI),
                              cd_path="pipeline.yml", ci_path="pipeline.yml")
        u.handle_scenario_1(make_release_def(), artifacts, unified, client)
        self.assertFalse((unified / "MyRelease.yml").exists())

    def test_needs_merge(self):
        d = tmp_dir()
        artifacts = [{"alias": "Drop", "definitionReference": {"definition": {"id": "10"}}}]
        # CI path differs from CD path → needs merge
        client = self._client(yaml.dump(SIMPLE_CD), yaml.dump(SIMPLE_CI),
                              cd_path="cd.yml", ci_path="ci.yml")
        u.handle_scenario_1(make_release_def(), artifacts, d / "Unified", client)
        data  = yaml.safe_load((d / "Unified" / "MyRelease.yml").read_text())
        names = [s["stage"] for s in data["stages"]]
        self.assertTrue(any("Build"  in n for n in names))
        self.assertTrue(any("Deploy" in n for n in names))

    def test_build_id_not_resolved_skips(self):
        d = tmp_dir()
        client = MagicMock()
        client.get_yaml_cd_build_id.return_value = None   # cannot resolve
        u.handle_scenario_1(make_release_def(), [], d / "Unified", client)
        self.assertFalse((d / "Unified" / "MyRelease.yml").exists())

    def test_cd_content_fetch_fails_skips(self):
        d      = tmp_dir()
        client = MagicMock()
        client.get_yaml_cd_build_id.return_value          = 99
        client.get_yaml_pipeline_file_path.return_value   = "cd.yml"
        client.get_yaml_pipeline_content.return_value     = None   # fetch fails
        u.handle_scenario_1(make_release_def(), [], d / "Unified", client)
        self.assertFalse((d / "Unified" / "MyRelease.yml").exists())


# ─── Scenario 2 ───────────────────────────────────────────────────────────────

class TestScenario2(unittest.TestCase):

    def _setup(self):
        d = tmp_dir()
        rel = d / "AzureDevOpsReleases"; rel.mkdir()
        (rel / "MyRelease-5.yml").write_text(yaml.dump(SIMPLE_CD))
        return d, rel

    def test_no_artifacts(self):
        d, rel = self._setup()
        client = MagicMock()
        client.get_classic_cd_triggers.return_value           = []
        client.get_classic_cd_environment_pools.return_value  = {}
        u.handle_scenario_2(make_release_def(has_environments=True), [], d / "Unified", rel, client)
        self.assertTrue((d / "Unified" / "MyRelease.yml").exists())

    def test_with_artifact(self):
        d, rel = self._setup()
        artifacts = [{"alias": "Drop", "definitionReference": {"definition": {"id": "20"}}}]
        client = MagicMock()
        client.get_classic_cd_triggers.return_value           = []
        client.get_classic_cd_environment_pools.return_value  = {}
        client.get_yaml_pipeline_content.return_value = yaml.dump(SIMPLE_CI)
        u.handle_scenario_2(make_release_def(has_environments=True), artifacts, d / "Unified", rel, client)
        data  = yaml.safe_load((d / "Unified" / "MyRelease.yml").read_text())
        names = [s["stage"] for s in data["stages"]]
        self.assertIn("Deploy", names)

    def test_missing_yamlizr_file(self):
        d = tmp_dir()
        rel = d / "AzureDevOpsReleases"; rel.mkdir()
        client = MagicMock()
        client.get_classic_cd_triggers.return_value           = []
        client.get_classic_cd_environment_pools.return_value  = {}
        u.handle_scenario_2(make_release_def(has_environments=True), [], d / "Unified", rel, client)
        self.assertFalse((d / "Unified" / "MyRelease.yml").exists())


# ─── Scenario 3 ───────────────────────────────────────────────────────────────

class TestScenario3(unittest.TestCase):

    def _setup(self):
        d = tmp_dir()
        rel = d / "AzureDevOpsReleases"; rel.mkdir()
        bld = d / "AzureDevOpsBuilds";   bld.mkdir()
        (rel / "MyRelease-5.yml").write_text(yaml.dump(SIMPLE_CD))
        return d, rel, bld

    def test_no_artifacts(self):
        d, rel, bld = self._setup()
        client = MagicMock()
        client.get_classic_cd_triggers.return_value           = []
        client.get_classic_cd_environment_pools.return_value  = {}
        u.handle_scenario_3(make_release_def(has_environments=True), [], d / "Unified", rel, bld, client)
        self.assertTrue((d / "Unified" / "MyRelease.yml").exists())

    def test_with_artifact(self):
        d, rel, bld = self._setup()
        (bld / "MyBuild-10.yml").write_text(yaml.dump(SIMPLE_CI))
        artifacts = [{"alias": "Drop", "definitionReference": {"definition": {"id": "10", "name": "MyBuild"}}}]
        client = MagicMock()
        client.get_classic_cd_triggers.return_value           = []
        client.get_classic_cd_environment_pools.return_value  = {}
        u.handle_scenario_3(make_release_def(has_environments=True), artifacts, d / "Unified", rel, bld, client)
        data  = yaml.safe_load((d / "Unified" / "MyRelease.yml").read_text())
        names = [s["stage"] for s in data["stages"]]
        self.assertIn("Deploy", names)

    def test_missing_ci_file(self):
        d, rel, bld = self._setup()
        artifacts = [{"alias": "Drop", "definitionReference": {"definition": {"id": "10", "name": "MissingPipeline"}}}]
        client = MagicMock()
        client.get_classic_cd_triggers.return_value           = []
        client.get_classic_cd_environment_pools.return_value  = {}
        u.handle_scenario_3(make_release_def(has_environments=True), artifacts, d / "Unified", rel, bld, client)
        self.assertFalse((d / "Unified" / "MyRelease.yml").exists())


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
