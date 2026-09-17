import { useEffect, useState } from 'react'
import { post, request } from './api'
import { Badge, MathText, shortId } from './ui'

type Progress={status?:string;open_subgoals?:string[];completed_subgoal_ids?:string[];assumptions?:string[];steps?:number}
type Route={id:string;ordinal:number;state:string;card_revision_id?:string|null;candidate_revision_id?:string|null;progress?:Progress;repairs?:number;priority?:number}
type Gap={id:string;route_id:string;target_revision_id:string;kind:string;details:{anchor?:string;detail?:string;evidence_revision_ids?:string[]};state:string}
type BudgetPart={shared_root_budget?:boolean;remaining_requests?:number;remaining_output_tokens?:number;held_requests?:number}
type SearchState={session:{root_run_id:string;phase:string;config?:{max_routes?:number;active_routes?:number;budget_policy?:string}}|null;routes:Route[];gaps:Gap[];memory:unknown[];decisions:{sequence:number;action:string;details:Record<string,unknown>}[];budget:Record<string,BudgetPart>}

export function SearchPanel({runId,eventSeq,onRefresh,onSelect,onNotice}:{runId:string;eventSeq:number;onRefresh:()=>Promise<void>;onSelect:(revisionId:string)=>void;onNotice:(message:string)=>void}){
  const [state,setState]=useState<SearchState|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState<string|null>(null)
  useEffect(()=>{let cancelled=false;request<SearchState>(`/runs/${runId}/search`).then(value=>{if(!cancelled){setState(value);setError('')}}).catch(error=>{if(!cancelled)setError((error as Error).message)});return()=>{cancelled=true}},[runId,eventSeq])
  async function intervene(route:Route,action:'pause'|'resume'|'priority',priority?:number){if(!state?.session)return;setBusy(`${route.id}:${action}`);try{await post(`/runs/${state.session.root_run_id}/search/routes/${route.id}/interventions`,{action,...(priority===undefined?{}:{priority})});await onRefresh();onNotice(action==='pause'?'路线已暂停。':action==='resume'?'路线已恢复派发资格。':'路线优先级已更新。')}catch(error){onNotice((error as Error).message)}finally{setBusy(null)}}
  if(error)return <p className="inline-error" role="alert">搜索状态读取失败：{error}</p>
  if(!state?.session)return null
  const config=state.session.config
  const shared=state.budget?.explore?.shared_root_budget===true
  const frozen=['final','terminated'].includes(state.session.phase)
  return <section className="search-panel" aria-label="受控搜索状态"><header><div><span className="eyebrow">BOUNDED SEARCH</span><h3>受控搜索 · {state.session.phase}</h3></div><small>{config?.max_routes??state.routes.length} 条候选路线，最多同时 {shared?1:(config?.active_routes??'—')} 条</small></header>
    <div className="search-budget" aria-label="搜索预算">{(shared?(['explore'] as const):(['explore','check','final'] as const)).map(part=>{const budget=state.budget?.[part]||{};return <div key={part}><strong>{shared?'共享总预算':{explore:'探索',check:'检查',final:'收尾'}[part]}</strong><span>请求余量 {budget.remaining_requests??'—'}</span><span>输出余量 {budget.remaining_output_tokens??'—'}</span>{budget.held_requests!==undefined&&<small>保留 {budget.held_requests}</small>}</div>})}</div>
    {shared&&<p>探索、检查与收尾共享总额度；派发时保留必要审查和收尾空间。</p>}
    <div className="search-routes">{state.routes.map(route=><article key={route.id} className="search-route"><div><strong>路线 {route.ordinal+1}</strong> <Badge state={route.state}/><small>优先级 {route.priority??0} · 修复 {route.repairs??0}</small></div>{route.progress?.status&&<p>当前报告：{route.progress.status}</p>}{(route.progress?.open_subgoals||[]).map((subgoal,index)=><MathText key={index} body={subgoal}/>)}<div className="button-row"><button className="text-button" disabled={!route.candidate_revision_id} onClick={()=>route.candidate_revision_id&&onSelect(route.candidate_revision_id)}>查看候选 {route.candidate_revision_id&&shortId(route.candidate_revision_id)}</button>{route.state==='paused'?<button disabled={busy!==null||frozen} onClick={()=>intervene(route,'resume')}>恢复路线</button>:<button disabled={busy!==null||frozen} onClick={()=>intervene(route,'pause')}>暂停路线</button>}<button disabled={busy!==null||frozen} onClick={()=>intervene(route,'priority',10)}>提高优先级</button><button disabled={busy!==null||frozen} onClick={()=>intervene(route,'priority',-10)}>降低优先级</button></div></article>)}</div>
    {state.gaps.length>0&&<section className="search-gaps"><h4>关键缺口</h4>{state.gaps.map(gap=><article key={gap.id}><div><Badge state={gap.state}/><strong>{gap.kind}</strong> <code>{shortId(gap.target_revision_id)}</code></div>{gap.details.anchor&&<div><small>定位：</small><MathText body={gap.details.anchor}/></div>}{gap.details.detail&&<MathText body={gap.details.detail}/>}<small>证据版本：{gap.details.evidence_revision_ids?.map(shortId).join('、')||'未提供'}</small></article>)}</section>}
    <details className="search-log"><summary>记忆与调度记录</summary><p>记忆条目 {state.memory.length} 项</p>{state.decisions.map(decision=><div key={decision.sequence}><strong>#{decision.sequence} · {decision.action}</strong><pre>{JSON.stringify(decision.details,null,2)}</pre></div>)}</details>
  </section>
}
