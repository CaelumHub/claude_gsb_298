"""变更影响分析（Change Impact Analysis）。

当有人修改一条用例（步骤 / 断言 / 超时 / 禁用）或一个环境（变量 / 依赖 /
运行参数）时，沿引用链

    用例 ──引用点──▶ 套件 ──▶ 定时计划 ──▶ 发布门禁
      │                │           └────▶ 历史报告/覆盖率（可比性）
      └──────────────▶ 测试计划（进度）
    环境 ──绑定/默认──▶ 套件 ──▶ 定时计划 ──▶ 发布门禁 / 历史报告

层层传递，给出**每一条完整传播路径**、影响范围汇总与风险提示。

设计上的四条硬约束（与团队需求逐条对应）：

1. **间接影响不漏**：不做"去重后的实体集合"，而是展开成路径（trace），
   用例 → 套件 → 计划 → 门禁的每一跳都在路径里；
2. **引用点分别呈现**：同一条用例被 N 个套件引用，就有 N 条独立路径，
   每个套件引用点（含它在用例列表中的位置）各报一次；
3. **无关项不误报**：只沿真实引用边传播；用例改名/改描述等元数据、环境
   改名/改描述不改变执行语义，标记为 ``semantic=False`` 的低风险变更，
   只提示引用它的组织对象，不扩散到门禁结论/报告可比性；
4. **多次分析稳定一致**：分析是纯函数（只读快照、不写任何状态），路径
   全部按确定键排序；同一改动重复分析得到字节级一致的 JSON，并据此生成
   稳定指纹（fingerprint）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

# 用例字段中会改变执行语义的部分
_CASE_SEMANTIC_FIELDS = ("steps", "timeout", "enabled")
# 环境字段中会改变执行语义的部分
_ENV_SEMANTIC_FIELDS = ("variables", "dependencies", "config",
                        "python_version", "base_image")

_FAILED_STATUSES = ("failed", "error", "timeout")


# ---------------------------------------------------------------------------
# 变更识别（diff）
# ---------------------------------------------------------------------------

def _to_jsonable(value: Any) -> Any:
    """把任意值规整成可 JSON 序列化、可稳定比较的形态。"""
    if isinstance(value, dict):
        return {str(k): _to_jsonable(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _stable(value: Any) -> str:
    return json.dumps(_to_jsonable(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def diff_case(before: dict, patch_or_after: dict) -> dict:
    """识别用例改动了哪些字段、属于什么类型、是否影响执行语义。

    返回::

        {"changed_fields": [...], "change_types": [...], "semantic": bool,
         "details": {field: {"before": ..., "after": ...}}, "risks": [...]}
    """
    after = dict(before)
    after.update(patch_or_after)

    changed_fields: list[str] = []
    details: dict[str, dict] = {}
    for field in ("name", "description", "priority", "tags",
                  "timeout", "enabled", "steps"):
        if field in patch_or_after and _stable(before.get(field)) != _stable(after.get(field)):
            changed_fields.append(field)
            details[field] = {"before": before.get(field), "after": after.get(field)}

    change_types: list[str] = []
    risks: list[str] = []
    semantic = False

    if "steps" in changed_fields:
        old_steps = before.get("steps") or []
        new_steps = after.get("steps") or []
        old_asserts = [s for s in old_steps if s.get("action") == "assert"]
        new_asserts = [s for s in new_steps if s.get("action") == "assert"]
        if _stable(old_asserts) != _stable(new_asserts):
            change_types.append("assertion")
            risks.append("断言发生变化：通过/失败判定口径改变，该用例的历史结果"
                         "与后续结果不可直接比较，依赖它的门禁结论可能翻转。")
        if _stable([s for s in old_steps if s.get("action") != "assert"]) != \
           _stable([s for s in new_steps if s.get("action") != "assert"]) or \
           len(old_steps) != len(new_steps):
            change_types.append("step")
            risks.append("执行步骤发生变化：覆盖的接口/场景与历史执行不一致，"
                         "历史报告的通过率、覆盖率对比需以本次改动为分界。")
        if "step" not in change_types and "assertion" not in change_types:
            # 步骤内容变了但既非断言也非动作（如 name 调整）
            change_types.append("step")
        semantic = True

    if "timeout" in changed_fields:
        change_types.append("timeout")
        old_t, new_t = before.get("timeout", 60), after.get("timeout", 60)
        risks.append(
            f"超时 {old_t}s → {new_t}s：超时阈值{'放宽，原 timeout 结果可能转为通过' if (new_t or 0) > (old_t or 0) else '收紧，可能新增 timeout 失败'}，"
            "会改变构建失败数与门禁判定。")
        semantic = True

    if "enabled" in changed_fields:
        change_types.append("enabled")
        if after.get("enabled"):
            risks.append("用例由禁用改为启用：套件实际执行用例数增加，"
                         "通过率分母与覆盖率口径变化。")
        else:
            risks.append("用例被禁用：它将在所有引用套件中被跳过，通过率分母变小、"
                         "覆盖率统计口径变化，绑定该用例的 P0 门禁条件可能受影响。")
        semantic = True

    for field, label in (("priority", "优先级"), ("tags", "标签")):
        if field in changed_fields:
            change_types.append(field)
            risks.append(f"{label}变化：不影响用例执行结果，但按{label}"
                         "聚合的报告分组统计口径会变化。")
    for field, label in (("name", "名称"), ("description", "描述")):
        if field in changed_fields:
            change_types.append(field)

    return {
        "changed_fields": sorted(set(changed_fields)),
        "change_types": sorted(set(change_types)),
        "semantic": semantic,
        "details": _to_jsonable(details),
        "risks": risks,
    }


def diff_environment(before: dict, patch_or_after: dict) -> dict:
    """识别环境改动字段与语义影响（变量 / 依赖 / 运行参数）。"""
    after = dict(before)
    after.update(patch_or_after)

    changed_fields: list[str] = []
    details: dict[str, dict] = {}
    for field in ("name", "description", "python_version", "base_image",
                  "variables", "dependencies", "config"):
        if field in patch_or_after and _stable(before.get(field)) != _stable(after.get(field)):
            changed_fields.append(field)
            details[field] = {"before": before.get(field), "after": after.get(field)}

    change_types: list[str] = []
    risks: list[str] = []
    semantic = False

    if "variables" in changed_fields:
        change_types.append("variable")
        old_keys = set((before.get("variables") or {}).keys())
        new_keys = set((after.get("variables") or {}).keys())
        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)
        modified = sorted(k for k in old_keys & new_keys
                          if _stable((before.get("variables") or {})[k]) !=
                          _stable((after.get("variables") or {})[k]))
        parts = []
        if added:
            parts.append(f"新增变量 {', '.join(added)}")
        if removed:
            parts.append(f"删除变量 {', '.join(removed)}")
        if modified:
            parts.append(f"更改变量 {', '.join(modified)}")
        risks.append("环境变量变化（" + "；".join(parts) + "）：请求地址、入参等"
                     "执行输入随之改变，该环境上的历史结果与后续结果不可直接比较。")
        semantic = True

    if "config" in changed_fields:
        change_types.append("runtime_config")
        cfg_before = before.get("config") or {}
        cfg_after = after.get("config") or {}
        sub = [k for k in set(cfg_before) | set(cfg_after)
               if _stable(cfg_before.get(k)) != _stable(cfg_after.get(k))]
        risks.append(f"运行参数变化（{', '.join(sorted(sub))}）：执行行为（目标地址、"
                     "延迟、失败率等）改变，同用例结果可能系统性翻转，门禁结论风险高。")
        semantic = True

    if "dependencies" in changed_fields:
        change_types.append("dependency")
        risks.append("依赖清单变化：运行时库版本可能不同，失败原因可能来自依赖而非"
                     "被测代码；历史报告对比时需标注依赖基线已变更。")
        semantic = True

    if "python_version" in changed_fields or "base_image" in changed_fields:
        change_types.append("runtime")
        risks.append("运行时基线（Python 版本/基础镜像）变化：跨基线结果不可直接比较。")
        semantic = True

    for field, label in (("name", "名称"), ("description", "描述")):
        if field in changed_fields:
            change_types.append(field)

    return {
        "changed_fields": sorted(set(changed_fields)),
        "change_types": sorted(set(change_types)),
        "semantic": semantic,
        "details": _to_jsonable(details),
        "risks": risks,
    }


# ---------------------------------------------------------------------------
# 影响分析
# ---------------------------------------------------------------------------

class _Node(dict):
    """传播路径上的一个节点（有序 dict，序列化稳定）。"""


def _node(kind: str, target_id: Optional[str], name: str, **extra: Any) -> dict:
    node = {"kind": kind, "id": target_id, "name": name}
    for k in sorted(extra):
        node[k] = extra[k]
    return node


class ImpactAnalyzer:
    """变更影响分析器。

    纯读取 ``registry``（实体存储）与 ``build_registry``（构建结果），
    不写任何状态；所有输出列表按确定键排序，保证重复分析稳定一致。
    """

    def __init__(self, registry, build_registry):
        self.registry = registry
        self.builds = build_registry

    # -- 公共入口 ---------------------------------------------------------
    def analyze_case(self, case_id: str, patch: Optional[dict] = None) -> dict:
        case = self.registry.store("cases").get(case_id)
        if case is None:
            return {"error": "用例不存在"}
        change = diff_case(case, patch or {})
        return self._build_result(
            subject=_node("case", case_id, case.get("name", case_id),
                          priority=case.get("priority"),
                          project_id=case.get("project_id")),
            project_id=case.get("project_id"),
            change=change,
            traces=self._case_traces(case, change),
        )

    def analyze_environment(self, env_id: str, patch: Optional[dict] = None) -> dict:
        env = self.registry.store("environments").get(env_id)
        if env is None:
            return {"error": "环境不存在"}
        change = diff_environment(env, patch or {})
        return self._build_result(
            subject=_node("environment", env_id, env.get("name", env_id),
                          project_id=env.get("project_id")),
            project_id=env.get("project_id"),
            change=change,
            traces=self._env_traces(env, change),
        )

    # -- 用例传播 ---------------------------------------------------------
    def _case_traces(self, case: dict, change: dict) -> list[list[dict]]:
        case_id = case["id"]
        project_id = case.get("project_id")
        semantic = change["semantic"]
        traces: list[list[dict]] = []

        case_node = _node("case", case_id, case.get("name", case_id),
                          priority=case.get("priority"))

        plans = self.registry.store("test_plans").query(
            where=[("project_id", "eq", project_id)])
        gates = self.registry.store("quality_gates").query(
            where=[("project_id", "eq", project_id)])

        # 1) 每个套件引用点一条路径（同用例被多套件引用 -> 多条）
        suites = self.registry.store("suites").query(
            where=[("project_id", "eq", project_id)],
            order_by="created_at", order="asc")
        suite_hit = False
        for suite in suites:
            case_ids = suite.get("case_ids") or []
            positions = [i for i, cid in enumerate(case_ids) if cid == case_id]
            if not positions:
                continue
            suite_hit = True
            for pos in positions:  # 同一套件内重复引用也按引用点分别呈现
                suite_node = _node(
                    "suite", suite["id"], suite.get("name", suite["id"]),
                    reference_index=pos,
                    reference_note=f"套件用例列表第 {pos + 1} 个引用")
                self._expand_from_suite(traces, [case_node, suite_node],
                                        suite, gates, semantic, case_id)

        # 2) 用例直接进测试计划（不经过套件的另一条引用边）
        for plan in plans:
            case_ids = plan.get("case_ids") or []
            for pos, cid in enumerate(case_ids):
                if cid != case_id:
                    continue
                plan_node = _node(
                    "test_plan", plan["id"], plan.get("name", plan["id"]),
                    reference_index=pos,
                    reference_note=f"计划用例列表第 {pos + 1} 个引用")
                progress = self._plan_progress_snapshot(plan)
                cur = progress["state_by_case"].get(case_id, "untested")
                traces.append([case_node, plan_node, _node(
                    "plan_progress", plan["id"],
                    f"计划进度 {progress['passed']}/{progress['total']}"
                    f"（{progress['progress_percent']}%）",
                    current_state=cur,
                    impact=("该用例后续结果可能改变计划通过/失败计数与进度百分比"
                            if semantic else
                            "执行结果不变，仅该用例在计划中的名称/分组展示可能变化"))])

        # 3) 不经过套件、直接按单条用例结果评估的门禁（require_p0 等）：
        #    门禁绑套件时已在套件路径下展开；这里补不依赖套件的提示。
        if not suite_hit and semantic:
            # 没有任何套件引用：语义变更只影响试跑与计划，提示"无下游执行链"
            pass

        # 4) 元数据变更（改名/描述）若没有任何引用，也给出一条只到用例的路径，
        #    让分析结果明确"无下游"而不是空列表造成歧义。
        if not traces:
            traces.append([case_node, _node(
                "none", None, "无下游引用",
                impact=("该用例当前没有被任何套件或测试计划引用，改动不影响"
                        "定时计划、门禁与历史报告。"))])

        return self._sort_traces(traces)

    def _expand_from_suite(self, traces: list, prefix: list[dict], suite: dict,
                           gates: list[dict], semantic: bool,
                           case_id: Optional[str] = None) -> None:
        """从套件节点继续向 门禁 / 计划 / 历史报告 / 定时计划 展开。"""
        suite_id = suite["id"]
        project_id = suite.get("project_id")

        # 套件本身就是历史报告的承载：语义变更 -> 报告可比性断点
        if semantic:
            recent = self._recent_builds(project_id, suite_id=suite_id, limit=5)
            traces.append(prefix + [_node(
                "report", None,
                f"历史报告/覆盖率（{len(recent)} 场历史构建）",
                builds=recent,
                comparable=False,
                impact="改动时间点之前与之后的构建结果口径不同，通过率/覆盖率"
                       "趋势对比需在此处分段标注。")])
        else:
            traces.append(prefix + [_node(
                "report", None, "历史报告（口径不变）",
                comparable=True,
                impact="元数据变更不改变执行结果，历史报告仍可连续比较。")])

        # 绑在套件上的门禁
        for gate in gates:
            if gate.get("scope") != "suite" or gate.get("suite_id") != suite_id:
                continue
            evaluation = self._gate_snapshot(gate)
            traces.append(prefix + [self._gate_node(gate, evaluation,
                                                    suite_id=suite_id)])

        # 套件被哪些定时计划引用
        schedules = self.registry.store("schedules").query(
            where=[("project_id", "eq", project_id)],
            order_by="created_at", order="asc")
        schedule_hit = False
        for schedule in schedules:
            if schedule.get("suite_id") != suite_id:
                continue
            schedule_hit = True
            sched_node = _node(
                "schedule", schedule["id"], schedule.get("name", schedule["id"]),
                cron=schedule.get("cron"),
                enabled=schedule.get("enabled", True),
                impact=("该计划到点会带着改动后的用例执行" if semantic
                        else "执行内容不变"),
            )
            # 计划维度的门禁
            tail: list[dict] = []
            for gate in gates:
                if gate.get("scope") == "schedule" and \
                        gate.get("schedule_id") == schedule["id"]:
                    evaluation = self._gate_snapshot(gate)
                    tail.append(self._gate_node(gate, evaluation,
                                                schedule_id=schedule["id"]))
            if tail:
                for gate_node in tail:
                    traces.append(prefix + [sched_node, gate_node])
            else:
                traces.append(prefix + [sched_node])

        # 套件未被任何定时计划引用时不额外造节点（真实引用边才传播）

    # -- 环境传播 ---------------------------------------------------------
    def _env_traces(self, env: dict, change: dict) -> list[list[dict]]:
        env_id = env["id"]
        project_id = env.get("project_id")
        semantic = change["semantic"]
        traces: list[list[dict]] = []

        env_node = _node("environment", env_id, env.get("name", env_id))

        suites = self.registry.store("suites").query(
            where=[("project_id", "eq", project_id)],
            order_by="created_at", order="asc")
        schedules = self.registry.store("schedules").query(
            where=[("project_id", "eq", project_id)],
            order_by="created_at", order="asc")
        gates = self.registry.store("quality_gates").query(
            where=[("project_id", "eq", project_id)])

        # 计划对环境的引用：env_id 显式指定优先，否则回落到套件 env_id
        def _resolved_env_id(schedule: dict) -> str:
            return schedule.get("env_id") or self._suite_env(schedule.get("suite_id"))

        # 环境与执行链只有两种真实引用关系：
        #   a) 套件默认环境（suite.env_id == env）；
        #   b) 定时计划显式指定环境（schedule.env_id == env），无论套件默认是谁。
        # 同一条「环境→套件→计划」路径只生成一次：遍历套件时把落在该环境上
        # 的计划（继承默认 / 显式覆盖都算）统一展开，不再单独造第二份前缀，
        # 否则显式指定恰好等于套件默认时会出现重复路径。
        bound = False
        for suite in suites:
            if suite.get("env_id") != env_id:
                continue
            bound = True
            suite_node = _node(
                "suite", suite["id"], suite.get("name", suite["id"]),
                reference_note="套件默认执行环境")
            self._expand_env_suite(traces, [env_node, suite_node], suite,
                                   schedules, gates, semantic, env_id,
                                   via="suite_default")

        # 显式指定该环境、但其套件默认环境是别的环境的计划：另一条独立引用边
        for schedule in schedules:
            if schedule.get("env_id") != env_id:
                continue
            suite = next((s for s in suites if s["id"] == schedule.get("suite_id")), None)
            if suite is not None and suite.get("env_id") == env_id:
                continue  # 已在上面套件默认分支里展开，避免重复
            bound = True
            sched_node = _node(
                "schedule", schedule["id"],
                schedule.get("name", schedule["id"]),
                cron=schedule.get("cron"),
                enabled=schedule.get("enabled", True),
                reference_note="计划显式指定执行环境")
            prefix = [env_node]
            if suite is not None:
                prefix.append(_node("suite", suite["id"],
                                    suite.get("name", suite["id"]),
                                    reference_note="计划绑定的套件"))
            self._expand_env_schedule(traces, prefix + [sched_node],
                                      schedule, gates, semantic)

        if not bound:
            traces.append([env_node, _node(
                "none", None, "无下游引用",
                impact="当前没有套件默认使用、也没有定时计划显式指定该环境，"
                       "改动不影响任何定时执行、门禁与历史报告。")])

        return self._sort_traces(traces)

    def _suite_env(self, suite_id: Optional[str]) -> Optional[str]:
        if not suite_id:
            return None
        suite = self.registry.store("suites").get(suite_id)
        return suite.get("env_id") if suite else None

    def _expand_env_suite(self, traces: list, prefix: list[dict], suite: dict,
                          schedules: list[dict], gates: list[dict],
                          semantic: bool, env_id: str, via: str) -> None:
        project_id = suite.get("project_id")
        # 历史构建（在该环境上跑该套件）
        if semantic:
            recent = self._recent_builds(project_id, suite_id=suite["id"],
                                         env_id=env_id, limit=5)
            traces.append(prefix + [_node(
                "report", None,
                f"该环境历史报告/覆盖率（{len(recent)} 场历史构建）",
                builds=recent, comparable=False,
                impact="环境变更后同套件结果可能系统性变化，趋势对比需在此分段。")])

        # 套件门禁
        for gate in gates:
            if gate.get("scope") == "suite" and gate.get("suite_id") == suite["id"]:
                traces.append(prefix + [self._gate_node(gate, self._gate_snapshot(gate),
                                                        suite_id=suite["id"])])

        # 哪些计划实际解析到该环境（显式 env 覆盖的不算套件默认边）
        hit_schedule = False
        for schedule in schedules:
            if schedule.get("suite_id") != suite["id"]:
                continue
            resolved = schedule.get("env_id") or suite.get("env_id")
            if via == "suite_default" and schedule.get("env_id") and \
                    schedule.get("env_id") != env_id:
                continue  # 被显式环境覆盖，与本边无关，不误报
            if resolved != env_id:
                continue
            hit_schedule = True
            sched_node = _node(
                "schedule", schedule["id"], schedule.get("name", schedule["id"]),
                cron=schedule.get("cron"),
                enabled=schedule.get("enabled", True),
                reference_note="继承套件默认环境" if not schedule.get("env_id")
                               else "计划显式指定环境")
            self._expand_env_schedule(traces, prefix + [sched_node],
                                      schedule, gates, semantic)

    def _expand_env_schedule(self, traces: list, prefix: list[dict],
                             schedule: dict, gates: list[dict],
                             semantic: bool) -> None:
        gate_nodes = [self._gate_node(g, self._gate_snapshot(g),
                                      schedule_id=schedule["id"])
                      for g in gates
                      if g.get("scope") == "schedule"
                      and g.get("schedule_id") == schedule["id"]]
        if gate_nodes:
            for gn in gate_nodes:
                traces.append(prefix + [gn])
        else:
            traces.append(prefix)

    # -- 快照计算（只读、确定性） -----------------------------------------
    def _recent_builds(self, project_id: str, suite_id: Optional[str] = None,
                       env_id: Optional[str] = None, limit: int = 5) -> list[dict]:
        out = []
        for build in self.builds.for_project(project_id).list_builds():
            if build.get("status") in ("pending", "running"):
                continue
            if suite_id is not None and build.get("suite_id") != suite_id:
                continue
            if env_id is not None and build.get("env_id") != env_id:
                continue
            out.append({
                "build_id": build["id"],
                "status": build.get("status"),
                "env_id": build.get("env_id"),
                "finished_at": build.get("finished_at"),
                "pass_rate": round(
                    build.get("passed", 0) /
                    max(1, build.get("total", 0) - build.get("skipped", 0)) * 100, 1)
                if build.get("total", 0) - build.get("skipped", 0) else 0.0,
            })
        return out[:limit]

    def _gate_snapshot(self, gate: dict) -> dict:
        """门禁最近一次结论快照（内联极简判定，避免与 gates 模块循环依赖问题）。"""
        from .gates import QualityGateManager  # 延迟导入
        if not hasattr(self, "_gate_mgr"):
            self._gate_mgr = QualityGateManager(self.registry, self.builds)
        return self._gate_mgr.evaluate_gate(gate)

    def _gate_node(self, gate: dict, evaluation: dict, **ref: Any) -> dict:
        status = evaluation.get("status", "no_data")
        checks = evaluation.get("checks") or []
        failed_checks = [c.get("message") for c in checks if not c.get("ok")]
        if not gate.get("enabled", True):
            impact = "门禁已停用，变更不影响其结论（重新启用后将按新口径判定）。"
        elif status == "no_data":
            impact = "门禁尚无评估数据；变更后首次执行将直接按新口径给出结论。"
        elif failed_checks:
            impact = ("门禁当前已不通过（" + "；".join(failed_checks) +
                      "），本次改动可能进一步改变其结论。")
        else:
            impact = "门禁当前通过；改动后口径变化可能使结论翻转，需重新评估。"
        return _node("quality_gate", gate["id"], gate.get("name", gate["id"]),
                     gate_scope=gate.get("scope"),
                     gate_enabled=gate.get("enabled", True),
                     gate_status=status,
                     build_id=evaluation.get("build_id"),
                     impact=impact, **{f"ref_{k}": v for k, v in ref.items()})

    def _plan_progress_snapshot(self, plan: dict) -> dict:
        from .plans import TestPlanManager
        if not hasattr(self, "_plan_mgr"):
            self._plan_mgr = TestPlanManager(self.registry, self.builds)
        progress = self._plan_mgr.plan_progress(plan)
        state_by_case = {c["case_id"]: c["state"] for c in progress["cases"]}
        progress["state_by_case"] = state_by_case
        return progress

    # -- 汇总与稳定性 ------------------------------------------------------
    @staticmethod
    def _trace_key(trace: list[dict]) -> str:
        # 用整条路径上每个节点的完整内容签名做键：同套件下多个报告/门禁节点
        # 的 kind/id 可能相同，仅靠 kind:id:index 会撞键导致丢路径。
        return "||".join(hashlib.sha256(_stable(n).encode("utf-8")).hexdigest()[:12]
                         for n in trace)

    def _sort_traces(self, traces: list[list[dict]]) -> list[list[dict]]:
        # 去重（理论上不会重复，去重保证幂等）+ 确定键排序
        seen = set()
        unique = []
        for tr in traces:
            key = self._trace_key(tr)
            if key in seen:
                continue
            seen.add(key)
            unique.append(tr)
        return sorted(unique, key=self._trace_key)

    def _build_result(self, subject: dict, project_id: str, change: dict,
                      traces: list[list[dict]]) -> dict:
        affected: dict[str, list[dict]] = {
            "suites": [], "schedules": [], "quality_gates": [],
            "test_plans": [], "reports": [],
        }
        seen_ids = {k: set() for k in affected}
        for trace in traces:
            # report 节点没有实体 id：用它所在路径上的套件/计划作区分键，
            # 保证两个套件各自的历史报告断点不会被合并成一条。
            context_id = next((n.get("id") for n in reversed(trace)
                               if n.get("kind") in ("suite", "schedule")), None)
            context_name = next((n.get("name") for n in reversed(trace)
                                 if n.get("kind") in ("suite", "schedule")), None)
            for node in trace:
                kind = node.get("kind")
                bucket = {"suite": "suites", "schedule": "schedules",
                          "quality_gate": "quality_gates",
                          "test_plan": "test_plans",
                          "report": "reports"}.get(kind)
                if not bucket:
                    continue
                if kind == "report":
                    key = ("report", context_id, node.get("comparable"))
                    item = {"id": context_id, "name": node.get("name"),
                            "context": context_name,
                            "comparable": node.get("comparable", True)}
                else:
                    key = (node.get("id"), node.get("reference_index"))
                    item = {
                        "id": node.get("id"),
                        "name": node.get("name"),
                        "reference_index": node.get("reference_index"),
                        "reference_note": node.get("reference_note"),
                    }
                if key in seen_ids[bucket]:
                    continue
                seen_ids[bucket].add(key)
                affected[bucket].append(item)

        # 报告可比性断点按「套件/计划上下文」去重计数：同套件内重复引用
        # 仍按引用点展开多条路径，但该套件的历史构建只有一份口径断点。
        report_break_contexts = set()
        for trace in traces:
            for node in trace:
                if node.get("kind") == "report" and node.get("comparable") is False:
                    ctx = next((n.get("id") for n in reversed(trace)
                                if n.get("kind") in ("suite", "schedule")),
                               node.get("name"))
                    report_break_contexts.add(ctx)

        # 汇总风险等级
        downstream = sum(len(v) for k, v in affected.items() if k != "reports")
        if change["semantic"] and any(
                n.get("gate_status") == "passed"
                for tr in traces for n in tr if n.get("kind") == "quality_gate"):
            risk_level = "high"
        elif change["semantic"] and downstream:
            risk_level = "medium"
        elif change["changed_fields"]:
            risk_level = "low"
        else:
            risk_level = "none"

        result = {
            "subject": subject,
            "project_id": project_id,
            "change": change,
            "risk_level": risk_level,
            "summary": {
                "suites": len(affected["suites"]),
                "schedules": len(affected["schedules"]),
                "quality_gates": len(affected["quality_gates"]),
                "test_plans": len(affected["test_plans"]),
                "report_breaks": len(report_break_contexts),
                "trace_count": len(traces),
            },
            "affected": {k: sorted(v, key=lambda x: (
                x.get("id") or "", x.get("reference_index") is None,
                x.get("reference_index") or 0))
                for k, v in affected.items()},
            "traces": traces,
        }
        result["fingerprint"] = self.fingerprint(result)
        return result

    @staticmethod
    def fingerprint(result: dict) -> str:
        """对分析结果内容算稳定指纹（不含 fingerprint 字段本身）。"""
        payload = {k: v for k, v in result.items() if k != "fingerprint"}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
