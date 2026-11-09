import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Synthetic preview only; all job/receipt traffic is intercepted before reaching providers.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38124'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'output/studio-card-cleanup')
await mkdir(output, { recursive: true })
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/chromium',
].filter(Boolean).find(existsSync)
assert.ok(executablePath, 'Install Chromium or set PLAYWRIGHT_BROWSER_PATH')
const source = await readFile(resolve(root, 'static/js/model_catalog.js'), 'utf8')
const catalog = Object.entries(JSON.parse(source.slice(source.indexOf('{'), source.lastIndexOf('}') + 1)))
    .map(([id, entry]) => ({ id, ...entry }))
const fixtures = ['unknown', 'unknown', 'received', 'download_failed', 'generating', 'queued'].map((status, index) => {
    const provider = index > 0 && index < 4 ? 'vids' : 'flow'
    const id = `studio_${String(index).padStart(32, '0')}`
    return {
        id, request_id: `cleanup-${index}`, provider, kind: 'video', model: '',
        prompt: `合成清理任务 ${index}`, title: `合成清理任务 ${index}`,
        state: index < 4 ? 'failed' : 'running', total: 1, created_at: 1730000000 + index,
        items: [{
            id: `${id}:0`, key: `${id}:0`, index: 0, provider, kind: 'video', status,
            receipt: provider === 'vids' ? `vids_${String(index).repeat(64)}` : '',
            message: index === 0 ? '服务已重启或停止，原请求结果待确认' : status,
            outputs: [],
        }],
    }
})
const original = JSON.stringify(fixtures)
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', reducedMotion: 'reduce' })
const page = await context.newPage()
page.setDefaultTimeout(15000)
const pageErrors = [], mutations = [], checks = []
let polls = 0
page.on('pageerror', (error) => pageErrors.push(error.message))
const json = (route, body) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
const card = (index) => page.locator(`.gallery-card[data-entry-key="${fixtures[index].items[0].key}"]`)
const record = (message) => { checks.push(message); console.log(`PASS ${message}`) }
async function countCards(count) {
    await page.waitForFunction((expected) => document.querySelectorAll('.gallery-card').length === expected, count)
}
async function poll() {
    const before = polls
    const response = page.waitForResponse((value) => value.url().endsWith('/api/admin/studio/jobs') && value.status() === 200)
    await page.getByRole('button', { name: '刷新状态', exact: true }).click()
    await response
    assert.ok(polls > before)
}
try {
    const login = await context.request.post(`${base}/api/login`, {
        data: { username: 'preview-admin', password: 'preview-password' },
    })
    assert.equal(login.status(), 200, 'Run only against scripts/preview_frontend.py')
    await context.addInitScript(() => localStorage.setItem('admin_username', 'preview-admin'))
    await context.route('**/*', async (route) => {
        const request = route.request(), url = new URL(request.url())
        if (url.origin !== base) return route.abort()
        if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
        if (!['GET', 'HEAD'].includes(request.method())) {
            mutations.push(`${request.method()} ${url.pathname}`)
            return route.abort()
        }
        if (url.pathname === '/health') return json(route, { backend_running: true, has_active_tokens: true, remote_browser_configured: true })
        if (url.pathname === '/api/admin/studio/jobs') {
            polls++
            return json(route, { success: true, jobs: fixtures })
        }
        if (url.pathname === '/api/admin/studio/models') return json(route, { data: catalog })
        if (url.pathname === '/api/admin/vids/status') return json(route, { configured: true, available: {}, quota: {}, recoverable: {} })
        if (url.pathname.startsWith('/api/admin/vids/v1/vids/generations/')) {
            const receipt = url.pathname.split('/').at(-1)
            const item = fixtures.flatMap((job) => job.items).find((entry) => entry.receipt === receipt)
            assert.ok(item)
            return json(route, { id: receipt, kind: 'video', status: item.status })
        }
        return json(route, { success: true, data: [], jobs: [], accounts: [] })
    })
    await page.goto(`${base}/manage#/studio`, { waitUntil: 'domcontentloaded' })
    await countCards(6)
    await card(0).getByRole('button', { name: '删除记录', exact: true }).waitFor()
    assert.equal(await card(0).getByRole('checkbox').isEnabled(), true)
    for (const index of [4, 5]) {
        assert.equal(await card(index).getByRole('checkbox').isDisabled(), true)
        assert.equal(await card(index).getByRole('button', { name: '删除记录', exact: true }).count(), 0)
    }
    await page.screenshot({ path: resolve(output, 'cleanup-before.png'), fullPage: true })
    await card(0).getByRole('button', { name: '删除记录', exact: true }).click()
    await countCards(5)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await countCards(5)
    assert.equal(await card(0).count(), 0)
    await poll()
    assert.equal(await card(0).count(), 0)
    record('Interrupted Flow card deletes directly and stays removed after reload and job polling')
    for (const index of [1, 2, 3]) {
        assert.equal(await card(index).getByRole('checkbox').isEnabled(), true)
        await card(index).locator('.gallery-card-select .el-checkbox').click()
        assert.equal(await card(index).getByRole('checkbox').isChecked(), true)
    }
    await page.getByRole('button', { name: '删除所选记录', exact: true }).click()
    await countCards(2)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await countCards(2)
    await poll()
    assert.equal(await page.locator('.gallery-card').count(), 2)
    assert.equal(await card(4).count(), 1)
    assert.equal(await card(5).count(), 1)
    assert.equal(JSON.stringify(fixtures), original)
    assert.deepEqual(mutations, [])
    assert.deepEqual(pageErrors, [])
    await page.screenshot({ path: resolve(output, 'cleanup-after.png'), fullPage: true })
    record('Unknown/received/download-failed cards bulk-delete; active work and original server receipts remain intact')
    record('No generation, retry, cancellation or download mutation was sent')
    await writeFile(resolve(output, 'browser-evidence.json'), JSON.stringify({ checks, polls, pageErrors, mutations }, null, 2))
} finally {
    await browser.close()
}
