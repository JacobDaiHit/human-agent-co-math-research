export interface RunOptions {
  request_budget:number;max_steps:number;max_review_rounds:number;max_children:number;max_depth:number;
  max_output_tokens:number;request_timeout_seconds:number;autonomous:boolean;
}

export const defaultRunOptions:RunOptions={request_budget:8,max_steps:8,max_review_rounds:2,max_children:2,max_depth:2,max_output_tokens:4096,request_timeout_seconds:120,autonomous:true}

export function RunOptionsFields({value,onChange,showBudget=true}:{value:RunOptions;onChange:(value:RunOptions)=>void;showBudget?:boolean}){
  const number=(key:keyof RunOptions,label:string,min:number,max:number)=><label className="field-label" key={key}>{label}<input type="number" min={min} max={max} required value={Number(value[key])} onChange={event=>onChange({...value,[key]:Number(event.target.value)})}/></label>
  return <>
    <label className="checkbox-label"><input type="checkbox" checked={value.autonomous} onChange={event=>onChange({...value,autonomous:event.target.checked})}/>允许在额度内连续研究、调用项目操作并请求审查</label>
    <p className="subtle">{value.autonomous?'研究步骤、审查和子任务共用已授权额度；达到上限后保存进度。':'手动单轮模式：本次仅生成一份研究草稿或审查意见。'}</p>
    <div className="form-grid">{showBudget&&number('request_budget','本任务请求上限',1,1000)}{value.autonomous&&number('max_steps','最多研究步骤',1,40)}</div>
    <details className="run-advanced"><summary>研究与输出限制</summary><div className="form-grid">
      {value.autonomous&&<>{number('max_review_rounds','最多审查修订轮数',0,2)}{number('max_children','最多子任务',0,12)}{number('max_depth','子任务最大层数',0,4)}</>}
      {number('max_output_tokens','单次输出 token 上限',256,16384)}{number('request_timeout_seconds','单次请求超时（秒）',1,600)}
    </div></details>
  </>
}
