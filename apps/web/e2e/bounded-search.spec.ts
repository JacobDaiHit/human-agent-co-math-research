import { expect, test } from '@playwright/test'
import type { APIRequestContext } from '@playwright/test'

const auth={Authorization:'Bearer browser-test-user'}
async function command(api:APIRequestContext,path:string,data:unknown){const response=await api.post('/api'+path,{headers:{...auth,'Idempotency-Key':crypto.randomUUID()},data});expect(response.ok(),await response.text()).toBeTruthy();return response.json()}

test('bounded-search run exposes its frozen state panel while legacy runs do not',async({page,request})=>{
  const project=await command(request,'/projects',{title:'浏览器验收 · 受控搜索',body:'证明 $x=x$。'})
  const run=await command(request,'/runs',{branch_id:project.branch_id,goal_object_id:project.object_id,provider:'fake',mode:'research',autonomous:true,request_budget:12,max_steps:8,max_children:4,max_depth:2,max_review_rounds:2,max_output_tokens:4096,cumulative_output_token_budget:32768,completion_policy:'reviewed_answer',solver_controller:'bounded_search_v1',search_config:{max_routes:3,active_routes:2,max_route_steps:3,max_repairs:1,final_output_tokens:4096,final_requests:1,check_requests:3,check_output_tokens:12288,deadline_seconds:1800,final_seconds:90,enable_repairs:true,enable_tools:true,enable_memory:true,enable_multi_route:true}})
  const search=await request.get(`/api/runs/${run.run_id}/search`,{headers:auth});expect(search.ok()).toBeTruthy();expect((await search.json()).session.phase).toBe('analysis')
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message))
  const routeOne='route-one',routeTwo='route-two'
  const fixture={session:{root_run_id:run.run_id,phase:'search',config:{max_routes:3,active_routes:2}},routes:[
    {id:routeOne,ordinal:0,state:'ready',card_revision_id:'card-one',candidate_revision_id:null,progress:{status:'progress',open_subgoals:['证明 $a=b$。']},repairs:0,priority:0},
    {id:routeTwo,ordinal:1,state:'paused',card_revision_id:'card-two',candidate_revision_id:null,progress:{status:'stalled',open_subgoals:['检查 $n=2$。']},repairs:1,priority:-1},
  ],gaps:[{id:'gap-one',route_id:routeOne,target_revision_id:'revision-one',kind:'missing_argument',details:{anchor:'步骤 $2$',detail:'需要说明 $a\\ne0$。',evidence_revision_ids:['evidence-one']},state:'open'}],memory:[{id:'memory-one'}],decisions:[{sequence:2,action:'dispatch_advance',details:{route_id:routeOne,reason:'new_subgoal'}}],budget:{explore:{remaining_requests:4,remaining_output_tokens:16000,held_requests:1},check:{remaining_requests:3,remaining_output_tokens:12288,held_requests:0},final:{remaining_requests:1,remaining_output_tokens:4096,held_requests:1}}}
  await page.route(`**/api/runs/${run.run_id}/search`,route=>route.fulfill({contentType:'application/json',body:JSON.stringify(fixture)}))
  const interventions:{url:string;body:unknown}[]=[]
  await page.route(`**/api/runs/${run.run_id}/search/routes/*/interventions`,async route=>{interventions.push({url:route.request().url(),body:route.request().postDataJSON()});await route.fulfill({contentType:'application/json',body:JSON.stringify({ok:true})})})
  await page.goto('/');await page.getByRole('button',{name:'◇ 浏览器验收 · 受控搜索',exact:true}).click();await page.getByRole('tab',{name:/^运行/}).click()
  const card=page.getByTestId('run-card');const panel=card.getByLabel('受控搜索状态');await expect(panel).toContainText('受控搜索 · search');await expect(panel).toContainText('路线 1');await expect(panel).toContainText('路线 2');await expect(panel).toContainText('需要说明');await expect(panel.getByLabel('搜索预算')).toContainText('探索')
  await panel.getByRole('button',{name:'暂停路线',exact:true}).click();await expect.poll(()=>interventions.length).toBe(1);expect(interventions[0]).toEqual({url:`http://127.0.0.1:18081/api/runs/${run.run_id}/search/routes/${routeOne}/interventions`,body:{action:'pause'}})
  await panel.getByRole('button',{name:'恢复路线',exact:true}).click();await expect.poll(()=>interventions.length).toBe(2);expect(interventions[1].body).toEqual({action:'resume'})
  await panel.getByRole('button',{name:'提高优先级',exact:true}).first().click();await expect.poll(()=>interventions.length).toBe(3);expect(interventions[2].body).toEqual({action:'priority',priority:10})
  await card.getByRole('button',{name:'任务额度与研究设置',exact:true}).click();await expect(page.getByRole('dialog')).toContainText('配置已在启动时冻结')
  expect(errors).toEqual([])
})
