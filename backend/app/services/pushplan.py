"""推送计划业务规则：状态流转、字段校验、待处理预演明细与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.rehearsal import rehearsal_service
from app.store import store

MODULE = "pushplan"
REQUIRED_FIELDS = ["方案编号", "推送内容", "目标人群"]
STATUS_ORDER = ["草稿", "方案审批中", "执行中", "已完成", "已撤回"]
ACTION_RULES = {"提交方案审批": "方案审批中", "启动推送": "执行中", "撤回推送": "已撤回"}
NEGATIVE_ACTIONS = []


class PushplanService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("方案编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 清单回看关联内容：把预演批次与触达结果汇总到推送计划列表
        batches = rehearsal_service.list_batches()
        reaches = rehearsal_service.list_reach_results()
        batch_by_plan: dict[int, list[dict[str, Any]]] = {}
        for batch in batches:
            if batch.get("推送计划id") is not None:
                batch_by_plan.setdefault(int(batch["推送计划id"]), []).append(batch)
        # 触达结果通过预演批次关联到推送计划
        batch_plan_map = {int(b["id"]): int(b["推送计划id"]) for b in batches if b.get("推送计划id") is not None}
        reach_by_plan: dict[int, int] = {}
        for reach in reaches:
            plan_id = batch_plan_map.get(int(reach.get("预演批次id", 0)))
            if plan_id is not None:
                reach_by_plan[plan_id] = reach_by_plan.get(plan_id, 0) + 1

        enriched = []
        for row in rows:
            item = dict(row)
            plan_batches = batch_by_plan.get(int(row.get("id", 0)), [])
            item["关联预演批次数"] = len(plan_batches)
            item["待处理预演批次号"] = row.get("待处理预演批次号")
            item["触达结果数"] = reach_by_plan.get(int(row.get("id", 0)), 0)
            enriched.append(item)
        total = len(enriched)
        start = max(page - 1, 0) * size
        return enriched[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_pending_detail(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """读取交由推送计划保存的推送预演名单待处理明细。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"推送方案 {entry_id} 不存在或已归档"
        batch_id = entry.get("待处理预演批次id")
        if batch_id is None:
            return {"方案编号": entry.get("方案编号"), "待处理明细": [], "预演批次": None}, ""
        batch = rehearsal_service.get_batch(int(batch_id))
        roster = rehearsal_service.get_roster(int(batch_id))
        return {
            "方案编号": entry.get("方案编号"),
            "推送计划状态": entry.get("status"),
            "预演批次": batch,
            "待处理明细": entry.get("待处理明细", []),
            "名单明细": roster,
            "决策动作": rehearsal_service.list_actions(batch_id=int(batch_id)),
            "触达结果": rehearsal_service.list_reach_results(batch_id=int(batch_id)),
        }, ""

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"推送方案 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于推送计划可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        before = {"status": entry.get("status")}
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        rehearsal_service._audit(MODULE, str(entry.get("方案编号")), action, before,
                                 {"status": target}, "值班管理员")
        return entry, f"推送方案已{action}"
