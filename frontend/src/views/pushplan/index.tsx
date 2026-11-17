import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'
import Drawer from '@/components/Drawer'

const ENDPOINT = '/api/pushplan'
const columns = [
  "方案编号",
  "推送内容",
  "目标人群",
  "推送渠道",
  "推送时间",
  "频控策略",
  "AB分组标识",
  "预计触达量"
]
const actions = [
  "提交方案审批",
  "启动推送",
  "撤回推送"
]
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

export default function PushplanView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  // 清单回看抽屉
  const [detailPlan, setDetailPlan] = useState<Row | null>(null)
  const [detail, setDetail] = useState<Dict | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const payload = await getJson(`${ENDPOINT}?${query}`)
      setRows((payload.items as Row[]) ?? [])
      setTotal((payload.total as number) ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '推送计划列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      await postJson(`${ENDPOINT}/${row.id}/actions`, { values: { action } })
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '推送计划操作失败')
    }
  }

  // 转到清单回看关联内容（核验码16703310）
  async function openPendingDetail(row: Row) {
    setErrorMessage('')
    try {
      const payload = await getJson(`${ENDPOINT}/${row.id}/pending-detail`)
      setDetailPlan(row)
      setDetail(payload)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '待处理明细读取失败')
    }
  }

  const rosterRows = ((detail?.['名单明细'] as Dict[]) ?? [])
  const actionRows = ((detail?.['决策动作'] as Dict[]) ?? [])
  const reachRows = ((detail?.['触达结果'] as Dict[]) ?? [])
  const batch = detail?.['预演批次'] as Dict | null

  return (
    <section className="page" data-module="pushplan">
      <header className="page-head">
        <div>
          <h2>推送计划管理</h2>
          <p className="page-desc">
            管理运营消息的推送计划。预演名单确认后保存为待处理明细，实时决策只读取同一预演批次生成发送动作，
            可在此回看名单、动作与触达结果。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出推送计划清单
          </button>
        </div>
      </header>

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
            <th>预演批次</th>
            <th>触达结果数</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>
                {row['待处理预演批次号'] ? (
                  <>
                    <span className="badge confirmed">{String(row['待处理预演批次号'])}</span>
                    <div className="pick-meta">关联批次 {String(row['关联预演批次数'] ?? 0)} 个</div>
                  </>
                ) : (
                  <span className="pick-meta">暂无预演（{String(row['关联预演批次数'] ?? 0)}）</span>
                )}
              </td>
              <td>{String(row['触达结果数'] ?? 0)}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openPendingDetail(row)}>回看预演明细</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无推送计划数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条推送计划记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {/* 清单回看：预演名单待处理明细 + 决策动作 + 触达结果 */}
      {detailPlan && (
        <Drawer
          title={`清单回看 · ${String(detailPlan['方案编号'])}`}
          onClose={() => { setDetailPlan(null); setDetail(null) }}
          footer={<button className="btn" type="button" onClick={() => void openPendingDetail(detailPlan)}>刷新关联内容</button>}
        >
          {!batch ? (
            <p className="empty-state">该推送计划尚无已确认的预演批次，请先到「实时决策」页完成发送前频控预演并确认。</p>
          ) : (
            <>
              <div className="kv-grid">
                <div className="kv-item"><span>预演批次号</span><strong>{String(batch['预演批次号'])}</strong></div>
                <div className="kv-item"><span>批次状态</span><strong>{String(batch['批次状态'])}</strong></div>
                <div className="kv-item"><span>推送计划状态</span><strong>{String(detail?.['推送计划状态'])}</strong></div>
                <div className="kv-item"><span>进入名单/候选</span><strong>{Number(batch['进入名单数'])} / {Number(batch['候选条数'])}</strong></div>
                <div className="kv-item"><span>实际发送次数</span><strong>{Number(batch['实际发送次数'])}</strong></div>
                <div className="kv-item"><span>触达结果条数</span><strong>{reachRows.length}</strong></div>
              </div>
              <div className="hash-text">版本凭证哈希：{String(batch['版本凭证哈希'])}</div>

              <div className="section-title">待处理预演名单明细（保存于推送计划）</div>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>用户</th><th>规则</th><th>触发事件</th><th>预演决策</th>
                    <th>进入正式批次</th><th>冷却截止时间</th><th>排除原因</th>
                  </tr>
                </thead>
                <tbody>
                  {rosterRows.map((item) => (
                    <tr key={String(item.id)}>
                      <td>{String(item['用户编号'])}</td>
                      <td>{String(item['规则编号'])}</td>
                      <td>{String(item['触发事件'])}</td>
                      <td><span className={`badge ${decisionBadge[String(item['预演决策'])] ?? ''}`}>{String(item['预演决策'])}</span></td>
                      <td>{item['是否进入正式发送批次'] ? '是' : '否'}</td>
                      <td>{String(item['冷却截止时间'] ?? '—')}</td>
                      <td>{String(item['排除原因'] || '—')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <div className="section-title">实时决策发送动作（只读取同一预演批次：{String(batch['预演批次号'])}）</div>
              <table className="data-table">
                <thead>
                  <tr><th>用户</th><th>规则</th><th>执行动作</th><th>渠道</th><th>动作状态</th><th>冷却截止时间</th></tr>
                </thead>
                <tbody>
                  {actionRows.map((item) => (
                    <tr key={String(item.id)}>
                      <td>{String(item['用户编号'])}</td>
                      <td>{String(item['规则编号'])}</td>
                      <td>{String(item['执行动作'])}</td>
                      <td>{String(item['推送渠道'])}</td>
                      <td>{String(item['动作状态'])}</td>
                      <td>{String(item['冷却截止时间'] ?? '—')}</td>
                    </tr>
                  ))}
                  {actionRows.length === 0 && <tr><td className="empty-state" colSpan={6}>暂无发送动作</td></tr>}
                </tbody>
              </table>

              <div className="section-title">触达结果核对</div>
              <table className="data-table">
                <thead>
                  <tr><th>用户</th><th>规则</th><th>渠道</th><th>触达结果</th><th>触达时间</th></tr>
                </thead>
                <tbody>
                  {reachRows.map((item) => (
                    <tr key={String(item.id)}>
                      <td>{String(item['用户编号'])}</td>
                      <td>{String(item['规则编号'])}</td>
                      <td>{String(item['推送渠道'])}</td>
                      <td><span className="badge include">{String(item['触达结果'])}</span></td>
                      <td>{String(item['触达时间'] ?? '—')}</td>
                    </tr>
                  ))}
                  {reachRows.length === 0 && <tr><td className="empty-state" colSpan={5}>正式发送后回写触达结果</td></tr>}
                </tbody>
              </table>
            </>
          )}
        </Drawer>
      )}
    </section>
  )
}
