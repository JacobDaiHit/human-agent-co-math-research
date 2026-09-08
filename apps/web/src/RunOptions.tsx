export interface RunOptions {
  request_budget:number;max_steps:number;max_review_rounds:number;max_children:number;max_depth:number;
  max_output_tokens:number;request_timeout_seconds:number;autonomous:boolean;
  thinking_mode:'provider_default'|'enabled'|'disabled';reasoning_effort:'provider_default'|'low'|'high'|'max';
  completion_policy:'draft'|'reviewed_answer';
}

export const defaultRunOptions:RunOptions={request_budget:8,max_steps:8,max_review_rounds:2,max_children:2,max_depth:2,max_output_tokens:4096,request_timeout_seconds:120,autonomous:true,thinking_mode:'provider_default',reasoning_effort:'provider_default',completion_policy:'draft'}

export function RunOptionsFields({value,onChange,showBudget=true}:{value:RunOptions;onChange:(value:RunOptions)=>void;showBudget?:boolean}){
  const number=(key:keyof RunOptions,label:string,min:number,max:number)=><label className="field-label" key={key}>{label}<input type="number" min={min} max={max} required value={Number(value[key])} onChange={event=>onChange({...value,[key]:Number(event.target.value)})}/></label>
  return <>
    <label className="checkbox-label"><input type="checkbox" checked={value.autonomous} onChange={event=>onChange({...value,autonomous:event.target.checked})}/>允许在额度内连续研究、调用项目操作并请求审查</label>
    <p className="subtle">{value.autonomous?'研究步骤、审查和子任务共用已授权额度；达到上限后保存进度。':'手动单轮模式：本次仅生成一份研究草稿或审查意见。'}</p>
    <div className="form-grid">{showBudget&&number('request_budget','本任务请求上限',1,1000)}{value.autonomous&&number('max_steps','最多研究步骤',1,40)}</div>
    <details className="run-advanced"><summary>研究与输出限制</summary><div className="form-grid">
      {value.autonomous&&<>{number('max_review_rounds','最多审查修订轮数',0,2)}{number('max_children','最多子任务',0,12)}{number('max_depth','子任务最大层数',0,4)}</>}
      {value.autonomous&&<label className="field-label">完成条件<select value={value.completion_policy} onChange={event=>onChange({...value,completion_policy:event.target.value as RunOptions['completion_policy']})}><option value="draft">允许以研究草稿收尾</option><option value="reviewed_answer">交付经独立审查的明确答案</option></select></label>}
      {number('max_output_tokens','单次输出 token 上限',256,65536)}{number('request_timeout_seconds','单次请求超时（秒）',1,600)}
      <label className="field-label">DeepSeek 思考模式<select value={value.thinking_mode} onChange={event=>onChange({...value,thinking_mode:event.target.value as RunOptions['thinking_mode'],reasoning_effort:'provider_default'})}><option value="provider_default">提供方默认</option><option value="enabled">开启</option><option value="disabled">关闭</option></select></label>
      <label className="field-label">DeepSeek 思考强度<select value={value.reasoning_effort} onChange={event=>onChange({...value,reasoning_effort:event.target.value as RunOptions['reasoning_effort'],...(event.target.value==='provider_default'?{}:{thinking_mode:'enabled' as const})})}><option value="provider_default">提供方默认</option><option value="low">Low</option><option value="high">High</option><option value="max">Max</option></select></label>
    </div></details>
  </>
}
