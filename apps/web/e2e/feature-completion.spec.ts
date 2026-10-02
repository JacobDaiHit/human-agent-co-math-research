import { expect, test } from '@playwright/test'
import type { APIRequestContext, Page } from '@playwright/test'

const human={Authorization:'Bearer browser-test-user'}
const worker={Authorization:'Bearer browser-test-worker'}
async function command(api:APIRequestContext,path:string,data:unknown,asWorker=false){
  const response=await api.post('/api'+path,{headers:{...(asWorker?worker:human),'Idempotency-Key':crypto.randomUUID()},data})
  expect(response.ok(),await response.text()).toBeTruthy();return response.json()
}
async function snapshot(api:APIRequestContext,project:string){const response=await api.get(`/api/projects/${project}/snapshot`,{headers:human});expect(response.ok()).toBeTruthy();return response.json()}
async function open(page:Page,title:string){await page.goto('/');await page.getByRole('button',{name:`◇ ${title}`,exact:true}).click();await expect(page.getByRole('heading',{name:title,exact:true})).toBeVisible()}
async function select(page:Page,body:string){await page.getByLabel('研究工作稿').getByTestId('manuscript-block').filter({hasText:body}).click()}
async function centered(page:Page,objectId:string){await expect.poll(async()=>{const stage=(await page.locator('.graph-stage').boundingBox())!,node=(await page.locator(`.react-flow__node[data-id="${objectId}"]`).boundingBox())!;return Math.hypot(node.x+node.width/2-stage.x-stage.width/2,node.y+node.height/2-stage.y-stage.height/2)}).toBeLessThan(3)}
async function settledGraph(page:Page){let previous='',stable=0;await expect.poll(async()=>{const current=await page.locator('.react-flow__viewport').getAttribute('style')||'';stable=current===previous?stable+1:0;previous=current;return stable}).toBeGreaterThan(1)}
async function cardIntersections(page:Page,state:any){
  const revisions=new Map<string,string>(state.objects.map((object:any)=>[object.revision.id,object.id])),ends:Record<string,string[]>={}
  for(const proof of state.proof_plans){const argument=revisions.get(proof.revision_id)!;for(const [kind,ids] of [['premise',proof.premise_revision_ids],['context',proof.context_revision_ids],['assumption',proof.assumption_revision_ids]] as [string,string[]][])for(const id of ids)ends[`${kind}-${id}-${proof.revision_id}`]=[revisions.get(id)!,argument];ends[`conclusion-${proof.revision_id}`]=[argument,revisions.get(proof.conclusion_revision_id)!]}
  for(const relation of state.relations)ends[relation.id]=[relation.source_id,relation.target_id]
  return page.evaluate(endpoints=>{
    const cards=[...document.querySelectorAll('.react-flow__node')].map(node=>({id:node.getAttribute('data-id')!,rect:node.querySelector('.research-node')!.getBoundingClientRect()})),intersections:string[]=[]
    let samples=0
    for(const edge of document.querySelectorAll('.react-flow__edge')){const id=edge.getAttribute('data-id')!,path=edge.querySelector('.react-flow__edge-path') as SVGPathElement,length=path.getTotalLength(),matrix=path.getScreenCTM()!;for(let distance=0;distance<=length;distance+=3){samples++;const point=path.getPointAtLength(distance).matrixTransform(matrix);for(const card of cards)if(!endpoints[id]?.includes(card.id)&&point.x>card.rect.left+2&&point.x<card.rect.right-2&&point.y>card.rect.top+2&&point.y<card.rect.bottom-2)intersections.push(`${id} crosses ${card.id}`)}}
    return {samples,intersections:[...new Set(intersections)]}
  },ends)
}

