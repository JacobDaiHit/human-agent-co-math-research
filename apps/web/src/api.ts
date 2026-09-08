export const API = '/api'
const pendingKeys=new Map<string,string>()

export class ApiError extends Error {
  status: number
  details: Record<string, unknown>
  constructor(status: number, details: Record<string, unknown>) {
    super(String(details.message || details.detail || `请求失败 (${status})`))
    this.status = status
    this.details = details
  }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  headers.set('X-MathAgent-Client', 'workbench')
  if (options.body) headers.set('Content-Type', 'application/json')
  const mutation=options.method && options.method !== 'GET'
  const fingerprint=mutation?`${options.method}:${path}:${String(options.body||'')}`:null
  if (fingerprint&&!headers.has('Idempotency-Key')) {
    if(!pendingKeys.has(fingerprint))pendingKeys.set(fingerprint,crypto.randomUUID())
    headers.set('Idempotency-Key',pendingKeys.get(fingerprint)!)
  }
  const response = await fetch(API + path, { ...options, headers, credentials: 'same-origin' })
  if(fingerprint&&response.status<500)pendingKeys.delete(fingerprint)
  if (!response.ok) {
    const details = await response.json().catch(() => ({ message: response.statusText }))
    throw new ApiError(response.status, details)
  }
  return response.json()
}

export const post = <T,>(path: string, data?: unknown) => request<T>(path, {
  method: 'POST', body: data === undefined ? undefined : JSON.stringify(data),
})

export interface Revision { id: string; object_id: string; body: string; payload: Record<string, unknown>; author: string; created_at: string }
export interface ResearchObject { id: string; kind: string; revision: Revision; adoption_state: string }
export interface Project { id: string; title: string; original_goal_id: string; event_seq: number }
export interface Presentation { branch_id?:string;version:number;hidden_object_ids:string[];collapsed_object_ids:string[];archived:boolean }
export interface Branch { id: string; name: string; parent_id: string | null; presentation?:Presentation }
export interface Support { status: string; conditions: string[]; reasons: string[]; plan_revision_ids: string[] }
export interface Proof { revision_id: string; conclusion_revision_id: string; body: string; premise_revision_ids: string[]; context_revision_ids: string[]; assumption_revision_ids: string[]; gaps: string[]; rule: string }
export interface Review { id: string; target_revision_id: string; kind: string; verdict: string; scope: string; coverage: string; findings: string[]; author: string }
export interface Attempt { id: string; number: number; state: string; read_set: Record<string,string>; stale_inputs: string[]; checkpoint: Record<string,unknown> }
export interface Run { id: string; state: string; goal_object_id: string; instruction: string; provider: string; attempts: Attempt[] }
export interface Snapshot {
  project: Project; branch: Branch; objects: ResearchObject[]; proof_plans: Proof[]; reviews: Review[];
  support: { claims: Record<string, Support>; plans: Record<string, Support>; limitation: string };
  manuscript: {id:string;kind:string;body:string;revision_id:string|null;position:number;version:number}[];
  relations: {id:string;source_id:string;target_id:string;kind:string}[];
  runs: Run[]; conflicts: {id:string;object_id:string;candidate_revision_id:string;current_revision_id:string;reason:string;resolution?:unknown}[];
  layout?: {version:number;positions:Record<string,{x:number;y:number}>};
  presentation?: Presentation;
  annotations?: {id:string;revision_id:string;body:string;anchor_quote:string|null;author:string;created_at:string}[];
}

export async function watchProject(projectId:string, after:number, onEvent:()=>void, onConnection:(connected:boolean)=>void, signal:AbortSignal) {
  let cursor=after
  while (!signal.aborted) {
    try {
      const response=await fetch(`${API}/projects/${projectId}/stream?after_seq=${cursor}`, {credentials:'same-origin',signal})
      if (!response.ok || !response.body) throw new Error('事件连接中断')
      onConnection(true)
      const reader=response.body.getReader(), decoder=new TextDecoder()
      let buffer=''
      try {
        while (!signal.aborted) {
          const chunk=await reader.read()
          if(chunk.done) break
          buffer+=decoder.decode(chunk.value,{stream:true})
          let boundary:number
          while ((boundary=buffer.indexOf('\n\n'))>=0) {
            const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2)
            const data=frame.split('\n').find(line=>line.startsWith('data: '))
            if(data){const event=JSON.parse(data.slice(6));if(event.seq>cursor){cursor=event.seq;onEvent()}}
          }
        }
      } finally { await reader.cancel().catch(()=>undefined) }
    } catch { if(signal.aborted) return }
    onConnection(false)
    await new Promise<void>(resolve=>{const done=()=>{clearTimeout(timer);signal.removeEventListener('abort',done);resolve()};const timer=window.setTimeout(done,1000);signal.addEventListener('abort',done,{once:true})})
  }
}
