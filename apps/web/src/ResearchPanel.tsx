import {useEffect,useState} from 'react'
import {request} from './api'
import {Badge,MathText,shortId,dialogueReasonLabels} from './ui'

interface Material {id:string;body:string;payload:{title?:string}}
interface Member {run_id:string;name:string;state:string;personal_note:Material|null}
interface Work {id:string;member_run_id:string;goal:string;state:string;independent:boolean;output_revision_id:string|null}
interface Message {id:string;sender_run_id:string;recipient_run_id:string;topic:string;body:string;runtime_notice?:boolean}
interface CacheUsage {hit_tokens:number|null;miss_tokens:number|null;hit_fraction:number|null;calls_with_cache_usage:number;calls_without_cache_usage:number}
interface UsageSummary {reported_tokens:{prompt_tokens:number;completion_tokens:number};all_usage_known:boolean;cache:CacheUsage;by_researcher:Record<string,{requests:number;input_tokens:number;output_tokens:number;cache:CacheUsage}>;conversations?:{dialogue_id:string;member:string|null;reset_reason:string|null;requests:number;cache:CacheUsage;prefix_changes:number}[]}
interface ResearchState {
  session:{root_run_id:string;state:string;answer:string|null;outcome:string|null};
  members:Member[];work:Work[];messages:Message[];
  shared_note:Material|null;solution:Material|null;
  budget:{remaining:number};output_budget:{enabled:boolean;remaining_output_tokens:number|null};
  usage_summary?:UsageSummary;
}
export function ResearchPanel({runId,eventSeq,onSelect}:{runId:string;eventSeq:number;onSelect:(id:string)=>void}){
  const [value,setValue]=useState<ResearchState|null>(null),[error,setError]=useState('')
  useEffect(()=>{let cancelled=false;request<ResearchState|null>('/runs/'+runId+'/research').then(result=>{if(!cancelled){setValue(result);setError('')}}).catch(e=>{if(!cancelled)setError(e.message)});return()=>{cancelled=true}},[runId,eventSeq])
  if(error)return <p className="inline-error">{error}</p>
  if(!value||value.session.root_run_id!==runId)return null
  const name=(id:string)=>{const member=value.members.find(member=>member.run_id===id);return member?.name==='lead'?'主研究者':'研究同伴 · '+(member?.name||id.slice(0,8))}
  const material=(title:string,item:Material|null)=>item&&<details><summary>{title}</summary><MathText body={item.body}/><button onClick={()=>onSelect(item.id)}>打开完整材料</button></details>
  const cache=(item:CacheUsage)=><>缓存命中 {item.hit_tokens===null?'未提供':item.hit_tokens.toLocaleString()} · 未命中 {item.miss_tokens===null?'未提供':item.miss_tokens.toLocaleString()}{item.hit_fraction!==null&&<> · 已报告输入的命中率 {(item.hit_fraction*100).toFixed(2)}%</>}{item.calls_without_cache_usage>0&&<> · {item.calls_without_cache_usage} 次请求未提供缓存数量</>}</>
  return <section className="continuous-research" aria-label="连续研究">
    <h3>整题研究</h3>
    <p className="subtle">剩余请求 {value.budget.remaining}{value.output_budget.enabled&&<> · 剩余输出长度 {value.output_budget.remaining_output_tokens}</>}。讨论不是投票，不要求双方达成一致。</p>
    {value.usage_summary&&<details><summary>全体成员的实际用量与缓存</summary><p>已报告输入 {value.usage_summary.reported_tokens.prompt_tokens.toLocaleString()} · 已报告输出 {value.usage_summary.reported_tokens.completion_tokens.toLocaleString()}</p>{!value.usage_summary.all_usage_known&&<p className="subtle">部分请求的用量不完整；缺失不是零。</p>}<p>{cache(value.usage_summary.cache)}</p>{Object.entries(value.usage_summary.by_researcher).map(([member,usage])=><p key={member}>{member==='lead'?'主研究者':member==='unassigned'?'未记录成员归属':member} · {usage.requests} 次请求 · 输入 {usage.input_tokens.toLocaleString()} · 输出 {usage.output_tokens.toLocaleString()} · {cache(usage.cache)}</p>)}{Boolean(value.usage_summary.conversations?.length)&&<details><summary>按对话查看接续与缓存</summary>{value.usage_summary.conversations!.map(dialogue=><p key={dialogue.dialogue_id}>{dialogue.member==='lead'?'主研究者':dialogue.member||'未记录成员'} · 对话 {shortId(dialogue.dialogue_id)} · {dialogue.requests} 次请求 · {dialogue.reset_reason?(dialogueReasonLabels[dialogue.reset_reason]||dialogue.reset_reason):'未记录重开原因'} · {cache(dialogue.cache)}{dialogue.prefix_changes>0&&<> · 旧输入变化 {dialogue.prefix_changes} 次（本地接续记录）</>}</p>)}</details>}</details>}
    {value.members.map(member=><section key={member.run_id}>
      <strong>{name(member.run_id)}</strong> <Badge state={member.state}/>
      {value.work.filter(work=>work.member_run_id===member.run_id&&['queued','running','waiting'].includes(work.state)).map(work=><div key={work.id}><p>{work.independent?'独立探索 · ':member.name==='lead'?'当前研究 · ':'针对性研究 · '}{work.goal}</p><Badge state={work.state}/></div>)}
      {material('个人工作稿',member.personal_note)}
    </section>)}
    {material('公共工作稿',value.shared_note)}
    {value.messages.length>0&&<details><summary>话题讨论（{value.messages.length} 条）</summary>{value.messages.map(message=><section key={message.id}><h4>{message.topic}</h4><p className="subtle">{message.runtime_notice?'运行状态说明':name(message.sender_run_id)+' → '+name(message.recipient_run_id)}</p><MathText body={message.body}/></section>)}</details>}
    <details><summary>局部任务与方向调整</summary>{value.work.map(work=><section key={work.id}><p>{name(work.member_run_id)} · {work.goal}</p><Badge state={work.state}/>{work.output_revision_id&&<button onClick={()=>onSelect(work.output_revision_id!)}>查看成果</button>}</section>)}</details>
    {value.solution&&<section><h4>{value.session.outcome==='solved'?'已提交解答':'已保存未完成研究'}</h4>{value.session.answer!==null&&<MathText body={'答案：'+value.session.answer}/>}<MathText body={value.solution.body}/><button onClick={()=>onSelect(value.solution!.id)}>打开提交正文</button><p className="subtle">这是研究者的提交，不是程序认证的数学结论。</p></section>}
  </section>
}