test('failure form pins target and assumptions while new revisions arrive',async({page,request})=>{
  const project=await command(request,'/projects',{title:'一期补齐 · 失败范围',body:'寻找猜想的适用条件。'})
  const claim=await command(request,'/objects',{branch_id:project.branch_id,kind:'claim',body:'失败目标 v1：每个实数均为正数。'})
  const premise=await command(request,'/objects',{branch_id:project.branch_id,kind:'context',body:'只考虑实数域。',payload:{role:'assumption'}})
  await open(page,'一期补齐 · 失败范围');await select(page,'失败目标 v1：每个实数均为正数。');await page.getByRole('button',{name:'记录尝试',exact:true}).click()
  const dialog=page.getByRole('dialog');await expect(dialog.getByLabel('本次尝试的目标版本')).toHaveValue(claim.revision_id)
  await dialog.getByLabel('本次尝试的结果').selectOption('refuted');await dialog.getByLabel('证据与适用范围').fill('只反驳当前全称命题。');await dialog.getByLabel('目标、尝试过程与失败位置').fill('取 x=0，即不满足严格大于零。');await dialog.getByRole('group',{name:'本次使用的前提版本'}).getByRole('checkbox',{name:/只考虑实数域/}).check();await dialog.getByLabel('证据说明',{exact:true}).fill('0 是实数且 0 > 0 为假。');await dialog.getByLabel('值得重试的条件（每行一项）').fill('把目标改成正实数后重新检查。')
  await command(request,`/objects/${claim.object_id}/revisions`,{branch_id:project.branch_id,expected_revision_id:claim.revision_id,body:'失败目标 v2：每个正实数均为正数。'})
  await expect(page.getByLabel('研究工作稿')).toContainText('失败目标 v2');await expect(dialog.getByLabel('本次尝试的目标版本')).toHaveValue(claim.revision_id)
  await dialog.getByRole('button',{name:'保存',exact:true}).click();await expect(dialog).toHaveCount(0)
  const result=await snapshot(request,project.project_id);const failure=result.objects.find((o:any)=>o.revision.payload.artifact_type==='failure')
  expect(failure.revision.payload.target_revision_id).toBe(claim.revision_id);expect(failure.revision.payload.assumption_revision_ids).toEqual([premise.revision_id]);expect(failure.revision.payload.outcome).toBe('refuted');expect(failure.revision.payload.retry_conditions).toEqual(['把目标改成正实数后重新检查。'])
  await expect(page.getByLabel('对象详情',{exact:true})).toContainText('只反驳当前全称命题。');await expect(page.getByLabel('对象详情',{exact:true})).toContainText(claim.revision_id)
})

test('manual source metadata survives reload without opening the source URL',async({page,request})=>{
  const external:string[]=[];page.on('request',req=>{if(req.url().startsWith('https://source.invalid'))external.push(req.url())})
  const project=await command(request,'/projects',{title:'一期补齐 · 来源定位',body:'把原始来源作为可定位的研究材料。'})
  await open(page,'一期补齐 · 来源定位');await page.getByRole('button',{name:'录入来源',exact:true}).click();const dialog=page.getByRole('dialog')
  await dialog.getByLabel('来源题名').fill('合成数学文献');await dialog.getByLabel('作者（每行一位，可留空）').fill('测试作者甲\n测试作者乙');await dialog.getByLabel('链接或文献标识').fill('https://source.invalid/paper');await dialog.getByLabel('来源定位').fill('第 3 页，命题 2');await dialog.getByLabel('访问日期').fill('2026-09-08');await dialog.getByLabel('来源核对状态').selectOption('unverified');await dialog.getByLabel('来源材料与摘录').fill('这是一份测试来源摘录，不代表已经核对原论文。');await dialog.getByRole('button',{name:'保存',exact:true}).click();await expect(dialog).toHaveCount(0)
  await expect(page.getByLabel('对象详情',{exact:true})).toContainText('第 3 页，命题 2');const state=await snapshot(request,project.project_id);const source=state.objects.find((o:any)=>o.revision.payload.artifact_type==='source');expect(source.revision.payload.authors).toEqual(['测试作者甲','测试作者乙']);expect(source.revision.payload.verification).toBe('unverified')
  await open(page,'一期补齐 · 来源定位');await select(page,'这是一份测试来源摘录');await expect(page.getByLabel('对象详情',{exact:true})).toContainText('合成数学文献');await expect(page.getByLabel('对象详情',{exact:true})).toContainText('来源待核对');expect(external).toEqual([])
})

