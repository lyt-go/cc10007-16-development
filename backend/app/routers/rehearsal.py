"""发送前频控预演接口。

覆盖：触发事件/人群快照读取、预演批次生成（完整业务凭证定版本、竞态幂等）、
预演名单与推送计划一并确认、实时决策发送动作、正式发送与触达结果核对、留痕查询。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, PageResult
from app.services.rehearsal import (
    CONFLICT_POLICIES,
    DEFAULT_CONFLICT_POLICY,
    rehearsal_service,
)

router = APIRouter(prefix="/api/rehearsal", tags=["发送前频控预演"])


class BatchCreatePayload(BaseModel):
    snapshot_id: int = Field(..., description="目标人群快照 id")
    event_ids: list[int] = Field(..., description="新增流程读取的触发事件 id 列表")
    realtime_ids: list[int] | None = Field(default=None, description="参与频控计算的实时决策规则 id 列表")
    conflict_policy: str = Field(default=DEFAULT_CONFLICT_POLICY, description="FIRST_IN_CHANNEL 或 HIGHER_PRIORITY")
    operator: str = "值班管理员"


class BatchIdPayload(BaseModel):
    operator: str = "值班管理员"


# ---------- 新流程输入读取 ----------
@router.get("/trigger-events")
def list_trigger_events() -> dict[str, Any]:
    """读取新增流程中的触发事件。"""
    return {"items": rehearsal_service.list_trigger_events(), "total": len(rehearsal_service.list_trigger_events())}


@router.get("/snapshots")
def list_snapshots() -> dict[str, Any]:
    """读取目标人群快照。"""
    items = rehearsal_service.list_snapshots()
    return {"items": items, "total": len(items)}


@router.get("/realtime-rules")
def list_realtime_rules() -> dict[str, Any]:
    """读取参与预演的实时决策规则（REAL-0002 为契约验证样本）。"""
    items = rehearsal_service.list_realtime_rules()
    return {"items": items, "total": len(items), "conflict_policies": CONFLICT_POLICIES}


# ---------- 预演批次 ----------
@router.post("/batches", response_model=ActionResult)
def create_batch(payload: BatchCreatePayload) -> ActionResult:
    """在新流程中读取触发事件并计算频控，生成推送预演名单明细数据集。

    结果版本由完整业务凭证哈希确认；同一凭证并发只留存一个数据集。
    """
    if not payload.event_ids:
        return ActionResult(ok=False, message="至少选择一个触发事件")
    batch, message = rehearsal_service.create_batch(
        snapshot_id=payload.snapshot_id,
        event_ids=payload.event_ids,
        realtime_ids=payload.realtime_ids,
        conflict_policy=payload.conflict_policy,
        operator=payload.operator,
    )
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    status: str | None = Query(default=None, description="待确认、已确认、已归档"),
    plan_id: int | None = Query(default=None, description="按推送计划 id 过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    rows = rehearsal_service.list_batches(status=status, plan_id=plan_id)
    total = len(rows)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=total, page=page, size=size)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    batch = rehearsal_service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"预演批次 {batch_id} 不存在")
    return batch


@router.get("/batches/{batch_id}/roster")
def get_roster(batch_id: int) -> dict[str, Any]:
    """推送预演名单明细数据集：含进入名单与各类频控排除行，可按数据批次追溯。"""
    batch = rehearsal_service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"预演批次 {batch_id} 不存在")
    roster = rehearsal_service.get_roster(batch_id)
    return {
        "batch": batch,
        "items": roster,
        "total": len(roster),
        "included": sum(1 for row in roster if row["预演决策"] == "进入名单"),
        "excluded": sum(1 for row in roster if row["预演决策"] != "进入名单"),
    }


@router.post("/batches/{batch_id}/confirm", response_model=ActionResult)
def confirm_batch(batch_id: int, payload: BatchIdPayload) -> ActionResult:
    """预演名单与推送计划状态一并确认；频控超限用户不能进入正式发送批次。"""
    batch, message = rehearsal_service.confirm_batch(batch_id, operator=payload.operator)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.post("/batches/{batch_id}/send", response_model=ActionResult)
def send_batch(batch_id: int, payload: BatchIdPayload) -> ActionResult:
    """按已确认预演批次执行正式发送；预演阶段实际发送次数始终为 0。"""
    batch, message = rehearsal_service.send_batch(batch_id, operator=payload.operator)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


# ---------- 实时决策结果（只读取同一预演批次） ----------
@router.get("/actions")
def list_actions(
    batch_id: int | None = Query(default=None),
    rule_code: str | None = Query(default=None, description="如 REAL-0002"),
) -> dict[str, Any]:
    items = rehearsal_service.list_actions(batch_id=batch_id, rule_code=rule_code)
    return {"items": items, "total": len(items)}


# ---------- 触达结果核对 ----------
@router.get("/reach-results")
def list_reach_results(batch_id: int | None = Query(default=None)) -> dict[str, Any]:
    items = rehearsal_service.list_reach_results(batch_id=batch_id)
    summary = {"成功": 0, "失败": 0, "未触达": 0}
    for row in items:
        summary[str(row.get("触达结果"))] = summary.get(str(row.get("触达结果")), 0) + 1
    return {"items": items, "total": len(items), "summary": summary}


# ---------- 留痕 ----------
@router.get("/audit-logs")
def list_audit_logs(
    module: str | None = Query(default=None),
    target_code: str | None = Query(default=None),
) -> dict[str, Any]:
    items = rehearsal_service.list_audit_logs(module=module, target_code=target_code)
    return {"items": items, "total": len(items)}
