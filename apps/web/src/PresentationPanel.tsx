import { useState } from 'react'
import type { Presentation, Snapshot } from './api'
import { ApiError, request } from './api'
import { MathPreview, Modal } from './ui'

export function PresentationPanel({snapshot,selected,onRefresh,onNotice}:{snapshot:Snapshot;selected:string|null;onRefresh:()=>Promise<void>;onNotice:(message:string)=>void}){
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const display=snapshot.presentation||{version:0,hidden_object_ids:[],collapsed_object_ids:[],archived:false}
  async function save(changes:Partial<Presentation>){
    setBusy(true);setError('')
    try{
      await request(`/branches/${snapshot.branch.id}/presentation`,{method:'PUT',body:JSON.stringify({expected_version:display.version,hidden_object_ids:display.hidden_object_ids,collapsed_object_ids:display.collapsed_object_ids,archived:display.archived,...changes})})
      await onRefresh();onNotice('分支显示设置已保存。')
    }catch(e){setError(e instanceof ApiError&&e.status===409?'显示设置已被其他操作更新，已重新载入，请按当前状态重试。':(e as Error).message);await onRefresh()}
    finally{setBusy(false)}
  }
  const isCollapsed=Boolean(selected&&display.collapsed_object_ids.includes(selected))
  return <>
    <button onClick={()=>{setError('');setOpen(true)}}>地图显示设置{display.hidden_object_ids.length?` · 隐藏 ${display.hidden_object_ids.length}`:''}</button>
    {selected&&<><button disabled={busy} onClick={()=>save({collapsed_object_ids:isCollapsed?display.collapsed_object_ids.filter(id=>id!==selected):[...display.collapsed_object_ids,selected]})}>{isCollapsed?'展开选中对象':'折叠选中对象'}</button><button disabled={busy||display.hidden_object_ids.includes(selected)} onClick={()=>save({hidden_object_ids:[...display.hidden_object_ids,selected]})}>在地图隐藏选中对象</button></>}
    {open&&<Modal title="分支显示与归档" onClose={()=>setOpen(false)}>
      {error&&<p className="inline-error" role="alert">{error}</p>}
      <p className="subtle">折叠简化节点正文，隐藏只影响地图显示；完整研究材料、证明依赖与工作稿仍保留。</p>
      <h3>已隐藏的对象</h3>{display.hidden_object_ids.length?<><ul className="presentation-list">{display.hidden_object_ids.map(id=><li key={id}><MathPreview body={snapshot.objects.find(o=>o.id===id)?.revision.body||id}/><button disabled={busy} onClick={()=>save({hidden_object_ids:display.hidden_object_ids.filter(item=>item!==id)})}>恢复显示</button></li>)}</ul><button disabled={busy} onClick={()=>save({hidden_object_ids:[]})}>恢复全部隐藏对象</button></>:<p className="subtle">没有隐藏对象。</p>}
      {display.collapsed_object_ids.length>0&&<p><button disabled={busy} onClick={()=>save({collapsed_object_ids:[]})}>展开全部折叠对象（{display.collapsed_object_ids.length}）</button></p>}
      <section className="detail-section"><h3>分支归档</h3><p>{display.archived?'此分支已归档，不会领取新的研究任务。':'此分支未归档。'}</p><p className="subtle">归档保留材料与历史。取消归档后，可在运行面板恢复分支任务。</p><button disabled={busy} onClick={()=>save({archived:!display.archived})}>{display.archived?'取消分支归档':'归档当前分支'}</button></section>
      <div className="modal-actions"><button onClick={()=>setOpen(false)}>关闭</button></div>
    </Modal>}
    {error&&!open&&<p className="inline-error" role="alert">{error}</p>}
  </>
}
