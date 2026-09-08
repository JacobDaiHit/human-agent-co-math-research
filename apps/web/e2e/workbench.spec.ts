import { test, expect } from '@playwright/test'
import type { Page, APIRequestContext } from '@playwright/test'
import { spawn } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import { resolve } from 'node:path'

let worker:ChildProcess
const auth={Authorization:'Bearer browser-test-user'}
async function command(api:APIRequestContext,path:string,data:unknown){const response=await api.post('/api'+path,{headers:{...auth,'Idempotency-Key':crypto.randomUUID()},data});expect(response.ok(),await response.text()).toBeTruthy();return response.json()}
async function snapshot(api:APIRequestContext,project:string,branch?:string){const r=await api.get(`/api/projects/${project}/snapshot${branch?`?branch_id=${branch}`:''}`,{headers:auth});expect(r.ok()).toBeTruthy();return r.json()}
async function openProject(page:Page,title:string){await page.goto('/');await page.getByRole('button',{name:`◇ ${title}`,exact:true}).click();await expect(page.getByRole('heading',{name:title,exact:true})).toBeVisible()}
async function selectClaim(page:Page,body:string){await page.getByLabel('研究工作稿').getByTestId('manuscript-block').filter({hasText:body}).click();await expect(page.getByLabel('对象详情',{exact:true})).toBeVisible()}

test.beforeAll(()=>{worker=spawn(resolve('../../.venv/Scripts/python.exe'),['-m','mathagent.runtime.worker','--api-url','http://127.0.0.1:18081','--fake-delay','4'],{windowsHide:true,stdio:'ignore',env:{...process.env,MATHAGENT_WORKER_TOKEN:'browser-test-worker',MATHAGENT_LOAD_ENV:'0',MATHAGENT_ENABLE_REAL_API:'0'}})})
test.afterAll(()=>worker?.kill())

test('manual research, annotations, manuscript CAS, browser drafts, and branch switching',async({page,request})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message))
  await page.goto('/');await page.getByRole('button',{name:'＋ 新建研究',exact:true}).click()
  const dialog=page.getByRole('dialog');await dialog.getByLabel('项目名称').fill('浏览器验收 · 手工研究');await dialog.getByLabel('最初的问题').fill('研究 $H(t)$ 的排序特征值是否可微。');await dialog.getByRole('button',{name:'创建研究',exact:true}).click()
  await expect(page.getByRole('heading',{name:'浏览器验收 · 手工研究',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'＋ 对象',exact:true}).click();await dialog.getByLabel('正文',{exact:true}).fill('引理 L：初始草稿');await dialog.getByRole('button',{name:'保存',exact:true}).click()
  const inspector=page.getByLabel('对象详情',{exact:true});await expect(inspector).toContainText('引理 L：初始草稿')
  await inspector.getByRole('button',{name:'编辑正文',exact:true}).click();await inspector.getByLabel('编辑对象正文').fill('引理 L：尚未提交的修改')
  await inspector.getByRole('button',{name:'关闭对象详情'}).click();await selectClaim(page,'引理 L：初始草稿');await expect(inspector.getByLabel('编辑对象正文')).toHaveValue('引理 L：尚未提交的修改')
  await page.reload();await selectClaim(page,'引理 L：初始草稿');await expect(inspector.getByLabel('编辑对象正文')).toHaveValue('引理 L：尚未提交的修改')
  await inspector.getByRole('button',{name:'保存新版本'}).click();await expect(page.getByRole('status')).toContainText('新版本已保存')
  await inspector.getByRole('tab',{name:'批注',exact:true}).click();await inspector.getByRole('button',{name:'＋ 添加批注'}).click();await dialog.getByLabel('引用原文（可选）').fill('尚未提交的修改');await dialog.getByLabel('批注内容').fill('需要明确交叉点附近的假设。');await dialog.getByRole('button',{name:'保存',exact:true}).click();await expect(inspector).toContainText('需要明确交叉点附近的假设。')
  await inspector.getByRole('button',{name:'关闭对象详情'}).click();await page.getByRole('button',{name:'＋ 工作稿笔记'}).click();await dialog.getByLabel('工作稿笔记').fill('先保留这个尚未完成的想法。');await dialog.getByRole('button',{name:'保存',exact:true}).click();await page.getByRole('button',{name:'编辑笔记',exact:true}).click();await dialog.getByLabel('工作稿笔记正文',{exact:true}).fill('修改后的笔记。');await dialog.getByRole('button',{name:'保存笔记'}).click();await expect(page.getByLabel('研究工作稿')).toContainText('修改后的笔记。')
  await page.getByRole('button',{name:'＋ 分支',exact:true}).click();await dialog.getByLabel('分支名称').fill('解析标签旁支');await dialog.getByLabel('衍生问题（可选，原问题仍保留）').fill('允许交换标签时，该例是否有解析分支？');await dialog.getByRole('button',{name:'保存',exact:true}).click();await expect(page.getByLabel('当前分支')).toHaveValue(/.+/);await expect(page.getByLabel('研究工作稿')).toContainText('允许交换标签')
  const second=await command(request,'/projects',{title:'切换目标项目',body:'第二个独立原问题'});await page.reload();await page.getByRole('button',{name:'◇ 切换目标项目',exact:true}).click();await expect(page.getByRole('heading',{name:'切换目标项目',exact:true})).toBeVisible();expect((await snapshot(request,second.project_id)).project.original_goal_id).toBe(second.object_id||second.original_goal_id)
  await expect(page.getByRole('alert')).toHaveCount(0);expect(errors).toEqual([])
})

