import { useEffect, useMemo, useRef, useState } from 'react'
import { Background, Handle, MarkerType, Position, ReactFlow, useNodesInitialized, useNodesState, useReactFlow } from '@xyflow/react'
import type { Edge, Node, NodeProps, ReactFlowInstance } from '@xyflow/react'
import type { ResearchObject, Snapshot } from './api'
import { connectionsFor, structuredLayout } from './graphLayout'
import type { Point } from './graphLayout'
import { Badge, kindLabels, MathText } from './ui'
import { RoutedEdge } from './RoutedEdge'

type ResearchNode=Node<{object:ResearchObject;status:string;collapsed:boolean;anchor:boolean;focus:'normal'|'selected'|'neighbor'|'muted'},'research'>
type GraphEdge=Edge&{type:'research'}
type Props={snapshot:Snapshot;selected:string|null;filter:string;proofView:boolean;onSelect:(id:string)=>void;onSave:(positions:Record<string,Point>)=>Promise<boolean>}
const colors={premise:'#3972bc',context:'#248b87',assumption:'#b98520',conclusion:'#3972bc',research:'#8b97aa'}
function Card({data,selected}:NodeProps<ResearchNode>){
  return <div className={`research-node kind-${data.object.kind} focus-${data.focus}${selected?' selected':''}${data.collapsed?' collapsed':''}${data.anchor?' research-anchor':''}`}>
    <Handle id="in" type="target" position={Position.Left} aria-label="输入连接点"/>
    <div className="node-caption"><span className="node-kind"><i/>{data.anchor?'原问题 · 研究起点':kindLabels[data.object.kind]}{data.collapsed?' · 已折叠':''}</span><Badge state={data.status}/></div>
    {data.collapsed?<div className="node-collapsed-note">正文已折叠，选中后可在详情中阅读</div>:<div className="node-body nowheel"><MathText body={data.object.revision.body}/></div>}
    <Handle id="out" type="source" position={Position.Right} aria-label="输出连接点"/>
  </div>
}
const nodeTypes={research:Card}
const edgeTypes={research:RoutedEdge}
const samePoint=(a:Point|undefined,b:Point)=>Boolean(a&&Math.abs(a.x-b.x)<.1&&Math.abs(a.y-b.y)<.1)
function InitialViewport({goalId,stage}:{goalId:string;stage:React.RefObject<HTMLDivElement|null>}){
  const initialized=useNodesInitialized(),flow=useReactFlow<ResearchNode,GraphEdge>(),ready=useRef(false)
  useEffect(()=>{
    if(!initialized||!flow.viewportInitialized||ready.current)return
    ready.current=true
    const target=flow.getNode(goalId)||flow.getNodes()[0]
    if((stage.current?.clientWidth||0)<750&&target)void flow.setCenter(target.position.x+(target.measured?.width||280)/2,target.position.y+(target.measured?.height||160)/2,{zoom:.85,duration:0})
    else void flow.fitView({padding:.16,minZoom:.7,maxZoom:1})
  },[initialized,flow.viewportInitialized,flow,goalId,stage])
  return null
}

