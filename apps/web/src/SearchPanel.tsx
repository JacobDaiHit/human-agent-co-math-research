import { useEffect, useState } from 'react'
import { request } from './api'
import { Badge, MathText, shortId } from './ui'

type Route={id:string;ordinal:number;state:string;candidate_revision_id?:string|null;progress?:{status?:string;open_subgoals?:string[]}}
type Gap={id:string;target_revision_id:string;kind:string;details:{anchor?:string;detail?:string};state:string}
type SearchState={session:{phase:string}|null;routes:Route[];gaps:Gap[];memory:unknown[];decisions:{sequence:number;action:string;details:Record<string,unknown>}[]}

export function SearchPanel({runId,eventSeq,onSelect}:{runId:string;eventSeq:number;onRefresh:()=>Promise<void>;onSelect:(revisionId:string)=>void;onNotice:(message:string)=>void}){
  const [state,setState]=useState<SearchState|null>(null),[error,setError]=useState('')
  useEffect(()=>{let cancelled=false;request<SearchState>('/runs/'+runId+'/search').then(value=>{if(!cancelled){setState(value);setError('')}}).catch(error=>{if(!cancelled)setError((error as Error).message)});return()=>{cancelled=true}},[runId,eventSeq])
  if(error)return <p className="inline-error" role="alert">历史状态读取失败：{error}</p>
  if(!state?.session)return null
  return <section className="search-panel" aria-label="历史搜索记录"><header><h3>历史搜索记录 · {state.session.phase}</h3></header>
    <p>旧流程仅保留读取和导出，不再派发任务。继续研究请从原题新建任务。</p>
    <div className="search-routes">{state.routes.map(route=><article key={route.id} className="search-route"><div><strong>路线 {route.ordinal+1}</strong> <Badge state={route.state}/></div>{route.progress?.status&&<p>当时报告：{route.progress.status}</p>}{(route.progress?.open_subgoals||[]).map((goal,index)=><MathText key={index} body={goal}/>)}<button disabled={!route.candidate_revision_id} onClick={()=>route.candidate_revision_id&&onSelect(route.candidate_revision_id)}>查看历史候选 {route.candidate_revision_id&&shortId(route.candidate_revision_id)}</button></article>)}</div>
    {state.gaps.map(gap=><article key={gap.id}><Badge state={gap.state}/><strong>{gap.kind}</strong>{gap.details.anchor&&<MathText body={gap.details.anchor}/>}<MathText body={gap.details.detail||''}/></article>)}
    <details><summary>历史记忆与调度记录</summary><p>记忆条目 {state.memory.length} 项</p>{state.decisions.map(decision=><div key={decision.sequence}><strong>#{decision.sequence} · {decision.action}</strong><pre>{JSON.stringify(decision.details,null,2)}</pre></div>)}</details>
  </section>
}