test('editing a lemma during independent worker execution preserves old input and quarantines output',async({page,request},testInfo)=>{
  const p=await command(request,'/projects',{title:'运行中修改引理',body:'验证论证依赖的版本隔离。'})
  const l=await command(request,'/objects',{branch_id:p.branch_id,kind:'claim',body:'引理 L_v1：原始陈述'})
  const c=await command(request,'/objects',{branch_id:p.branch_id,kind:'claim',body:'结论 C 依赖于 L'})
  const proof=await command(request,'/proof-plans',{branch_id:p.branch_id,conclusion_revision_id:c.revision_id,body:'由 L 推得 C；这是工作流测试夹具。',premise_revision_ids:[l.revision_id]})
  await openProject(page,'运行中修改引理');await selectClaim(page,'引理 L_v1：原始陈述')
  await page.getByRole('button',{name:'从此处研究',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'开始执行',exact:true}).click()
  await expect.poll(async()=>{const s=await snapshot(request,p.project_id);return s.runs[0]?.state}).toBe('running')
  await page.getByTestId('run-card').getByRole('button',{name:'引理 L_v1：原始陈述',exact:true}).click();const inspector=page.getByLabel('对象详情',{exact:true});await inspector.getByRole('button',{name:'编辑正文',exact:true}).click();await inspector.getByLabel('编辑对象正文').fill('引理 L_v2：补充条件后的陈述');await inspector.getByRole('button',{name:'保存新版本'}).click();await expect(page.getByRole('status')).toContainText('1 个运行仍读取旧版')
  await expect.poll(async()=>{const s=await snapshot(request,p.project_id);return s.runs[0].attempts[0].state},{timeout:15000}).toBe('quarantined')
  const s=await snapshot(request,p.project_id);expect(s.support.plans[proof.revision_id].status).toBe('needs_recheck');const attempt=s.runs[0].attempts[0];expect(attempt.read_set[l.object_id]).toBe(l.revision_id);expect(attempt.checkpoint.completion.output_branch_id).not.toBe(p.branch_id);expect(s.objects.find((o:any)=>o.id===l.object_id).revision.body).toContain('L_v2')
  await inspector.getByRole('tab',{name:'版本',exact:true}).click();await inspector.getByRole('button',{name:/v1/}).click();await expect(inspector).toContainText('引理 L_v1：原始陈述');await expect(inspector).toContainText('引理 L_v2：补充条件后的陈述')
  await page.screenshot({path:testInfo.outputPath('lemma-version-isolation.png'),fullPage:true})
})

test('two editors retain a conflict candidate and resolve it explicitly',async({page,context,request})=>{
  const p=await command(request,'/projects',{title:'并行编辑冲突',body:'并行编辑测试'});await command(request,'/objects',{branch_id:p.branch_id,kind:'claim',body:'同一个引理的 v1'})
  const other=await context.newPage();await openProject(page,'并行编辑冲突');await openProject(other,'并行编辑冲突')
  for(const tab of [page,other]){await selectClaim(tab,'同一个引理的 v1');await tab.getByRole('button',{name:'编辑正文',exact:true}).click()}
  await page.getByLabel('编辑对象正文').fill('第一个编辑者的新内容');await other.getByLabel('编辑对象正文').fill('第二个编辑者的候选内容');await page.getByRole('button',{name:'保存新版本'}).click();await expect(page.getByRole('status')).toContainText('新版本已保存');await other.getByRole('button',{name:'保存新版本'}).click();await expect(other.getByLabel('对象详情',{exact:true})).toContainText('第二个编辑者的候选内容');await other.getByRole('button',{name:'采用候选内容'}).click();await expect(other.getByRole('status')).toContainText('冲突已处理');const s=await snapshot(request,p.project_id);expect(s.conflicts[0].resolution).toBeTruthy();expect(s.objects.some((o:any)=>o.revision.body==='第二个编辑者的候选内容')).toBeTruthy();await other.close()
})

