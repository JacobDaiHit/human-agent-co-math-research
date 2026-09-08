import { BaseEdge, getBezierPath, useNodes } from '@xyflow/react'
import type { EdgeProps } from '@xyflow/react'

type Point={x:number;y:number}
type Rectangle=Point&{id:string;width:number;height:number}
const padding=18
function inside(point:Point,rect:Rectangle,gap=0){return point.x>rect.x-gap&&point.x<rect.x+rect.width+gap&&point.y>rect.y-gap&&point.y<rect.y+rect.height+gap}
function blocked(a:Point,b:Point,rect:Rectangle,gap=0){
  const left=rect.x-gap,right=rect.x+rect.width+gap,top=rect.y-gap,bottom=rect.y+rect.height+gap
  let low=0,high=1
  for(const [start,delta,min,max] of [[a.x,b.x-a.x,left,right],[a.y,b.y-a.y,top,bottom]]){
    if(Math.abs(delta)<.001){if(start<=min||start>=max)return false}
    else{const first=(min-start)/delta,last=(max-start)/delta;low=Math.max(low,Math.min(first,last));high=Math.min(high,Math.max(first,last));if(low>=high)return false}
  }
  return low<high&&high>0&&low<1
}
function clear(points:Point[],obstacles:Rectangle[]){return points.every((point,index)=>index===0||!obstacles.some(rect=>blocked(points[index-1],point,rect,8)))}
function clean(points:Point[]){return points.filter((point,index)=>index===0||point.x!==points[index-1].x||point.y!==points[index-1].y).filter((point,index,all)=>!index||index===all.length-1||!((point.x===all[index-1].x&&point.x===all[index+1].x)||(point.y===all[index-1].y&&point.y===all[index+1].y)))}
function roundedPath(points:Point[]){
  let path=`M ${points[0].x} ${points[0].y}`
  for(let index=1;index<points.length-1;index++){
    const before=points[index-1],point=points[index],after=points[index+1],first=Math.hypot(point.x-before.x,point.y-before.y),last=Math.hypot(after.x-point.x,after.y-point.y),radius=Math.min(10,first/2,last/2)
    path+=` L ${point.x+(before.x-point.x)*radius/first} ${point.y+(before.y-point.y)*radius/first} Q ${point.x} ${point.y} ${point.x+(after.x-point.x)*radius/last} ${point.y+(after.y-point.y)*radius/last}`
  }
  return path+` L ${points[points.length-1].x} ${points[points.length-1].y}`
}
function curveHits(path:string,obstacles:Rectangle[]){
  const numbers=path.match(/-?\d*\.?\d+(?:e[-+]?\d+)?/gi)?.map(Number)
  if(!numbers||numbers.length!==8)return false
  const [x0,y0,x1,y1,x2,y2,x3,y3]=numbers
  let before={x:x0,y:y0}
  for(let index=1;index<=48;index++){const t=index/48,u=1-t,point={x:u*u*u*x0+3*u*u*t*x1+3*u*t*t*x2+t*t*t*x3,y:u*u*u*y0+3*u*u*t*y1+3*u*t*t*y2+t*t*t*y3};if(obstacles.some(rect=>blocked(before,point,rect,8)))return true;before=point}
  return false
}

/** Only detour when the normal curve crosses another card. Every drag updates the obstacles. */
export function RoutedEdge(props:EdgeProps){
  const nodes=useNodes(),rectangles=nodes.map(node=>({id:node.id,x:node.position.x,y:node.position.y,width:node.measured?.width||280,height:node.measured?.height||210}))
  const obstacles=rectangles.filter(rect=>rect.id!==props.source&&rect.id!==props.target)
  let [path,labelX,labelY]=getBezierPath({...props,curvature:.2})
  if(curveHits(path,obstacles)){
    const source={x:props.sourceX,y:props.sourceY},target={x:props.targetX,y:props.targetY},start={x:source.x+24,y:source.y},end={x:target.x-24,y:target.y}
    if(!rectangles.some(rect=>inside(start,rect,8)||inside(end,rect,8))){
      const lanes=[...new Set([source.y,target.y,...rectangles.flatMap(rect=>[rect.y-padding,rect.y+rect.height+padding])])]
      let best:Point[]|undefined,bestLength=Infinity
      const consider=(middle:Point[])=>{if(!clear(middle,rectangles))return;const points=clean([source,...middle,target]),length=points.reduce((total,point,index)=>total+(index?Math.hypot(point.x-points[index-1].x,point.y-points[index-1].y):0),0)+points.length*10;if(length<bestLength){best=points;bestLength=length}}
      for(const lane of lanes)consider([start,{x:start.x,y:lane},{x:end.x,y:lane},end])
      if(!best){
        const sides=[Math.min(...rectangles.map(rect=>rect.x))-padding,Math.max(...rectangles.map(rect=>rect.x+rect.width))+padding]
        const sourceLanes=[...lanes].sort((a,b)=>Math.abs(a-source.y)-Math.abs(b-source.y)).slice(0,12),targetLanes=[...lanes].sort((a,b)=>Math.abs(a-target.y)-Math.abs(b-target.y)).slice(0,12)
        for(const from of sourceLanes)for(const to of targetLanes)for(const side of sides)consider([start,{x:start.x,y:from},{x:side,y:from},{x:side,y:to},{x:end.x,y:to},end])
      }
      if(best){path=roundedPath(best);const segments=best.slice(1).map((point,index)=>({a:best![index],b:point,length:Math.hypot(point.x-best![index].x,point.y-best![index].y)})).sort((a,b)=>b.length-a.length);labelX=(segments[0].a.x+segments[0].b.x)/2;labelY=(segments[0].a.y+segments[0].b.y)/2}
    }
  }
  return <BaseEdge id={props.id} path={path} markerEnd={props.markerEnd} style={props.style} interactionWidth={props.interactionWidth} label={props.label} labelX={labelX} labelY={labelY} labelStyle={props.labelStyle} labelShowBg={props.labelShowBg} labelBgStyle={props.labelBgStyle} labelBgPadding={props.labelBgPadding} labelBgBorderRadius={props.labelBgBorderRadius}/>
}
