import type { ResearchObject, Snapshot } from './api'

export interface Connection {id:string;source:string;target:string;kind:'premise'|'context'|'assumption'|'conclusion'|'research';label:string}
export interface Point {x:number;y:number}

export function connectionsFor(snapshot:Snapshot):Connection[]{
  const revisions=new Map(snapshot.objects.map(object=>[object.revision.id,object]))
  const result:Connection[]=[]
  const add=(edge:Connection)=>{if(edge.source!==edge.target)result.push(edge)}
  for(const proof of snapshot.proof_plans){
    const argument=revisions.get(proof.revision_id)
    if(!argument)continue
    for(const [kind,ids,label] of [['premise',proof.premise_revision_ids,'共同前提'],['context',proof.context_revision_ids,'上下文'],['assumption',proof.assumption_revision_ids,'未解除假设']] as const){
      for(const id of ids){const source=revisions.get(id);if(source)add({id:`${kind}-${id}-${proof.revision_id}`,source:source.id,target:argument.id,kind,label})}
    }
    const conclusion=revisions.get(proof.conclusion_revision_id)
    if(conclusion)add({id:`conclusion-${proof.revision_id}`,source:argument.id,target:conclusion.id,kind:'conclusion',label:'支持方案'})
  }
  const ids=new Set(snapshot.objects.map(object=>object.id))
  for(const relation of snapshot.relations)if(ids.has(relation.source_id)&&ids.has(relation.target_id))add({id:relation.id,source:relation.source_id,target:relation.target_id,kind:'research',label:({inspires:'启发',attempts:'尝试解决',refutes:'反驳',rewrites:'改写',similar:'相似'} as Record<string,string>)[relation.kind]||relation.kind})
  return result
}

/** Directed layers come from recorded edges; research cycles share a layer. */
export function structuredLayout(objects:ResearchObject[],connections:Connection[],goalId?:string):Record<string,Point>{
  const connected=new Set(connections.flatMap(edge=>[edge.source,edge.target]))
  const priority:Record<string,number>={problem:0,context:1,claim:2,argument:3,artifact:4,activity:5}
  const ordered=[...objects].sort((a,b)=>(priority[a.kind]??9)-(priority[b.kind]??9)||a.revision.created_at.localeCompare(b.revision.created_at)||a.id.localeCompare(b.id))
  const order=new Map(ordered.map((object,index)=>[object.id,index]))
  const adjacency=new Map(ordered.map(object=>[object.id,new Set<string>()]))
  for(const edge of connections)adjacency.get(edge.source)?.add(edge.target)
  let sequence=0
  const indices=new Map<string,number>(),low=new Map<string,number>(),stack:string[]=[],inStack=new Set<string>(),groups:string[][]=[]
  function visit(id:string){
    indices.set(id,sequence);low.set(id,sequence++);stack.push(id);inStack.add(id)
    for(const next of adjacency.get(id)||[]){if(!indices.has(next)){visit(next);low.set(id,Math.min(low.get(id)!,low.get(next)!))}else if(inStack.has(next))low.set(id,Math.min(low.get(id)!,indices.get(next)!))}
    if(low.get(id)===indices.get(id)){const group:string[]=[];let next:string;do{next=stack.pop()!;inStack.delete(next);group.push(next)}while(next!==id);groups.push(group.sort((a,b)=>order.get(a)!-order.get(b)!))}
  }
  for(const object of ordered)if(!indices.has(object.id))visit(object.id)
  const membership=new Map(groups.flatMap((group,index)=>group.map(id=>[id,index] as const)))
  const next=groups.map(()=>new Set<number>()),incoming=groups.map(()=>0),ranks=groups.map(()=>0)
  for(const edge of connections){const source=membership.get(edge.source)!,target=membership.get(edge.target)!;if(source!==target&&!next[source].has(target)){next[source].add(target);incoming[target]++}}
  const queue=groups.map((_,index)=>index).filter(index=>incoming[index]===0)
  queue.sort((a,b)=>order.get(groups[a][0])!-order.get(groups[b][0])!)
  while(queue.length){const current=queue.shift()!;for(const target of next[current]){ranks[target]=Math.max(ranks[target],ranks[current]+1);if(--incoming[target]===0)queue.push(target)}}
  const layers=new Map<number,string[]>()
  groups.forEach((group,index)=>{if(group.some(id=>connected.has(id)))layers.set(ranks[index],[...(layers.get(ranks[index])||[]),...group])})
  const positions:Record<string,Point>={}
  for(const [rank,ids] of [...layers].sort(([a],[b])=>a-b)){
    const centre=(id:string)=>{const sources=connections.filter(edge=>edge.target===id&&positions[edge.source]).map(edge=>positions[edge.source].y);return sources.length?sources.reduce((a,b)=>a+b,0)/sources.length:order.get(id)!*280}
    ids.sort((a,b)=>centre(a)-centre(b)||order.get(a)!-order.get(b)!)
    let previous=-216
    ids.forEach((id,row)=>{const parents=connections.filter(edge=>edge.target===id&&positions[edge.source]).map(edge=>positions[edge.source].y);const preferred=parents.length?parents.reduce((a,b)=>a+b,0)/parents.length:64+row*280;const y=Math.max(64,previous+280,preferred);positions[id]={x:64+rank*380,y};previous=y})
  }
  const anchor=ordered.find(object=>object.id===goalId&&!connected.has(object.id))
  if(anchor&&connections.length){for(const point of Object.values(positions))point.y+=280;positions[anchor.id]={x:64,y:64}}
  const isolated=ordered.filter(object=>!connected.has(object.id)&&!positions[object.id]),columns=Math.max(2,Math.min(3,layers.size||3)),bottom=Object.keys(positions).length?Math.max(...Object.values(positions).map(point=>point.y))+280:64
  isolated.forEach((object,index)=>{positions[object.id]={x:64+(index%columns)*380,y:bottom+Math.floor(index/columns)*280}})
  return positions
}
