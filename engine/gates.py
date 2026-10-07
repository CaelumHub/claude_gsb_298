"""发布门禁（Quality Gate）管理与判定。

门禁挂在「套件」或「定时计划」上，在其最近一场已结束构建上评估一组
条件，给出 ``passed / failed / warning`` 结论：

- ``min_pass_rate``  通过率下限（百分比，0~100）
- ``max_failed``     失败/错误/超时用例数上限
- ``require_p0``     P0 用例必须全部通过

门禁实体结构见 :meth:`QualityGateManager.create`。判定本身是无副作用的
快照计算（只读构建结果），因此同一份数据多次评估结论稳定一致。
"""

from __future__ import annotations

from typing import Optional

from .models import new_id

GATE_SCOPES = ["suite", "schedule"]
GATE_CONDITIONS = ["min_pass_rate", "max_failed", "require_p0"]

FAILED_STATUSES = ("failed", "error", "timeout")


class QualityGateManager:
    """发布门禁管理与评估。"""

    def __init__(self, registry, build_registry):
        self.registry = registry
        self.builds = build_registry

    # -- CRUD -------------------------------------------------------------
    def create(self, project_id: str, payload: dict) -> dict:
        scope = payload.get("scope", "suite")
        if scope not in GATE_SCOPES:
            scope = "suite"
        gate = {
            "id": new_id("gate"),
            "project_id": project_id,
            "name": payload.get("name", "未命名门禁"),
            "description": payload.get("description", ""),
            "scope": scope,
            "suite_id": payload.get("suite_id"),
            "schedule_id": payload.get("schedule_id"),
            "conditions": self._normalize_conditions(payload.get("conditions") or {}),
            "enabled": bool(payload.get("enabled", True)),
            "created_at": __import__("time").time(),
        }
        self.registry.store("quality_gates").insert(gate)
        return gate

    def list(self, project_id: str) -> list[dict]:
        return self.registry.store("quality_gates").query(
            where=[("project_id", "eq", project_id)],
            order_by="created_at", order="asc")

    def get(self, gate_id: str) -> Optional[dict]:
        return self.registry.store("quality_gates").get(gate_id)

    def update(self, gate_id: str, patch: dict) -> Optional[dict]:
        allowed = {k: patch[k] for k in
                   ("name", "description", "scope", "suite_id", "schedule_id",
                    "conditions", "enabled") if k in patch}
        if "conditions" in allowed:
            allowed["conditions"] = self._normalize_conditions(allowed["conditions"])
        return self.registry.store("quality_gates").update(gate_id, allowed)

    def delete(self, gate_id: str) -> bool:
        return self.registry.store("quality_gates").delete(gate_id)

    @staticmethod
    def _normalize_conditions(raw: dict) -> dict:
        cond: dict = {}
        if "min_pass_rate" in raw and raw["min_pass_rate"] is not None:
            cond["min_pass_rate"] = max(0.0, min(100.0, float(raw["min_pass_rate"])))
        if "max_failed" in raw and raw["max_failed"] is not None:
            cond["max_failed"] = max(0, int(raw["max_failed"]))
        if "require_p0" in raw:
            cond["require_p0"] = bool(raw["require_p0"])
        return cond

    # -- 评估 -------------------------------------------------------------
    def _latest_build_id(self, gate: dict) -> Optional[str]:
        """找门禁绑定对象最近一场已结束构建的 id。"""
        project_id = gate["project_id"]
        if gate.get("scope") == "schedule":
            schedule_id = gate.get("schedule_id")
            runs = self.registry.store("schedule_runs").query(
                where=[("schedule_id", "eq", schedule_id)],
                order_by="fired_at", order="desc", limit=200)
            for run in runs:
                build = self.builds.for_project(project_id).get(run.get("build_id"))
                if build and build.get("status") not in ("pending", "running"):
                    return build["id"]
            return None
        # 套件维度：该套件最近一场已结束构建
        suite_id = gate.get("suite_id")
        for build in self.builds.for_project(project_id).list_builds():
            if build.get("suite_id") == suite_id and \
                    build.get("status") not in ("pending", "running"):
                return build["id"]
        return None

    def evaluate(self, gate_id: str) -> dict:
        gate = self.get(gate_id)
        if gate is None:
            return {"error": "门禁不存在"}
        return self.evaluate_gate(gate)

    def evaluate_gate(self, gate: dict) -> dict:
        """对单个门禁做一次判定（无副作用、确定性）。"""
        project_id = gate["project_id"]
        result: dict = {
            "gate_id": gate["id"],
            "gate_name": gate.get("name"),
            "scope": gate.get("scope"),
            "suite_id": gate.get("suite_id"),
            "schedule_id": gate.get("schedule_id"),
            "enabled": gate.get("enabled", True),
            "build_id": None,
            "status": "no_data",
            "checks": [],
        }
        if not gate.get("enabled", True):
            result["status"] = "disabled"
            return result

        build_id = self._latest_build_id(gate)
        result["build_id"] = build_id
        if not build_id:
            return result

        store = self.builds.for_project(project_id)
        build = store.get(build_id) or {}
        total = build.get("total", 0)
        skipped = build.get("skipped", 0)
        finished = total - skipped
        failed_count = sum(build.get(s, 0) for s in FAILED_STATUSES)
        pass_rate = round(build.get("passed", 0) / finished * 100, 1) if finished else 0.0

        conditions = gate.get("conditions") or {}
        checks = []

        threshold = conditions.get("min_pass_rate")
        if threshold is not None:
            checks.append({
                "condition": "min_pass_rate",
                "expected": threshold,
                "actual": pass_rate,
                "ok": pass_rate >= threshold,
                "message": f"通过率 {pass_rate}%（要求 ≥ {threshold:g}%）",
            })

        max_failed = conditions.get("max_failed")
        if max_failed is not None:
            checks.append({
                "condition": "max_failed",
                "expected": max_failed,
                "actual": failed_count,
                "ok": failed_count <= max_failed,
                "message": f"失败 {failed_count} 条（要求 ≤ {max_failed} 条）",
            })

        if conditions.get("require_p0"):
            p0 = store.results(build_id, where=[("priority", "eq", "P0")])
            p0_failed = [r for r in p0 if r.get("status") in FAILED_STATUSES]
            checks.append({
                "condition": "require_p0",
                "expected": 0,
                "actual": len(p0_failed),
                "ok": not p0_failed,
                "message": ("P0 全部通过" if not p0_failed else
                            f"P0 失败 {len(p0_failed)} 条：" +
                            "、".join(r.get("case_name", r.get("case_id", "?"))
                                      for r in p0_failed[:5])),
                "case_ids": [r.get("case_id") for r in p0_failed],
            })

        result["checks"] = checks
        result["status"] = "passed" if checks and all(c["ok"] for c in checks) else "failed"
        return result
