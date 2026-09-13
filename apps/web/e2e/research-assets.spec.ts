import { expect, test } from '@playwright/test'
import type { APIRequestContext, Page } from '@playwright/test'

const auth = { Authorization: 'Bearer browser-test-user' }
async function command(api: APIRequestContext, path: string, data: unknown) {
  const response = await api.post(`/api${path}`, { headers: { ...auth, 'Idempotency-Key': crypto.randomUUID() }, data })
  expect(response.ok(), await response.text()).toBeTruthy(); return response.json()
}
async function openProject(page: Page, title: string) { await page.goto('/'); await page.getByRole('button', { name: `◇ ${title}`, exact: true }).click(); await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible() }
async function selectObject(page: Page, body: string) { await page.getByLabel('研究工作稿').getByTestId('manuscript-block').filter({ hasText: body }).click(); await expect(page.getByLabel('对象详情', { exact: true })).toBeVisible() }

test('article composer creates one selected section and renders MathText', async ({ page, request }, testInfo) => {
  const project = await command(request, '/projects', { title: '资产验收 · 文章', body: '原始问题应固定纳入文章。' }); await command(request, '/objects', { branch_id: project.branch_id, kind: 'claim', body: '局部结论 $x^2$。' }); await openProject(page, '资产验收 · 文章'); await page.getByRole('button', { name: '整理文章', exact: true }).click()
  const dialog = page.getByRole('dialog'); await dialog.getByLabel('文章标题').fill('局部研究文章'); const section = dialog.locator('fieldset').filter({ hasText: '问题与假设' }).first(); await section.locator('label').filter({ hasText: '局部结论' }).first().locator('input[type="checkbox"]').check(); await expect(section.locator('.katex').first()).toBeVisible(); await dialog.getByRole('button', { name: '保存文章草稿', exact: true }).click(); await expect(dialog).toContainText('文章草稿已保存'); await expect(dialog).toContainText('仍需对整篇文章独立审查'); await page.screenshot({ path: testInfo.outputPath('article-composer.png'), fullPage: true })
})

test('source-only merge shows full three-way text and refreshes', async ({ page, request }, testInfo) => {
  const project = await command(request, '/projects', { title: '资产验收 · 合并', body: '合并目标。' }); const branch = await command(request, '/branches', { source_branch_id: project.branch_id, name: '来源旁支' }); await command(request, '/objects', { branch_id: branch.branch_id, kind: 'claim', body: '来源新增结论 $\\beta=\\frac{2}{3}$。' }); await openProject(page, '资产验收 · 合并')
  await page.locator('.header-actions').getByRole('button', { name: '比较与合并', exact: true }).last().click(); const dialog = page.getByRole('dialog'); await dialog.getByLabel('来源分支').selectOption(branch.branch_id); await dialog.getByRole('button', { name: '生成合并预览', exact: true }).click(); await expect(dialog).toContainText('来源新增结论'); await dialog.getByText('查看全文', { exact: true }).first().click(); await expect(dialog).toContainText('来源新增结论'); const item = dialog.locator('fieldset').filter({ hasText: '来源新增结论' }).first(); await item.getByLabel('决议理由').fill('纳入来源分支新增成果。'); await dialog.getByRole('button', { name: '提交合并', exact: true }).click(); await expect(dialog).toContainText('合并已保存'); await page.screenshot({ path: testInfo.outputPath('source-only-merge.png'), fullPage: true })
})

test('permanent deletion clears unsaved drafts across branches and old history', async ({ page, request }, testInfo) => {
  const project = await command(request, '/projects', { title: '资产验收 · 删除', body: '删除测试原问题。' })
  const material = await command(request, '/objects', { branch_id: project.branch_id, kind: 'claim', body: '待永久删除的研究材料。' })
  await openProject(page, '资产验收 · 删除')
  await selectObject(page, '待永久删除的研究材料。')
  const inspector = page.getByLabel('对象详情', { exact: true })
  await inspector.getByRole('button', { name: '编辑正文', exact: true }).click()
  await inspector.getByLabel('编辑对象正文').fill('不能复活的未保存草稿哨兵')
  await page.evaluate(({objectId, revisionId}) => {
    sessionStorage.setItem(`mathagent-draft:other-branch:${objectId}`, JSON.stringify({
      base:{id:revisionId,object_id:objectId,body:'旧草稿',payload:{}},body:'旁支缓存哨兵',role:'assumption',
    }))
  }, {objectId:material.object_id,revisionId:material.revision_id})
  await inspector.getByRole('tab', {name:'版本',exact:true}).click()
  await expect(inspector.locator('.version-list button')).toHaveCount(1)
  await inspector.getByRole('tab', {name:'正文',exact:true}).click()
  await inspector.getByRole('button', { name: '永久删除', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('此操作不可恢复')
  await dialog.getByLabel('请输入“永久删除”确认').fill('永久删除')
  await dialog.getByRole('button', { name: '确认永久删除', exact: true }).click()
  await expect(dialog).toContainText('删除已完成')
  // Wait for SSE refresh: its state change must not close the result dialog.
  await expect(inspector.getByRole('button', {name:'编辑正文',exact:true})).toBeDisabled()
  await expect(dialog).toBeVisible()
  await expect.poll(() => page.evaluate(objectId => Object.keys(sessionStorage).filter(key => key.startsWith('mathagent-draft:') && key.endsWith(':'+objectId)), material.object_id)).toEqual([])
  await dialog.getByRole('button', { name: '关闭', exact: true }).click()
  await openProject(page, '资产验收 · 删除')
  await selectObject(page, '此材料已永久删除')
  await expect(inspector.getByRole('button', {name:'编辑正文',exact:true})).toBeDisabled()
  await expect(inspector.getByRole('button', {name:'暂时采用',exact:true})).toBeDisabled()
  await expect(inspector).not.toContainText('不能复活的未保存草稿哨兵')
  await inspector.getByRole('tab', {name:'版本',exact:true}).click()
  await inspector.locator('.version-list button').first().click()
  await expect(inspector).not.toContainText('待永久删除的研究材料。')
  await page.screenshot({ path: testInfo.outputPath('permanent-delete-tombstone.png'), fullPage: true })
})

test('code sandbox reports unavailable and disables enable', async ({ page, request }, testInfo) => {
  await command(request, '/projects', { title: '资产验收 · 沙箱', body: '离线沙箱状态。' }); await openProject(page, '资产验收 · 沙箱'); await page.getByRole('tab', { name: '运行', exact: true }).click(); const panel = page.getByLabel('请求额度与调用账本'); await expect(panel).toContainText('离线代码沙箱'); await expect(panel).toContainText('环境不可用'); await expect(panel.getByRole('button', { name: '启用沙箱', exact: true })).toBeDisabled(); await expect(panel).toContainText('仅允许离线标准库'); await page.screenshot({ path: testInfo.outputPath('code-sandbox-unavailable.png'), fullPage: true })
})
