export interface RunOptions {
  request_budget:number;max_output_tokens:number;request_timeout_seconds:number;autonomous:boolean;
  thinking_mode:'provider_default'|'enabled'|'disabled';
  reasoning_effort:'provider_default'|'low'|'high'|'max';
  unknown_recovery:'stop'|'once';cumulative_output_token_budget:number|null;
  solver_controller:'continuous_research'|'legacy'|'bounded_search_v1';
  discussion:boolean;research_deadline_seconds:number;max_researchers:number;literature:boolean;
}
export const defaultRunOptions:RunOptions={
  request_budget:1000,max_output_tokens:131072,request_timeout_seconds:3600,autonomous:true,
  thinking_mode:'provider_default',reasoning_effort:'provider_default',unknown_recovery:'stop',
  cumulative_output_token_budget:null,solver_controller:'continuous_research',
  discussion:true,research_deadline_seconds:86400,max_researchers:4,literature:true,
}
export function RunOptionsFields({value,onChange,showBudget=true,allowAutonomyChange=true}:{value:RunOptions;onChange:(value:RunOptions)=>void;showBudget?:boolean;allowAutonomyChange?:boolean}){
  const number=(key:keyof RunOptions,label:string,min:number,max:number)=><label className="field-label" key={key}>{label}<input type="number" min={min} max={max} required value={Number(value[key])} onChange={event=>onChange({...value,[key]:Number(event.target.value)})}/></label>
  return <>
    <label className="checkbox-label"><input type="checkbox" disabled={!allowAutonomyChange} checked={value.autonomous} onChange={event=>onChange({...value,autonomous:event.target.checked})}/>连续研究：允许根据计算结果继续推导、调整任务和按需讨论</label>
    {value.autonomous&&<label className="checkbox-label"><input type="checkbox" checked={value.discussion} onChange={event=>onChange({...value,discussion:event.target.checked})}/>允许按需邀请研究同伴，可独立探索或针对性合作</label>}
    {value.autonomous&&<label className="checkbox-label"><input type="checkbox" checked={value.literature} onChange={event=>onChange({...value,literature:event.target.checked})}/>允许检索和读取数学文献（闭卷测试请关闭）</label>}
    <p className="subtle">{value.autonomous?'主研究者和同伴共用总额度。没有固定路线数、推进次数或必经审查。':'单轮草稿：只生成一份研究材料，不自动继续。'}</p>
    <div className="form-grid">{showBudget&&number('request_budget','整题累计请求上限',1,1000)}{value.autonomous&&number('research_deadline_seconds','研究时限（秒）',1,604800)}{value.autonomous&&value.discussion&&number('max_researchers','同时研究上限（含主研究者）',1,16)}</div>
    <details className="run-advanced"><summary>输出与调用设置</summary><div className="form-grid">
      {number('max_output_tokens','单次输出长度上限',1,393216)}{number('request_timeout_seconds','单次请求超时（秒）',1,3600)}
      <label className="field-label">累计输出长度上限<input type="number" min={1} max={10000000} value={value.cumulative_output_token_budget??''} placeholder="不限制" onChange={event=>onChange({...value,cumulative_output_token_budget:event.target.value===''?null:Number(event.target.value)})}/><small>按提供方的输出单位计量，主研究者和同伴共用。</small></label>
      <label className="field-label">结果不明的请求<select value={value.unknown_recovery} onChange={event=>onChange({...value,unknown_recovery:event.target.value as RunOptions['unknown_recovery']})}><option value="stop">停止并等待对账</option><option value="once">在原预算内续试一次</option></select><small>原请求仍计入占用，不会因重试退回费用。</small></label>
      <label className="field-label">DeepSeek 思考模式<select value={value.thinking_mode} onChange={event=>onChange({...value,thinking_mode:event.target.value as RunOptions['thinking_mode'],reasoning_effort:'provider_default'})}><option value="provider_default">提供方默认</option><option value="enabled">开启</option><option value="disabled">关闭</option></select></label>
      <label className="field-label">DeepSeek 思考强度<select value={value.reasoning_effort} onChange={event=>onChange({...value,reasoning_effort:event.target.value as RunOptions['reasoning_effort'],...(event.target.value==='provider_default'?{}:{thinking_mode:'enabled' as const})})}><option value="provider_default">提供方默认</option><option value="low">较低</option><option value="high">较高</option><option value="max">最高</option></select></label>
    </div></details>
  </>
}
