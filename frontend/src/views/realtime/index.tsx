import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import Drawer from '@/components/Drawer'

const ENDPOINT = '/api/realtime'
const REHEARSAL = '/api/rehearsal'
const columns = [
  "规则编号",
  "触发事件",
  "决策条件",
  "执行动作",
  "冷却时间秒",
  "优先级",
  "分流比例",
  "生效时段"
]
const actions = [
  "提交规则评审",
  "生效规则",
  "暂停规则",
  "归档规则",
  "恢复规则"
]
const statusBadge: Record<string, string> = {
  草稿: 'pending',
  规则评审中: 'pending',
  已生效: 'confirmed',
  已暂停: 'pending',
  已归档: 'archived'
}
const decisionBadge: Record<string, string> = {
  进入名单: 'include',
  频控排除: 'exclude',
  冷却冲突排除: 'exclude',
  分流排除: 'split'
}

type Row = Record<string, string | number | null>
type Dict = Record<string, unknown>

async function getJson(path: string): Promise<Dict> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}`)
  }
  return response.json()
}

async function postJson(path: string, body: Dict = {}): Promise<Dict> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.message ?? '操作未生效')
  }
  return payload
}

export default function RealtimeView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  // 预演工作台状态
  const [events, setEvents] = useState<Dict[]>([])
  const [snapshots, setSnapshots] = useState<Dict[]>([])
  const [rules, setRules] = useState<Dict[]>([])
  const [policies, setPolicies] = useState<Record<string, string>>({})
  const [snapshotId, setSnapshotId] = useState<number | null>(null)
  const [eventIds, setEventIds] = useState<number[]>([])
  const [ruleIds, setRuleIds] = useState<number[]>([])
  const [policy, setPolicy] = useState('HIGHER_PRIORITY')
  const [batches, setBatches] = useState<Dict[]>([])
  const [busy, setBusy] = useState(false)

  // 抽屉状态
  const [rosterBatch, setRosterBatch] = useState<Dict | null>(null)
  const [rosterRows, setRosterRows] = useState<Dict[]>([])
  const [auditRule, setAuditRule] = useState<Row | null>(null)
  const [auditLogs, setAuditLogs] = useState<Dict[]>([])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const payload = await getJson(`${ENDPOINT}?${query}`)
      setRows((payload.items as Row[]) ?? [])
      setTotal((payload.total as number) ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '实时决策列表读取失败')
    }
  }, [filters])

  const loadWorkbench = useCallback(async () => {
    try {
      const [eventPayload, snapshotPayload, rulePayload, batchPayload] = await Promise.all([
        getJson(`${REHEARSAL}/trigger-events`),
        getJson(`${REHEARSAL}/snapshots`),
        getJson(`${REHEARSAL}/realtime-rules`),
        getJson(`${REHEARSAL}/batches?size=100`),
      ])
      setEvents(eventPayload.items as Dict[])
      setSnapshots(snapshotPayload.items as Dict[])
      const ruleItems = (rulePayload.items as Dict[]) ?? []
      setRules(ruleItems)
      setPolicies((rulePayload.conflict_policies as Record<string, string>) ?? {})
      setBatches(batchPayload.items as Dict[])
      const firstSnapshot = (snapshotPayload.items as Dict[])[0]
      setSnapshotId((prev) => prev ?? ((firstSnapshot?.id as number | undefined) ?? null))
      // REAL-0002 作为契约验证样本默认勾选
      const contractRuleIds = ruleItems
        .filter((r) => r['规则编号'] === 'REAL-0002')
        .map((r) => r.id as number)
      setRuleIds((prev) => (prev.length ? prev : contractRuleIds))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '预演工作台数据读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
    void loadWorkbench()
  }, [reload, loadWorkbench])

  // 改动完毕时刷新业务页（核验码1640e167）：所有动作完成后统一重载列表与工作台
  const refreshAll = useCallback(async () => {
    await Promise.all([reload(), loadWorkbench()])
  }, [reload, loadWorkbench])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      await postJson(`${ENDPOINT}/${row.id}/actions`, { values: { action } })
      await refreshAll()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '实时决策操作失败')
    }
  }

  function toggle(list: number[], id: number, setter: (next: number[]) => void) {
    setter(list.includes(id) ? list.filter((item) => item !== id) : [...list, id])
  }

  async function createRehearsal() {
    if (snapshotId === null) {
      setErrorMessage('请先选择目标人群快照')
      return
    }
    if (eventIds.length === 0) {
      setErrorMessage('请至少选择一个触发事件')
      return
    }
    if (ruleIds.length === 0) {
      setErrorMessage('请至少选择一条实时决策规则（REAL-0002 为契约验证样本）')
      return
    }
    setBusy(true)
    setErrorMessage('')
    try {
      const payload = await postJson(`${REHEARSAL}/batches`, {
        snapshot_id: snapshotId,
        event_ids: eventIds,
        realtime_ids: ruleIds,
        conflict_policy: policy,
      })
      await loadWorkbench()
      const batch = payload.entry as Dict
      await openRoster(batch)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '预演批次生成失败')
    } finally {
      setBusy(false)
    }
  }

  async function openRoster(batch: Dict) {
    try {
      const payload = await getJson(`${REHEARSAL}/batches/${batch.id}/roster`)
      setRosterBatch(batch)
      setRosterRows((payload.items as Dict[]) ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '预演名单读取失败')
    }
  }

  async function confirmBatch(batch: Dict) {
    setBusy(true)
    setErrorMessage('')
    try {
      await postJson(`${REHEARSAL}/batches/${batch.id}/confirm`, {})
      await refreshAll()
      const latest = (await getJson(`${REHEARSAL}/batches/${batch.id}`)) as Dict
      await openRoster(latest)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '预演确认失败')
    } finally {
      setBusy(false)
    }
  }

  async function sendBatch(batch: Dict) {
    setBusy(true)
    setErrorMessage('')
    try {
      await postJson(`${REHEARSAL}/batches/${batch.id}/send`, {})
      await refreshAll()
      const latest = (await getJson(`${REHEARSAL}/batches/${batch.id}`)) as Dict
      await openRoster(latest)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '正式发送失败')
    } finally {
      setBusy(false)
    }
  }

  async function openAudit(row: Row) {
    try {
      const payload = await getJson(`${ENDPOINT}/${row.id}/audit-logs`)
      setAuditRule(row)
      setAuditLogs((payload.items as Dict[]) ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '留痕读取失败')
    }
  }

  return (
    <section className="page" data-module="realtime">
      <header className="page-head">
        <div>
          <h2>实时决策管理</h2>
          <p className="page-desc">
            配置实时运营决策规则并执行发送前频控预演：读取触发事件与目标人群快照计算频控名单，
            REAL-0002 为契约验证样本，预演不产生实际发送。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出实时决策清单
          </button>
        </div>
      </header>

      {/* 发送前频控预演工作台 */}
      <div className="workbench">
        <h3>发送前频控预演工作台</h3>
        <div className="workbench-grid">
          <div>
            <span className="stat-label">① 目标人群快照（新流程读取）</span>
            <div className="pick-list">
              {snapshots.map((snap) => (
                <label key={String(snap.id)} className="pick-row">
                  <input
                    type="radio"
                    name="snapshot"
                    checked={snapshotId === snap.id}
                    onChange={() => setSnapshotId(snap.id as number)}
                  />
                  <span>
                    {String(snap['快照编号'])} · {String(snap['快照名称'])}
                    <div className="pick-meta">
                      渠道 {String(snap['推送渠道'])} ｜ 人群 {String(snap['目标人群'])} ｜ {String((snap['用户列表'] as string[]).length)} 人
                    </div>
                  </span>
                </label>
              ))}
            </div>
          </div>

          <div>
            <span className="stat-label">② 触发事件（新流程读取，可多选）</span>
            <div className="pick-list">
              {events.map((evt) => (
                <label key={String(evt.id)} className="pick-row">
                  <input
                    type="checkbox"
                    checked={eventIds.includes(evt.id as number)}
                    onChange={() => toggle(eventIds, evt.id as number, setEventIds)}
                  />
                  <span>
                    {String(evt['事件编号'])} · {String(evt['事件名称'])} · 用户 {String(evt['用户编号'])}
                    <div className="pick-meta">{String(evt['事件时间'])} ｜ {String(evt['事件载荷'])}</div>
                  </span>
                </label>
              ))}
            </div>
          </div>

          <div>
            <span className="stat-label">③ 实时决策规则（REAL-0002 为契约验证样本）</span>
            <div className="pick-list">
              {rules.map((rule) => (
                <label key={String(rule.id)} className="pick-row">
                  <input
                    type="checkbox"
                    checked={ruleIds.includes(rule.id as number)}
                    onChange={() => toggle(ruleIds, rule.id as number, setRuleIds)}
                  />
                  <span>
                    {String(rule['规则编号'])} · {String(rule['触发事件'])}
                    <div className="pick-meta">
                      冷却 {String(rule['冷却时间秒'])} 秒 ｜ 优先级 {String(rule['优先级'])} ｜ 分流 {String(rule['分流比例'])}% ｜ {String(rule['status'])}
                    </div>
                  </span>
                </label>
              ))}
            </div>
          </div>

          <div>
            <span className="stat-label">④ 同一冷却窗口冲突裁决策略（当前窗口只执行一个动作）</span>
            <div className="pick-list">
              {Object.entries(policies).map(([key, label]) => (
                <label key={key} className="pick-row">
                  <input type="radio" name="policy" checked={policy === key} onChange={() => setPolicy(key)} />
                  <span>
                    {key}
                    <div className="pick-meta">{String(label)}</div>
                  </span>
                </label>
              ))}
            </div>
          </div>
        </div>
        <div className="workbench-actions">
          <button className="btn primary" type="button" disabled={busy} onClick={() => void createRehearsal()}>
            生成推送预演名单明细数据集
          </button>
          <span className="pick-meta">版本由完整业务凭证（事件+人群+规则+频控口径+历史发送+裁决策略）哈希确认；相同凭证竞态只留存一个数据集</span>
        </div>
      </div>

      {/* 预演批次列表 */}
      <div className="section-title">推送预演名单明细数据集（{batches.length}）</div>
      <table className="data-table">
        <thead>
          <tr>
            <th>预演批次号</th>
            <th>批次状态</th>
            <th>关联推送计划</th>
            <th>候选/进入/排除</th>
            <th>实际发送次数</th>
            <th>裁决策略</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {batches.map((batch) => (
            <tr key={String(batch.id)}>
              <td>{String(batch['预演批次号'])}</td>
              <td><span className={`badge ${statusBadge[String(batch['批次状态'])] ?? ''}`}>{String(batch['批次状态'])}</span></td>
              <td>{String(batch['关联方案编号'] ?? '—')}</td>
              <td>
                {Number(batch['候选条数'])} / <span className="ok-text">{Number(batch['进入名单数'])}</span> /
                <span className="error-text"> {Number(batch['频控排除数']) + Number(batch['冷却冲突排除数']) + Number(batch['分流排除数'])}</span>
              </td>
              <td>{Number(batch['实际发送次数'])}</td>
              <td>{String(batch['裁决策略说明'])}</td>
              <td>{String(batch['创建时间'])}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openRoster(batch)}>名单明细</button>
                {batch['批次状态'] === '待确认' && (
                  <button className="link" type="button" disabled={busy} onClick={() => void confirmBatch(batch)}>一并确认</button>
                )}
                {batch['批次状态'] === '已确认' && (
                  <button className="link" type="button" disabled={busy} onClick={() => void sendBatch(batch)}>正式发送</button>
                )}
              </td>
            </tr>
          ))}
          {batches.length === 0 && (
            <tr><td className="empty-state" colSpan={8}>尚无预演批次，请在上方工作台生成</td></tr>
          )}
        </tbody>
      </table>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态/版本</th>
            <th>决策动作（结果驱动）</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>
                <span className={`badge ${statusBadge[String(row.status)] ?? ''}`}>{String(row.status)}</span>
                <div className="pick-meta">v{String(row.version ?? 1)}</div>
              </td>
              <td>
                待执行 {String(row['待执行动作数'] ?? 0)} ｜ 已执行 {String(row['已执行动作数'] ?? 0)}
                <div className="pick-meta">累计 {String(row['决策动作总数'] ?? 0)} 个发送动作</div>
              </td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void openAudit(row)}>留痕</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td className="empty-state" colSpan={columns.length + 3}>暂无实时决策数据</td></tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条实时决策记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {/* 预演名单明细抽屉 */}
      {rosterBatch && (
        <Drawer
          title={`预演名单明细 · ${String(rosterBatch['预演批次号'])}`}
          onClose={() => setRosterBatch(null)}
          footer={
            <>
              {rosterBatch['批次状态'] === '待确认' && (
                <button className="btn primary" type="button" disabled={busy} onClick={() => void confirmBatch(rosterBatch)}>
                  预演名单与推送计划一并确认
                </button>
              )}
              {rosterBatch['批次状态'] === '已确认' && (
                <button className="btn primary" type="button" disabled={busy} onClick={() => void sendBatch(rosterBatch)}>
                  按已确认名单正式发送
                </button>
              )}
            </>
          }
        >
          <div className="kv-grid">
            <div className="kv-item"><span>批次状态</span><strong>{String(rosterBatch['批次状态'])}</strong></div>
            <div className="kv-item"><span>关联推送计划</span><strong>{String(rosterBatch['关联方案编号'] ?? '—')}</strong></div>
            <div className="kv-item"><span>推送渠道</span><strong>{String(rosterBatch['推送渠道'])}</strong></div>
            <div className="kv-item"><span>进入名单</span><strong>{Number(rosterBatch['进入名单数'])} 人</strong></div>
            <div className="kv-item"><span>频控/冲突/分流排除</span><strong>
              {Number(rosterBatch['频控排除数'])} / {Number(rosterBatch['冷却冲突排除数'])} / {Number(rosterBatch['分流排除数'])}
            </strong></div>
            <div className="kv-item"><span>预演实际发送次数</span><strong>{Number(rosterBatch['实际发送次数'])}</strong></div>
          </div>
          <div className="hash-text">版本凭证哈希：{String(rosterBatch['版本凭证哈希'])}</div>

          {Array.isArray(rosterBatch['冲突裁决留痕']) && (rosterBatch['冲突裁决留痕'] as Dict[]).length > 0 && (
            <>
              <div className="section-title">同一冷却窗口冲突裁决留痕</div>
              {(rosterBatch['冲突裁决留痕'] as Dict[]).map((trace, idx) => (
                <div key={idx} className="trace-box">
                  用户 {String(trace['用户编号'])}：保留 {String((trace['保留批次行'] as Dict)['规则编号'])}，
                  排除 {String((trace['排除批次行'] as Dict)['规则编号'])}（{String(trace['裁决说明'])}）
                </div>
              ))}
            </>
          )}

          <div className="section-title">名单明细（频控超限用户不会进入正式发送批次）</div>
          <table className="data-table">
            <thead>
              <tr>
                <th>用户</th><th>触发事件</th><th>规则</th><th>预演决策</th>
                <th>进入正式批次</th><th>冷却截止时间</th><th>排除原因</th>
              </tr>
            </thead>
            <tbody>
              {rosterRows.map((item) => (
                <tr key={String(item.id)}>
                  <td>{String(item['用户编号'])}</td>
                  <td>{String(item['触发事件'])}</td>
                  <td>{String(item['规则编号'])}</td>
                  <td><span className={`badge ${decisionBadge[String(item['预演决策'])] ?? ''}`}>{String(item['预演决策'])}</span></td>
                  <td>{item['是否进入正式发送批次'] ? '是' : '否'}</td>
                  <td>{String(item['冷却截止时间'] ?? '—')}</td>
                  <td>{String(item['排除原因'] || '—')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Drawer>
      )}

      {/* 生命周期留痕抽屉 */}
      {auditRule && (
        <Drawer title={`生命周期留痕 · ${String(auditRule['规则编号'])}`} onClose={() => setAuditRule(null)}>
          <ul className="audit-list">
            {auditLogs.map((log) => (
              <li key={String(log.id)}>
                <strong>{String(log['动作'])}</strong>
                <div>变更前：{log['变更前'] ? JSON.stringify(log['变更前']) : '—'}</div>
                <div>变更后：{log['变更后'] ? JSON.stringify(log['变更后']) : '—'}</div>
                {log['备注'] ? <div>备注：{String(log['备注'])}</div> : null}
                <div className="audit-meta">{String(log['操作人'])} ｜ {String(log['操作时间'])}</div>
              </li>
            ))}
            {auditLogs.length === 0 && <li className="empty-state">暂无留痕</li>}
          </ul>
        </Drawer>
      )}
    </section>
  )
}
