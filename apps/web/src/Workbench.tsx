import { useCallback, useEffect, useRef, useState } from 'react'
import { ActionDialog } from './ActionDialog'
import type { ProviderStatus } from './ActionDialog'
import { API, post, request, watchProject } from './api'
import type { Branch, Project, Snapshot } from './api'
import { Inspector } from './Inspector'
import { ResearchGraph } from './ResearchGraph'
import { RunPanel } from './RunPanel'
import { Badge, kindLabels, MathInputHint, MathPreview, MathText, Modal } from './ui'
import { useWorkbenchTools } from './webmcp'
import { BudgetPanel } from './BudgetPanel'
import type { Budget } from './BudgetPanel'
import { PresentationPanel } from './PresentationPanel'

type View='split'|'manuscript'|'graph'|'runs'|'history'
function lastWorkspace():{projectId?:string;branchId?:string}{try{const value=JSON.parse(localStorage.getItem('mathagent:last-workspace')||'{}');return value&&typeof value==='object'?{projectId:typeof value.projectId==='string'?value.projectId:undefined,branchId:typeof value.branchId==='string'?value.branchId:undefined}:{}}catch{return {}}}
interface SearchResult {object_id:string|null;revision_id:string|null;branch_id:string;branch_name:string;version:number;is_current:boolean;status:string;support_status:string|null;revision_state?:string;snippet:string}
interface ActivityEvent {id:string;seq:number;type:string;author:string;created_at:string;payload:Record<string,unknown>}
const eventLabels:Record<string,string>={'project.created':'创建研究项目','object.created':'添加研究对象','revision.created':'保存对象新版本','revision.conflict':'保存冲突候选','proof.created':'添加证明方案','review.recorded':'记录审查','adoption.changed':'改变采用状态','branch.created':'创建分支','run.created':'安排研究任务','attempt.claimed':'工作者开始执行','attempt.completed':'保存执行产物','run.intervention':'收到人工干预','run.resumed':'恢复研究任务','annotation.created':'添加版本批注','workspace.layout_updated':'保存地图布局','conflict.resolved':'处理版本冲突','manuscript.block_added':'添加稿件段落','manuscript.block_revised':'修改稿件段落'}