export function ResearchGraph(props:Props){return <GraphCanvas key={props.snapshot.branch.id} {...props}/>}
function GraphCanvas({snapshot,selected,filter,proofView,onSelect,onSave}:Props){
  const [nodes,setNodes,onNodesChange]=useNodesState<ResearchNode>([])
  const [zoom,setZoom]=useState(100),[legend,setLegend]=useState(false),[saveState,setSaveState]=useState<'saved'|'saving'|'failed'>('saved')
  const flow=useRef<ReactFlowInstance<ResearchNode,GraphEdge>|null>(null),onSaveRef=useRef(onSave),saving=useRef(false),mounted=useRef(true)
  const stage=useRef<HTMLDivElement>(null)
  const optimistic=useRef<Record<string,Point>>({}),pending=useRef<Record<string,Point>>({}),automatic=useRef<Record<string,Point>>({})
  onSaveRef.current=onSave
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
  useEffect(()=>{const element=stage.current;if(!element)return;let width=0,height=0;const observer=new ResizeObserver(([entry])=>{const next=entry.contentRect;if(width&&height&&flow.current){const viewport=flow.current.getViewport();void flow.current.setViewport({...viewport,x:viewport.x+(next.width-width)/2,y:viewport.y+(next.height-height)/2})}width=next.width;height=next.height});observer.observe(element);return()=>observer.disconnect()},[])
  const connections=useMemo(()=>connectionsFor(snapshot),[snapshot.objects,snapshot.proof_plans,snapshot.relations])
  const generated=useMemo(()=>structuredLayout(snapshot.objects,connections,snapshot.project.original_goal_id),[snapshot.objects,connections,snapshot.project.original_goal_id])
  const related=useMemo(()=>new Set(connections.filter(edge=>edge.source===selected||edge.target===selected).flatMap(edge=>[edge.source,edge.target])),[connections,selected])
  const status=(object:ResearchObject)=>snapshot.support.claims[object.revision.id]?.status||snapshot.support.plans[object.revision.id]?.status||object.adoption_state
  const visible=useMemo(()=>{
    let ids=new Set(snapshot.objects.map(object=>object.id))
    if(proofView&&selected){ids=new Set([selected]);let changed=true;while(changed){changed=false;for(const edge of connections.filter(edge=>edge.kind!=='research'))if(ids.has(edge.source)||ids.has(edge.target)){if(!ids.has(edge.source)||!ids.has(edge.target))changed=true;ids.add(edge.source);ids.add(edge.target)}}}
    return snapshot.objects.filter(object=>ids.has(object.id)&&!snapshot.presentation?.hidden_object_ids.includes(object.id)).filter(object=>filter==='all'||(filter==='needs_recheck'&&['needs_recheck','pending_review','disputed'].includes(status(object)))||(filter==='draft'&&object.adoption_state==='draft')||(filter==='failures'&&Boolean(object.revision.payload.outcome)))
  },[snapshot.objects,snapshot.presentation,snapshot.support,connections,selected,proofView,filter])
  useEffect(()=>{
    for(const object of snapshot.objects){
      if(!automatic.current[object.id]){
        const proposed={...generated[object.id]}
        while(Object.entries(automatic.current).some(([id,point])=>id!==object.id&&Math.abs(point.x-proposed.x)<300&&Math.abs(point.y-proposed.y)<250))proposed.y+=280
        automatic.current[object.id]=proposed
      }
      if(optimistic.current[object.id]&&samePoint(snapshot.layout?.positions[object.id],optimistic.current[object.id]))delete optimistic.current[object.id]
    }
    setNodes(previous=>visible.map(object=>{
      const old=previous.find(node=>node.id===object.id)
      return {id:object.id,type:'research',zIndex:10,position:old?.dragging?old.position:optimistic.current[object.id]||snapshot.layout?.positions[object.id]||automatic.current[object.id],selected:object.id===selected,data:{object,status:status(object),anchor:object.id===snapshot.project.original_goal_id,collapsed:Boolean(snapshot.presentation?.collapsed_object_ids.includes(object.id)),focus:!selected?'normal':object.id===selected?'selected':related.has(object.id)?'neighbor':'muted'}}
    }))
  },[visible,snapshot.layout,snapshot.presentation,selected,related,generated,setNodes])
  const edges=useMemo<GraphEdge[]>(()=>{
    const ids=new Set(visible.map(object=>object.id))
    return connections.filter(edge=>ids.has(edge.source)&&ids.has(edge.target)&&(!proofView||edge.kind!=='research')).map(edge=>{
      const direct=edge.source===selected||edge.target===selected,muted=Boolean(selected&&!direct),color=colors[edge.kind]
      return {id:edge.id,source:edge.source,target:edge.target,sourceHandle:'out',targetHandle:'in',type:'research',label:edge.label,className:`connection-${edge.kind}${direct?' connection-active':muted?' connection-muted':''}`,style:{stroke:color,strokeWidth:direct?2.3:edge.kind==='research'?1.4:1.8,strokeDasharray:edge.kind==='research'?'3 6':edge.kind==='assumption'?'8 5':undefined,opacity:muted?.18:edge.kind==='research'?.72:1},markerEnd:{type:MarkerType.ArrowClosed,width:14,height:14,color},labelStyle:{fill:color,fontSize:12,fontWeight:direct?650:450,opacity:muted?.25:1},labelBgStyle:{fill:'#fff',fillOpacity:muted?.6:.94},labelBgPadding:[7,4] as [number,number],labelBgBorderRadius:5,interactionWidth:16,zIndex:direct?5:1} satisfies GraphEdge
    })
  },[connections,visible,proofView,selected])

  async function flush(){
    if(saving.current||!Object.keys(pending.current).length)return
    const batch={...pending.current};pending.current={};saving.current=true;setSaveState('saving')
    let saved=false
    try{saved=await onSaveRef.current(batch)}catch{saved=false}
    saving.current=false
    if(!saved)pending.current={...batch,...pending.current}
    if(mounted.current){setSaveState(saved?'saved':'failed');if(saved&&Object.keys(pending.current).length)void flush()}
  }
  function retain(positions:Record<string,Point>){optimistic.current={...optimistic.current,...positions};pending.current={...pending.current,...positions};setNodes(previous=>previous.map(node=>({...node,position:positions[node.id]||node.position})));void flush()}
  function arrange(){automatic.current={...generated};retain(generated);window.setTimeout(()=>overview(),80)}
  function overview(){void flow.current?.fitView({padding:.18,minZoom:.35,maxZoom:1,duration:300})}
  function focus(){const id=selected||snapshot.project.original_goal_id,target=flow.current?.getNode(id);if(target)void flow.current?.setCenter(target.position.x+(target.measured?.width||280)/2,target.position.y+(target.measured?.height||160)/2,{zoom:1,duration:220})}
  return <section className="graph-pane directed-graph" aria-label="研究地图">
    <div className="graph-toolbar"><div className="graph-title"><strong>{proofView?'证明依赖':'研究地图'}</strong><small>{visible.length} 个对象 · 箭头表示已记录关系</small></div><div className="graph-tools"><button onClick={focus} title="聚焦选中对象，直接关联保持高亮">聚焦</button><button onClick={overview}>全图</button><button onClick={arrange} disabled={!nodes.length}>重新整理</button><button aria-pressed={legend} onClick={()=>setLegend(!legend)}>图例</button></div></div>
    {legend&&<div className="graph-legend" aria-label="关系图例"><span><i className="legend-proof"/>前提与支持方案</span><span><i className="legend-context"/>上下文</span><span><i className="legend-assumption"/>未解除假设</span><span><i className="legend-research"/>研究关系</span><small>从左侧输入，到右侧输出；选中对象突出直接关联。整理与拖动只改变位置。</small></div>}
    <div className="graph-stage" ref={stage}><ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} onNodesChange={onNodesChange} onNodeClick={(_,node)=>onSelect(node.id)} onNodeDragStop={(_,node)=>retain({[node.id]:node.position})} onInit={instance=>{flow.current=instance}} onMove={(_,viewport)=>setZoom(Math.round(viewport.zoom*100))} minZoom={.3} maxZoom={1.8} nodesConnectable={false} deleteKeyCode={null} edgesFocusable={false}><InitialViewport goalId={snapshot.project.original_goal_id} stage={stage}/><Background gap={24} color="#d8e2ef"/></ReactFlow>
      <div className="graph-navigation"><button aria-label="放大地图" onClick={()=>flow.current?.zoomIn({duration:180})}>＋</button><span className="graph-zoom" aria-label="地图缩放比例">{zoom}%</span><button aria-label="缩小地图" onClick={()=>flow.current?.zoomOut({duration:180})}>−</button></div>
      <div className={`layout-status layout-${saveState}`} aria-live="polite">{saveState==='saving'?'正在保存布局…':saveState==='failed'?<><span>布局尚未保存</span><button onClick={()=>void flush()}>重试保存</button></>:'拖动节点后自动保存布局'}</div>
      {!visible.length&&<div className="graph-empty">当前筛选下没有对象</div>}
    </div>
  </section>
}
