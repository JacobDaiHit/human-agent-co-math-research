export interface RunOptions {
  request_budget:number;max_steps:number;max_review_rounds:number;max_children:number;max_depth:number;
  max_output_tokens:number;request_timeout_seconds:number;autonomous:boolean;
  thinking_mode:'provider_default'|'enabled'|'disabled';reasoning_effort:'provider_default'|'low'|'high'|'max';
  completion_policy:'draft'|'reviewed_answer';
  length_recovery:'none'|'high';
  unknown_recovery:'stop'|'once';
  cumulative_output_token_budget:number|null;
  solver_controller:'legacy'|'bounded_search_v1';
  search_config:SearchConfig;
}

export interface SearchConfig {max_routes:number;active_routes:number;max_route_steps:number;max_repairs:number;final_output_tokens:number;final_requests:number;check_requests:number;check_output_tokens:number;deadline_seconds:number;final_seconds:number;enable_repairs:boolean;enable_tools:boolean;enable_memory:boolean;enable_multi_route:boolean}
export const defaultSearchConfig:SearchConfig={max_routes:3,active_routes:2,max_route_steps:3,max_repairs:1,final_output_tokens:4096,final_requests:1,check_requests:3,check_output_tokens:12288,deadline_seconds:1800,final_seconds:90,enable_repairs:true,enable_tools:true,enable_memory:true,enable_multi_route:true}
export const defaultRunOptions:RunOptions={request_budget:8,max_steps:8,max_review_rounds:2,max_children:2,max_depth:2,max_output_tokens:4096,request_timeout_seconds:120,autonomous:true,thinking_mode:'provider_default',reasoning_effort:'provider_default',completion_policy:'draft',length_recovery:'none',unknown_recovery:'stop',cumulative_output_token_budget:null,solver_controller:'legacy',search_config:defaultSearchConfig}