export function Workbench(){
  const [projects,setProjects]=useState<Project[]>([]),[snapshot,setSnapshot]=useState<Snapshot|null>(null),[branches,setBranches]=useState<Branch[]>([]),[selected,setSelected]=useState<string|null>(null),[historicalId,setHistoricalId]=useState<string|null>(null),[view,setView]=useState<View>('split'),[filter,setFilter]=useState('all'),[proofView,setProofView]=useState(false),[action,setAction]=useState<string|null>(null),[connected,setConnected]=useState(false),[streamConnected,setStreamConnected]=useState(true),[notice,setNotice]=useState(''),[error,setError]=useState(''),[loading,setLoading]=useState(true),[providers,setProviders]=useState<ProviderStatus[]>([]),[budget,setBudget]=useState<Budget|null>(null),[query,setQuery]=useState(''),[searchResults,setSearchResults]=useState<SearchResult[]|null>(null),[events,setEvents]=useState<ActivityEvent[]>([]),[note,setNote]=useState<Snapshot['manuscript'][number]|null>(null),[noteBody,setNoteBody]=useState(''),[noteBusy,setNoteBusy]=useState(false)
  const current=useRef<{projectId?:string;branchId?:string}>(lastWorkspace()),generation=useRef(0),reload=useRef<()=>Promise<void>>(async()=>{})
  const notify=useCallback((message:string)=>setNotice(message),[])
  const refresh=useCallback(async(projectId=current.current.projectId,branchId?:string)=>{
    if(branchId===undefined&&projectId===current.current.projectId)branchId=current.current.branchId
    if(projectId)current.current={projectId,branchId}
    const gen=++generation.current
    const list=await request<{projects:Project[]}>('/projects')
    const id=projectId&&list.projects.some(project=>project.id===projectId)?projectId:list.projects[0]?.id
    if(id!==projectId)branchId=undefined
    if(!id){setProjects(list.projects);setLoading(false);return}
    const [snap,bs]=await Promise.all([request<Snapshot>(`/projects/${id}/snapshot${branchId?`?branch_id=${branchId}`:''}`),request<{branches:Branch[]}>(`/projects/${id}/branches`)])
    if(gen!==generation.current)return
    current.current={projectId:id,branchId:snap.branch.id};try{localStorage.setItem('mathagent:last-workspace',JSON.stringify(current.current))}catch{};setProjects(list.projects);setSnapshot(snap);setBranches(bs.branches);setLoading(false)
    request<Budget>(`/projects/${id}/budget`).then(data=>{if(current.current.projectId===id)setBudget(data)}).catch(()=>setBudget(null))
  },[])
  reload.current=async()=>refresh()
  useEffect(()=>{post('/session').then(async()=>{setConnected(true);await refresh();const status=await request<{providers:ProviderStatus[]}>('/providers/status').catch(()=>null);if(status)setProviders(status.providers)}).catch(e=>{setError(e.message);setLoading(false)})},[refresh])
  useEffect(()=>{
    if(!snapshot)return
    const abort=new AbortController();let timer:number|undefined
    watchProject(snapshot.project.id,snapshot.project.event_seq,()=>{clearTimeout(timer);timer=window.setTimeout(()=>reload.current().catch(e=>setError(e.message)),120)},setStreamConnected,abort.signal)
    return()=>{abort.abort();clearTimeout(timer)}
  },[snapshot?.project.id])
  useEffect(()=>{if(!notice)return;const timer=setTimeout(()=>setNotice(''),7000);return()=>clearTimeout(timer)},[notice])
  useEffect(()=>{
    if(view!=='history'||!snapshot)return
    let cancelled=false
    async function load(){let cursor=0;const items:ActivityEvent[]=[];const target=snapshot!.project.event_seq;while(cursor<target&&!cancelled){const batch=await request<{events:ActivityEvent[];last_seq:number}>(`/projects/${snapshot!.project.id}/events?after_seq=${cursor}`);items.push(...batch.events);if(batch.last_seq<=cursor)break;cursor=batch.last_seq}if(!cancelled)setEvents(items)}
    load().catch(e=>{if(!cancelled)setError(e.message)});return()=>{cancelled=true}
  },[view,snapshot?.project.event_seq,snapshot?.project.id])
  useEffect(()=>{if(!snapshot||!query.trim()){setSearchResults(null);return}let cancelled=false;const timer=setTimeout(()=>request<{results:SearchResult[]}>(`/projects/${snapshot.project.id}/search?q=${encodeURIComponent(query.trim())}`).then(r=>{if(!cancelled)setSearchResults(r.results)}).catch(e=>{if(!cancelled)setError(e.message)}),250);return()=>{cancelled=true;clearTimeout(timer)}},[query,snapshot?.project.id])
  const object=snapshot?.objects.find(o=>o.id===selected)||null
  const select=(id:string)=>{setSelected(id);setHistoricalId(null);if(view==='runs'||view==='history')setView('split')}
  useWorkbenchTools(snapshot,()=>refresh(),select)
  async function chooseProject(id:string,branchId?:string){setSelected(null);setHistoricalId(null);setQuery('');setLoading(true);try{await refresh(id,branchId)}catch(e){setError((e as Error).message);setLoading(false)}}
  async function example(){setLoading(true);try{const result=await post<{project_id:string;branch_id:string}>('/examples/hermitian',{});await refresh(result.project_id,result.branch_id);notify('已载入可精确验算的示例。示例材料有明确来源，不计作模型发现。')}catch(e){setError((e as Error).message);setLoading(false)}}
  async function completed(result?:{project_id?:string;branch_id?:string;object_id?:string},message?:string){await refresh(result?.project_id||current.current.projectId,result?.branch_id||current.current.branchId);if(result?.object_id)select(result.object_id);if(action==='run'||action==='review-run')setView('runs');if(message)notify(message)}
  async function saveLayout(positions:Record<string,{x:number;y:number}>){if(!snapshot)return false;try{await request(`/branches/${snapshot.branch.id}/layout`,{method:'PUT',body:JSON.stringify({expected_version:snapshot.layout?.version||0,positions:{...snapshot.layout?.positions,...positions}})});await refresh();return true}catch(e){notify((e as Error).message);await refresh();return false}}
  async function exportProject(){if(!snapshot)return;try{const result=await post<{download_url:string}>('/exports',{project_id:snapshot.project.id,branch_id:snapshot.branch.id});const response=await fetch(API+result.download_url,{credentials:'same-origin'});if(!response.ok)throw new Error('导出文件下载失败，请重试。');const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download=`MathAgent-${snapshot.branch.name}.zip`;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);notify('已导出固定版本的工作稿、证据清单与研究产物。')}catch(e){setError((e as Error).message)}}
  async function saveNote(){if(!note||!snapshot)return;setNoteBusy(true);try{await post(`/manuscript/blocks/${note.id}/revisions`,{branch_id:snapshot.branch.id,expected_body:note.body,expected_version:note.version,body:noteBody});setNote(null);await refresh();notify('工作稿笔记已保存，旧内容保留在段落历史中。')}catch(e){setError((e as Error).message)}finally{setNoteBusy(false)}}
  const showInspector=object&&!['runs','history'].includes(view)
  const activeRuns=snapshot?.runs.filter(r=>['queued','running','pause_requested','steer_requested','cancel_requested'].includes(r.state)).length||0
  const recheck=snapshot?.objects.filter(o=>snapshot.support.claims[o.revision.id]?.status==='needs_recheck'||snapshot.support.plans[o.revision.id]?.status==='needs_recheck').length||0
  const unresolved=snapshot?.conflicts.filter(c=>!c.resolution).length||0
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark">∴</span><span>MathAgent<small>研究工作台</small></span></div><button className="new-project" onClick={()=>setAction('project')} disabled={!connected}>＋ 新建研究</button><div className="section-label">研究项目</div><nav aria-label="研究项目">{projects.map(p=><button key={p.id} className={snapshot?.project.id===p.id?'project active':'project'} onClick={()=>chooseProject(p.id)}><span>◇</span>{p.title}</button>)}</nav><button className="example-link" disabled={!connected||loading} onClick={example}>打开特征值反例示例 ↗</button><div className="sidebar-bottom"><span className={connected&&streamConnected?'connection-dot':'connection-dot offline'}/>{connected?(streamConnected?'本地状态已同步':'正在重新连接'):'正在连接'}<small>研究材料保存在本机</small></div></aside>
    <main className={showInspector?'has-inspector':''}><header className="workspace-header"><div><span className="eyebrow">WORKSPACE</span><h1>{snapshot?.project.title||'从一个数学问题开始'}</h1></div>{snapshot&&<div className="header-actions"><label className="sr-only" htmlFor="branch-picker">当前分支</label><select id="branch-picker" className="branch-picker" value={snapshot.branch.id} onChange={e=>chooseProject(snapshot.project.id,e.target.value)}>{branches.map(b=><option key={b.id} value={b.id}>{b.name==='main'?'主线':b.name}{b.presentation?.archived?' · 已归档':''}</option>)}</select><button onClick={()=>setAction('branch')}>＋ 分支</button><button onClick={exportProject}>导出</button></div>}</header>
      {error&&<div role="alert" className="error-banner">{error}<button onClick={()=>setError('')}>关闭</button></div>}
      {notice&&<div role="status" className="notice-banner">{notice}<button aria-label="关闭提示" onClick={()=>setNotice('')}>×</button></div>}
      {snapshot?<><div className="workspace-toolbar"><div className="view-tabs" role="tablist" aria-label="工作区视图">{([['split','图与稿'],['manuscript','工作稿'],['graph','研究地图'],['runs',`运行${activeRuns?` · ${activeRuns}`:''}`],['history','活动历史']] as [View,string][]).map(([v,label])=><button key={v} role="tab" aria-selected={view===v} className={view===v?'active':''} onClick={()=>setView(v)}>{label}</button>)}</div><div className="toolbar-actions"><button onClick={()=>setAction('object')}>＋ 对象</button><button className="primary" onClick={()=>setAction('run')}>启动研究</button></div></div>
        <div className="research-strip"><span>{snapshot.objects.length} 个研究对象</span>{snapshot.presentation?.archived&&<span className="badge badge-paused">当前分支已归档</span>}{recheck>0&&<button className="text-button attention" onClick={()=>{setFilter('needs_recheck');setView('graph')}}>{recheck} 项需要重查</button>}{unresolved>0&&<button className="text-button attention" onClick={()=>select(snapshot.conflicts.find(c=>!c.resolution)!.object_id)}>{unresolved} 项修改冲突</button>}<div className="search-box"><input type="search" aria-label="搜索研究内容" placeholder="搜索正文、旧版或旁支…" value={query} onChange={e=>setQuery(e.target.value)}/>{searchResults&&<div className="search-results" role="region" aria-label="搜索结果">{searchResults.length?searchResults.map((r,i)=><button key={i} onClick={async()=>{await refresh(snapshot.project.id,r.branch_id);setSelected(r.object_id);setHistoricalId(r.is_current?null:r.revision_id);setView('split');setQuery('')}}><small>{r.branch_name} · v{r.version} {r.revision_state==='conflict_candidate'?'冲突候选':r.is_current?'当前版本':'历史版本'}</small><MathPreview body={r.snippet}/><div><Badge state={r.status}/>{r.support_status&&<Badge state={r.support_status}/>}</div></button>):<p>没有找到匹配内容</p>}</div>}</div></div>
        <div className={`workspace-body view-${view}${showInspector?' with-inspector':''}`}>
          <div className="workspace-primary">{['split','graph','manuscript'].includes(view)&&<><div className="content-controls">{view!=='manuscript'&&<><label>显示<select aria-label="地图筛选" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部对象</option><option value="needs_recheck">待审与争议</option><option value="draft">草稿</option><option value="failures">尝试与失败</option></select></label><label className="checkbox-label"><input type="checkbox" checked={proofView} disabled={!selected} onChange={e=>setProofView(e.target.checked)}/>选中结论的前提与后果</label></>}<button onClick={()=>setAction('block')}>＋ 工作稿笔记</button><button onClick={()=>setAction('failure')}>记录尝试</button><button onClick={()=>setAction('source')}>录入来源</button>{view!=='manuscript'&&<PresentationPanel snapshot={snapshot} selected={selected} onRefresh={()=>refresh()} onNotice={notify}/>}</div><div className={`split-workspace layout-${view}`}>
            {view!=='manuscript'&&<ResearchGraph snapshot={snapshot} selected={selected} filter={filter} proofView={proofView} onSelect={select} onSave={saveLayout}/>}
            {view!=='graph'&&<section className="paper-pane" aria-label="研究工作稿"><div className="paper-heading"><span className="eyebrow">MANUSCRIPT</span><h2>研究工作稿</h2><small>原问题、局部成果和未决步骤</small></div>{snapshot.manuscript.map(b=>{const obj=b.revision_id?snapshot.objects.find(o=>o.revision.id===b.revision_id):null;const st=obj?(snapshot.support.claims[obj.revision.id]?.status||snapshot.support.plans[obj.revision.id]?.status||obj.adoption_state):'draft';return <article tabIndex={0} className={`manuscript-block${obj?.id===selected?' selected':''}`} key={b.id} data-testid="manuscript-block" onClick={()=>{if(obj)select(obj.id)}} onKeyDown={e=>{if(e.key==='Enter'&&obj)select(obj.id)}}><div className="block-caption"><span>{obj?kindLabels[obj.kind]:'自由笔记'}</span><Badge state={st}/>{!obj&&<button onClick={e=>{e.stopPropagation();setNote(b);setNoteBody(b.body)}}>编辑笔记</button>}</div><MathText body={b.body}/></article>})}</section>}
          </div></>}
          {view==='runs'&&<><BudgetPanel key={snapshot.project.id} projectId={snapshot.project.id} budget={budget} providers={providers} onRefresh={()=>refresh()}/><RunPanel snapshot={snapshot} onRefresh={()=>refresh()} onSelect={select} onNotice={notify} onOpenOutput={async(branchId,revisionId)=>{try{await refresh(snapshot.project.id,branchId);const s=await request<Snapshot>(`/projects/${snapshot.project.id}/snapshot?branch_id=${branchId}`);const output=s.objects.find(o=>o.revision.id===revisionId);if(output)select(output.id)}catch(e){setError((e as Error).message)}}}/></>}
          {view==='history'&&<section className="history-view"><h2>活动历史</h2><p className="subtle">按实际发生顺序记录；执行先后不代表数学推导。</p><ol className="timeline">{events.slice().reverse().map(e=><li key={e.id}><div><span>{eventLabels[e.type]||e.type}</span><time>{new Date(e.created_at).toLocaleString('zh-CN')}</time></div><small>{e.author} · 事件 {e.seq}</small><details><summary>操作回执</summary><pre>{JSON.stringify(e.payload,null,2)}</pre></details></li>)}</ol></section>}
          </div>{showInspector&&object&&<Inspector key={`${snapshot.branch.id}-${object.id}`} object={object} snapshot={snapshot} onRefresh={()=>refresh()} onSelect={select} onAction={setAction} onNotice={notify} onClose={()=>setSelected(null)} historicalId={historicalId}/>}
        </div>
      </>:<section className="empty-workspace"><div className="empty-math">∑</div><h2>{loading?'正在打开研究空间…':'保留每一条值得继续的思路'}</h2><p>写下问题、猜想或一段尚未完成的证明。<br/>草稿、论证和它们的版本会保存在同一个空间。</p><div className="button-row centered"><button className="primary" onClick={()=>setAction('project')} disabled={!connected}>新建研究问题</button><button onClick={example} disabled={!connected||loading}>打开特征值示例</button></div></section>}
    </main>
    {action&&<ActionDialog action={action} snapshot={snapshot} selected={object} providers={providers} onClose={()=>setAction(null)} onComplete={completed}/>}
    {note&&<Modal title="编辑工作稿笔记" onClose={()=>setNote(null)}><form onSubmit={e=>{e.preventDefault();saveNote()}}><label className="field-label">正文<textarea aria-label="工作稿笔记正文" autoFocus rows={10} value={noteBody} onChange={e=>setNoteBody(e.target.value)}/></label><MathInputHint/><div className="modal-actions"><button type="button" onClick={()=>setNote(null)}>取消</button><button className="primary" disabled={noteBusy}>保存笔记</button></div></form></Modal>}
  </div>
}







