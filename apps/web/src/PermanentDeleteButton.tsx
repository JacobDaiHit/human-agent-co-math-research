import { useRef, useState } from 'react'
import { request } from './api'
import { Modal } from './ui'

type DeletePreview = {
  preview_token: string
  object_count: number
  revision_count: number
  warnings: string[]
}
type DeleteResult = { deleted: boolean; storage_cleanup_pending: boolean; warnings: string[] }

export function PermanentDeleteButton({ objectId, onDeleted }:{
  objectId: string
  onDeleted: () => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [preview, setPreview] = useState<DeletePreview | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [result, setResult] = useState<DeleteResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const operationKey = useRef<string | null>(null)
  const deletePayload = useRef<{preview_token:string;confirmation:'永久删除'} | null>(null)

  async function close() {
    setOpen(false)
    if (result?.deleted) await onDeleted()
  }
  async function openPreview() {
    setOpen(true); setPreview(null); setResult(null); setConfirmation(''); setError(''); setBusy(true)
    operationKey.current = null; deletePayload.current = null
    try {
      setPreview(await request<DeletePreview>(`/objects/${encodeURIComponent(objectId)}/deletion-preview`))
    } catch (value) {
      setError(value instanceof Error ? value.message : '无法读取删除预览。')
    } finally { setBusy(false) }
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!preview) { setError('预览已失效，请重新加载。'); return }
    if (confirmation !== '永久删除') { setError('请输入“永久删除”后再提交。'); return }
    setBusy(true); setError('')
    try {
      const payload = deletePayload.current || { preview_token: preview.preview_token, confirmation: '永久删除' as const }
      deletePayload.current = payload
      operationKey.current ||= crypto.randomUUID()
      const value = await request<DeleteResult>(`/objects/${encodeURIComponent(objectId)}/permanent-delete`, {
        method: 'POST', body: JSON.stringify(payload), headers: { 'Idempotency-Key': operationKey.current },
      })
      setResult(value)
    } catch (value) {
      const message = value instanceof Error ? value.message : '删除失败。'
      setError(message.includes('过期') || message.includes('409') ? '删除预览已过期，请重新加载；未重复提交删除。' : message)
    } finally { setBusy(false) }
  }
  async function retryCleanup() {
    if (!deletePayload.current || !operationKey.current || !result?.storage_cleanup_pending) return
    setBusy(true); setError('')
    try {
      const value = await request<DeleteResult>(`/objects/${encodeURIComponent(objectId)}/permanent-delete`, {
        method: 'POST', body: JSON.stringify(deletePayload.current), headers: { 'Idempotency-Key': operationKey.current },
      })
      setResult(value)
    } catch (value) {
      setError(value instanceof Error ? value.message : '存储清理重试失败。')
    } finally { setBusy(false) }
  }

  return <>
    <button type="button" onClick={openPreview}>永久删除</button>
    {open && <Modal title="永久删除" onClose={() => { void close() }}>
      <form onSubmit={submit}>
        {error && <p className="inline-error" role="alert">{error}</p>}
        {!result && <>
          <p className="inline-warning">此操作不可恢复，会清除关联副本及项目运行文本日志；保留无正文的引用占位与费用账本。已下载副本和外部备份不受影响。</p>
          {busy && !preview && <p className="subtle" role="status">正在读取删除预览…</p>}
          {preview && <>
            <p>将删除 {preview.object_count} 个对象、{preview.revision_count} 个版本。</p>
            {preview.warnings.length > 0 && <div className="detail-note"><strong>预览提示</strong><ul>{preview.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul></div>}
            <label className="field-label">请输入“永久删除”确认
              <input autoFocus value={confirmation} onChange={event => setConfirmation(event.target.value)} placeholder="永久删除" autoComplete="off" />
            </label>
          </>}
          <div className="modal-actions"><button type="button" onClick={() => { void close() }}>取消</button><button className="primary" disabled={busy || !preview || confirmation !== '永久删除'}>确认永久删除</button></div>
        </>}
        {result && <>
          <p className="detail-note" role="status">{result.deleted ? '删除已完成。' : '删除未完成。'}{result.storage_cleanup_pending && ' 部分存储清理未完成，需要重试或人工处理。'}</p>
          {result.warnings.length > 0 && <div className="detail-note"><strong>处理提示</strong><ul>{result.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul></div>}
          {result.storage_cleanup_pending && <div className="button-row"><button type="button" onClick={retryCleanup} disabled={busy}>重试存储清理</button><p className="subtle">复用本次删除的固定范围与幂等回执，不重新确认或扩大删除范围。</p></div>}
          <div className="modal-actions"><button type="button" onClick={() => { void close() }}>关闭</button></div>
        </>}
      </form>
    </Modal>}
  </>
}
