"""用例 / 环境变更影响分析。

分析器把平台中的引用关系视为一张有向图：

    用例 ─┬─> 套件引用点 ─> 定时计划 ─> 未来构建 ─> 发布门禁
          ├─> 套件引用点 ─> 发布门禁
          └─> 套件引用点 ─> 测试计划进度
    环境 ──> 套件默认环境 / 计划覆盖环境 ─> 同上

历史构建不会被改写，但变更会破坏变更前后通过率、耗时、覆盖率趋势的可比性，
因此单独作为历史影响输出。运行中的构建在提交时已经取得用例与环境快照，不
列入未来影响。

设计上不做集合去重后只返回实体：同一条用例在 ``suite.case_ids`` 中出现多次
时，每个位置都是独立引用点，下游对象会保留全部 ``reference_id``。所有输出
按稳定键排序，同一数据状态下重复分析结果一致。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional

RISK_LEVELS = ("high", "medium", "low", "none")
_RISK_RANK = {"high": 0, "medium": 1, "low": 2, "none": 3}

ACTIVE_BUILD_STATUSES = ("pending", "running")
FINISHED_STATUSES = ("passed", "failed", "cancelled", "error")

_CASE_RISK_BY_CHANGE = {
    "assertion": "high",
    "step": "high",
    "enabled": "high",
    "timeout": "medium",
    "priority": "low",
    "metadata": "low",
}
_ENV_RISK_BY_CHANGE = {
    "dependency": "high",
    "runtime_variable": "high",
    "runtime": "high",
    "runtime_config": "medium",
    "metadata": "low",
}


def max_risk(*levels: Optional[str]) -> str:
    """返回一组风险级别中最高的一个。"""
    actual = [x for x in levels if x in _RISK_RANK]
    return min(actual, key=lambda x: _RISK_RANK[x]) if actual else "none"


def _stable(records: Iterable[dict]) -> list[dict]:
    return sorted(records, key=lambda x: x.get("id", ""))


def _ids(records: Iterable[dict]) -> list[str]:
    return sorted({r.get("id") for r in records if r.get("id")})


def _canonical(value: Any) -> str:
    """供字段比较的稳定 JSON 文本。"""
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _unique_strs(value: Any) -> list[str]:
    return sorted({str(x) for x in _as_list(value) if x})


def _enabled(record: Optional[dict]) -> bool:
    return True if record is None else record.get("enabled", True) is not False


def _is_assert_step(step: dict) -> bool:
    return step.get("action") == "assert"


def _assert_key(step: dict) -> tuple:
    return (
        "assert",
        step.get("type", "equals"),
        _canonical(step.get("actual")),
        _canonical(step.get("expected")),
    )


def _behavior_step_key(step: dict) -> tuple:
    if _is_assert_step(step):
        return _assert_key(step)
    return (step.get("action"), _canonical({k: v for k, v in step.items()
                                            if k not in ("name", "description")}))


def detect_case_changes(before: dict, after: dict) -> list[dict]:
    """比较用例前后两个版本，返回稳定、去重后的变更项。"""
    changes: list[dict] = []

    def add(kind: str, field: str, message: str, *, old: Any = None, new: Any = None,
            risk: Optional[str] = None) -> None:
        changes.append({
            "kind": kind,
            "field": field,
            "message": message,
            "old": old,
            "new": new,
            "risk": risk or _CASE_RISK_BY_CHANGE.get(kind, "low"),
        })

    scalar_fields = {
        "timeout": ("timeout", "超时时间"),
        "enabled": ("enabled", "启用状态"),
        "priority": ("priority", "优先级"),
        "name": ("metadata", "名称"),
        "description": ("metadata", "描述"),
    }
    for field, (kind, label) in scalar_fields.items():
        if field in after and before.get(field) != after.get(field):
            msg = f"{label}由 {before.get(field)!r} 变为 {after.get(field)!r}"
            add(kind, field, msg, old=before.get(field), new=after.get(field))

    if "tags" in after and sorted(before.get("tags") or []) != sorted(after.get("tags") or []):
        add("metadata", "tags", "标签发生变化，可能影响分组统计与报告口径",
            old=before.get("tags") or [], new=after.get("tags") or [])

    if "steps" in after and _canonical(before.get("steps") or []) != _canonical(after.get("steps") or []):
        old_steps = before.get("steps") or []
        new_steps = after.get("steps") or []

        old_asserts = [_assert_key(s) for s in old_steps if _is_assert_step(s)]
        new_asserts = [_assert_key(s) for s in new_steps if _is_assert_step(s)]
        if Counter(old_asserts) != Counter(new_asserts):
            old_counter, new_counter = Counter(old_asserts), Counter(new_asserts)
            removed, added = [], []
            for i, step in enumerate(old_steps):
                key = _assert_key(step)
                if _is_assert_step(step) and old_counter[key] > new_counter[key]:
                    removed.append(dict(step))
                    old_counter[key] -= 1
            for i, step in enumerate(new_steps):
                key = _assert_key(step)
                if _is_assert_step(step) and new_counter[key] > old_counter[key]:
                    added.append(dict(step))
                    new_counter[key] -= 1
            add("assertion", "steps.assertions", "断言条件发生变化，可能直接改变通过/失败结论",
                old=removed, new=added, risk="high")

        old_behavior = [_behavior_step_key(s) for s in old_steps if not _is_assert_step(s)]
        new_behavior = [_behavior_step_key(s) for s in new_steps if not _is_assert_step(s)]
        old_sequence = [_behavior_step_key(s) for s in old_steps]
        new_sequence = [_behavior_step_key(s) for s in new_steps]
        if old_behavior != new_behavior:
            add("step", "steps.behavior", "请求、脚本、赋值或等待等执行步骤发生变化",
                old=old_steps, new=new_steps, risk="high")
        elif old_sequence != new_sequence:
            add("step", "steps.order", "步骤顺序或数量发生变化，可能改变变量上下文",
                old=len(old_steps), new=len(new_steps), risk="medium")
        else:
            add("metadata", "steps.labels", "步骤名称或描述发生变化，不改变执行行为",
                old=old_steps, new=new_steps, risk="low")

    # 同类同字段只保留一条，避免一条复杂 steps 修改产生重复提示。
    deduped: dict[tuple, dict] = {}
    for c in changes:
        deduped[(c["kind"], c["field"])] = c
    return sorted(deduped.values(), key=lambda x: (_RISK_RANK[x["risk"]], x["field"]))


def detect_environment_changes(before: dict, after: dict) -> list[dict]:
    """比较环境前后两个版本，区分变量、依赖、运行参数与元数据。"""
    changes: list[dict] = []

    def add(kind: str, field: str, message: str, *, old: Any = None, new: Any = None,
            risk: Optional[str] = None) -> None:
        changes.append({
            "kind": kind,
            "field": field,
            "message": message,
            "old": old,
            "new": new,
            "risk": risk or _ENV_RISK_BY_CHANGE.get(kind, "medium"),
        })

    for field, label in (("python_version", "Python 版本"), ("base_image", "基础镜像")):
        if field in after and before.get(field) != after.get(field):
            add("runtime", field, f"{label}由 {before.get(field)!r} 变为 {after.get(field)!r}",
                old=before.get(field), new=after.get(field))

    for field, label in (("name", "环境名称"), ("description", "环境描述")):
        if field in after and before.get(field) != after.get(field):
            add("metadata", field, f"{label}发生变化，不改变执行快照",
                old=before.get(field), new=after.get(field), risk="low")

    old_vars = before.get("variables") or {}
    new_vars = after.get("variables") or {}
    if "variables" in after and old_vars != new_vars:
        keys = sorted(set(old_vars) | set(new_vars))
        details = []
        for key in keys:
            if key not in old_vars:
                details.append({"key": key, "change": "added", "value": new_vars[key]})
            elif key not in new_vars:
                details.append({"key": key, "change": "removed", "value": old_vars[key]})
            elif old_vars[key] != new_vars[key]:
                details.append({"key": key, "change": "changed",
                                "old": old_vars[key], "new": new_vars[key]})
        add("runtime_variable", "variables", "环境变量发生变化，可能改变请求、脚本和断言取值",
            old=old_vars, new=new_vars)
        changes[-1]["details"] = details

    old_deps = {d.get("name"): d for d in before.get("dependencies") or [] if d.get("name")}
    new_deps = {d.get("name"): d for d in after.get("dependencies") or [] if d.get("name")}
    if "dependencies" in after and old_deps != new_deps:
        details = []
        for name in sorted(set(old_deps) | set(new_deps)):
            if name not in old_deps:
                details.append({"name": name, "change": "added", "dependency": new_deps[name]})
            elif name not in new_deps:
                details.append({"name": name, "change": "removed", "dependency": old_deps[name]})
            elif old_deps[name] != new_deps[name]:
                details.append({"name": name, "change": "changed",
                                "old": old_deps[name], "new": new_deps[name]})
        add("dependency", "dependencies", "依赖版本或依赖集合发生变化，可能改变运行时行为",
            old=before.get("dependencies") or [], new=after.get("dependencies") or [])
        changes[-1]["details"] = details

    old_cfg = before.get("config") or {}
    new_cfg = after.get("config") or {}
    if "config" in after and old_cfg != new_cfg:
        keys = sorted(set(old_cfg) | set(new_cfg))
        details = [{"key": k, "old": old_cfg.get(k), "new": new_cfg.get(k)}
                   for k in keys if old_cfg.get(k) != new_cfg.get(k)]
        add("runtime_config", "config", "运行参数发生变化，可能改变目标地址、延迟或失败率",
            old=old_cfg, new=new_cfg)
        changes[-1]["details"] = details

    deduped: dict[tuple, dict] = {}
    for c in changes:
        deduped[(c["kind"], c["field"])] = c
    return sorted(deduped.values(), key=lambda x: (_RISK_RANK[x["risk"]], x["field"]))


class ImpactAnalyzer:
    """根据当前实体存储与构建结果计算变更影响。"""

    def __init__(self, registry, build_registry):
        self.registry = registry
        self.build_registry = build_registry

    def _store(self, name: str):
        if hasattr(self.registry, "optional_store"):
            return self.registry.optional_store(name)
        return self.registry.store(name)

    # ------------------------------------------------------------------ 入口
    def analyze(self, target_type: str, target: dict, baseline: Optional[dict] = None) -> dict:
        if target_type == "case":
            return self.analyze_case(target, baseline)
        if target_type == "environment":
            return self.analyze_environment(target, baseline)
        raise ValueError("target_type 必须是 case 或 environment")

    def analyze_case(self, case: dict, baseline: Optional[dict] = None) -> dict:
        case_id = case.get("id") or (baseline or {}).get("id")
        if not case_id:
            raise ValueError("缺少用例 id")
        old = baseline or self.registry.store("cases").get(case_id)
        if old is None:
            raise ValueError("用例不存在")
        new = dict(old)
        new.update(case or {})
        new["id"] = case_id
        project_id = old.get("project_id") or new.get("project_id")
        changes = detect_case_changes(old, new)
        change_kinds = [c["kind"] for c in changes]
        behavior_changed = any(k != "metadata" for k in change_kinds)

        suites = self._project_records("suites", project_id)
        suite_refs = []
        impacted_suites = []
        for suite in suites:
            refs = list(enumerate(suite.get("case_ids") or []))
            matched = [pos for pos, cid in refs if cid == case_id]
            if not matched:
                continue
            impacted_suites.append(suite)
            for pos in matched:
                ref_id = f"{suite['id']}#case-ref-{pos + 1}"
                suite_refs.append({
                    "id": ref_id,
                    "type": "suite_case_reference",
                    "suite_id": suite["id"],
                    "suite_name": suite.get("name", suite["id"]),
                    "case_id": case_id,
                    "position": pos,
                    "enabled": True,
                    "path": ["case", ref_id],
                })

        suite_ids = _ids(impacted_suites)
        schedules = self._impacted_schedules(project_id, suite_ids, suite_refs,
                                             env_id=None)
        gates = self._impacted_gates(project_id, suite_ids, suite_refs,
                                     target_type="case", env_id=None,
                                     change_kinds=change_kinds)
        plans = self._impacted_plans(project_id, suite_ids, suite_refs,
                                     target_type="case", env_id=None,
                                     change_kinds=change_kinds)
        reports = (self._case_historical_builds(project_id, suite_ids, case_id, change_kinds)
                   if behavior_changed else [])
        paths = self._case_paths(suite_refs, schedules, gates, plans)
        risks = self._risk_messages("case", changes)

        return self._result(
            target_type="case",
            target={
                "id": case_id,
                "project_id": project_id,
                "name": new.get("name"),
                "before": old,
                "after": new,
            },
            changes=changes,
            behavior_changed=behavior_changed,
            suite_references=suite_refs,
            schedules=schedules,
            gates=gates,
            plans=plans,
            historical_reports=reports,
            paths=paths,
            risks=risks,
        )

    def analyze_environment(self, env: dict, baseline: Optional[dict] = None) -> dict:
        env_id = env.get("id") or (baseline or {}).get("id")
        if not env_id:
            raise ValueError("缺少环境 id")
        old = baseline or self.registry.store("environments").get(env_id)
        if old is None:
            raise ValueError("环境不存在")
        new = dict(old)
        new.update(env or {})
        new["id"] = env_id
        project_id = old.get("project_id") or new.get("project_id")
        changes = detect_environment_changes(old, new)
        change_kinds = [c["kind"] for c in changes]
        behavior_changed = any(k != "metadata" for k in change_kinds)

        suites = self._project_records("suites", project_id)
        schedules_all = self._project_records("schedules", project_id)
        suite_by_id = {s["id"]: s for s in suites}

        suite_refs = []
        binding_seen: set[tuple[str, str]] = set()

        def add_env_ref(ref_type: str, suite: dict, *, schedule: Optional[dict] = None,
                        binding: str = "suite_default") -> None:
            key = (suite["id"], schedule["id"] if schedule else "suite")
            if key in binding_seen:
                return
            binding_seen.add(key)
            ref_id = (f"{schedule['id']}#schedule-env" if schedule
                      else f"{suite['id']}#suite-default-env")
            suite_refs.append({
                "id": ref_id,
                "type": ref_type,
                "schedule_id": schedule["id"] if schedule else None,
                "schedule_name": schedule.get("name", schedule["id"]) if schedule else None,
                "suite_id": suite["id"],
                "suite_name": suite.get("name", suite["id"]),
                "env_id": env_id,
                "binding": binding,
                "enabled": _enabled(schedule) if schedule else True,
                "path": ["environment", ref_id],
            })

        impacted_suite_ids: set[str] = set()
        for suite in suites:
            if suite.get("env_id") == env_id:
                impacted_suite_ids.add(suite["id"])
                add_env_ref("suite_environment_binding", suite)

        for schedule in schedules_all:
            suite = suite_by_id.get(schedule.get("suite_id"))
            if not suite:
                continue
            effective_env = schedule.get("env_id") or suite.get("env_id")
            if effective_env != env_id:
                continue
            impacted_suite_ids.add(suite["id"])
            add_env_ref("schedule_environment_binding", suite, schedule=schedule,
                        binding="schedule_override" if schedule.get("env_id") else "suite_default")

        impacted_suites = [s for s in suites if s["id"] in impacted_suite_ids]
        # 环境下的历史构建本身就是直接证据：即使套件后来改绑环境，旧趋势仍不可直接对比。
        schedules = self._impacted_schedules(project_id, sorted(impacted_suite_ids),
                                             suite_refs, env_id=env_id)
        gates = self._impacted_gates(project_id, sorted(impacted_suite_ids), suite_refs,
                                     target_type="environment", env_id=env_id,
                                     change_kinds=change_kinds)
        plans = self._impacted_plans(project_id, sorted(impacted_suite_ids), suite_refs,
                                     target_type="environment", env_id=env_id,
                                     change_kinds=change_kinds)
        reports = (self._environment_historical_builds(project_id, env_id, change_kinds)
                   if behavior_changed else [])
        paths = self._environment_paths(suite_refs, schedules, gates, plans)
        risks = self._risk_messages("environment", changes)

        return self._result(
            target_type="environment",
            target={
                "id": env_id,
                "project_id": project_id,
                "name": new.get("name"),
                "before": old,
                "after": new,
            },
            changes=changes,
            behavior_changed=behavior_changed,
            suite_references=suite_refs,
            schedules=schedules,
            gates=gates,
            plans=plans,
            historical_reports=reports,
            paths=paths,
            risks=risks,
        )

    # ------------------------------------------------------------------ 数据
    def _project_records(self, store_name: str, project_id: str) -> list[dict]:
        store = self._store(store_name)
        if store is None:
            return []
        records = store.query(where=[("project_id", "eq", project_id)])
        return _stable(records)

    def _optional_project_records(self, store_name: str, project_id: str) -> list[dict]:
        return self._project_records(store_name, project_id)

    @staticmethod
    def _suite_case_refs(suite_id: str, all_refs: list[dict]) -> list[dict]:
        return [r for r in all_refs if r.get("suite_id") == suite_id
                and r.get("type") == "suite_case_reference"]

    @staticmethod
    def _env_refs_for_suite(suite_id: str, all_refs: list[dict]) -> list[dict]:
        return [r for r in all_refs if r.get("suite_id") == suite_id
                and r.get("type", "").endswith("_environment_binding")]

    def _impacted_schedules(self, project_id: str, suite_ids: list[str],
                            suite_refs: list[dict], env_id: Optional[str]) -> list[dict]:
        out = []
        suites_by_id = {s["id"]: s for s in self._project_records("suites", project_id)}
        for schedule in self._project_records("schedules", project_id):
            if schedule.get("suite_id") not in suite_ids:
                continue
            if env_id is not None:
                suite = suites_by_id.get(schedule.get("suite_id"))
                effective_env = schedule.get("env_id") or (suite or {}).get("env_id")
                if effective_env != env_id:
                    continue
            refs = self._suite_case_refs(schedule["suite_id"], suite_refs)
            env_refs = [
                r for r in self._env_refs_for_suite(schedule["suite_id"], suite_refs)
                if r.get("schedule_id") == schedule["id"]
                or (not r.get("schedule_id") and not schedule.get("env_id"))
            ]
            out.append({
                "id": schedule["id"],
                "type": "schedule",
                "name": schedule.get("name", schedule["id"]),
                "project_id": project_id,
                "suite_id": schedule.get("suite_id"),
                "env_id": schedule.get("env_id"),
                "cron": schedule.get("cron"),
                "enabled": _enabled(schedule),
                "active": _enabled(schedule),
                "suite_references": [r["id"] for r in refs],
                "environment_references": [r["id"] for r in env_refs],
                "risk": "high" if _enabled(schedule) else "low",
                "risk_message": "定时计划下一次触发会使用变更后的快照" if _enabled(schedule)
                else "计划已禁用，不会产生新的定时执行；历史记录仍保留",
            })
        return _stable(out)

    def _scope_suite_ids(self, record: dict) -> list[str]:
        if "suite_ids" in record:
            ids = _unique_strs(record.get("suite_ids"))
        elif record.get("suite_id"):
            ids = [str(record.get("suite_id"))]
        else:
            ids = []
        return ids

    def _scope_env_ids(self, record: dict) -> list[str]:
        if "env_ids" in record:
            return _unique_strs(record.get("env_ids"))
        if record.get("env_id"):
            return [str(record.get("env_id"))]
        return []

    def _scope_matches_env(self, record: dict, target_type: str,
                           env_id: Optional[str]) -> bool:
        if target_type != "environment" or env_id is None:
            return True
        scoped = self._scope_env_ids(record)
        return not scoped or env_id in scoped

    def _impacted_gates(self, project_id: str, suite_ids: list[str],
                        suite_refs: list[dict], target_type: str,
                        env_id: Optional[str], change_kinds: list[str]) -> list[dict]:
        out = []
        for gate in self._optional_project_records("release_gates", project_id):
            linked = sorted(set(self._scope_suite_ids(gate)) & set(suite_ids))
            if not linked or not self._scope_matches_env(gate, target_type, env_id):
                continue
            refs: list[str] = []
            for sid in linked:
                refs.extend(r["id"] for r in self._suite_case_refs(sid, suite_refs))
                refs.extend(r["id"] for r in self._env_refs_for_suite(sid, suite_refs))
            evidence = self._gate_evidence(gate, linked)
            risk = self._policy_risk(change_kinds, bool(gate.get("enabled", True)))
            rule_risks = self._gate_rule_risks(gate, change_kinds)
            out.append({
                "id": gate["id"],
                "type": "release_gate",
                "name": gate.get("name", gate["id"]),
                "project_id": project_id,
                "suite_ids": self._scope_suite_ids(gate),
                "env_ids": self._scope_env_ids(gate),
                "enabled": _enabled(gate),
                "active": _enabled(gate),
                "rules": gate.get("rules") or {},
                "current_conclusion": evidence.get("conclusion"),
                "current_build_id": evidence.get("build_id"),
                "current_evidence": evidence,
                "suite_references": sorted(set(refs)),
                "risk": risk,
                "risk_message": "下一次门禁评估可能翻转；既有结论只代表变更前快照"
                if _enabled(gate) else "门禁已停用，不影响后续发布判断",
                "rule_risks": rule_risks,
            })
        return _stable(out)

    def _impacted_plans(self, project_id: str, suite_ids: list[str],
                        suite_refs: list[dict], target_type: str,
                        env_id: Optional[str], change_kinds: list[str]) -> list[dict]:
        out = []
        for plan in self._optional_project_records("test_plans", project_id):
            linked = sorted(set(self._scope_suite_ids(plan)) & set(suite_ids))
            if not linked or not self._scope_matches_env(plan, target_type, env_id):
                continue
            refs: list[str] = []
            for sid in linked:
                refs.extend(r["id"] for r in self._suite_case_refs(sid, suite_refs))
                refs.extend(r["id"] for r in self._env_refs_for_suite(sid, suite_refs))
            progress = self._plan_progress(plan, linked)
            out.append({
                "id": plan["id"],
                "type": "test_plan",
                "name": plan.get("name", plan["id"]),
                "project_id": project_id,
                "status": plan.get("status"),
                "suite_ids": self._scope_suite_ids(plan),
                "env_ids": self._scope_env_ids(plan),
                "enabled": _enabled(plan),
                "active": _enabled(plan),
                "suite_references": sorted(set(refs)),
                "current_progress": progress,
                "risk": self._policy_risk(change_kinds, _enabled(plan)),
                "risk_message": "计划进度的最新状态将在下一次执行后重算；变更前里程碑需标记口径",
            })
        return _stable(out)

    # ------------------------------------------------------------------ 评估
    def _finished_builds(self, project_id: str) -> list[dict]:
        store = self.build_registry.for_project(project_id)
        builds = [b for b in store.list_builds()
                  if b.get("status") not in ACTIVE_BUILD_STATUSES]
        return sorted(builds, key=lambda b: (b.get("created_at", 0), b.get("id", "")),
                      reverse=True)

    def _latest_build(self, project_id: str, suite_ids: list[str],
                      env_ids: Optional[list[str]] = None) -> Optional[dict]:
        scoped_suites = set(suite_ids)
        scoped_envs = set(env_ids or [])
        for build in self._finished_builds(project_id):
            if build.get("suite_id") not in scoped_suites:
                continue
            if scoped_envs and build.get("env_id") not in scoped_envs:
                continue
            return build
        return None

    def _read_coverage(self, project_id: str, build_id: str):
        return self.build_registry.for_project(project_id).read_coverage(build_id)

    def _gate_evidence(self, gate: dict, suite_ids: list[str]) -> dict:
        project_id = gate.get("project_id")
        env_ids = self._scope_env_ids(gate)
        build = self._latest_build(project_id, suite_ids, env_ids or None)
        if build is None:
            return {"conclusion": gate.get("last_conclusion", "no_evidence"),
                    "build_id": gate.get("last_build_id")}
        summary = self._build_summary(project_id, build)
        rules = gate.get("rules") or {}
        conclusion = gate.get("last_conclusion") or build.get("status")
        reasons = []
        min_pass_rate = rules.get("min_pass_rate")
        if min_pass_rate is not None and summary["pass_rate"] < float(min_pass_rate):
            conclusion = "failed"
            reasons.append(f"通过率 {summary['pass_rate']}% 低于阈值 {min_pass_rate}%")
        min_cov = rules.get("min_coverage")
        if min_cov is not None:
            cov = summary.get("coverage_percent")
            if cov is None:
                reasons.append("尚无覆盖率证据")
            elif cov < float(min_cov):
                conclusion = "failed"
                reasons.append(f"覆盖率 {cov}% 低于阈值 {min_cov}%")
        if rules.get("require_all_p0") and summary.get("p0_failed", 0) > 0:
            conclusion = "failed"
            reasons.append("存在 P0 用例失败")
        return {
            "conclusion": conclusion,
            "build_id": build.get("id"),
            "status": build.get("status"),
            "summary": summary,
            "reasons": reasons,
        }

    def _build_summary(self, project_id: str, build: dict) -> dict:
        total = build.get("total", 0)
        skipped = build.get("skipped", 0)
        finished = total - skipped
        pass_rate = round(build.get("passed", 0) / finished * 100, 1) if finished else 0.0
        cov = self._read_coverage(project_id, build["id"])
        p0_failed = 0
        by_priority = build.get("by_priority") or {}
        for status in ("failed", "error", "timeout"):
            p0_failed += by_priority.get("P0", {}).get(status, 0)
        return {
            "total": total,
            "passed": build.get("passed", 0),
            "failed": build.get("failed", 0),
            "error": build.get("error", 0),
            "timeout": build.get("timeout", 0),
            "skipped": skipped,
            "pass_rate": pass_rate,
            "coverage_percent": cov.get("percent") if cov else None,
            "duration": build.get("duration", 0.0),
            "p0_failed": p0_failed,
        }

    def _gate_rule_risks(self, gate: dict, change_kinds: list[str]) -> list[dict]:
        rules = gate.get("rules") or {}
        out = []
        if "min_pass_rate" in rules and any(k in ("assertion", "step", "enabled",
                                                  "timeout", "runtime_variable",
                                                  "dependency", "runtime_config",
                                                  "runtime") for k in change_kinds):
            out.append({"rule": "min_pass_rate", "risk": "high",
                        "message": "通过率阈值可能因结果变化而被触发"})
        if "min_coverage" in rules and any(k in ("step", "assertion", "enabled",
                                                 "runtime_variable", "dependency",
                                                 "runtime_config", "runtime") for k in change_kinds):
            out.append({"rule": "min_coverage", "risk": "medium",
                        "message": "覆盖率统计与通过率联动，趋势口径需要在变更点分段"})
        if rules.get("require_all_p0") and "priority" in change_kinds:
            out.append({"rule": "require_all_p0", "risk": "medium",
                        "message": "优先级变化可能改变 P0 门禁判定集合"})
        return out

    def _plan_progress(self, plan: dict, linked_suites: list[str]) -> dict:
        explicit = plan.get("progress")
        if isinstance(explicit, dict):
            return explicit
        project_id = plan.get("project_id")
        env_ids = self._scope_env_ids(plan)
        build_ids = []
        totals = Counter()
        for sid in dict.fromkeys(linked_suites):
            build = self._latest_build(project_id, [sid], env_ids or None)
            if not build:
                continue
            build_ids.append(build["id"])
            for key in ("total", "passed", "failed", "error", "timeout", "skipped"):
                totals[key] += build.get(key, 0)
        total = totals["total"]
        completed = total - totals["skipped"]
        return {
            "total": total,
            "completed": completed,
            "passed": totals["passed"],
            "failed": totals["failed"] + totals["error"] + totals["timeout"],
            "skipped": totals["skipped"],
            "completion_rate": round(completed / total * 100, 1) if total else 0.0,
            "pass_rate": round(totals["passed"] / completed * 100, 1) if completed else 0.0,
            "basis": "latest_finished_build_per_suite",
            "build_ids": build_ids,
        }

    def _policy_risk(self, change_kinds: list[str], active: bool) -> str:
        if not active:
            return "low"
        if any(k in ("assertion", "step", "enabled", "dependency", "runtime",
                     "runtime_variable") for k in change_kinds):
            return "high"
        if any(k in ("timeout", "runtime_config") for k in change_kinds):
            return "medium"
        return "low" if change_kinds else "none"

    # ------------------------------------------------------------------ 历史
    def _case_historical_builds(self, project_id: str, suite_ids: list[str],
                                case_id: str, change_kinds: list[str]) -> list[dict]:
        store = self.build_registry.for_project(project_id)
        scoped = set(suite_ids)
        out = []
        for build in self._finished_builds(project_id):
            if build.get("suite_id") not in scoped:
                continue
            records = store.results(build["id"], where=[("case_id", "eq", case_id)],
                                    limit=1)
            if not records:
                continue
            item = self._historical_item(project_id, build, change_kinds)
            item["case_result"] = {
                "status": records[0].get("status"),
                "duration": records[0].get("duration"),
            }
            item["reasons"].append("历史构建执行过同一套件引用点，变更后通过率与耗时不可直接环比")
            out.append(item)
        return out

    def _environment_historical_builds(self, project_id: str, env_id: str,
                                       change_kinds: list[str]) -> list[dict]:
        out = []
        for build in self._finished_builds(project_id):
            if build.get("env_id") != env_id:
                continue
            item = self._historical_item(project_id, build, change_kinds)
            item["reasons"].append("历史构建使用同一环境快照，环境基线变更前后不可直接对比")
            out.append(item)
        return out

    def _historical_item(self, project_id: str, build: dict,
                         change_kinds: list[str]) -> dict:
        summary = self._build_summary(project_id, build)
        reasons = []
        if any(k in ("assertion", "step", "enabled", "runtime_variable", "dependency",
                     "runtime", "runtime_config") for k in change_kinds):
            reasons.append("通过率、失败原因和覆盖率趋势需要按变更点分段")
        if "timeout" in change_kinds:
            reasons.append("超时阈值变化会影响 timeout 统计与耗时分布")
        return {
            "id": build["id"],
            "type": "historical_build",
            "build_id": build["id"],
            "project_id": project_id,
            "suite_id": build.get("suite_id"),
            "env_id": build.get("env_id"),
            "name": build.get("name") or build["id"],
            "status": build.get("status"),
            "trigger": build.get("trigger"),
            "created_at": build.get("created_at"),
            "finished_at": build.get("finished_at"),
            "summary": summary,
            "comparability": "baseline_changed",
            "reasons": reasons,
            "risk": "medium",
        }

    # ------------------------------------------------------------------ 输出
    def _case_paths(self, refs, schedules, gates, plans) -> list[dict]:
        schedule_by_suite = {}
        for s in schedules:
            schedule_by_suite.setdefault(s["suite_id"], []).append(s)
        paths = []
        for ref in refs:
            sid = ref["suite_id"]
            chain = ["case", ref["id"]]
            for schedule in schedule_by_suite.get(sid, []):
                paths.append(self._path(chain + [schedule["id"]], "case_to_schedule"))
            for gate in gates:
                if sid in gate.get("suite_ids", []):
                    paths.append(self._path(chain + [gate["id"]], "case_to_gate"))
            for plan in plans:
                if sid in plan.get("suite_ids", []):
                    paths.append(self._path(chain + [plan["id"]], "case_to_plan"))
        return sorted(paths, key=lambda p: p["id"])

    def _environment_paths(self, refs, schedules, gates, plans) -> list[dict]:
        paths = []
        for ref in refs:
            sid = ref["suite_id"]
            chain = ["environment", ref["id"]]
            for schedule in schedules:
                if schedule["suite_id"] != sid:
                    continue
                ref_ids = set(schedule.get("environment_references") or [])
                if ref_ids and ref["id"] not in ref_ids:
                    continue
                paths.append(self._path(chain + [schedule["id"]], "environment_to_schedule"))
            for gate in gates:
                if sid in gate.get("suite_ids", []) and ref["id"] in set(gate.get("suite_references") or []):
                    paths.append(self._path(chain + [gate["id"]], "environment_to_gate"))
            for plan in plans:
                if sid in plan.get("suite_ids", []) and ref["id"] in set(plan.get("suite_references") or []):
                    paths.append(self._path(chain + [plan["id"]], "environment_to_plan"))
        return sorted(paths, key=lambda p: p["id"])

    @staticmethod
    def _path(nodes: list[str], kind: str) -> dict:
        return {"id": "|".join(nodes), "type": kind, "nodes": nodes}

    def _risk_messages(self, target_type: str, changes: list[dict]) -> list[dict]:
        messages = []
        kinds = {c["kind"] for c in changes}
        if target_type == "case":
            if "assertion" in kinds:
                messages.append({"level": "high", "message": "断言口径变化可能让既有失败转通过或通过转失败，发布门禁必须重新评估"})
            if "step" in kinds:
                messages.append({"level": "high", "message": "执行步骤变化可能改变请求覆盖范围、变量上下文和覆盖率统计"})
            if "enabled" in kinds:
                messages.append({"level": "high", "message": "禁用后用例仍占总数但会计为跳过，可能虚高通过率并拉低计划完成率"})
            if "timeout" in kinds:
                messages.append({"level": "medium", "message": "超时阈值变化会影响 timeout 数量、耗时分位值和门禁稳定性"})
        else:
            if "runtime_variable" in kinds:
                messages.append({"level": "high", "message": "环境变量会注入执行上下文，可能改变请求目标、脚本结果和断言实际值"})
            if "dependency" in kinds:
                messages.append({"level": "high", "message": "依赖变化可能引入兼容性问题，建议先在非生产环境重跑引用套件"})
            if "runtime" in kinds:
                messages.append({"level": "high", "message": "运行时镜像或语言版本变化可能造成跨环境结果差异"})
            if "runtime_config" in kinds:
                messages.append({"level": "medium", "message": "运行参数变化会影响延迟、失败率或目标地址，历史趋势需分段比较"})
        if not messages and kinds:
            messages.append({"level": "low", "message": "本次主要是元数据变化，不改变执行结果；报告展示和分组口径可能变化"})
        return sorted(messages, key=lambda x: _RISK_RANK[x["level"]])

    def _result(self, *, target_type: str, target: dict, changes: list[dict],
                behavior_changed: bool, suite_references: list[dict],
                schedules: list[dict], gates: list[dict], plans: list[dict],
                historical_reports: list[dict], paths: list[dict],
                risks: list[dict]) -> dict:
        active_schedules = [x for x in schedules if x.get("active")]
        active_gates = [x for x in gates if x.get("active")]
        active_plans = [x for x in plans if x.get("active")]
        # 纯名称、描述、标签等元数据变化不改变执行结果，不能误报为定时执行、
        # 门禁结论或计划进度风险；套件引用关系仍保留，便于展示改名影响。
        if changes and not behavior_changed:
            schedules = []
            gates = []
            plans = []
            active_schedules = []
            active_gates = []
            active_plans = []
        impacted_suite_ids = sorted({r["suite_id"] for r in suite_references})
        levels = [c.get("risk") for c in changes] + [
            x.get("risk") for x in active_schedules + active_gates + active_plans
        ]
        if historical_reports and behavior_changed:
            levels.append("medium")
        coverage_statistics = {
            "comparability": "baseline_changed" if (behavior_changed and historical_reports) else
            ("future_only" if behavior_changed else "not_affected"),
            "historical_build_count": 0,
            "historical_builds": [],
            "future_suite_count": len(impacted_suite_ids) if behavior_changed else 0,
            "future_schedule_count": len(active_schedules) if behavior_changed else 0,
        }
        if behavior_changed:
            coverage_statistics["historical_builds"] = [{
                "build_id": item["build_id"],
                "suite_id": item.get("suite_id"),
                "env_id": item.get("env_id"),
                "coverage_percent": (item.get("summary") or {}).get("coverage_percent"),
                "pass_rate": (item.get("summary") or {}).get("pass_rate"),
                "baseline": "before_change",
            } for item in historical_reports
                if (item.get("summary") or {}).get("coverage_percent") is not None]
            coverage_statistics["historical_build_count"] = len(coverage_statistics["historical_builds"])
        risk_level = max_risk(*levels) if levels else "none"
        return {
            "target_type": target_type,
            "target": {k: v for k, v in target.items()
                       if k not in ("before", "after")},
            "change_count": len(changes),
            "changes": changes,
            "behavior_changed": behavior_changed,
            "risk_level": risk_level,
            "impacted": {
                "suite_count": len(impacted_suite_ids),
                "suite_reference_count": len(suite_references),
                "schedule_count": len(active_schedules),
                "release_gate_count": len(active_gates),
                "test_plan_count": len(active_plans),
                "historical_build_count": len(historical_reports),
            },
            "suite_references": sorted(suite_references, key=lambda x: x["id"]),
            "suites": [{"id": sid} for sid in impacted_suite_ids],
            "schedules": schedules,
            "release_gates": gates,
            "test_plans": plans,
            "historical_reports": historical_reports,
            "coverage_statistics": coverage_statistics,
            "paths": paths,
            "risk_tips": risks,
            "excluded": self._excluded(schedules, gates, plans),
        }

    @staticmethod
    def _excluded(schedules, gates, plans) -> list[dict]:
        out = []
        for schedule in schedules:
            if not schedule.get("active"):
                out.append({"id": schedule["id"], "type": "schedule",
                            "reason": "定时计划已禁用"})
        for gate in gates:
            if not gate.get("active"):
                out.append({"id": gate["id"], "type": "release_gate",
                            "reason": "发布门禁已禁用"})
        for plan in plans:
            if not plan.get("active"):
                out.append({"id": plan["id"], "type": "test_plan",
                            "reason": "测试计划已归档或禁用"})
        return sorted(out, key=lambda x: (x["type"], x["id"]))