export function RunOptionsFields({value,onChange,showBudget=true}:{value:RunOptions;onChange:(value:RunOptions)=>void;showBudget?:boolean}){
  const number=(key:keyof RunOptions,label:string,min:number,max:number)=><label className="field-label" key={key}>{label}<input type="number" min={min} max={max} required value={Number(value[key])} onChange={event=>onChange({...value,[key]:Number(event.target.value)})}/></label>
  return <>
    <label className="field-label">求解控制器<select aria-label="求解控制器" value={value.solver_controller} onChange={event=>{const solver_controller=event.target.value as RunOptions['solver_controller'];onChange(solver_controller==='bounded_search_v1'?{...value,solver_controller,autonomous:true,request_budget:12,cumulative_output_token_budget:value.cumulative_output_token_budget??32768,search_config:{...value.search_config,final_output_tokens:4096,check_output_tokens:12288}}:{...value,solver_controller})}}><option value="legacy">传统研究流程</option><option value="bounded_search_v1">受控多路线搜索</option></select></label>
    {value.solver_controller==='bounded_search_v1'&&<p className="subtle">控制器会在固定额度内安排路线、局部检查和收尾；数学候选仍需保存并按实际证据审查。</p>}
    <label className="checkbox-label"><input type="checkbox" checked={value.autonomous} onChange={event=>onChange({...value,autonomous:event.target.checked})}/>允许在额度内连续研究、调用项目操作并请求审查</label>
    <p className="subtle">{value.autonomous?'研究步骤、审查和子任务共用已授权额度；达到上限后保存进度。':'手动单轮模式：本次仅生成一份研究草稿或审查意见。'}</p>
    <div className="form-grid">{showBudget&&number('request_budget','本任务请求上限',1,1000)}{value.autonomous&&number('max_steps','最多研究步骤',1,40)}</div>
    <details className="run-advanced"><summary>研究与输出限制</summary><div className="form-grid">
      {value.autonomous&&value.solver_controller==='legacy'&&<>{number('max_review_rounds','最多审查修订轮数',0,2)}{number('max_children','最多子任务',0,12)}{number('max_depth','子任务最大层数',0,4)}</>}
      {value.autonomous&&<label className="field-label">完成条件<select value={value.completion_policy} onChange={event=>onChange({...value,completion_policy:event.target.value as RunOptions['completion_policy']})}><option value="draft">允许以研究草稿收尾</option><option value="reviewed_answer">交付经独立审查的明确答案</option></select></label>}
      <label className="field-label">DeepSeek 输出截断恢复<select value={value.length_recovery} onChange={event=>onChange({...value,length_recovery:event.target.value as RunOptions['length_recovery']})}><option value="none">保存失败并停止</option><option value="high">在原额度内以 High 恢复一次</option></select></label>
      <label className="field-label">传输中断后结果不明<select value={value.unknown_recovery} onChange={event=>onChange({...value,unknown_recovery:event.target.value as RunOptions['unknown_recovery']})}><option value="stop">停止并等待对账</option><option value="once">保留占用，原预算内续试一次</option></select><small>主任务与子任务共享一次续试；旧请求费用仍待对账。</small></label>
      {number('max_output_tokens','单次输出 token 上限',256,65536)}{number('request_timeout_seconds','单次请求超时（秒）',1,600)}
      <label className="field-label">累计输出 token 上限<input type="number" min={256} max={10000000} value={value.cumulative_output_token_budget??''} placeholder="不限制" onChange={event=>onChange({...value,cumulative_output_token_budget:event.target.value===''?null:Number(event.target.value)})}/>{value.solver_controller==='bounded_search_v1'&&value.cumulative_output_token_budget!==null&&value.cumulative_output_token_budget<value.search_config.final_output_tokens+value.search_config.check_output_tokens+512&&<small>受控搜索至少需要最终输出、检查输出及 $512$ token 余量；当前值会被后端拒绝。</small>}</label>
      <label className="field-label">DeepSeek 思考模式<select value={value.thinking_mode} onChange={event=>onChange({...value,thinking_mode:event.target.value as RunOptions['thinking_mode'],reasoning_effort:'provider_default'})}><option value="provider_default">提供方默认</option><option value="enabled">开启</option><option value="disabled">关闭</option></select></label>
      <label className="field-label">DeepSeek 思考强度<select value={value.reasoning_effort} onChange={event=>onChange({...value,reasoning_effort:event.target.value as RunOptions['reasoning_effort'],...(event.target.value==='provider_default'?{}:{thinking_mode:'enabled' as const})})}><option value="provider_default">提供方默认</option><option value="low">Low</option><option value="high">High</option><option value="max">Max</option></select></label>
    </div></details>
    {value.solver_controller==='bounded_search_v1'&&<details className="run-advanced"><summary>受控搜索高级设置</summary><div className="form-grid">
      {(['max_routes','active_routes','max_route_steps','max_repairs','final_output_tokens','final_requests','check_requests','check_output_tokens','deadline_seconds','final_seconds'] as (keyof SearchConfig)[]).map(key=><label className="field-label" key={key}>{({max_routes:'候选路线数',active_routes:'同时活跃路线',max_route_steps:'单路线步骤上限',max_repairs:'每路线修复次数',final_output_tokens:'最终输出 token',final_requests:'最终请求保留',check_requests:'检查请求额度',check_output_tokens:'检查输出 token',deadline_seconds:'会话期限（秒）',final_seconds:'收尾预留（秒）'} as Record<string,string>)[key]}<input type="number" value={value.search_config[key] as number} min={key==='max_routes'?1:key==='active_routes'?1:key==='max_route_steps'?1:key==='max_repairs'||key==='check_requests'||key==='check_output_tokens'?0:key==='final_output_tokens'?256:1} max={key==='max_routes'?3:key==='active_routes'?2:key==='max_route_steps'?8:key==='max_repairs'?2:key==='final_requests'?3:key==='check_requests'?12:undefined} onChange={event=>onChange({...value,search_config:{...value.search_config,[key]:Number(event.target.value)}})}/></label>)}
      <div className="choice-list">{(['enable_repairs','enable_tools','enable_memory','enable_multi_route'] as (keyof SearchConfig)[]).map(key=><label className="checkbox-label" key={key}><input type="checkbox" checked={value.search_config[key] as boolean} onChange={event=>onChange({...value,search_config:{...value.search_config,[key]:event.target.checked}})}/>{({enable_repairs:'启用定点修复',enable_tools:'启用工具检查',enable_memory:'启用题内记忆',enable_multi_route:'启用多路线'} as Record<string,string>)[key]}</label>)}</div>
    </div></details>}
  </>
}
