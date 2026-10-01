import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { normalizeMathDelimiters } from './math'

export const kindLabels:Record<string,string>={problem:'问题',context:'上下文',claim:'命题',argument:'论证',artifact:'研究产物',activity:'活动'}
export const stateLabels:Record<string,string>={idle:'等待新任务',waiting_discussion:'等待讨论回复',waiting:'等待回复',replaced:'已换方向',researching:'研究中',draft:'草稿',pending_review:'待审查',adopted:'人工采用',disputed:'有争议',withdrawn:'已撤回',supported:'有完整支持',conditional:'条件性支持',needs_recheck:'需要重查',no_current_support:'当前缺少支持',queued:'排队中',running:'正在研究',completed:'本次执行完成',paused:'已暂停',pause_requested:'等待暂停生效',cancel_requested:'等待停止生效',cancelled:'已停止',steer_requested:'等待引导生效',interrupted:'执行中断',failed:'运行出错',expired:'租约过期',quarantined:'已隔离',unresolved:'暂未解决',argument_error:'具体论证错误',method_obstruction:'方法障碍',refuted:'存在反驳',unknown:'待对账',budget_exhausted:'额度已耗尽',step_limit:'达到研究步骤上限',reconciliation_required:'等待人工对账',active:'进行中',succeeded:'已完成',rejected:'未接受'}
export const evidenceLabels:Record<string,string>={human_review:'人工审查',llm_review:'模型审查',candidate_proof:'候选论证',numerical_experiment:'数值实验',exact_computation:'精确计算',formal_check:'形式化检查'}
export const shortId=(id:string)=>id.slice(0,8)
export function Badge({state}:{state:string}){return <span className={`badge badge-${state}`}>{stateLabels[state]||({reserved:'已预留',dispatched:'已发出',spent:'已使用',released:'已释放'} as Record<string,string>)[state]||state}</span>}
export function MathText({body}:{body:string}){return <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]} components={{img:({src,alt})=><span className="image-reference">图片引用：{alt||'未提供说明'}{src&&<> · <a href={src} target="_blank" rel="noreferrer noopener">手动打开图片来源</a></>}</span>}}>{normalizeMathDelimiters(body)}</ReactMarkdown>}
export function MathPreview({body}:{body:string}){return <div className="math-preview"><MathText body={body}/></div>}
export function MathInputHint(){return <p className="subtle math-input-hint">数学公式与符号请使用 LaTeX：行内 <code>{'$\\alpha_i^2$'}</code>，独立公式 <code>{'$$\\frac{a}{b}$$'}</code>；也支持 <code>{'\\(...\\)'}</code> 与 <code>{'\\[...\\]'}</code>。代码块按原文保留。</p>}
export function Modal({title,children,onClose}:{title:string;children:ReactNode;onClose:()=>void}){
  const ref=useRef<HTMLDialogElement>(null)
  useEffect(()=>{const dialog=ref.current;dialog?.showModal();return ()=>dialog?.close()},[])
  return <dialog ref={ref} className="modal" onCancel={event=>{event.preventDefault();onClose()}} onClick={event=>{if(event.target===event.currentTarget)onClose()}} aria-labelledby="modal-title"><div className="modal-header"><h2 id="modal-title">{title}</h2><button aria-label="关闭对话框" className="icon-button" onClick={onClose}>×</button></div>{children}</dialog>
}
