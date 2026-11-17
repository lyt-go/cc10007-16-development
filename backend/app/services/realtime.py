"""实时决策业务规则：状态流转、字段校验、频控预演联动与筛选口径都收在这里。

生命周期（核验码165f94a2）：草稿 → 规则评审中 → 已生效 → 已暂停，已生效/已暂停可归档，
归档后可恢复；变更切换、归档与恢复全部写 audit_log 留痕。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.rehearsal import rehearsal_service
from app.store import store

MODULE = "realtime"
REQUIRED_FIELDS = ["规则编号", "触发事件", "决策条件"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已暂停", "已归档"]
ACTION_RULES = {
    "提交规则评审": "规则评审中",
    "生效规则": "已生效",
    "暂停规则": "已暂停",
    "归档规则": "已归档",
}
# 恢复动作的目标状态由规则当前归档前的上下文决定，单独维护
RECOVER_ACTION = "恢复规则"
NEGATIVE_ACTIONS = []
EDITABLE_STATUSES = ["草稿", "已暂停"]


class RealtimeService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 实时决策结果驱动推送计划列表：把同规则编号产生的发送动作汇总回列表
        action_rows = rehearsal_service.list_actions()
        summary: dict[str, dict[str, int]] = {}
        for action in action_rows:
            bucket = summary.setdefault(str(action.get("规则编号")), {"待执行": 0, "已执行": 0, "已取消": 0})
            bucket[str(action.get("动作状态"))] = bucket.get(str(action.get("动作状态")), 0) + 1
        enriched = []
        for row in rows:
            item = dict(row)
            stat = summary.get(str(row.get("规则编号")), {"待执行": 0, "已执行": 0, "已取消": 0})
            item["待执行动作数"] = stat.get("待执行", 0)
            item["已执行动作数"] = stat.get("已执行", 0)
            item["已取消动作数"] = stat.get("已取消", 0)
            item["决策动作总数"] = sum(stat.values())
            enriched.append(item)
        total = len(enriched)
        start = max(page - 1, 0) * size
        return enriched[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for extra in ("执行动作", "冷却时间秒", "优先级", "分流比例", "生效时段"):
            if extra in values:
                entry[extra] = values.get(extra)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["version"] = 1
        rows.append(entry)
        rehearsal_service._audit(MODULE, str(entry.get("规则编号")), "登记规则", None,
                                 {"status": entry["status"], "version": 1}, "值班管理员")
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"决策规则 {entry_id} 不存在或已归档"
        before = {"status": entry.get("status")}

        if action == RECOVER_ACTION:
            if entry.get("status") != "已归档":
                return None, "仅已归档规则可以恢复"
            target = "已暂停"
            entry["status"] = target
            entry["pending"] = True
            entry["abnormal"] = False
            rehearsal_service._audit(MODULE, str(entry.get("规则编号")), RECOVER_ACTION, before,
                                     {"status": target}, "值班管理员", "归档规则恢复后进入已暂停，需重新生效")
            return entry, "决策规则已恢复为已暂停"

        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于实时决策可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target not in ("已暂停", "已归档")
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        rehearsal_service._audit(MODULE, str(entry.get("规则编号")), action, before,
                                 {"status": target}, "值班管理员")
        return entry, f"决策规则已{action}"

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """变更切换：仅草稿/已暂停可改字段，每次变更版本号 +1 并留痕。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"决策规则 {entry_id} 不存在或已归档"
        if entry.get("status") not in EDITABLE_STATUSES:
            return None, f"规则当前为「{entry.get('status')}」，仅草稿/已暂停状态允许变更切换"
        editable = ["触发事件", "决策条件", "执行动作", "冷却时间秒", "优先级", "分流比例", "生效时段"]
        changed = {field: values[field] for field in editable if field in values and values[field] != entry.get(field)}
        if not changed:
            return entry, "没有检测到字段变化，版本未切换"
        before = {"version": entry.get("version", 1), **{field: entry.get(field) for field in changed}}
        entry.update(changed)
        entry["version"] = int(entry.get("version", 1)) + 1
        rehearsal_service._audit(MODULE, str(entry.get("规则编号")), "变更切换", before,
                                 {"version": entry["version"], **changed}, "值班管理员",
                                 "规则字段变更，版本号递增，历史版本留痕可回溯")
        return entry, f"规则已变更切换至版本 v{entry['version']}"

    def list_audit_logs(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"决策规则 {entry_id} 不存在或已归档"
        return rehearsal_service.list_audit_logs(module=MODULE, target_code=str(entry.get("规则编号"))), ""