test('map presentation survives reload without changing proof dependencies',async({page,request},testInfo)=>{
  const project=await command(request,'/projects',{title:'一期补齐 · 分支显示',body:'显示选择不能改变数学依赖。'})
  const premise=await command(request,'/objects',{branch_id:project.branch_id,kind:'claim',body:'前提 L：这里保留足够长的正文，用来观察折叠后的节点显示。'})
  const conclusion=await command(request,'/objects',{branch_id:project.branch_id,kind:'claim',body:'结论 C 依赖于 L。'})
  await command(request,'/proof-plans',{branch_id:project.branch_id,conclusion_revision_id:conclusion.revision_id,body:'在 L 成立的范围内得到 C。',premise_revision_ids:[premise.revision_id]})
  const before=await snapshot(request,project.project_id)
  await open(page,'一期补齐 · 分支显示');await select(page,'前提 L：这里保留足够长的正文');await page.getByRole('button',{name:'折叠选中对象',exact:true}).click();await expect(page.locator(`[data-id="${premise.object_id}"]`)).toContainText('已折叠')
  await page.getByRole('button',{name:'在地图隐藏选中对象',exact:true}).click();await expect(page.locator(`[data-id="${premise.object_id}"]`)).toHaveCount(0);await expect(page.getByLabel('研究工作稿')).toContainText('前提 L：这里保留足够长的正文')
  await open(page,'一期补齐 · 分支显示');await expect(page.locator(`[data-id="${premise.object_id}"]`)).toHaveCount(0);await page.getByRole('button',{name:/地图显示设置/}).click();const dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'恢复显示',exact:true}).click();await expect(page.locator(`[data-id="${premise.object_id}"]`)).toContainText('已折叠');await dialog.getByRole('button',{name:'归档当前分支'}).click();await expect(dialog).toContainText('此分支已归档');await dialog.getByRole('button',{name:'关闭',exact:true}).click();await expect(page.getByLabel('当前分支')).toContainText('已归档')
  const after=await snapshot(request,project.project_id);expect(after.proof_plans).toEqual(before.proof_plans);expect(after.support).toEqual(before.support);expect(after.objects.length).toBe(before.objects.length);expect(after.presentation.archived).toBeTruthy()
  await page.screenshot({path:testInfo.outputPath('presentation-preserves-proof.png'),fullPage:true})
})

