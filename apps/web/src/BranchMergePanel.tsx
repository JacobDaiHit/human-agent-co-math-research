import { useState } from 'react'
import { post, request } from './api'
import type { Branch, Snapshot } from './api'
import { kindLabels, MathText, Modal } from './ui'

type MergeItem = {
  object_id: string
  kind: string
  status: string
  source_body: string | null
  target_body: string | null
  ancestor_body: string | null
  source_revision_id: string
  target_revision_id: string
  common_ancestor_revision_id: string | null
  semantic_review_required: boolean
}
type Preview = { items: MergeItem[]; preview_token: string }
type Resolution = { choice: 'source' | 'target' | 'rewrite'; body: string; reason: string }

const choiceLabels: Record<Resolution['choice'], string> = {
  source: '采用来源分支', target: '保留目标分支', rewrite: '改写后合并',
}

function shortBody(body: string | null) {
  const value = (body || '无正文').trim().split('\n').map(line => line.replace(/^#{1,6}\s+/, '')).join('\n')
  return value.length > 420 ? `${value.slice(0, 420)}…` : value
}

export function BranchMergePanel({ snapshot, branches, onRefresh }:{
  snapshot: Snapshot
  branches: Branch[]
  onRefresh: () => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [sourceBranchId, setSourceBranchId] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [resolutions, setResolutions] = useState<Record<string, Resolution>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const candidates = branches.filter(branch => branch.id !== snapshot.branch.id)

  function chooseSource(value: string) {
    setSourceBranchId(value); setPreview(null); setResolutions({}); setError(''); setDone(false)
  }
  async function loadPreview() {
    if (!sourceBranchId) { setError('请先选择来源分支。'); return }
    setBusy(true); setError(''); setDone(false); setPreview(null); setResolutions({})
    try {
      const value = await request<Preview>(`/branches/${snapshot.branch.id}/merge-preview?source_branch_id=${encodeURIComponent(sourceBranchId)}`)
      setPreview({ ...value, items: value.items.filter(item => !['unchanged', 'target_only'].includes(item.status)) })
    } catch (value) {
      setError(value instanceof Error ? value.message : '预览失败，请重新预览。')
    } finally { setBusy(false) }
  }
  const update = (id: string, change: Partial<Resolution>) => setResolutions(current => {
    const prior = current[id] || { choice: 'source' as const, body: '', reason: '' }
    return { ...current, [id]: { ...prior, ...change } }
  })
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!preview) { setError('请先生成合并预览。'); return }
    const selected = preview.items.map(item => ({ item, resolution: resolutions[item.object_id] }))
    if (selected.some(({ resolution }) => !resolution?.reason.trim())) { setError('每项合并决议都必须填写理由。'); return }
    if (selected.some(({ resolution }) => resolution?.choice === 'rewrite' && !resolution.body.trim())) { setError('选择改写时必须填写改写正文。'); return }
    setBusy(true); setError(''); setDone(false)
    try {
      await post(`/branches/${snapshot.branch.id}/merge`, {
        source_branch_id: sourceBranchId,
        preview_token: preview.preview_token,
        resolutions: selected.map(({ item, resolution }) => ({
          object_id: item.object_id, choice: resolution!.choice,
          ...(resolution!.choice === 'rewrite' ? { body: resolution!.body } : {}),
          reason: resolution!.reason,
        })),
      })
      await onRefresh(); setDone(true); setPreview(null); setResolutions({})
    } catch (value) {
      const message = value instanceof Error ? value.message : '合并提交失败。'
      setError(message.includes('过期') || message.includes('409') ? '预览已过期，请重新预览；未提交旧决议。' : message)
    } finally { setBusy(false) }
  }

  if (!open) return <button onClick={() => { setOpen(true); setError('') }}>比较与合并</button>
  return <Modal title="比较与合并" onClose={() => setOpen(false)}>
    <form onSubmit={submit}>
      {error && <p className="inline-error" role="alert">{error}</p>}
      {done && <p className="detail-note" role="status">合并已保存。定义和假设需逐项核对；旧审查不会转移到改写版本。</p>}
      <label className="field-label">来源分支
        <select value={sourceBranchId} onChange={event => chooseSource(event.target.value)} disabled={busy} required>
          <option value="">请选择同项目的其它分支</option>
          {candidates.map(branch => <option key={branch.id} value={branch.id}>{branch.name}</option>)}
        </select>
      </label>
      <div className="button-row"><button type="button" onClick={loadPreview} disabled={busy || !sourceBranchId}>{busy ? '正在处理…' : '生成合并预览'}</button></div>
      {preview && <>
        <p className="subtle">仅显示需要处理的项目。定义和假设需逐项核对；旧审查不会转移到改写版本。</p>
        {preview.items.length === 0 && <p className="subtle">没有需要决议的差异。</p>}
        {preview.items.map(item => { const resolution = resolutions[item.object_id] || { choice: 'source' as const, body: '', reason: '' }; return <fieldset className="choice-list" key={item.object_id}>
          <legend>{kindLabels[item.kind] || item.kind} · {item.status}</legend>
          <div className="form-grid">{([['来源版本',item.source_body,item.source_revision_id],['目标版本',item.target_body,item.target_revision_id],['共同祖先',item.ancestor_body,item.common_ancestor_revision_id]] as [string,string|null,string|null][]).map(([label,body,revisionId]) => <div key={label}><small>{label} · {revisionId?.slice(0,8) || '无版本'}</small><MathText body={shortBody(body)} /><details><summary>查看全文</summary><MathText body={body || '无正文'} /></details></div>)}</div>
          {item.semantic_review_required && <p className="inline-warning">需要语义核对后再决定。</p>}
          <div className="choice-list"><span className="field-label">合并决议</span>{(Object.keys(choiceLabels) as Resolution['choice'][]).map(choice => <label className="checkbox-label" key={choice}><input type="radio" name={`choice-${item.object_id}`} checked={resolution.choice === choice} onChange={() => update(item.object_id, { choice })} />{choiceLabels[choice]}</label>)}</div>
          {resolution.choice === 'rewrite' && <label className="field-label">改写正文<textarea rows={4} value={resolution.body} onChange={event => update(item.object_id, { body: event.target.value })} required /></label>}
          <label className="field-label">决议理由<textarea rows={2} value={resolution.reason} onChange={event => update(item.object_id, { reason: event.target.value })} required placeholder="说明为何采用该版本或如何改写" /></label>
        </fieldset> })}
        <div className="modal-actions"><button type="button" onClick={() => setOpen(false)}>取消</button><button className="primary" disabled={busy || preview.items.length === 0}>{busy ? '正在提交…' : '提交合并'}</button></div>
      </>}
      {!preview && <div className="modal-actions"><button type="button" onClick={() => setOpen(false)}>关闭</button></div>}
    </form>
  </Modal>
}
