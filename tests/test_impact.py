"""变更影响分析单元测试。

覆盖需求的四条硬约束：

1. 间接影响不漏：用例 → 套件 → 定时计划 → 门禁 / 历史报告逐层传递；
2. 引用点分别呈现：同一用例被多套件、同套件内多次引用时按引用点各报一次；
3. 无关项不误报：环境/元数据变更只沿真实引用边传播；
4. 多次分析稳定一致：纯函数 + 确定排序，指纹相同。

另覆盖门禁判定与测试计划进度。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (EnvironmentManager, ImpactAnalyzer, QualityGateManager,
                    TestPlanManager, diff_case, diff_environment)
from storage import BuildStoreRegistry, StoreRegistry


ASSERT_STEP = {"action": "assert", "type": "equals", "actual": "${x}", "expected": 1}
ASSERT_STEP_2 = {"action": "assert", "type": "equals", "actual": "${x}", "expected": 2}


class ImpactFixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = StoreRegistry(os.path.join(self.tmp.name, "store"))
        self.builds = BuildStoreRegistry(os.path.join(self.tmp.name, "builds"))
        self.env_mgr = EnvironmentManager(self.registry, self.tmp.name)
        self.gates = QualityGateManager(self.registry, self.builds)
        self.plans = TestPlanManager(self.registry, self.builds)
        self.impact = ImpactAnalyzer(self.registry, self.builds)

        self.pid = self.registry.store("projects").insert({"name": "P"})
        self.e1 = self.env_mgr.create(self.pid, {"name": "dev", "config": {
            "base_url": "http://a", "latency_ms": 0, "fail_rate": 0.0}})
        self.e2 = self.env_mgr.create(self.pid, {"name": "stg", "config": {
            "base_url": "http://b", "latency_ms": 0, "fail_rate": 0.1}})

        def case(name, priority="P1"):
            return self.registry.store("cases").insert({
                "project_id": self.pid, "name": name, "priority": priority,
                "tags": [], "timeout": 60, "enabled": True, "steps": [dict(ASSERT_STEP)]})

        self.c1, self.c2, self.c3, self.c4 = (case("c1", "P0"), case("c2"),
                                              case("c3"), case("c4"))
        # S1（dev）引用 c1,c2；S2（stg 默认）引用 c1,c3,c1 —— c1 共 3 个引用点
        self.s1 = self.registry.store("suites").insert({
            "project_id": self.pid, "name": "S1", "env_id": self.e1["id"],
            "case_ids": [self.c1, self.c2]})
        self.s2 = self.registry.store("suites").insert({
            "project_id": self.pid, "name": "S2", "env_id": self.e2["id"],
            "case_ids": [self.c1, self.c3, self.c1]})

        # 定时1：S1 继承默认环境；定时2：S2 但显式指定 dev
        self.sch1 = self.registry.store("schedules").insert({
            "project_id": self.pid, "name": "定时1", "cron": "* * * * *",
            "suite_id": self.s1, "env_id": None, "enabled": True})
        self.sch2 = self.registry.store("schedules").insert({
            "project_id": self.pid, "name": "定时2", "cron": "* * * * *",
            "suite_id": self.s2, "env_id": self.e1["id"], "enabled": False})

        self.g1 = self.registry.store("quality_gates").insert({
            "project_id": self.pid, "name": "门禁S1", "scope": "suite",
            "suite_id": self.s1, "conditions": {"min_pass_rate": 90.0, "require_p0": True},
            "enabled": True})
        self.g2 = self.registry.store("quality_gates").insert({
            "project_id": self.pid, "name": "门禁定时1", "scope": "schedule",
            "schedule_id": self.sch1, "conditions": {"max_failed": 0},
            "enabled": True})

        self.plan = self.registry.store("test_plans").insert({
            "project_id": self.pid, "name": "验收计划",
            "case_ids": [self.c1, self.c4]})

    def add_build(self, build_id, suite_id, env_id, results, fired_schedule=None):
        store = self.builds.for_project(self.pid)
        store.create(build_id, suite_id=suite_id, env_id=env_id)
        store.set_total(build_id, len(results))
        for i, (cid, cname, pri, status) in enumerate(results):
            store.record_result(build_id, {"case_id": cid, "case_name": cname,
                                           "group": "g", "priority": pri,
                                           "status": status, "duration": 0.1,
                                           "logs": []})
        store.finish(build_id, "failed" if any(r[3] != "passed" for r in results)
                     else "passed")
        if fired_schedule:
            self.registry.store("schedule_runs").insert({
                "schedule_id": fired_schedule, "project_id": self.pid,
                "build_id": build_id, "fired_at": time.time(), "status": "submitted"})

    def cleanup(self):
        self.tmp.cleanup()


class TestDiff(unittest.TestCase):
    def test_assertion_change_is_semantic(self):
        case = {"steps": [dict(ASSERT_STEP)], "timeout": 60, "enabled": True}
        d = diff_case(case, {"steps": [dict(ASSERT_STEP_2)]})
        self.assertTrue(d["semantic"])
        self.assertIn("assertion", d["change_types"])

    def test_step_rename_only(self):
        before = {"steps": [{"action": "request", "name": "A"}]}
        d = diff_case(before, {"steps": [{"action": "request", "name": "B"}]})
        # 非断言动作内容变化也算语义变更（覆盖场景不同）
        self.assertTrue(d["semantic"])

    def test_metadata_not_semantic(self):
        d = diff_case({"name": "a", "description": ""}, {"name": "b"})
        self.assertFalse(d["semantic"])
        self.assertEqual(d["change_types"], ["name"])

    def test_enable_disable(self):
        d = diff_case({"enabled": True}, {"enabled": False})
        self.assertIn("enabled", d["change_types"])
        self.assertTrue(d["semantic"])

    def test_env_variable_vs_rename(self):
        self.assertTrue(diff_environment({"variables": {"A": "1"}},
                                         {"variables": {"A": "2"}})["semantic"])
        self.assertFalse(diff_environment({"name": "x"}, {"name": "y"})["semantic"])
        self.assertIn("runtime_config",
                      diff_environment({"config": {"fail_rate": 0.0}},
                                       {"config": {"fail_rate": 0.5}})["change_types"])


class TestCaseImpact(unittest.TestCase):
    def setUp(self):
        self.fx = ImpactFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_reference_points_reported_separately(self):
        """c1 被 S1 引用 1 次、S2 引用 2 次 -> 套件影响条目共 3 个引用点。"""
        r = self.fx.impact.analyze_case(self.fx.c1, {"steps": [dict(ASSERT_STEP_2)]})
        refs = [(a["id"], a["reference_index"]) for a in r["affected"]["suites"]]
        self.assertEqual(sorted(refs), sorted([
            (self.fx.s1, 0), (self.fx.s2, 0), (self.fx.s2, 2)]))

    def test_transitive_propagation(self):
        """断言改动要层层传到 套件->计划->门禁，且报告可比性断点不漏。"""
        r = self.fx.impact.analyze_case(self.fx.c1, {"steps": [dict(ASSERT_STEP_2)]})
        kinds = [[n["kind"] for n in tr] for tr in r["traces"]]
        self.assertIn(["case", "suite", "schedule", "quality_gate"], kinds)
        self.assertIn(["case", "suite", "quality_gate"], kinds)
        self.assertIn(["case", "test_plan", "plan_progress"], kinds)
        # 两个套件各产生一个报告可比性断点
        self.assertIn(["case", "suite", "report"], kinds)
        self.assertEqual(r["summary"]["report_breaks"], 2)
        # 汇总里的门禁/计划数量
        self.assertEqual(r["summary"]["quality_gates"], 2)
        self.assertEqual(r["summary"]["test_plans"], 1)
        self.assertEqual(r["summary"]["schedules"], 2)

    def test_each_trace_keeps_full_path(self):
        r = self.fx.impact.analyze_case(self.fx.c1, {"steps": [dict(ASSERT_STEP_2)]})
        gate_traces = [tr for tr in r["traces"]
                       if tr[-1]["kind"] == "quality_gate"]
        # 两条门禁路径都必须带着完整链路（case/suite 起点不能被去重掉）
        for tr in gate_traces:
            self.assertEqual(tr[0]["kind"], "case")
            self.assertIn("suite", [n["kind"] for n in tr])

    def test_stable_and_idempotent(self):
        patch = {"steps": [dict(ASSERT_STEP_2)]}
        r1 = self.fx.impact.analyze_case(self.fx.c1, patch)
        r2 = self.fx.impact.analyze_case(self.fx.c1, patch)
        self.assertEqual(r1["fingerprint"], r2["fingerprint"])
        self.assertEqual(r1["traces"], r2["traces"])

    def test_unreferenced_case_no_false_positive(self):
        r = self.fx.impact.analyze_case(self.fx.c4, {"timeout": 10})
        # c4 只在测试计划里，不在任何套件/计划执行链
        kinds = [n["kind"] for tr in r["traces"] for n in tr]
        self.assertNotIn("schedule", kinds)
        self.assertNotIn("quality_gate", kinds)
        self.assertIn("plan_progress", kinds)

    def test_metadata_change_low_risk_and_comparable(self):
        r = self.fx.impact.analyze_case(self.fx.c1, {"name": "新名字"})
        self.assertEqual(r["risk_level"], "low")
        # 元数据变更路径中的历史报告仍标记为可比
        report_nodes = [n for tr in r["traces"] for n in tr
                        if n["kind"] == "report"]
        self.assertTrue(report_nodes)
        self.assertTrue(all(n.get("comparable") for n in report_nodes))

    def test_timeout_and_disable_risks(self):
        r = self.fx.impact.analyze_case(self.fx.c1, {"timeout": 5})
        self.assertIn("timeout", r["change"]["change_types"])
        r2 = self.fx.impact.analyze_case(self.fx.c1, {"enabled": False})
        self.assertTrue(any("禁用" in x for x in r2["change"]["risks"]))


class TestEnvironmentImpact(unittest.TestCase):
    def setUp(self):
        self.fx = ImpactFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_env_propagates_through_default_and_explicit_binding(self):
        """dev 改动：经 S1 默认绑定 + 定时2 显式指定两条边传播。"""
        r = self.fx.impact.analyze_environment(
            self.fx.e1["id"], {"variables": {"BASE_URL": "http://new"}})
        suite_ids = {a["id"] for a in r["affected"]["suites"]}
        self.assertEqual(suite_ids, {self.fx.s1, self.fx.s2})
        schedule_ids = {a["id"] for a in r["affected"]["schedules"]}
        self.assertEqual(schedule_ids, {self.fx.sch1, self.fx.sch2})
        gate_ids = {a["id"] for a in r["affected"]["quality_gates"]}
        self.assertEqual(gate_ids, {self.fx.g1, self.fx.g2})

    def test_env_change_no_false_positive_to_other_suite(self):
        """改 staging：只影响 S2；S1 与定时1 不使用该环境，不得误报。"""
        r = self.fx.impact.analyze_environment(
            self.fx.e2["id"], {"variables": {"REGION": "prod"}})
        ids = [n.get("id") for tr in r["traces"] for n in tr]
        self.assertNotIn(self.fx.s1, ids)
        self.assertNotIn(self.fx.sch1, ids)
        self.assertIn(self.fx.s2, ids)

    def test_env_report_break_scoped_to_env_builds(self):
        # S1 在 dev 上有一场历史构建
        self.fx.add_build("b1", self.fx.s1, self.fx.e1["id"],
                          [(self.fx.c1, "c1", "P0", "passed"),
                           (self.fx.c2, "c2", "P1", "failed")],
                          fired_schedule=self.fx.sch1)
        r = self.fx.impact.analyze_environment(
            self.fx.e1["id"], {"config": {"base_url": "http://a",
                                          "latency_ms": 0, "fail_rate": 0.5}})
        breaks = [n for tr in r["traces"] for n in tr
                  if n["kind"] == "report" and n.get("comparable") is False]
        self.assertTrue(breaks)
        self.assertEqual(breaks[0]["builds"][0]["build_id"], "b1")

    def test_env_stable(self):
        patch = {"variables": {"BASE_URL": "http://new"}}
        r1 = self.fx.impact.analyze_environment(self.fx.e1["id"], patch)
        r2 = self.fx.impact.analyze_environment(self.fx.e1["id"], patch)
        self.assertEqual(r1["fingerprint"], r2["fingerprint"])


class TestGatesAndPlans(unittest.TestCase):
    def setUp(self):
        self.fx = ImpactFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_gate_evaluation(self):
        self.fx.add_build("b1", self.fx.s1, self.fx.e1["id"],
                          [(self.fx.c1, "c1", "P0", "passed"),
                           (self.fx.c2, "c2", "P1", "failed")],
                          fired_schedule=self.fx.sch1)
        ev = self.fx.gates.evaluate(self.fx.g1)
        self.assertEqual(ev["status"], "failed")  # 通过率 50% < 90%
        names = {c["condition"]: c for c in ev["checks"]}
        self.assertTrue(names["min_pass_rate"]["actual"] == 50.0)
        self.assertTrue(names["require_p0"]["ok"])
        # 计划门禁从 schedule_runs 找最近构建：50% 通过且 max_failed=0
        ev2 = self.fx.gates.evaluate(self.fx.g2)
        self.assertEqual(ev2["status"], "failed")
        self.assertEqual(ev2["build_id"], "b1")

    def test_gate_passes_when_all_green(self):
        self.fx.add_build("b2", self.fx.s1, self.fx.e1["id"],
                          [(self.fx.c1, "c1", "P0", "passed"),
                           (self.fx.c2, "c2", "P1", "passed")],
                          fired_schedule=self.fx.sch1)
        self.assertEqual(self.fx.gates.evaluate(self.fx.g1)["status"], "passed")

    def test_plan_progress(self):
        self.fx.add_build("b1", self.fx.s1, self.fx.e1["id"],
                          [(self.fx.c1, "c1", "P0", "passed"),
                           (self.fx.c2, "c2", "P1", "failed")])
        prog = self.fx.plans.progress(self.fx.plan)
        self.assertEqual(prog["total"], 2)
        self.assertEqual(prog["passed"], 1)      # c1 最近通过
        self.assertEqual(prog["untested"], 1)    # c4 无记录
        self.assertEqual(prog["progress_percent"], 50.0)

    def test_plan_uses_latest_result(self):
        # 旧构建失败、新构建通过 -> 取最近一场
        self.fx.add_build("bold", self.fx.s1, self.fx.e1["id"],
                          [(self.fx.c1, "c1", "P0", "failed"),
                           (self.fx.c2, "c2", "P1", "passed")])
        time.sleep(0.01)
        self.fx.add_build("bnew", self.fx.s1, self.fx.e1["id"],
                          [(self.fx.c1, "c1", "P0", "passed"),
                           (self.fx.c2, "c2", "P1", "passed")])
        prog = self.fx.plans.progress(self.fx.plan)
        c1_state = next(c for c in prog["cases"] if c["case_id"] == self.fx.c1)
        self.assertEqual(c1_state["state"], "passed")
        self.assertEqual(c1_state["last_build_id"], "bnew")


if __name__ == "__main__":
    unittest.main()