test('budget exhausted task can be configured and resumed through the UI',async({page,request})=>{
  const project=await command(request,'/projects',{title:'一期补齐 · 额度恢复',body:'在保存进度后显式调整额度。'})
  const update=await request.put(`/api/projects/${project.project_id}/runtime-settings`,{headers:{...human,'Idempotency-Key':crypto.randomUUID()},data:{request_budget:0,allow_real_api:false,allowed_providers:['fake']}});expect(update.ok()).toBeTruthy()
  const run=await command(request,'/runs',{branch_id:project.branch_id,goal_object_id:project.object_id,instruction:'仅作本地预算状态测试。',provider:'fake',request_budget:1})
  const claimed=await command(request,`/runs/${run.run_id}/claim`,{},true);await command(request,`/attempts/${claimed.attempt_id}/requests`,{token:claimed.token},true)
  await open(page,'一期补齐 · 额度恢复');await page.getByRole('tab',{name:'运行',exact:true}).click();await expect(page.getByTestId('run-card')).toContainText('额度已耗尽')
  await page.getByRole('button',{name:'运行设置',exact:true}).click();let dialog=page.getByRole('dialog');await dialog.getByLabel('项目累计请求上限').fill('3');await dialog.getByRole('button',{name:'保存设置',exact:true}).click();await expect(dialog).toHaveCount(0)
  await page.getByRole('button',{name:'调整额度并恢复',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('整题累计请求上限').fill('2');await dialog.getByRole('button',{name:'保存并恢复任务'}).click();await expect(dialog).toHaveCount(0);await expect(page.getByTestId('run-card')).toContainText('排队中')
  const options=await request.get(`/api/runs/${run.run_id}/options`,{headers:human});expect((await options.json()).request_budget).toBe(2)
})

test('new research exposes autonomous limits and readable raw-output records',async({page,request})=>{
  const errors:string[]=[],external:string[]=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',req=>{if(req.url().startsWith('https://source.invalid'))external.push(req.url())})
  await command(request,'/projects',{title:'一期补齐 · 运行记录',body:'保存可审查的研究步骤。'});await open(page,'一期补齐 · 运行记录');await page.getByRole('button',{name:'启动研究',exact:true}).click();const dialog=page.getByRole('dialog');await expect(dialog.getByRole('checkbox',{name:'连续研究：允许根据计算结果继续推导、调整任务和按需讨论'})).toBeChecked();await expect(dialog.getByLabel('研究时限（秒）')).toHaveValue('86400');await dialog.getByLabel('整题累计请求上限').fill('3');await dialog.getByRole('checkbox',{name:'连续研究：允许根据计算结果继续推导、调整任务和按需讨论'}).uncheck();await dialog.getByRole('button',{name:'开始执行',exact:true}).click();await expect(dialog).toHaveCount(0)
  // Synthetic read responses exercise safe rendering; no provider is called.
  await page.route('**/api/runs/*/steps',route=>route.fulfill({json:{steps:[{id:'synthetic-step',number:1,state:'completed',body:'候选推导需要逐步核查。\n\n![不应自动下载](https://source.invalid/image.png)',actions:[{type:'write_draft',status:'completed',result:{saved:true}}],created_at:'2026-09-08T00:00:00Z'}]}}))
  await page.route('**/api/runs/*/calls',route=>route.fulfill({json:{calls:[{request_id:'synthetic-request',attempt_id:'synthetic-attempt',call_config:{provider:'fake',model:'synthetic-model',parameters:{max_tokens:4096},prompt_template_version:'test-v1',prompt_sha256:'test-hash'},raw_text:'<script>throw new Error("must remain text")</script>\n可见的中断输出',raw_sha256:'raw-test-hash',finish_reason:'length',complete:false,usage:{total_tokens:12},provider_request_id:null,created_at:'2026-09-08T00:00:00Z'}]}}))
  await page.getByText('研究步骤、调用配置与可见原输出',{exact:true}).click();await expect(page.getByTestId('run-card')).toContainText('候选推导需要逐步核查。');await page.getByText('查看可见原输出（可能中断）',{exact:true}).click();await expect(page.locator('.raw-output')).toContainText('<script>throw new Error');expect(errors).toEqual([]);expect(external).toEqual([]);await expect(page.getByRole('link',{name:'手动打开图片来源'})).toBeVisible()
})

test('LaTeX renders across graph manuscript review history and run targets without rewriting code',async({page,request},testInfo)=>{
  const project=await command(request,'/projects',{title:'一期补齐 · LaTeX 全视图',body:'检查数学表达是否跨视图一致。'})
  const original=[String.raw`公式目标 $\alpha_i^2=\frac{a_i}{b_i}$。`,String.raw`兼容行内 \(\beta_j^3\)，以及独立公式：`,String.raw`\[\sum_{k=1}^{n} k = \frac{n(n+1)}{2}\]`,'```text',String.raw`\(code_stays_raw\)`,String.raw`\[code_block\]`,'```'].join('\n\n')
  const claim=await command(request,'/objects',{branch_id:project.branch_id,kind:'claim',body:original})
  await command(request,'/reviews',{branch_id:project.branch_id,target_revision_id:claim.revision_id,kind:'human_review',verdict:'inconclusive',coverage:'partial',scope:String.raw`只检查 $\alpha_i$ 与 $\beta_j$。`,findings:[String.raw`尚需证明 $\frac{a_i}{b_i}>0$。`]})
  await open(page,'一期补齐 · LaTeX 全视图');const graph=page.locator(`[data-id="${claim.object_id}"]`);await expect(graph.locator('.katex').first()).toBeVisible();await expect(graph.locator('.katex')).toHaveCount(3)
  const manuscript=page.getByLabel('研究工作稿').getByTestId('manuscript-block').filter({hasText:'公式目标'});await expect(manuscript.locator('.katex')).toHaveCount(3);await expect(manuscript.locator('pre code')).toContainText(String.raw`\(code_stays_raw\)`);await expect(manuscript.locator('pre code .katex')).toHaveCount(0)
  await manuscript.click();const inspector=page.getByLabel('对象详情',{exact:true});await expect(inspector.locator('.math-body .katex')).toHaveCount(3);await inspector.getByRole('tab',{name:'证据',exact:true}).click();await expect(inspector.locator('.evidence-card .katex')).toHaveCount(3)
  await command(request,`/objects/${claim.object_id}/revisions`,{branch_id:project.branch_id,expected_revision_id:claim.revision_id,body:String.raw`新版本 $\gamma_{n+1}=\frac{1}{n^2}$。`});await inspector.getByRole('tab',{name:'版本',exact:true}).click();await inspector.getByRole('button',{name:/v1/}).click();await expect(inspector.locator('.version-compare .katex')).toHaveCount(7)
  await inspector.getByRole('tab',{name:'正文',exact:true}).click();await inspector.getByRole('button',{name:'从此处研究',exact:true}).click();const dialog=page.getByRole('dialog');await expect(dialog.locator('.selected-context .katex')).toHaveCount(1);await dialog.getByRole('checkbox',{name:'连续研究：允许根据计算结果继续推导、调整任务和按需讨论'}).uncheck();await dialog.getByRole('button',{name:'开始执行',exact:true}).click();await expect(page.locator('.run-heading .katex')).toHaveCount(1)
  await page.screenshot({path:testInfo.outputPath('latex-run-target.png'),fullPage:true})
})

test('operation permissions remain separate from real-model authorization',async({page,request})=>{
  const project=await command(request,'/projects',{title:'一期补齐 · 研究操作权限',body:'人类编辑与研究者权限分别处理。'})
  await open(page,'一期补齐 · 研究操作权限');await page.getByRole('tab',{name:'运行',exact:true}).click();await page.getByRole('button',{name:'研究者操作权限',exact:true}).click();const dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'取消全部勾选'}).click();await dialog.getByRole('checkbox',{name:'读取授权对象与版本'}).check();await dialog.getByRole('checkbox',{name:'保存研究草稿'}).check();await dialog.getByRole('button',{name:'保存操作权限'}).click();await expect(dialog).toHaveCount(0)
  const policy=await request.get(`/api/projects/${project.project_id}/agent-policy`,{headers:human});expect((await policy.json()).allowed_operations.sort()).toEqual(['read_object','write_draft'])
  const settings=await request.get(`/api/projects/${project.project_id}/runtime-settings`,{headers:human});expect((await settings.json()).allow_real_api).toBeFalsy()
})

