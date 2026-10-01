import {useEffect,useState} from 'react'
import {request} from './api'
import {Badge,MathText} from './ui'

interface Material {id:string;body:string;payload:{title?:string}}
interface Member {run_id:string;name:string;state:string;personal_note:Material|null}
interface Work {id:string;member_run_id:string;goal:string;state:string;independent:boolean;output_revision_id:string|null}
interface Message {id:string;sender_run_id:string;recipient_run_id:string;topic:string;body:string;runtime_notice?:boolean}
interface ResearchState {
  session:{root_run_id:string;state:string;answer:string|null;outcome:string|null};
  members:Member[];work:Work[];messages:Message[];
  shared_note:Material|null;solution:Material|null;
  budget:{remaining:number};output_budget:{enabled:boolean;remaining_output_tokens:number|null};
}
export function ResearchPanel({runId,eventSeq,onSelect}:{runId:string;eventSeq:number;onSelect:(id:string)=>void}){
  const [value,setValue]=useState<ResearchState|null>(null),[error,setError]=useState('')
  useEffect(()=>{let cancelled=false;request<ResearchState|null>('/runs/'+runId+'/research').then(result=>{if(!cancelled){setValue(result);setError('')}}).catch(e=>{if(!cancelled)setError(e.message)});return()=>{cancelled=true}},[runId,eventSeq])
  if(error)return <p className="inline-error">{error}</p>
  if(!value||value.session.root_run_id!==runId)return null
  const name=(id:string)=>value.members.find(member=>member.run_id===id)?.name==='lead'?'主研究者':'研究同伴'
  const material=(title:string,item:Material|null)=>item&&<details><summary>{title}</summary><MathText body={item.body}/><button onClick={()=>onSelect(item.id)}>打开完整材料</button></details>
  return <section className="continuous-research" aria-label="连续研究">
    <h3>整题研究</h3>
    <p className="subtle">剩余请求 {value.budget.remaining}{value.output_budget.enabled&&<> · 剩余输出长度 {value.output_budget.remaining_output_tokens}</>}。讨论不是投票，不要求双方达成一致。</p>
    {value.members.map(member=><section key={member.run_id}>
      <strong>{name(member.run_id)}</strong> <Badge state={member.state}/>
      {value.work.filter(work=>work.member_run_id===member.run_id&&['queued','running','waiting'].includes(work.state)).map(work=><div key={work.id}><p>{work.independent?'独立探索 · ':''}{work.goal}</p><Badge state={work.state}/></div>)}
      {material('个人工作稿',member.personal_note)}
    </section>)}
    {material('公共工作稿',value.shared_note)}
    {value.messages.length>0&&<details><summary>话题讨论（{value.messages.length} 条）</summary>{value.messages.map(message=><section key={message.id}><h4>{message.topic}</h4><p className="subtle">{message.runtime_notice?'运行状态说明':name(message.sender_run_id)+' → '+name(message.recipient_run_id)}</p><MathText body={message.body}/></section>)}</details>}
    <details><summary>局部任务与方向调整</summary>{value.work.map(work=><section key={work.id}><p>{name(work.member_run_id)} · {work.goal}</p><Badge state={work.state}/>{work.output_revision_id&&<button onClick={()=>onSelect(work.output_revision_id!)}>查看成果</button>}</section>)}</details>
    {value.solution&&<section><h4>{value.session.outcome==='solved'?'已提交解答':'已保存未完成研究'}</h4>{value.session.answer!==null&&<MathText body={'答案：'+value.session.answer}/>}<MathText body={value.solution.body}/><button onClick={()=>onSelect(value.solution!.id)}>打开提交正文</button><p className="subtle">这是研究者的提交，不是程序认证的数学结论。</p></section>}
  </section>
}
