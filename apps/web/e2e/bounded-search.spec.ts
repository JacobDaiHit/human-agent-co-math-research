import { expect, test } from '@playwright/test'
import type { APIRequestContext } from '@playwright/test'

const auth={Authorization:'Bearer browser-test-user'}
async function command(api:APIRequestContext,path:string,data:unknown){const response=await api.post('/api'+path,{headers:{...auth,'Idempotency-Key':crypto.randomUUID()},data});expect(response.ok(),await response.text()).toBeTruthy();return response.json()}

test('continuous research shows actual task and no mandatory review controls',async({page,request},testInfo)=>{
  const project=await command(request,'/projects',{title:'浏览器验收 · 连续研究',body:'证明 $x=x$。'})
  const run=await command(request,'/runs',{branch_id:project.branch_id,goal_object_id:project.object_id,autonomous:true,request_budget:12,discussion:true})
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message))
  await page.goto('/');await page.getByRole('button',{name:'◇ 浏览器验收 · 连续研究',exact:true}).click();await page.getByRole('tab',{name:/^运行/}).click()
  const panel=page.getByLabel('连续研究')
  await expect(panel).toContainText('主研究者');await expect(panel).toContainText('研究原题');await expect(panel).toContainText('剩余请求 12')
  await page.getByTestId('run-card').getByRole('button',{name:'任务额度与研究设置',exact:true}).click()
  const dialog=page.getByRole('dialog');await expect(dialog.getByLabel('研究时限（秒）')).toHaveValue('86400')
  await expect(dialog.getByRole('checkbox',{name:'允许按需邀请研究同伴，可独立探索或针对性合作'})).toBeChecked()
  await expect(dialog.getByLabel('同时研究上限（含主研究者）')).toHaveValue('4')
  await expect(dialog.getByRole('checkbox',{name:'允许检索和读取数学文献（闭卷测试请关闭）'})).toBeChecked()
  await expect(dialog.getByText('最多研究步骤',{exact:true})).toHaveCount(0)
  await expect(dialog.getByText('最多审查轮数',{exact:true})).toHaveCount(0)
  await dialog.getByRole('button',{name:'取消',exact:true}).click()
  const response=await request.get('/api/runs/'+run.run_id+'/research',{headers:auth});const state=await response.json()
  state.members.push({run_id:'peer',name:'peer',state:'waiting_discussion',personal_note:{id:'note',body:'独立得到了 $x-x=0$，此前未读取主研究者意见。',payload:{}}})
  state.work.push({id:'peer-work',member_run_id:'peer',goal:'独立研究等式。',state:'running',independent:true,output_revision_id:null})
  state.messages=[{id:'topic',sender_run_id:'peer',recipient_run_id:run.run_id,topic:'等式的两种解释',body:'我得到的推导是 $x-x=0$，请比较，而不是按人数投票。'}]
  state.shared_note={id:'shared',body:'公共工作稿保留两种推导。',payload:{}}
  state.members.push({run_id:'targeted',name:'targeted',state:'idle',personal_note:null})
  state.work.push({id:'targeted-work',member_run_id:'targeted',goal:'推导给定引理的一个局部结论。',state:'running',independent:false,output_revision_id:null})
  state.session.outcome='solved';state.session.answer=null
  state.solution={id:'proof',body:'这是只包含完整证明的提交，没有单独短答案。',payload:{}}
  state.computations=[{job_id:'background-job',run_id:run.run_id,status:'running'},
    {job_id:'finished-job',run_id:'targeted',status:'complete',ref:'revision:result',reason:'signal_exit'}]
  const cache={hit_tokens:10,miss_tokens:90,hit_fraction:0.1,calls_with_cache_usage:2,calls_without_cache_usage:1}
  state.usage_summary={reported_tokens:{prompt_tokens:100,completion_tokens:20,total_tokens:120},all_usage_known:false,cache,
    by_researcher:{lead:{requests:2,input_tokens:10,output_tokens:10,cache:{...cache,hit_tokens:10,miss_tokens:0,hit_fraction:1,calls_without_cache_usage:1}},
      targeted:{requests:1,input_tokens:90,output_tokens:10,cache:{...cache,hit_tokens:0,miss_tokens:90,hit_fraction:0,calls_with_cache_usage:1,calls_without_cache_usage:0}}}}
  state.usage_summary.conversations=[{dialogue_id:'saved-dialogue',member:'lead',reset_reason:'initial',requests:2,cache,prefix_changes:0}]
  await page.route('**/api/runs/'+run.run_id+'/research',route=>route.fulfill({json:state}))
  await page.route('**/api/runs/'+run.run_id+'/calls',route=>route.fulfill({json:{calls:[
    {request_id:'first',call_config:{provider:'fake',model:'local',research_context:{dialogue_id:'saved-dialogue',reset_reason:'initial',previous_input_preserved:null}},response_metadata:{model:'provider-returned-model'},complete:true,usage:{prompt_tokens:10,completion_tokens:10,total_tokens:20,prompt_cache_hit_tokens:0,prompt_cache_miss_tokens:10}},
    {request_id:'second',call_config:{provider:'fake',model:'local',research_context:{dialogue_id:'saved-dialogue',reset_reason:null,previous_input_preserved:true,capacity:1000000,input_estimate:42}},complete:true,usage:{prompt_tokens:90,completion_tokens:10}}
  ]}}))
  await page.reload();await page.getByRole('tab',{name:/^运行/}).click()
  await expect(panel).toContainText('独立探索 · 独立研究等式')
  await expect(panel).toContainText('针对性研究 · 推导给定引理的一个局部结论')
  await expect(panel).toContainText('这是只包含完整证明的提交，没有单独短答案')
  await expect(panel).toContainText('不是程序认证的数学结论')
  await panel.getByText('计算任务（2 项）',{exact:true}).click()
  await expect(panel).toContainText('仍在计算')
  await expect(panel).toContainText('进程被信号终止')
  await expect(panel.getByRole('button',{name:'打开完整计算结果',exact:true})).toBeVisible()
  await panel.getByText('全体成员的实际用量与缓存',{exact:true}).click()
  await expect(panel).toContainText('已报告输入的命中率 10.00%')
  await expect(panel).toContainText('1 次请求未提供缓存数量')
  await expect(panel).toContainText('缺失不是零')
  await expect(panel).toContainText('已报告合计 120')
  await expect(panel).toContainText('输入（含缓存读取）另计')
  await panel.getByText('按对话查看接续与缓存',{exact:true}).click()
  await expect(panel).toContainText('对话 saved-di · 2 次请求 · 开始研究')
  await panel.getByText('个人工作稿',{exact:true}).click();await expect(panel).toContainText('独立得到了')
  await panel.getByText('话题讨论（1 条）',{exact:true}).click();await expect(panel).toContainText('等式的两种解释')
  const trace=page.getByTestId('run-card').getByText('研究步骤、调用配置与可见原输出',{exact:true})
  await trace.click()
  await expect(page.locator('.trace-call').first()).toContainText('缓存命中 0')
  await expect(page.locator('.trace-call').first()).toContainText('请求时选择：local · 提供方返回：provider-returned-model')
  await expect(page.locator('.trace-call').last()).toContainText('缓存命中 未提供')
  await expect(page.locator('.trace-call').last()).toContainText('未返回或历史未记录')
  await expect(page.locator('.trace-call').last()).toContainText('旧输入原样保留（本地接续记录，不代表服务端命中）')
  await page.screenshot({path:testInfo.outputPath('continuous-research.png'),fullPage:true})
  expect(errors).toEqual([])
})