test('directed graph keeps connected endpoints and dragged positions during delayed save and SSE',async({page,request},testInfo)=>{
  const project=await command(request,'/projects',{title:'一期补齐 · 有向地图交互',body:String.raw`原问题：给定 $\alpha=\frac{1}{2}$，证明 $2\alpha=1$。`})
  const premise=await command(request,'/objects',{branch_id:project.branch_id,kind:'claim',body:String.raw`前提 $\alpha=\frac{1}{2}$。`})
  const conclusion=await command(request,'/objects',{branch_id:project.branch_id,kind:'claim',body:String.raw`结论 $2\alpha=1$。`})
  const proof=await command(request,'/proof-plans',{branch_id:project.branch_id,conclusion_revision_id:conclusion.revision_id,body:String.raw`将 $\alpha=\frac{1}{2}$ 代入得到 $2\alpha=1$。`,premise_revision_ids:[premise.revision_id]})
  const unrelated=await command(request,'/objects',{branch_id:project.branch_id,kind:'artifact',body:'尚未连接的另一个研究想法。'})
  await open(page,'一期补齐 · 有向地图交互');await select(page,'前提');await page.getByLabel('研究地图').getByRole('button',{name:'聚焦',exact:true}).click()
  const node=page.locator(`[data-id="${premise.object_id}"]`);await expect(node.locator('.research-node')).toHaveClass(/focus-selected/);await expect(page.locator(`[data-id="${proof.object_id}"] .research-node`)).toHaveClass(/focus-neighbor/);await expect(page.locator(`[data-id="${unrelated.object_id}"] .research-node`)).toHaveClass(/focus-muted/)
  await page.getByLabel('研究地图').getByRole('button',{name:'图例',exact:true}).click();await expect(page.getByLabel('关系图例')).toContainText('研究关系')
  let release!:()=>void;const held=new Promise<void>(resolve=>{release=resolve});const path=`**/api/branches/${project.branch_id}/layout`;await page.route(path,async route=>{await held;await route.continue()})
  const saving=page.waitForRequest(req=>req.method()==='PUT'&&req.url().endsWith(`/branches/${project.branch_id}/layout`))
  await node.locator('.node-caption').hover();const box=(await node.boundingBox())!;await page.mouse.move(box.x+box.width/2,box.y+18);await page.mouse.down();await page.mouse.move(box.x+box.width/2+70,box.y+68,{steps:10});await page.mouse.up();await saving
  const dragged=await node.evaluate(element=>(element as HTMLElement).style.transform);await command(request,'/objects',{branch_id:project.branch_id,kind:'artifact',body:'拖动保存期间到达的新活动产物。'});await expect(page.getByLabel('研究工作稿')).toContainText('拖动保存期间到达的新活动产物');await expect.poll(()=>node.evaluate(element=>(element as HTMLElement).style.transform)).toBe(dragged);release();await expect.poll(async()=>(await snapshot(request,project.project_id)).layout.version).toBeGreaterThan(0);await page.unroute(path)
  const endpointDistances=await page.evaluate(({source,target,edgeId})=>{
    const path=document.querySelector(`.react-flow__edge[data-id="${edgeId}"] .react-flow__edge-path`) as SVGPathElement
    const sourceHandle=document.querySelector(`[data-id="${source}"] .react-flow__handle-right`)!.getBoundingClientRect(),targetHandle=document.querySelector(`[data-id="${target}"] .react-flow__handle-left`)!.getBoundingClientRect()
    const matrix=path.getScreenCTM()!,start=path.getPointAtLength(0).matrixTransform(matrix),end=path.getPointAtLength(path.getTotalLength()).matrixTransform(matrix)
    return {start:Math.hypot(start.x-sourceHandle.right,start.y-sourceHandle.y-sourceHandle.height/2),end:Math.hypot(end.x-targetHandle.left,end.y-targetHandle.y-targetHandle.height/2),arrow:path.getAttribute('marker-end')}
  },{source:premise.object_id,target:proof.object_id,edgeId:`premise-${premise.revision_id}-${proof.revision_id}`})
  expect(endpointDistances.start).toBeLessThan(4);expect(endpointDistances.end).toBeLessThan(4);expect(endpointDistances.arrow).toContain('url(')
  const saved=(await snapshot(request,project.project_id)).layout.positions[premise.object_id];await page.getByRole('button',{name:'放大地图',exact:true}).click();await expect.poll(async()=>Number((await page.getByLabel('地图缩放比例').textContent())!.replace('%',''))).toBeGreaterThan(100);await page.getByLabel('研究地图').getByRole('button',{name:'聚焦',exact:true}).click();await centered(page,premise.object_id);await settledGraph(page);await page.screenshot({path:testInfo.outputPath('directed-graph-focused.png'),fullPage:true});await page.getByLabel('对象详情',{exact:true}).getByRole('button',{name:'关闭对象详情'}).click();await page.getByRole('tab',{name:'研究地图',exact:true}).click();await page.getByLabel('研究地图').getByRole('button',{name:'全图',exact:true}).click();await settledGraph(page);await page.screenshot({path:testInfo.outputPath('directed-graph-overview.png'),fullPage:true})
  await page.reload();await expect(page.getByRole('heading',{name:'一期补齐 · 有向地图交互',exact:true})).toBeVisible();expect((await snapshot(request,project.project_id)).layout.positions[premise.object_id]).toEqual(saved)
  await page.setViewportSize({width:390,height:844});await page.getByRole('tab',{name:'研究地图',exact:true}).click();await page.getByLabel('研究地图').getByRole('button',{name:'聚焦',exact:true}).click();await centered(page,(await snapshot(request,project.project_id)).project.original_goal_id);await settledGraph(page);await expect(page.getByRole('button',{name:'放大地图',exact:true})).toBeVisible();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();await page.screenshot({path:testInfo.outputPath('directed-graph-mobile.png'),fullPage:true})
  await page.setViewportSize({width:1440,height:1000});const exampleResponse=page.waitForResponse(response=>response.url().endsWith('/examples/hermitian')&&response.request().method()==='POST');await page.getByRole('button',{name:'打开特征值反例示例 ↗',exact:true}).click();const example=await(await exampleResponse).json();await expect(page.getByRole('heading',{name:'Hermitian 交叉与排序 · 可复算示例',exact:true})).toBeVisible();await page.getByLabel('研究地图').getByRole('button',{name:'全图',exact:true}).click();await settledGraph(page);await expect(page.locator('.research-node')).toHaveCount(9);expect(await page.locator('.research-node .katex').count()).toBeGreaterThan(10)
  const hermitian=await snapshot(request,example.project_id),routingBefore=await cardIntersections(page,hermitian);expect(routingBefore.intersections).toEqual([]);await page.screenshot({path:testInfo.outputPath('hermitian-directed-graph.png'),fullPage:true});await page.getByLabel('研究地图').getByRole('button',{name:'聚焦',exact:true}).click();await settledGraph(page)
  const goalCard=page.locator(`[data-id="${hermitian.project.original_goal_id}"]`),longProof=hermitian.proof_plans.find((plan:any)=>hermitian.objects.find((object:any)=>object.revision.id===plan.revision_id)?.adoption_state==='withdrawn'),longPath=page.locator(`.react-flow__edge[data-id="conclusion-${longProof.revision_id}"] .react-flow__edge-path`),beforePath=await longPath.getAttribute('d'),goalBox=(await goalCard.boundingBox())!;await page.mouse.move(goalBox.x+goalBox.width/2,goalBox.y+18);await page.mouse.down();await page.mouse.move(goalBox.x+goalBox.width/2,goalBox.y+108,{steps:10});await page.mouse.up();await expect(longPath).not.toHaveAttribute('d',beforePath!);await expect.poll(async()=>(await snapshot(request,example.project_id)).layout.version).toBeGreaterThan(0)
  const routingAfter=await cardIntersections(page,hermitian);expect(routingAfter.intersections).toEqual([]);await testInfo.attach('routing-verification.json',{body:JSON.stringify({before_drag:routingBefore,after_drag:routingAfter}),contentType:'application/json'});await page.screenshot({path:testInfo.outputPath('hermitian-directed-focused.png'),fullPage:true})
})

