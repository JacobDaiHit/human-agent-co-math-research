import { useState } from 'react'
import { request } from './api'
import { Modal } from './ui'

const operations=[['read_object','读取授权对象与版本'],['search_project','查找项目材料'],['write_draft','保存研究草稿'],['revise_object','提出对象修订'],['propose_proof','提出证明方案'],['record_failure','保存失败与重试条件'],['record_source','记录文献来源'],['create_branch','创建研究分支'],['spawn_task','提出子任务'],['request_review','请求独立审查'],['discuss','参与项目内讨论'],['calculate','调用固定精确计算']] as const

export function AgentPolicyPanel({projectId,onRefresh}:{projectId:string;onRefresh:()=>Promise<void>}){
  const [open,setOpen]=useState(false),[allowed,setAllowed]=useState<string[]|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
  async function show(){setOpen(true);setAllowed(null);setError('');try{const policy=await request<{allowed_operations:string[]}>(`/projects/${projectId}/agent-policy`);setAllowed(policy.allowed_operations)}catch(e){setError((e as Error).message)}}
  async function save(){if(!allowed)return;setBusy(true);setError('');try{await request(`/projects/${projectId}/agent-policy`,{method:'PUT',body:JSON.stringify({allowed_operations:allowed})});await onRefresh();setOpen(false)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <><button className="agent-policy-trigger" onClick={show}>研究者操作权限</button>{open&&<Modal title="研究者操作权限" onClose={()=>setOpen(false)}><form onSubmit={event=>{event.preventDefault();save()}}><p className="subtle">选择模型在本项目中可以发起的操作。人工编辑与采用权保持独立；真实模型许可和各层额度仍然有效。</p>{error&&<p className="inline-error" role="alert">{error}</p>}{allowed?<><div className="button-row"><button type="button" onClick={()=>setAllowed(operations.map(([id])=>id))}>勾选全部操作</button><button type="button" onClick={()=>setAllowed([])}>取消全部勾选</button></div><fieldset className="choice-list"><legend>允许的项目操作</legend>{operations.map(([id,label])=><label key={id}><input type="checkbox" checked={allowed.includes(id)} onChange={event=>setAllowed(event.target.checked?[...allowed,id]:allowed.filter(value=>value!==id))}/><span>{label}</span></label>)}</fieldset><p className="subtle">固定计算只执行已提供的确定性检查，不运行模型生成的任意代码，也不进行网页检索。</p></>:!error&&<p>正在读取权限…</p>}<div className="modal-actions"><button type="button" onClick={()=>setOpen(false)}>取消</button><button className="primary" disabled={busy||allowed===null}>保存操作权限</button></div></form></Modal>}</>
}
