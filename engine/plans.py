"""测试计划（Test Plan）管理与进度统计。

一个测试计划引用一组用例（``case_ids``），其进度不是独立存储的状态，
而是按各用例**最近一场已结束构建**的结果实时聚合：

- ``passed``     最近结果为通过
- ``failed``     最近结果为失败/错误/超时
- ``untested``   尚无执行记录

计划进度因此天然与执行历史联动；当某条用例被修改，它未来的执行结果可能
改变计划进度，这正是变更影响分析要提示的内容（见 :mod:`engine.impact`）。
"""

from __future__ import annotations

from typing import Optional

from .models import new_id

FAILED_STATUSES = ("failed", "error", "timeout")


class TestPlanManager:
    """测试计划管理与进度计算。"""

    def __init__(self, registry, build_registry):
        self.registry = registry
        self.builds = build_registry

    # -- CRUD -------------------------------------------------------------
    def create(self, project_id: str, payload: dict) -> dict:
        plan = {
            "id": new_id("plan"),
            "project_id": project_id,
            "name": payload.get("name", "未命名测试计划"),
            "description": payload.get("description", ""),
            "case_ids": list(payload.get("case_ids") or []),
            "status": payload.get("status", "active"),
            "created_at": __import__("time").time(),
        }
        self.registry.store("test_plans").insert(plan)
        return plan

    def list(self, project_id: str) -> list[dict]:
        return self.registry.store("test_plans").query(
            where=[("project_id", "eq", project_id)],
            order_by="created_at", order="asc")

    def get(self, plan_id: str) -> Optional[dict]:
        return self.registry.store("test_plans").get(plan_id)

    def update(self, plan_id: str, patch: dict) -> Optional[dict]:
        allowed = {k: patch[k] for k in
                   ("name", "description", "case_ids", "status") if k in patch}
        return self.registry.store("test_plans").update(plan_id, allowed)

    def delete(self, plan_id: str) -> bool:
        return self.registry.store("test_plans").delete(plan_id)

    # -- 进度 -------------------------------------------------------------
    def latest_case_results(self, project_id: str) -> dict[str, dict]:
        """取项目内每条用例最近一场已结束构建的结果。

        构建按创建时间倒序（``list_builds`` 已保证），因此每遇到一条用例
        的第一个结果就是最近结果，顺序处理即可，结果对同一数据快照稳定。
        """
        latest: dict[str, dict] = {}
        for build in self.builds.for_project(project_id).list_builds():
            if build.get("status") in ("pending", "running"):
                continue
            store = self.builds.for_project(project_id)
            for record in store.results(build["id"]):
                cid = record.get("case_id")
                if cid and cid not in latest:
                    # 结果分片里不冗余存 build_id，这里补上以便进度定位来源构建
                    record = dict(record)
                    record["build_id"] = build["id"]
                    latest[cid] = record
        return latest

    def progress(self, plan_id: str) -> dict:
        plan = self.get(plan_id)
        if plan is None:
            return {"error": "测试计划不存在"}
        return self.plan_progress(plan)

    def plan_progress(self, plan: dict, latest: Optional[dict] = None) -> dict:
        """计算计划进度；``latest`` 可传入复用的最近结果快照（性能/一致性）。"""
        project_id = plan["project_id"]
        case_ids = list(plan.get("case_ids") or [])
        if latest is None:
            latest = self.latest_case_results(project_id)

        details = []
        counts = {"passed": 0, "failed": 0, "untested": 0}
        for cid in case_ids:
            record = latest.get(cid)
            if record is None:
                state = "untested"
            elif record.get("status") in FAILED_STATUSES:
                state = "failed"
            elif record.get("status") == "passed":
                state = "passed"
            else:  # skipped 等不算通过也不算失败，按未测处理
                state = "untested"
            counts[state] += 1
            details.append({
                "case_id": cid,
                "state": state,
                "last_status": record.get("status") if record else None,
                "last_build_id": record.get("build_id") if record else None,
            })

        total = len(case_ids)
        done = counts["passed"] + counts["failed"]
        return {
            "plan_id": plan["id"],
            "plan_name": plan.get("name"),
            "project_id": project_id,
            "total": total,
            "passed": counts["passed"],
            "failed": counts["failed"],
            "untested": counts["untested"],
            "progress_percent": round(done / total * 100, 1) if total else 0.0,
            "pass_percent": round(counts["passed"] / total * 100, 1) if total else 0.0,
            "cases": details,
        }
