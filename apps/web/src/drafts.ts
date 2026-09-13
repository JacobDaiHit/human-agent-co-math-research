import type { Revision } from './api'

export interface ObjectDraft {base:Revision;body:string;role:string}
const memory=new Map<string,ObjectDraft>()
export const draftKey=(branch:string,object:string)=>`mathagent-draft:${branch}:${object}`
export function getDraft(key:string):ObjectDraft|null{
  if(memory.has(key))return memory.get(key)!
  try{const d=JSON.parse(sessionStorage.getItem(key)||'null');if(d&&typeof d.body==='string'&&typeof d.role==='string'&&typeof d.base?.id==='string'&&d.base.payload)return d}catch{/* Storage can be unavailable in a private browser. */}
  return null
}
export function saveDraft(key:string,value:ObjectDraft|null){
  if(value)memory.set(key,value);else memory.delete(key)
  try{if(value)sessionStorage.setItem(key,JSON.stringify(value));else sessionStorage.removeItem(key)}catch{/* The in-memory draft still survives object navigation. */}
}
export function clearDeletedDrafts(_branch:string,objectIds:string[]){
  const deleted=new Set(objectIds)
  for(const key of memory.keys())if(key.startsWith('mathagent-draft:')&&deleted.has(key.slice(key.lastIndexOf(':')+1)))memory.delete(key)
  try{for(let i=sessionStorage.length-1;i>=0;i--){const key=sessionStorage.key(i);if(key?.startsWith('mathagent-draft:')&&deleted.has(key.slice(key.lastIndexOf(':')+1)))sessionStorage.removeItem(key)}}catch{/* Storage can be unavailable in a private browser. */}
}
