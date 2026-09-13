import { useState } from 'react'
import type { Snapshot } from './api'
import { post, request } from './api'
import { kindLabels, MathInputHint, MathPreview, MathText, Modal } from './ui'

type ArticleSection = {
  heading: string
  revision_ids: string[]
  transition: string
  enabled: boolean
}

type ArticleResponse = {
  object_id?: string
  revision_id?: string
  issues?: { code: string; message: string }[]
}

const sectionDefaults: ArticleSection[] = [
  { heading: '问题与假设', revision_ids: [], transition: '', enabled: true },
  { heading: '主要论证', revision_ids: [], transition: '', enabled: true },
  { heading: '结论与未决事项', revision_ids: [], transition: '', enabled: true },
]

function preview(body: string) {
  const safe = body.trim().split('\n').map(line => line.replace(/^#{1,6}\s+/, '')).join('\n')
  return safe.length > 220 ? `${safe.slice(0, 220)}…` : safe
}

export function ArticleComposer({ snapshot, onRefresh, onSelect }:{
  snapshot: Snapshot
  onRefresh: () => Promise<void>
  onSelect: (id: string) => void
}) {
  const [title, setTitle] = useState('')
  const [sections, setSections] = useState<ArticleSection[]>(sectionDefaults)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<ArticleResponse | null>(null)
  const [open, setOpen] = useState(false)

  const updateSection = (index: number, change: Partial<ArticleSection>) => {
    setSections(current => current.map((section, i) => i === index ? { ...section, ...change } : section))
  }
  const toggleRevision = (index: number, revisionId: string) => {
    const section = sections[index]
    const revision_ids = section.revision_ids.includes(revisionId)
      ? section.revision_ids.filter(id => id !== revisionId)
      : [...section.revision_ids, revisionId]
    setSections(current => current.map((item, i) => i === index
      ? { ...item, revision_ids }
      : { ...item, revision_ids: item.revision_ids.filter(id => id !== revisionId) }))
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setResult(null)
    try {
      const payload = {
        branch_id: snapshot.branch.id,
        title: title.trim(),
        sections: sections.filter(section => section.enabled && section.revision_ids.length > 0).map(({ heading, revision_ids, transition }) => ({
          heading, revision_ids, transition,
        })),
      }
      if (!payload.title) throw new Error('请填写文章标题。')
      if (payload.sections.length === 0) {
        throw new Error('请至少为一个文章分组选择材料。')
      }
      const created = await post<ArticleResponse>('/articles', payload)
      let checked = created
      if (created.revision_id) {
        try {
          const check = await request<{issues?: {code: string; message: string}[]}>(`/articles/${encodeURIComponent(created.revision_id)}/check?branch_id=${encodeURIComponent(snapshot.branch.id)}`)
          checked = {...created, issues: [...(created.issues||[]), ...(check.issues||[])]}
        } catch {
          checked = {...created, issues: [...(created.issues||[]), {code:'article_check_unavailable', message:'文章检查暂时不可用，请稍后重新检查。'}]}
        }
      }
      setResult({...checked, issues: Array.from(new Map((checked.issues||[]).map(issue => [`${issue.code}:${issue.message}`, issue])).values())})
      await onRefresh()
      if (created.object_id) onSelect(created.object_id)
    } catch (value) {
      setError(value instanceof Error ? value.message : '文章保存失败。')
    } finally {
      setBusy(false)
    }
  }

  if (!open) return <button type="button" onClick={() => { setOpen(true); setError(''); setResult(null) }}>整理文章</button>
  if (result) { const saved = result; return <Modal title="文章检查结果" onClose={() => setOpen(false)}><div className="detail-note" role="status"><p>文章草稿已保存，仍需对整篇文章独立审查；节点审查不能代替全文通过。</p>{saved.issues && saved.issues.length > 0 && <><strong>检查问题</strong><ul>{saved.issues.map(issue => <li key={`${issue.code}:${issue.message}`}>{issue.message}</li>)}</ul></>}</div><div className="modal-actions"><button type="button" onClick={() => { setOpen(false); if (saved.object_id) onSelect(saved.object_id) }}>查看文章</button><button type="button" onClick={() => setOpen(false)}>关闭</button></div></Modal> }
  return <Modal title="整理文章" onClose={() => setOpen(false)}>
    <form onSubmit={submit}>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <label className="field-label">文章标题
        <input autoFocus value={title} onChange={event => setTitle(event.target.value)} required maxLength={200} placeholder="例如：关于该问题的研究记录" />
      </label>
      <p className="subtle">勾选要纳入文章的当前版本材料；保存后文章作为未采用草稿写入当前分支。</p>
      {sections.map((section, index) => <fieldset className="choice-list" key={section.heading}>
        <legend><label className="checkbox-label"><input type="checkbox" checked={section.enabled} onChange={event => updateSection(index, { enabled: event.target.checked })} />{section.heading}</label></legend>
        {section.enabled && <>
          <label className="field-label">衔接说明
            <textarea rows={2} value={section.transition} onChange={event => updateSection(index, { transition: event.target.value })} placeholder="说明本组与前后部分如何衔接（可留空）" />
          </label>
          <div aria-label={`${section.heading}的材料`}>
            {snapshot.objects.filter(object => object.id !== snapshot.project.original_goal_id && object.revision.payload.deleted !== true).map(object => <label className="checkbox-label" key={object.revision.id}>
              <input type="checkbox" checked={section.revision_ids.includes(object.revision.id)} onChange={() => toggleRevision(index, object.revision.id)} />
              <span><small>{kindLabels[object.kind] || object.kind} · {object.revision.id.slice(0, 8)}</small><MathPreview body={preview(object.revision.body)} /></span>
            </label>)}
          </div>
        </>}
      </fieldset>)}
      <MathInputHint />
      <div className="modal-actions">
        <button type="button" onClick={() => setOpen(false)}>取消</button>
        <button className="primary" disabled={busy}>{busy ? '正在保存…' : '保存文章草稿'}</button>
      </div>
    </form>
  </Modal>
}