test('graph layout survives new SSE events; mobile view remains usable',async({page,request},testInfo)=>{
  const p=await command(request,'/projects',{title:'地图布局与重连',body:'图与稿同步测试'});const l=await command(request,'/objects',{branch_id:p.branch_id,kind:'claim',body:'可拖动节点'})
  await openProject(page,'地图布局与重连');await page.getByRole('tab',{name:'研究地图',exact:true}).click();const node=page.locator(`[data-id="${l.object_id}"]`);await expect(node).toBeVisible();const box=(await node.boundingBox())!;await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2+90,box.y+box.height/2+70,{steps:8});await page.mouse.up();await expect.poll(async()=>(await snapshot(request,p.project_id)).layout.version).toBeGreaterThan(0)
  const positions=(await snapshot(request,p.project_id)).layout.positions;await command(request,'/objects',{branch_id:p.branch_id,kind:'claim',body:'SSE 新增的命题'});await expect(page.getByLabel('研究地图')).toContainText('SSE 新增的命题');expect((await snapshot(request,p.project_id)).layout.positions).toEqual(positions)
  await page.screenshot({path:testInfo.outputPath('research-map-desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});await page.getByRole('tab',{name:'工作稿',exact:true}).click();await expect(page.getByLabel('研究工作稿')).toContainText('SSE 新增的命题');expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();await page.screenshot({path:testInfo.outputPath('manuscript-mobile.png'),fullPage:true})
})

test('WebMCP tools validate and use the same durable draft path in an injected registry',async({page,request})=>{
  await page.addInitScript(()=>{(window as any).registeredTools={};Object.defineProperty(document,'modelContext',{value:{registerTool(tool:any,{signal}:any){(window as any).registeredTools[tool.name]=tool;signal.addEventListener('abort',()=>delete (window as any).registeredTools[tool.name])}}})})
  await command(request,'/projects',{title:'工具契约验收',body:'WebMCP 测试项目'});await openProject(page,'工具契约验收')
  const names=await page.evaluate(()=>Object.keys((window as any).registeredTools));expect(names.sort()).toEqual(['create_research_draft','read_current_research'])
  expect(await page.evaluate(async()=>{try{await (window as any).registeredTools.create_research_draft.execute({kind:'claim',body:''});return false}catch{return true}})).toBeTruthy()
  const result=await page.evaluate(async()=>{const tools=(window as any).registeredTools;await tools.create_research_draft.execute({kind:'claim',body:'通过同一保存路径创建的草稿'});return tools.read_current_research.execute({})});expect(result.objects.some((o:any)=>o.revision.body==='通过同一保存路径创建的草稿')).toBeTruthy();await expect(page.getByLabel('对象详情',{exact:true})).toContainText('通过同一保存路径创建的草稿')
})

test('fixed exact example exports a downloadable snapshot and preserves the derived branch',async({page},testInfo)=>{
  await page.goto('/');await page.getByRole('button',{name:'打开特征值反例示例 ↗',exact:true}).click()
  await expect(page.getByRole('heading',{name:'Hermitian 交叉与排序 · 可复算示例',exact:true})).toBeVisible()
  await page.getByRole('tab',{name:'工作稿',exact:true}).click()
  await expect(page.getByLabel('研究工作稿').locator('.katex annotation').filter({hasText:String.raw`\det(\lambda I-H)=\lambda^2-t^2`}).first()).toHaveText(String.raw`\det(\lambda I-H)=\lambda^2-t^2`)
  await expect(page.getByLabel('研究工作稿')).toContainText('未调用模型')
  const downloading=page.waitForEvent('download');await page.getByRole('button',{name:'导出',exact:true}).click();const downloaded=await downloading;expect(await downloaded.failure()).toBeNull();expect(downloaded.suggestedFilename()).toContain('.zip');await downloaded.saveAs(testInfo.outputPath('hermitian-export.zip'))
  await page.getByLabel('当前分支').selectOption({label:'analytic-labels'});await expect(page.getByLabel('研究工作稿').locator('.katex annotation').filter({hasText:String.raw`\mu_1(t)=t`}).first()).toHaveText(String.raw`\mu_1(t)=t`);await expect(page.getByLabel('研究工作稿').locator('.katex annotation').filter({hasText:String.raw`\mu_2(t)=-t`}).first()).toHaveText(String.raw`\mu_2(t)=-t`);await expect(page.getByLabel('研究工作稿')).toContainText('这个猜想是否正确')
  await page.getByLabel('研究工作稿').getByTestId('manuscript-block').filter({hasText:String.raw`\mu_1(t)=t`}).scrollIntoViewIfNeeded()
  await page.screenshot({path:testInfo.outputPath('hermitian-derived-research.png'),fullPage:true})
})

test('request settings and completed fake ledger are visible at mobile width',async({page,request})=>{
  await command(request,'/projects',{title:'请求账本界面',body:'请求账本验收问题'})
  await openProject(page,'请求账本界面');await page.setViewportSize({width:390,height:844});await page.getByRole('tab',{name:'运行',exact:true}).click();await page.getByRole('button',{name:'运行设置',exact:true}).click()
  const dialog=page.getByRole('dialog');await dialog.getByLabel('项目累计请求上限').fill('2');await dialog.getByRole('button',{name:'保存设置',exact:true}).click();await expect(page.getByLabel('请求额度与调用账本')).toContainText('剩余 2 / 2')
  await page.getByRole('button',{name:'启动研究',exact:true}).click();await dialog.getByLabel('本任务请求上限').fill('1');await dialog.getByRole('button',{name:'开始执行',exact:true}).click();await expect(page.getByTestId('run-card')).toContainText('本次执行完成',{timeout:15000});await expect(page.getByLabel('请求额度与调用账本')).toContainText('剩余 1 / 2');await page.getByText('查看请求记录',{exact:true}).click();await expect(page.locator('.ledger')).toContainText('模拟执行');await expect(page.locator('.ledger')).toContainText('已使用')
  await page.getByText('执行记录与输入版本',{exact:true}).click();await page.getByRole('button',{name:'查看本次研究产物',exact:true}).click();await expect(page.getByLabel('对象详情',{exact:true})).toContainText('模拟执行产物')
})

test('open annotation and review dialogs keep their original target through SSE revision updates',async({page,request})=>{
  const p=await command(request,'/projects',{title:'对话框版本边界',body:'记录意见时保持明确版本'})
  const l=await command(request,'/objects',{branch_id:p.branch_id,kind:'claim',body:'版本一：待检查句子'})
  await openProject(page,'对话框版本边界');await selectClaim(page,'版本一：待检查句子');const inspector=page.getByLabel('对象详情',{exact:true});const dialog=page.getByRole('dialog')
  await inspector.getByRole('tab',{name:'批注',exact:true}).click();await inspector.getByRole('button',{name:'＋ 添加批注'}).click();await dialog.getByLabel('引用原文（可选）').fill('待检查句子');await dialog.getByLabel('批注内容').fill('这条批注只针对版本一。')
  const v2=await command(request,`/objects/${l.object_id}/revisions`,{branch_id:p.branch_id,expected_revision_id:l.revision_id,body:'版本二：已经改写'});await expect(page.getByLabel('研究工作稿')).toContainText('版本二：已经改写');await expect(dialog.locator('.selected-context')).toContainText('版本一：待检查句子');await dialog.getByRole('button',{name:'保存',exact:true}).click();await expect(dialog).toHaveCount(0);let s=await snapshot(request,p.project_id);expect(s.annotations[0].revision_id).toBe(l.revision_id)
  await inspector.getByRole('tab',{name:'版本',exact:true}).click();await inspector.getByRole('button',{name:/v1/}).click();await expect(inspector).toContainText('这条批注只针对版本一。')
  await inspector.getByRole('tab',{name:'证据',exact:true}).click();await inspector.getByRole('button',{name:'记录审查',exact:true}).click();await dialog.getByLabel('具体检查范围').fill('只检查版本二的局部步骤');await dialog.getByLabel('具体审查意见（每行一项）').fill('需要继续检查边界条件。')
  await command(request,`/objects/${l.object_id}/revisions`,{branch_id:p.branch_id,expected_revision_id:v2.revision_id,body:'版本三：继续改写'});await expect(page.getByLabel('研究工作稿')).toContainText('版本三：继续改写');await expect(dialog.locator('.selected-context')).toContainText('版本二：已经改写');await dialog.getByRole('button',{name:'保存',exact:true}).click();await expect(dialog).toHaveCount(0);s=await snapshot(request,p.project_id);expect(s.reviews[0].target_revision_id).toBe(v2.revision_id)
})




