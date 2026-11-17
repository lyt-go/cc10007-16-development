"""发送前频控预演领域服务。

职责（对应业务核验口径）：
1. 读取新增流程中的触发事件（trigger_event）与目标人群快照（audience_snapshot）；
2. 在新流程中读取触发事件并按实时决策规则计算频控，生成「推送预演名单明细数据集」；
3. 结果版本由完整业务凭证（规则/事件/人群/频控口径/历史发送摘要/冲突裁决策略）哈希确认；
4. 同一凭证竞态并发只留存一个预演批次（仓库锁 + 凭证幂等）；
5. 预演全程不产生任何发送，频控超限用户只能以「排除」形态留在数据集里，不能进入正式发送批次；
6. 实时决策结果只读取同一预演批次并据此生成发送动作，数据批次可一路追溯。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from app.store import store

REALTIME_MODULE = "realtime"
PUSHPLAN_MODULE = "pushplan"

# 频控口径：同一渠道单日发送上限（与推送计划「频控策略」字段一致）
DAILY_CHANNEL_LIMIT = 3
# 同一冷却窗口命中不同优先级推送时的两种裁决策略
CONFLICT_POLICIES = {
    "FIRST_IN_CHANNEL": "保留先进入渠道的推送",
    "HIGHER_PRIORITY": "保留优先级更高的推送",
}
DEFAULT_CONFLICT_POLICY = "HIGHER_PRIORITY"

BATCH_STATUS = ["待确认", "已确认", "已归档"]
ROSTER_DECISIONS = ["进入名单", "频控排除", "分流排除", "冷却冲突排除"]
ACTION_STATUSES = ["待执行", "已执行", "已取消"]
REACH_RESULTS = ["成功", "失败", "未触达"]


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class RehearsalService:
    """预演批次与频控计算的唯一入口，路由层不做业务判断。"""

    # ---------- 基础读取 ----------
    def list_trigger_events(self, event_ids: list[int] | None = None) -> list[dict[str, Any]]:
        rows = store.rows("trigger_event")
        if event_ids:
            wanted = set(event_ids)
            rows = [row for row in rows if int(row.get("id", 0)) in wanted]
        return sorted(rows, key=lambda row: str(row.get("事件时间")))

    def list_snapshots(self) -> list[dict[str, Any]]:
        return store.rows("audience_snapshot")

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        return store.find("audience_snapshot", snapshot_id)

    def list_realtime_rules(self) -> list[dict[str, Any]]:
        return store.rows(REALTIME_MODULE)

    def _active_rules(self, realtime_ids: list[int] | None) -> list[dict[str, Any]]:
        rules = [row for row in store.rows(REALTIME_MODULE) if row.get("status") != "已归档"]
        if realtime_ids:
            wanted = set(realtime_ids)
            rules = [row for row in rules if int(row.get("id", 0)) in wanted]
        return rules

    # ---------- 频控口径 ----------
    def _history_for(self, user_id: str, channel: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows("send_history")
            if row.get("用户编号") == user_id and row.get("推送渠道") == channel
        ]

    def _daily_count(self, user_id: str, channel: str, day: str) -> int:
        return sum(
            1 for row in self._history_for(user_id, channel)
            if str(row.get("发送时间", "")).startswith(day)
        )

    def _in_cooldown(self, user_id: str, channel: str, event_time: datetime, cooldown_seconds: int) -> dict[str, Any] | None:
        """命中冷却窗口时返回上一条发送记录，否则返回 None。"""
        for row in self._history_for(user_id, channel):
            sent_at = _parse_ts(str(row["发送时间"]))
            if sent_at <= event_time and event_time < sent_at + timedelta(seconds=cooldown_seconds):
                return row
        return None

    @staticmethod
    def _in_split(user_id: str, rule_code: str, ratio: int) -> bool:
        """分流比例：对「规则+用户」做稳定哈希取模，保证同一用户结果可复算、可追溯。"""
        if ratio >= 100:
            return True
        if ratio <= 0:
            return False
        bucket = int(hashlib.md5(f"{rule_code}|{user_id}".encode("utf-8")).hexdigest(), 16) % 100
        return bucket < ratio

    # ---------- 候选与频控计算 ----------
    def _build_candidates(
        self,
        events: list[dict[str, Any]],
        snapshot: dict[str, Any],
        rules: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        audience = set(snapshot.get("用户列表", []))
        channel = str(snapshot.get("推送渠道", ""))
        rule_by_event: dict[str, list[dict[str, Any]]] = {}
        for rule in rules:
            rule_by_event.setdefault(str(rule.get("触发事件", "")), []).append(rule)

        candidates: list[dict[str, Any]] = []
        for event in events:
            user_id = str(event.get("用户编号", ""))
            if user_id not in audience:
                continue  # 不在目标人群快照内，不进入预演数据集
            for rule in rule_by_event.get(str(event.get("事件名称", "")), []):
                event_time = _parse_ts(str(event["事件时间"]))
                cooldown = _int(rule.get("冷却时间秒"), 0)
                ratio = max(0, min(100, _int(rule.get("分流比例"), 100)))
                candidate = {
                    "用户编号": user_id,
                    "事件编号": event.get("事件编号"),
                    "事件名称": event.get("事件名称"),
                    "事件时间": event.get("事件时间"),
                    "规则id": rule.get("id"),
                    "规则编号": rule.get("规则编号"),
                    "执行动作": rule.get("执行动作"),
                    "推送渠道": channel,
                    "优先级": _int(rule.get("优先级")),
                    "冷却时间秒": cooldown,
                    "分流比例": ratio,
                    "规则版本": rule.get("version", 1),
                    "规则状态": rule.get("status"),
                    "_event_dt": event_time,
                }
                candidates.append(candidate)
        return candidates

    def _adjudicate_conflicts(
        self,
        candidates: list[dict[str, Any]],
        policy: str,
    ) -> tuple[set[int], list[dict[str, Any]]]:
        """同一用户在同一冷却窗口命中两条不同优先级推送时，按策略只保留一个动作。

        返回（被冷却冲突排除的候选下标集合、冲突裁决留痕列表）。
        """
        excluded: set[int] = set()
        traces: list[dict[str, Any]] = []
        by_user: dict[str, list[int]] = {}
        for idx, candidate in enumerate(candidates):
            by_user.setdefault(candidate["用户编号"], []).append(idx)

        for user_id, indexes in by_user.items():
            # 两两判定：事件时间间隔落在较长冷却窗口内即视为同一冷却窗口命中
            for pos_a, idx_a in enumerate(indexes):
                for idx_b in indexes[pos_a + 1:]:
                    a, b = candidates[idx_a], candidates[idx_b]
                    if a["规则编号"] == b["规则编号"]:
                        continue
                    window = max(a["冷却时间秒"], b["冷却时间秒"])
                    gap = abs((a["_event_dt"] - b["_event_dt"]).total_seconds())
                    if gap >= window:
                        continue
                    if policy == "FIRST_IN_CHANNEL":
                        winner, loser = (a, b) if a["_event_dt"] <= b["_event_dt"] else (b, a)
                        reason = "同一冷却窗口命中多条推送，按「保留先进入渠道的推送」裁决"
                    else:
                        winner, loser = (a, b) if a["优先级"] >= b["优先级"] else (b, a)
                        reason = "同一冷却窗口命中多条推送，按「保留优先级更高的推送」裁决"
                    loser_idx = idx_b if winner is a else idx_a
                    if loser_idx in excluded:
                        continue
                    excluded.add(loser_idx)
                    traces.append({
                        "用户编号": user_id,
                        "保留批次行": {"规则编号": winner["规则编号"], "事件编号": winner["事件编号"], "优先级": winner["优先级"]},
                        "排除批次行": {"规则编号": loser["规则编号"], "事件编号": loser["事件编号"], "优先级": loser["优先级"]},
                        "裁决策略": policy,
                        "裁决说明": reason,
                    })
        return excluded, traces

    def _evaluate(
        self,
        candidates: list[dict[str, Any]],
        snapshot: dict[str, Any],
        policy: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        conflict_excluded, conflict_traces = self._adjudicate_conflicts(candidates, policy)
        roster: list[dict[str, Any]] = []
        channel = str(snapshot.get("推送渠道", ""))

        for idx, candidate in enumerate(candidates):
            event_time = candidate.pop("_event_dt")
            day = event_time.date().isoformat()
            base = {
                "用户编号": candidate["用户编号"],
                "事件编号": candidate["事件编号"],
                "触发事件": candidate["事件名称"],
                "事件时间": candidate["事件时间"],
                "规则编号": candidate["规则编号"],
                "执行动作": candidate["执行动作"],
                "推送渠道": channel,
                "优先级": candidate["优先级"],
                "冷却时间秒": candidate["冷却时间秒"],
                "分流比例": candidate["分流比例"],
                "规则版本": candidate["规则版本"],
                "规则状态": candidate["规则状态"],
                "频控口径": f"{channel}单日上限{DAILY_CHANNEL_LIMIT}次；冷却窗口{candidate['冷却时间秒']}秒",
                "裁决策略": policy,
            }

            if idx in conflict_excluded:
                row = {**base, "预演决策": "冷却冲突排除", "是否进入正式发送批次": False,
                       "排除原因": "同一冷却窗口命中不同优先级推送，裁决后保留另一条",
                       "冷却截止时间": None}
            elif self._daily_count(candidate["用户编号"], channel, day) >= DAILY_CHANNEL_LIMIT:
                row = {**base, "预演决策": "频控排除", "是否进入正式发送批次": False,
                       "排除原因": f"{channel}当日已发送{DAILY_CHANNEL_LIMIT}次，触达单日频控上限",
                       "冷却截止时间": None}
            elif (last := self._in_cooldown(candidate["用户编号"], channel, event_time, candidate["冷却时间秒"])) is not None:
                cutoff = (_parse_ts(str(last["发送时间"])) + timedelta(seconds=candidate["冷却时间秒"])).isoformat()
                row = {**base, "预演决策": "频控排除", "是否进入正式发送批次": False,
                       "排除原因": "距上次发送仍在冷却窗口内",
                       "冷却截止时间": cutoff}
            elif not self._in_split(candidate["用户编号"], str(candidate["规则编号"]), candidate["分流比例"]):
                row = {**base, "预演决策": "分流排除", "是否进入正式发送批次": False,
                       "排除原因": f"未命中规则分流比例 {candidate['分流比例']}%（稳定哈希取模）",
                       "冷却截止时间": None}
            else:
                cutoff = (event_time + timedelta(seconds=candidate["冷却时间秒"])).isoformat()
                row = {**base, "预演决策": "进入名单", "是否进入正式发送批次": True,
                       "排除原因": "",
                       "冷却截止时间": cutoff}
            roster.append(row)

        roster.sort(key=lambda row: (str(row["事件时间"]), str(row["规则编号"]), str(row["用户编号"])))
        return roster, conflict_traces

    # ---------- 完整业务凭证与版本 ----------
    def _build_credential(
        self,
        snapshot: dict[str, Any],
        events: list[dict[str, Any]],
        rules: list[dict[str, Any]],
        policy: str,
    ) -> dict[str, Any]:
        history_digest = [
            {
                "用户编号": row.get("用户编号"),
                "推送渠道": row.get("推送渠道"),
                "发送时间": row.get("发送时间"),
            }
            for row in sorted(
                store.rows("send_history"),
                key=lambda item: (str(item.get("用户编号")), str(item.get("发送时间"))),
            )
        ]
        return {
            "目标人群快照": {
                "快照编号": snapshot.get("快照编号"),
                "目标人群": snapshot.get("目标人群"),
                "推送渠道": snapshot.get("推送渠道"),
                "用户列表": sorted(snapshot.get("用户列表", [])),
                "快照时间": snapshot.get("快照时间"),
            },
            "触发事件": [
                {"事件编号": row.get("事件编号"), "用户编号": row.get("用户编号"),
                 "事件名称": row.get("事件名称"), "事件时间": row.get("事件时间")}
                for row in events
            ],
            "决策规则": [
                {"规则编号": row.get("规则编号"), "触发事件": row.get("触发事件"),
                 "冷却时间秒": row.get("冷却时间秒"), "优先级": row.get("优先级"),
                 "分流比例": row.get("分流比例"), "version": row.get("version", 1),
                 "status": row.get("status")}
                for row in sorted(rules, key=lambda item: str(item.get("规则编号")))
            ],
            "频控口径": {"单日渠道上限": DAILY_CHANNEL_LIMIT},
            "历史发送摘要": history_digest,
            "冷却冲突裁决策略": policy,
        }

    # ---------- 批次创建（幂等 + 竞态去重） ----------
    def create_batch(
        self,
        *,
        snapshot_id: int,
        event_ids: list[int],
        realtime_ids: list[int] | None = None,
        conflict_policy: str = DEFAULT_CONFLICT_POLICY,
        operator: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        policy = conflict_policy if conflict_policy in CONFLICT_POLICIES else DEFAULT_CONFLICT_POLICY
        with store.lock:
            snapshot = self.get_snapshot(snapshot_id)
            if snapshot is None:
                return None, f"目标人群快照 {snapshot_id} 不存在"
            events = self.list_trigger_events(event_ids)
            if not events:
                return None, "未读取到任何触发事件，无法生成推送预演名单"
            unknown = set(event_ids) - {int(row["id"]) for row in events}
            if unknown:
                return None, f"触发事件不存在：{sorted(unknown)}"
            rules = self._active_rules(realtime_ids)
            if not rules:
                return None, "没有可用的实时决策规则（草稿/评审中/生效/暂停），请检查规则选择"

            credential = self._build_credential(snapshot, events, rules, policy)
            version_hash = _sha(credential)

            # 竞态：同一完整业务凭证只留存一个推送预演名单明细数据集
            existed = store.find_by("rehearsal_batch", "版本凭证哈希", version_hash)
            if existed is not None:
                return existed, "相同完整业务凭证的预演批次已存在，直接复用既有数据集（竞态幂等）"

            candidates = self._build_candidates(events, snapshot, rules)
            roster, conflict_traces = self._evaluate(candidates, snapshot, policy)
            for row in roster:
                row["id"] = store.next_id("rehearsal_roster")

            included = [row for row in roster if row["预演决策"] == "进入名单"]
            excluded = [row for row in roster if row["预演决策"] != "进入名单"]
            batch_no = f"REH-{datetime.now().strftime('%Y%m%d')}-{store.next_id('rehearsal_batch'):04d}"
            plan = store.find_by(PUSHPLAN_MODULE, "方案编号", snapshot.get("关联方案编号"))
            batch = {
                "id": store.next_id("rehearsal_batch"),
                "预演批次号": batch_no,
                "版本凭证哈希": version_hash,
                "完整业务凭证": credential,
                "快照编号": snapshot.get("快照编号"),
                "关联方案编号": snapshot.get("关联方案编号"),
                "推送计划id": plan.get("id") if plan else None,
                "推送渠道": snapshot.get("推送渠道"),
                "裁决策略": policy,
                "裁决策略说明": CONFLICT_POLICIES[policy],
                "批次状态": "待确认",
                "候选条数": len(roster),
                "进入名单数": len(included),
                "频控排除数": sum(1 for row in excluded if row["预演决策"] == "频控排除"),
                "冷却冲突排除数": sum(1 for row in excluded if row["预演决策"] == "冷却冲突排除"),
                "分流排除数": sum(1 for row in excluded if row["预演决策"] == "分流排除"),
                "实际发送次数": 0,  # 预演不得增加实际发送次数
                "冲突裁决留痕": conflict_traces,
                "名单行id": [row["id"] for row in roster],
                "创建人": operator,
                "创建时间": datetime.now().isoformat(timespec="seconds"),
                "确认时间": None,
            }
            store.insert("rehearsal_batch", batch)
            for row in roster:
                row["预演批次id"] = batch["id"]
                store.rows("rehearsal_roster").append(row)
            self._audit("rehearsal_batch", batch_no, "生成预演批次",
                        None, {"批次状态": "待确认", "版本凭证哈希": version_hash}, operator,
                        f"读取{len(events)}个触发事件与快照{snapshot.get('快照编号')}，预演不产生发送")
            return batch, "推送预演名单明细数据集已生成，等待与推送计划一并确认"

    # ---------- 预演名单与推送计划状态一并确认 ----------
    def confirm_batch(self, batch_id: int, operator: str = "值班管理员") -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            batch = store.find("rehearsal_batch", batch_id)
            if batch is None:
                return None, f"预演批次 {batch_id} 不存在"
            if batch["批次状态"] != "待确认":
                return None, f"预演批次当前为「{batch['批次状态']}」，仅待确认批次可确认"
            plan = store.find(PUSHPLAN_MODULE, int(batch["推送计划id"])) if batch.get("推送计划id") else None
            if plan is None:
                return None, "预演批次未关联推送计划，无法一并确认"
            if plan["status"] in ("已完成", "已撤回"):
                return None, f"推送计划「{plan['方案编号']}」状态为{plan['status']}，不能再确认预演名单"

            roster = self.get_roster(batch_id)
            # 双保险：任何频控超限用户不能进入正式发送批次
            leaked = [row for row in roster if row["是否进入正式发送批次"] and row["预演决策"] != "进入名单"]
            if leaked:
                return None, "存在频控超限用户被标记进入正式发送批次，确认已拦截"

            batch["批次状态"] = "已确认"
            batch["确认时间"] = datetime.now().isoformat(timespec="seconds")
            # 预演名单交由推送计划待处理明细保存
            plan["待处理预演批次id"] = batch["id"]
            plan["待处理预演批次号"] = batch["预演批次号"]
            plan["待处理明细"] = [
                {
                    "名单行id": row["id"], "用户编号": row["用户编号"], "规则编号": row["规则编号"],
                    "触发事件": row["触发事件"], "预演决策": row["预演决策"],
                    "是否进入正式发送批次": row["是否进入正式发送批次"],
                    "冷却截止时间": row["冷却截止时间"], "排除原因": row["排除原因"],
                }
                for row in roster
            ]
            plan["预计触达量"] = batch["进入名单数"]
            plan["pending"] = True
            self._materialize_actions(batch, roster, operator)
            self._audit("rehearsal_batch", batch["预演批次号"], "确认预演批次",
                        {"批次状态": "待确认"}, {"批次状态": "已确认"}, operator,
                        f"与推送计划{plan['方案编号']}一并确认，正式发送批次仅含{batch['进入名单数']}名通过频控用户")
            return batch, f"预演名单已确认并保存为推送计划{plan['方案编号']}的待处理明细，实时决策动作已按同批次生成"

    def _materialize_actions(self, batch: dict[str, Any], roster: list[dict[str, Any]], operator: str) -> None:
        """实时决策结果只读取同一预演批次并据此生成发送动作。"""
        for row in roster:
            if not row["是否进入正式发送批次"]:
                continue
            action = {
                "id": store.next_id("decision_action"),
                "预演批次id": batch["id"],
                "预演批次号": batch["预演批次号"],
                "版本凭证哈希": batch["版本凭证哈希"],
                "用户编号": row["用户编号"],
                "规则编号": row["规则编号"],
                "触发事件": row["触发事件"],
                "事件编号": row["事件编号"],
                "执行动作": row["执行动作"],
                "推送渠道": row["推送渠道"],
                "冷却截止时间": row["冷却截止时间"],
                "动作状态": "待执行",
                "来源名单行id": row["id"],
                "创建时间": datetime.now().isoformat(timespec="seconds"),
            }
            store.insert("decision_action", action)

    # ---------- 查询 ----------
    def list_batches(self, status: str | None = None, plan_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows("rehearsal_batch")
        if status:
            rows = [row for row in rows if row.get("批次状态") == status]
        if plan_id is not None:
            rows = [row for row in rows if row.get("推送计划id") == plan_id]
        return sorted(rows, key=lambda row: -int(row.get("id", 0)))

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find("rehearsal_batch", batch_id)

    def get_roster(self, batch_id: int) -> list[dict[str, Any]]:
        batch = self.get_batch(batch_id)
        if batch is None:
            return []
        wanted = set(batch.get("名单行id", []))
        rows = [row for row in store.rows("rehearsal_roster") if int(row.get("id", 0)) in wanted]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def list_actions(self, batch_id: int | None = None, rule_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows("decision_action")
        if batch_id is not None:
            rows = [row for row in rows if row.get("预演批次id") == batch_id]
        if rule_code:
            rows = [row for row in rows if row.get("规则编号") == rule_code]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    # ---------- 正式发送：只跑已确认批次，预演不发送 ----------
    def send_batch(self, batch_id: int, operator: str = "值班管理员") -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            batch = self.get_batch(batch_id)
            if batch is None:
                return None, f"预演批次 {batch_id} 不存在"
            if batch["批次状态"] != "已确认":
                return None, "仅已确认的预演批次可启动正式发送"
            actions = self.list_actions(batch_id=batch_id)
            pending = [row for row in actions if row["动作状态"] == "待执行"]
            if not pending:
                return None, "该批次没有待执行的发送动作"

            sent = 0
            for action in pending:
                action["动作状态"] = "已执行"
                action["执行时间"] = datetime.now().isoformat(timespec="seconds")
                result = {
                    "id": store.next_id("reach_result"),
                    "预演批次id": batch_id,
                    "预演批次号": batch["预演批次号"],
                    "版本凭证哈希": batch["版本凭证哈希"],
                    "用户编号": action["用户编号"],
                    "规则编号": action["规则编号"],
                    "推送渠道": action["推送渠道"],
                    "执行动作": action["执行动作"],
                    "触达结果": "成功",
                    "触达时间": action["执行时间"],
                    "来源动作id": action["id"],
                }
                store.insert("reach_result", result)
                store.insert("send_history", {
                    "id": store.next_id("send_history"),
                    "用户编号": action["用户编号"],
                    "推送渠道": action["推送渠道"],
                    "发送时间": action["执行时间"],
                    "关联方案编号": batch["关联方案编号"],
                    "触达结果": "成功",
                })
                sent += 1
            batch["实际发送次数"] = int(batch.get("实际发送次数", 0)) + sent

            plan = store.find(PUSHPLAN_MODULE, int(batch["推送计划id"])) if batch.get("推送计划id") else None
            if plan is not None:
                plan["status"] = "已完成"
                plan["pending"] = False
                plan["完成时间"] = datetime.now().isoformat(timespec="seconds")
            self._audit("rehearsal_batch", batch["预演批次号"], "正式发送",
                        None, {"实际发送次数": sent}, operator,
                        "正式发送批次严格等于预演名单中通过频控的用户")
            return batch, f"正式发送完成：{sent} 条动作已执行并回写触达结果，频控超限用户未进入批次"

    def list_reach_results(self, batch_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows("reach_result")
        if batch_id is not None:
            rows = [row for row in rows if row.get("预演批次id") == batch_id]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    # ---------- 留痕 ----------
    def _audit(
        self,
        module: str,
        target_code: str,
        action: str,
        before: Any,
        after: Any,
        operator: str,
        remark: str = "",
    ) -> dict[str, Any]:
        entry = {
            "id": store.next_id("audit_log"),
            "模块": module,
            "对象编号": target_code,
            "动作": action,
            "变更前": before,
            "变更后": after,
            "操作人": operator,
            "操作时间": datetime.now().isoformat(timespec="seconds"),
            "备注": remark,
        }
        store.insert("audit_log", entry)
        return entry

    def list_audit_logs(self, module: str | None = None, target_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows("audit_log")
        if module:
            rows = [row for row in rows if row.get("模块") == module]
        if target_code:
            rows = [row for row in rows if row.get("对象编号") == target_code]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))


rehearsal_service = RehearsalService()
