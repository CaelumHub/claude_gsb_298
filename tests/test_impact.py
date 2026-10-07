"""变更影响分析测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.impact import ImpactAnalyzer, detect_case_changes, detect_environment_changes
from storage import BuildStoreRegistry, StoreRegistry


class ImpactFixture:
    def __init__(self, root: str):
        self.registry = StoreRegistry(os.path.join(root, "store"), shard_size=20)
        self.builds = BuildStoreRegistry(os.path.join(root, "builds"))
        self.project_id = "proj_1"
        self.registry.store("projects").insert({"id": self.project_id, "name": "P"})
        self.env1 = self.registry.store("environments").insert({"id": "env_1", "project_id": self.project_id, "name": "dev"})
        self.env2 = self.registry.store("environments").insert({"id": "env_2", "project_id": self.project_id, "name": "staging"})
        self.case1 = self.registry.store("cases").insert({
            "id": "case_1", "project_id": self.project_id, "name": "核心用例",
            "priority": "P0", "timeout": 10, "enabled": True,
            "steps": [{"action": "assert", "type": "equals", "actual": "x", "expected": 1}],
        })
        self.case2 = self.registry.store("cases").insert({
            "id": "case_2", "project_id": self.project_id, "name": "无关用例",
            "priority": "P2", "timeout": 10, "enabled": True, "steps": [],
        })
        # case_1 在套件 1 中被引用两次，在套件 2 中再引用一次。
        self.suite1 = self.registry.store("suites").insert({
            "id": "suite_1", "project_id": self.project_id, "name": "冒烟",
            "env_id": self.env1, "case_ids": [self.case1, self.case2, self.case1],
        })
        self.suite2 = self.registry.store("suites").insert({
            "id": "suite_2", "project_id": self.project_id, "name": "回归",
            "env_id": self.env2, "case_ids": [self.case1],
        })
        self.registry.store("schedules").insert({
            "id": "sch_active_1", "project_id": self.project_id, "name": "启用计划1",
            "cron": "* * * * *", "suite_id": self.suite1, "env_id": self.env1,
            "enabled": True,
        })
        self.registry.store("schedules").insert({
            "id": "sch_disabled", "project_id": self.project_id, "name": "禁用计划",
            "cron": "* * * * *", "suite_id": self.suite1, "env_id": self.env1,
            "enabled": False,
        })
        self.registry.store("schedules").insert({
            "id": "sch_active_2", "project_id": self.project_id, "name": "启用计划2",
            "cron": "* * * * *", "suite_id": self.suite2, "env_id": self.env2,
            "enabled": True,
        })
        self.registry.store("release_gates").insert({
            "id": "gate_1", "project_id": self.project_id, "name": "dev 门禁",
            "suite_ids": [self.suite1], "env_ids": [self.env1], "enabled": True,
            "rules": {"min_pass_rate": 80, "min_coverage": 60, "require_all_p0": True},
        })
        self.registry.store("release_gates").insert({
            "id": "gate_cross", "project_id": self.project_id, "name": "仅 dev 跨套件门禁",
            "suite_ids": [self.suite1, self.suite2], "env_ids": [self.env1],
            "enabled": True, "rules": {"min_pass_rate": 100},
        })
        self.registry.store("test_plans").insert({
            "id": "plan_1", "project_id": self.project_id, "name": "发布计划",
            "suite_ids": [self.suite1, self.suite2],
            "env_ids": [self.env1, self.env2], "enabled": True,
        })

        store = self.builds.for_project(self.project_id)
        store.create("build_1", suite_id=self.suite1, env_id=self.env1, name="历史1",
                     trigger="schedule")
        store.set_total("build_1", 3)
        for i in range(3):
            store.record_result("build_1", {
                "case_id": self.case1 if i != 1 else self.case2,
                "case_name": "核心用例" if i != 1 else "无关用例",
                "status": "passed", "duration": 0.1, "logs": [],
                "group": "g", "priority": "P0" if i != 1 else "P2",
            })
        store.finish("build_1", "passed")
        store.write_coverage("build_1", {"percent": 88.0})

        store.create("build_2", suite_id=self.suite2, env_id=self.env2, name="历史2",
                     trigger="schedule")
        store.set_total("build_2", 1)
        store.record_result("build_2", {
            "case_id": self.case1, "case_name": "核心用例", "status": "failed",
            "duration": 0.2, "logs": [], "group": "g", "priority": "P0",
        })
        store.finish("build_2", "failed")
        self.analyzer = ImpactAnalyzer(self.registry, self.builds)


class TestChangeDetection(unittest.TestCase):
    def test_case_assertion_and_timeout(self):
        before = {"steps": [{"action": "assert", "type": "equals", "actual": "a", "expected": 1}], "timeout": 10}
        after = {"steps": [{"action": "assert", "type": "equals", "actual": "a", "expected": 2}], "timeout": 20}
        changes = detect_case_changes(before, after)
        kinds = {c["kind"] for c in changes}
        self.assertIn("assertion", kinds)
        self.assertIn("timeout", kinds)

    def test_metadata_only(self):
        before = {"name": "a", "variables": {"X": "1"}, "config": {"latency_ms": 1}}
        after = {"name": "b", "variables": {"X": "1"}, "config": {"latency_ms": 1}}
        self.assertEqual([c["kind"] for c in detect_case_changes(before, after)], ["metadata"])
        env_changes = detect_environment_changes(
            {"name": "a", "variables": {"X": "1"}, "config": {"latency_ms": 1}},
            {"name": "b", "variables": {"X": "1"}, "config": {"latency_ms": 1}},
        )
        self.assertEqual([c["kind"] for c in env_changes], ["metadata"])

    def test_environment_changes_classified(self):
        before = {"variables": {"A": "1"}, "dependencies": [{"name": "requests", "constraint": ">=2"}],
                  "config": {"latency_ms": 1}}
        after = {"variables": {"A": "2"}, "dependencies": [{"name": "requests", "constraint": ">=3"}],
                 "config": {"latency_ms": 2}}
        kinds = {c["kind"] for c in detect_environment_changes(before, after)}
        self.assertEqual(kinds, {"runtime_variable", "dependency", "runtime_config"})


class TestImpactGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = ImpactFixture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_case_impact_duplicate_references_and_indirect_downstream(self):
        case = self.fx.registry.store("cases").get(self.fx.case1)
        analysis = self.fx.analyzer.analyze("case", {"steps": [
            {"action": "assert", "type": "equals", "actual": "x", "expected": 2},
        ]}, baseline=case)

        refs = analysis["suite_references"]
        self.assertEqual(len(refs), 3)
        self.assertEqual(analysis["impacted"]["suite_reference_count"], 3)
        self.assertEqual(analysis["impacted"]["suite_count"], 2)
        ref_ids = [r["id"] for r in refs]
        self.assertEqual(len(ref_ids), len(set(ref_ids)))
        self.assertIn("suite_1#case-ref-1", ref_ids)
        self.assertIn("suite_1#case-ref-3", ref_ids)
        self.assertIn("suite_2#case-ref-1", ref_ids)

        schedules = {s["id"]: s for s in analysis["schedules"]}
        self.assertEqual(analysis["impacted"]["schedule_count"], 2)
        self.assertIn("sch_disabled", schedules)
        self.assertFalse(schedules["sch_disabled"]["active"])

        self.assertEqual(analysis["impacted"]["release_gate_count"], 2)
        self.assertEqual({g["id"] for g in analysis["release_gates"]},
                         {"gate_1", "gate_cross"})
        self.assertEqual(analysis["impacted"]["test_plan_count"], 1)
        self.assertEqual(analysis["historical_reports"][0]["build_id"], "build_2")
        self.assertEqual(analysis["risk_level"], "high")
        coverage_baselines = analysis["coverage_statistics"]["historical_builds"]
        self.assertEqual({b["build_id"] for b in coverage_baselines}, {"build_1"})

        # 每个引用点都要独立传递到计划、门禁和计划；不能因实体相同而合并。
        paths = analysis["paths"]
        self.assertTrue(any("suite_1#case-ref-1" in p["id"] and "gate_1" in p["id"] for p in paths))
        self.assertTrue(any("suite_1#case-ref-3" in p["id"] and "gate_1" in p["id"] for p in paths))
        self.assertTrue(any("suite_2#case-ref-1" in p["id"] and "plan_1" in p["id"] for p in paths))
        self.assertTrue(any("gate_cross" in p["id"] for p in paths))

    def test_repeated_analysis_is_stable(self):
        case = self.fx.registry.store("cases").get(self.fx.case1)
        patch = {"enabled": False, "timeout": 30}
        first = self.fx.analyzer.analyze("case", patch, baseline=case)
        second = self.fx.analyzer.analyze("case", patch, baseline=case)
        self.assertEqual(first, second)

    def test_unrelated_case_has_no_impact(self):
        unrelated_record = {
            "id": "case_unrelated", "project_id": self.fx.project_id, "name": "无人引用",
            "priority": "P3", "timeout": 1, "enabled": True,
            "steps": [{"action": "assert", "type": "equals", "actual": "y", "expected": 8}],
        }
        self.fx.registry.store("cases").insert(unrelated_record)
        analysis = self.fx.analyzer.analyze("case", {"name": "无人引用-改名"},
                                           baseline=unrelated_record)
        self.assertEqual(analysis["suite_references"], [])
        self.assertEqual(analysis["schedules"], [])
        self.assertEqual(analysis["release_gates"], [])
        self.assertEqual(analysis["test_plans"], [])
        self.assertEqual(analysis["historical_reports"], [])
        self.assertEqual(analysis["coverage_statistics"]["comparability"], "not_affected")
        self.assertFalse(analysis["behavior_changed"])

    def test_referenced_metadata_change_does_not_report_execution_risk(self):
        case = self.fx.registry.store("cases").get(self.fx.case1)
        analysis = self.fx.analyzer.analyze("case", {"name": "核心用例改名"}, baseline=case)
        self.assertTrue(analysis["suite_references"])
        self.assertEqual(analysis["schedules"], [])
        self.assertEqual(analysis["release_gates"], [])
        self.assertEqual(analysis["test_plans"], [])
        self.assertEqual(analysis["historical_reports"], [])
        self.assertEqual(analysis["risk_level"], "low")

    def test_environment_effective_binding_avoids_false_positive(self):
        env2 = self.fx.registry.store("environments").get(self.fx.env2)
        analysis = self.fx.analyzer.analyze("environment", {
            "variables": {"NEW": "changed"},
        }, baseline=env2)

        schedule_ids = {s["id"] for s in analysis["schedules"]}
        self.assertEqual(schedule_ids, {"sch_active_2"})
        gate_ids = {g["id"] for g in analysis["release_gates"]}
        self.assertEqual(gate_ids, set())
        self.assertEqual({p["id"] for p in analysis["test_plans"]}, {"plan_1"})
        self.assertEqual({b["build_id"] for b in analysis["historical_reports"]}, {"build_2"})
        self.assertTrue(any(r["suite_id"] == "suite_2" for r in analysis["suite_references"]))
        self.assertFalse(any(r["suite_id"] == "suite_1" for r in analysis["suite_references"]))

    def test_environment_dev_includes_suite_default_and_cross_scope_gate(self):
        env1 = self.fx.registry.store("environments").get(self.fx.env1)
        analysis = self.fx.analyzer.analyze("environment", {
            "config": {"latency_ms": 99},
        }, baseline=env1)
        self.assertEqual({s["id"] for s in analysis["schedules"]},
                         {"sch_active_1", "sch_disabled"})
        self.assertEqual({g["id"] for g in analysis["release_gates"]},
                         {"gate_1", "gate_cross"})
        self.assertEqual({b["build_id"] for b in analysis["historical_reports"]}, {"build_1"})


if __name__ == "__main__":
    unittest.main()