test('local SSE updates rendered graph and manuscript within two seconds of revision submission',async({page,request},testInfo)=>{
  const project=await command(request,'/projects',{title:'一期补齐 · 图稿更新计时',body:'原问题：等待修订。'})
  await open(page,'一期补齐 · 图稿更新计时');const before=await snapshot(request,project.project_id),goal=before.objects.find((object:any)=>object.id===before.project.original_goal_id)
  await centered(page,goal.id);const started=performance.now()
  await command(request,`/objects/${goal.id}/revisions`,{branch_id:project.branch_id,expected_revision_id:goal.revision.id,body:String.raw`计时修订已显示：$\beta_1=\frac{3}{7}$。`})
  await expect(page.locator(`[data-id="${goal.id}"] .node-body`)).toContainText('计时修订已显示');await expect(page.getByLabel('研究工作稿')).toContainText('计时修订已显示');await expect(page.locator(`[data-id="${goal.id}"] .katex`)).toBeVisible();await expect(page.getByLabel('研究工作稿').locator('.katex')).toBeVisible()
  const elapsedMs=performance.now()-started;await testInfo.attach('local-ui-update-timing.json',{body:JSON.stringify({elapsed_ms:elapsedMs,threshold_ms:2000,scope:'Local browser: revision submission through SSE to rendered graph and manuscript, including LaTeX',real_model_requests:0},null,2),contentType:'application/json'});expect(elapsedMs).toBeLessThan(2000)
})

