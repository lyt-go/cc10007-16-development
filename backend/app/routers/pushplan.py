"""推送计划接口：维护推送方案，覆盖审批、启动、撤回与预演待处理明细回看。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.pushplan import PushplanService

router = APIRouter(prefix="/api/pushplan", tags=["推送计划"])

service = PushplanService()

LIST_FIELDS = ["方案编号", "推送内容", "目标人群", "推送渠道", "推送时间", "频控策略", "AB分组标识", "预计触达量"]
STATUSES = ["草稿", "方案审批中", "执行中", "已完成", "已撤回"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按方案编号检索"),
    status: str | None = Query(default=None, description="草稿、方案审批中、执行中、已完成、已撤回"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按方案编号与状态过滤推送计划列表，并回看关联预演批次与触达结果。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出推送计划清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "pushplan", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条推送方案明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"推送方案 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/pending-detail")
def get_pending_detail(entry_id: int) -> dict[str, Any]:
    """推送计划待处理明细：保存的推送预演名单明细数据集、决策动作与触达结果。"""
    detail, message = service.get_pending_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=message)
    return detail


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条推送方案，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="推送方案已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条推送方案执行提交方案审批、启动推送、撤回推送；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
