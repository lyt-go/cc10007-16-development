"""发送前频控预演全链路核验脚本（核验码 167ed322）。

覆盖核验口径：
- 触发事件、目标人群快照、冷却时间、分流比例核对；
- 完整业务凭证定版本，竞态只留存一个预演名单明细数据集；
- 预演不得增加实际发送次数；频控超限用户不能进入正式发送批次；
- 预演名单与推送计划状态一并确认，待处理明细保存；
- 实时决策结果只读取同一预演批次生成发送动作，批次可追溯；
- 同一冷却窗口两条不同优先级推送的两种裁决策略；
- REAL-0002 契约验证样本驱动推送计划列表；触达结果核对；
- 实时决策生命周期流转、变更切换、归档与恢复留痕。

直接运行：.venv/bin/python tests/test_rehearsal_flow.py
"""
from __future__ import annotations

import threading

from app.services.pushplan import PushplanService
from app.services.realtime import RealtimeService
from app.services.rehearsal import rehearsal_service as rs
from app.store import store

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def main() -> None:
    print("== 1. 输入读取：触发事件与目标人群快照 ==")
    events = rs.list_trigger_events()
    snapshots = rs.list_snapshots()
    check("读取到触发事件", len(events) == 9)
    check("读取到人群快照", len(snapshots) == 3)
    check("REAL-0002 契约样本存在", any(r.get("规则编号") == "REAL-0002" for r in rs.list_realtime_rules()))

    print("== 2. REAL-0002 契约预演（支付成功人群/短信，事件1,2,3,4,6） ==")
    history_before = len(store.rows("send_history"))
    batch, message = rs.create_batch(snapshot_id=1, event_ids=[1, 2, 3, 4, 6], realtime_ids=[2])
    check("批次生成成功", batch is not None, message)
    roster = rs.get_roster(batch["id"])
    by_user = {row["用户编号"]: row for row in roster}
    check("候选条数=5", batch["候选条数"] == 5)
    check("进入名单=3 (U1001/U1002/U1006)", batch["进入名单数"] == 3)
    check("U1003 单日上限频控排除", by_user["U1003"]["预演决策"] == "频控排除"
          and "单日" in by_user["U1003"]["排除原因"])
    check("U1004 冷却窗口频控排除", by_user["U1004"]["预演决策"] == "频控排除"
          and by_user["U1004"]["冷却截止时间"] == "2026-11-17T10:30:00")
    check("频控用户不进正式批次", not by_user["U1003"]["是否进入正式发送批次"]
          and not by_user["U1004"]["是否进入正式发送批次"])

    print("== 3. 验证核对：触发事件、目标人群、冷却时间、分流比例（完整业务凭证） ==")
    cred = batch["完整业务凭证"]
    check("凭证含目标人群", cred["目标人群快照"]["目标人群"] == "支付成功人群")
    check("凭证含触发事件5条", len(cred["触发事件"]) == 5)
    check("凭证含冷却时间7200秒", cred["决策规则"][0]["冷却时间秒"] == 7200)
    check("凭证含分流比例100", cred["决策规则"][0]["分流比例"] == 100)
    check("凭证含历史发送摘要", len(cred["历史发送摘要"]) == history_before)
    check("版本凭证哈希为64位", len(batch["版本凭证哈希"]) == 64)

    print("== 4. 竞态：同一完整业务凭证只留存一个数据集 ==")
    ids: list[int] = []
    def worker() -> None:
        b, _ = rs.create_batch(snapshot_id=1, event_ids=[1, 2, 3, 4, 6], realtime_ids=[2])
        ids.append(b["id"])
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("8线程同凭证只产生1个批次", len(set(ids)) == 1 and ids[0] == batch["id"])

    print("== 5. 预演不得增加实际发送次数 ==")
    check("send_history 条数预演前后不变", len(store.rows("send_history")) == history_before)
    check("批次实际发送次数=0", batch["实际发送次数"] == 0)

    print("== 6. 预演名单与推送计划状态一并确认 ==")
    confirmed, msg = rs.confirm_batch(batch["id"])
    check("确认成功", confirmed is not None, msg)
    check("批次状态=已确认", confirmed["批次状态"] == "已确认")
    plan = store.find("pushplan", batch["推送计划id"])
    check("推送计划保存待处理预演批次号", plan.get("待处理预演批次号") == batch["预演批次号"])
    check("待处理明细已保存", len(plan.get("待处理明细", [])) == 5)
    check("推送计划预计触达量=进入名单数", plan["预计触达量"] == 3)
    again, msg2 = rs.confirm_batch(batch["id"])
    check("重复确认被拦截", again is None and "待确认" in msg2)

    print("== 7. 实时决策结果只读取同一预演批次生成发送动作 ==")
    actions = rs.list_actions(batch_id=batch["id"])
    check("动作数=进入名单数3", len(actions) == 3)
    check("动作全部挂同一批次哈希", all(a["版本凭证哈希"] == batch["版本凭证哈希"] for a in actions))
    check("动作初始为待执行", all(a["动作状态"] == "待执行" for a in actions))
    check("REAL-0002 动作过滤", all(a["规则编号"] == "REAL-0002" for a in rs.list_actions(rule_code="REAL-0002")))

    print("== 8. 正式发送：超限用户不进批次 + 触达结果核对 + 追溯 ==")
    sent, msg = rs.send_batch(batch["id"])
    check("发送成功", sent is not None, msg)
    reaches = rs.list_reach_results(batch["id"])
    sent_users = {r["用户编号"] for r in reaches}
    check("触达结果=3", len(reaches) == 3)
    check("发送用户不含U1003/U1004", not (sent_users & {"U1003", "U1004"}))
    check("发送用户=U1001/U1002/U1006", sent_users == {"U1001", "U1002", "U1006"})
    check("触达结果全部成功", all(r["触达结果"] == "成功" for r in reaches))
    check("批次实际发送次数=3", sent["实际发送次数"] == 3)
    check("名单->动作->触达->批次哈希可追溯",
          all(r["版本凭证哈希"] == batch["版本凭证哈希"] for r in reaches))
    check("历史发送已回写", len(store.rows("send_history")) == history_before + 3)

    print("== 9. 同一冷却窗口两条不同优先级推送的裁决 ==")
    # 快照3=APP推送全量候选；规则1=REAL-0001(加购,优先级10)，规则3=REAL-0003(支付,优先级30)
    b_high, _ = rs.create_batch(snapshot_id=3, event_ids=[1, 5, 7, 8, 9],
                                realtime_ids=[1, 3], conflict_policy="HIGHER_PRIORITY")
    traces_high = b_high["冲突裁决留痕"]
    u1001_high = next(t for t in traces_high if t["用户编号"] == "U1001")
    check("高优先级策略：U1001保留REAL-0003", u1001_high["保留批次行"]["规则编号"] == "REAL-0003")
    check("高优先级策略：U1001排除REAL-0001", u1001_high["排除批次行"]["规则编号"] == "REAL-0001")
    check("当前窗口U1001只剩一个可执行动作",
          len([r for r in rs.get_roster(b_high["id"]) if r["用户编号"] == "U1001" and r["是否进入正式发送批次"]]) == 1)

    b_first, _ = rs.create_batch(snapshot_id=3, event_ids=[1, 5, 7, 8, 9],
                                 realtime_ids=[1, 3], conflict_policy="FIRST_IN_CHANNEL")
    u1005_first = next(t for t in b_first["冲突裁决留痕"] if t["用户编号"] == "U1005")
    # U1005: 09:20加购先到，09:40支付后到 → 先进入渠道的是 REAL-0001
    check("先进入渠道策略：U1005保留REAL-0001", u1005_first["保留批次行"]["规则编号"] == "REAL-0001")
    check("两种策略产出不同凭证版本", b_high["版本凭证哈希"] != b_first["版本凭证哈希"])

    print("== 10. REAL-0002 契约样本驱动实时决策列表 ==")
    svc = RealtimeService()
    items, total = svc.list_entries()
    real2 = next(item for item in items if item["规则编号"] == "REAL-0002")
    check("列表含决策动作汇总", real2["决策动作总数"] == 3 and real2["已执行动作数"] == 3)

    print("== 11. 生命周期：变更切换、归档与恢复留痕（核验码165f94a2） ==")
    rule3 = svc.get_entry(3)
    base_version = rule3["version"]
    blocked, blocked_msg = svc.update_entry(3, {"优先级": 50})
    # REAL-0003 当前为已生效，变更应被拦截
    check("生效态禁止变更切换", blocked is None and "仅草稿/已暂停" in blocked_msg)
    svc.run_action(3, "暂停规则")
    updated, msg = svc.update_entry(3, {"优先级": 50, "冷却时间秒": 1800})
    check("暂停态变更切换版本递增", updated is not None and updated["version"] == base_version + 1, msg)
    archived, _ = svc.run_action(3, "归档规则")
    check("归档成功", archived["status"] == "已归档")
    none_batch, none_msg = rs.create_batch(snapshot_id=3, event_ids=[1], realtime_ids=[3])
    check("归档规则不参与预演", none_batch is None and "没有可用" in none_msg)
    recovered, _ = svc.run_action(3, "恢复规则")
    check("恢复后进入已暂停", recovered is not None and recovered["status"] == "已暂停")
    logs, _ = svc.list_audit_logs(3)
    actions_seen = [log["动作"] for log in logs]
    check("留痕含暂停/变更/归档/恢复",
          {"暂停规则", "变更切换", "归档规则", "恢复规则"}.issubset(set(actions_seen)))
    change_log = next(log for log in logs if log["动作"] == "变更切换")
    check("变更留痕记录前后值", change_log["变更前"].get("优先级") != change_log["变更后"].get("优先级"))

    print("== 12. 推送计划清单回看关联内容（核验码16703310） ==")
    push_service = PushplanService()
    detail, msg = push_service.get_pending_detail(batch["推送计划id"])
    check("待处理明细可回看", detail is not None and len(detail["名单明细"]) == 5)
    check("回看含同批次决策动作", len(detail["决策动作"]) == 3)
    check("回看含触达结果", len(detail["触达结果"]) == 3)
    plan_items, _ = push_service.list_entries()
    push2 = next(item for item in plan_items if item["方案编号"] == "PUSH-0002")
    check("推送计划列表关联预演批次", push2["关联预演批次数"] >= 1 and push2["触达结果数"] == 3)

    print(f"\n核验结果：PASS={PASS} FAIL={FAIL}")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