// Exact decoded proof/review text from the 20260908T093518Z acceptance report.
// JSON source escaping is not a second layer of LaTeX escaping; keep aligned row breaks.
test('actual decoded provider proof and review render without folding legitimate LaTeX backslashes',async({page,request},testInfo)=>{
  const fixture={"proof":"由平方差恒等式，\\[\n2\\big((a+b+c)^2-3(ab+bc+ca)\\big)=2(a^2+b^2+c^2-ab-bc-ca)=(a-b)^2+(b-c)^2+(c-a)^2\\ge0.\n\\] 因此 $(a+b+c)^2\\ge3(ab+bc+ca)$。等号成立当且仅当 $a-b=b-c=c-a=0$，即 $a=b=c$。","review":"独立验证目标证明。计算：\\[\n(a+b+c)^2-3(ab+bc+ca)=a^2+b^2+c^2+2ab+2bc+2ca-3ab-3bc-3ca=a^2+b^2+c^2-ab-bc-ca.\n\\] 因此乘以 2 得 \\[\n2((a+b+c)^2-3(ab+bc+ca))=2(a^2+b^2+c^2-ab-bc-ca).\n\\] 另一方面，展开平方和：\\[\n(a-b)^2+(b-c)^2+(c-a)^2=(a^2-2ab+b^2)+(b^2-2bc+c^2)+(c^2-2ca+a^2)=2(a^2+b^2+c^2-ab-bc-ca).\n\\] 所以原式左边恒等于平方和，因平方和对于实数 $a,b,c$ 非负，故 \\((a+b+c)^2\\ge3(ab+bc+ca)\\)。等号当且仅当平方和各项为 0，即 $a-b=0$、$b-c=0$、$c-a=0$，等价于 $a=b=c$。","findings":["平方和展开正确：\\(2((a+b+c)^2-3(ab+bc+ca)) = 2(a^2+b^2+c^2-ab-bc-ca)\\)。","非负性论证成立：平方和 \\((a-b)^2+(b-c)^2+(c-a)^2\\ge0\\)。","等号条件正确：当且仅当 \\(a=b=c\\)。"],"aligned":"对任意实数 $a,b,c$，计算：\n$$\n\\begin{aligned}\n(a+b+c)^2-3(ab+bc+ca) &= a^2+b^2+c^2+2ab+2bc+2ca-3ab-3bc-3ca \\\\\n&= a^2+b^2+c^2-ab-bc-ca \\\\\n&= \\frac12\\big((a-b)^2+(b-c)^2+(c-a)^2\\big) \\ge 0.\n\\end{aligned}\n$$\n因此 $(a+b+c)^2\\ge3(ab+bc+ca)$。等号成立当且仅当 $(a-b)^2+(b-c)^2+(c-a)^2=0$，即 $a=b=c$。","independent":"独立证明：\n$$\n(a+b+c)^2-3(ab+bc+ca)=a^2+b^2+c^2-ab-bc-ca=\\frac{1}{2}\\left[(a-b)^2+(b-c)^2+(c-a)^2\\right]\\ge 0.\n$$\n因此 $(a+b+c)^2\\ge3(ab+bc+ca)$。等号成立当且仅当 $a-b=b-c=c-a=0$，即 $a=b=c$。"}
  const project=await command(request,'/projects',{title:'实际模型输出 · LaTeX 回放',body:'回放已经留存的证明与审查；不请求模型。'})
  const claim=await command(request,'/objects',{branch_id:project.branch_id,kind:'claim',body:'要证 $(a+b+c)^2 \\ge 3(ab+bc+ca)$。'})
  const proof=await command(request,'/proof-plans',{branch_id:project.branch_id,conclusion_revision_id:claim.revision_id,body:fixture.proof,premise_revision_ids:[]})
  await command(request,'/reviews',{branch_id:project.branch_id,target_revision_id:proof.revision_id,kind:'human_review',verdict:'inconclusive',coverage:'partial',scope:'离线回放真实审查的显示文本。',findings:fixture.findings})
  const examples=[{body:fixture.proof,label:'由平方差恒等式',count:4,object_id:proof.object_id},{body:fixture.review,label:'独立验证目标证明',count:9,object_id:''},{body:fixture.aligned,label:'对任意实数',count:5,object_id:''},{body:fixture.independent,label:'独立证明：',count:4,object_id:''}]
  for(const example of examples.slice(1)){const record=await command(request,'/objects',{branch_id:project.branch_id,kind:'artifact',body:example.body});example.object_id=record.object_id}
  const slash=String.fromCharCode(92),matrix=slash+'begin{pmatrix}1&2'+slash.repeat(2)+'3&4'+slash+'end{pmatrix}',literal=slash.repeat(2)+'frac{1}{2}'
  await command(request,'/objects',{branch_id:project.branch_id,kind:'artifact',body:'合法矩阵换行\n\n$$\n'+matrix+'\n$$\n\n代码原文： `'+literal+'`\n\n```latex\n'+literal+'\n'+slash+'[x'+slash+']\n```'})
  await open(page,'实际模型输出 · LaTeX 回放')
  for(const example of examples){const graph=page.locator('[data-id="'+example.object_id+'"]'),block=page.getByLabel('研究工作稿').getByTestId('manuscript-block').filter({hasText:example.label});await expect(graph.locator('.katex')).toHaveCount(example.count);await expect(block.locator('.katex')).toHaveCount(example.count)}
  await expect(page.locator('.katex-error')).toHaveCount(0)
  const paper=page.getByLabel('研究工作稿'),aligned=paper.getByTestId('manuscript-block').filter({hasText:'对任意实数'}),matrixBlock=paper.getByTestId('manuscript-block').filter({hasText:'合法矩阵换行'})
  await expect(aligned.locator('.katex-display annotation')).toHaveText(fixture.aligned.split('$$')[1].trim());await expect(aligned.locator('.mtable')).toHaveCount(1)
  await expect(matrixBlock.locator('.katex-display annotation')).toHaveText(matrix);await expect(matrixBlock.locator('.mtable')).toHaveCount(1);await expect(matrixBlock.locator('pre code')).toHaveText(literal+'\n'+slash+'[x'+slash+']\n');await expect(matrixBlock.locator('code .katex')).toHaveCount(0)
  const stored=await snapshot(request,project.project_id);for(const example of examples)expect(stored.objects.find((object:any)=>object.id===example.object_id).revision.body).toBe(example.body)
  await select(page,'由平方差恒等式');const inspector=page.getByLabel('对象详情',{exact:true});await expect(inspector.locator('.math-body .katex')).toHaveCount(4);await inspector.getByRole('tab',{name:'证据',exact:true}).click();await expect(inspector.locator('.evidence-card .katex')).toHaveCount(3);await expect(inspector.locator('.katex-error')).toHaveCount(0)
  await inspector.getByRole('button',{name:'关闭对象详情'}).click();await page.getByRole('tab',{name:'工作稿',exact:true}).click();await aligned.scrollIntoViewIfNeeded();await page.screenshot({path:testInfo.outputPath('actual-provider-aligned-latex.png')})
})
