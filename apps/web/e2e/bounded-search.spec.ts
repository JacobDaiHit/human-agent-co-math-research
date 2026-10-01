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
  const dialog=page.getByRole('dialog');await expect(dialog.getByLabel('研究时限（秒）')).toHaveValue('1800')
  await expect(dialog.getByRole('checkbox',{name:'允许邀请一名研究同伴，先独立探索，再交换推导'})).toBeChecked()
  await expect(dialog.getByText('最多研究步骤',{exact:true})).toHaveCount(0)
  await expect(dialog.getByText('最多审查轮数',{exact:true})).toHaveCount(0)
  await dialog.getByRole('button',{name:'取消',exact:true}).click()
  const response=await request.get('/api/runs/'+run.run_id+'/research',{headers:auth});const state=await response.json()
  state.members.push({run_id:'peer',name:'peer',state:'waiting_discussion',personal_note:{id:'note',body:'独立得到了 $x-x=0$，此前未读取主研究者意见。',payload:{}}})
  state.work.push({id:'peer-work',member_run_id:'peer',goal:'独立研究等式。',state:'running',independent:true,output_revision_id:null})
  state.messages=[{id:'topic',sender_run_id:'peer',recipient_run_id:run.run_id,topic:'等式的两种解释',body:'我得到的推导是 $x-x=0$，请比较，而不是按人数投票。'}]
  state.shared_note={id:'shared',body:'公共工作稿保留两种推导。',payload:{}}
  await page.route('**/api/runs/'+run.run_id+'/research',route=>route.fulfill({json:state}))
  await page.reload();await page.getByRole('tab',{name:/^运行/}).click()
  await expect(panel).toContainText('独立探索 · 独立研究等式')
  await panel.getByText('个人工作稿',{exact:true}).click();await expect(panel).toContainText('独立得到了')
  await panel.getByText('话题讨论（1 条）',{exact:true}).click();await expect(panel).toContainText('等式的两种解释')
  await page.screenshot({path:testInfo.outputPath('continuous-research.png'),fullPage:true})
  expect(errors).toEqual([])
})
