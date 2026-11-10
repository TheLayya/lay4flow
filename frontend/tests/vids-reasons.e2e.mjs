import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { extname, resolve } from 'node:path'
import { createServer } from 'node:http'
import { chromium } from 'playwright'

// Synthetic receipts, one synthetic job and intercepted APIs only. No real generation.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const build = resolve(frontend, '../static/app')
const output = resolve(frontend, '../tmp/vids-pool-20261111')
await mkdir(output, { recursive: true })
const server = createServer(async (request, response) => {
    const path = new URL(request.url, 'http://127.0.0.1').pathname
    const target = path.startsWith('/static/app/assets/')
        ? resolve(build, 'assets', decodeURIComponent(path.slice('/static/app/assets/'.length)))
        : resolve(build, 'index.html')
    if (!target.startsWith(build + '/') && !target.startsWith(build + '\\')) return response.writeHead(404).end()
    try {
        const type = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }[extname(target)] || 'application/octet-stream'
        response.writeHead(200, { 'Content-Type': type })
        response.end(await readFile(target))
    } catch { response.writeHead(404).end() }
})
await new Promise((done) => server.listen(0, '127.0.0.1', done))
const base = `http://127.0.0.1:${server.address().port}`
const executablePath = [process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/chromium',
].find((path) => path && existsSync(path))
assert.ok(executablePath)
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
const page = await context.newPage()
page.setDefaultTimeout(20000)
const errors = [], writes = [], reads = [], checks = []
page.on('pageerror', (error) => errors.push(error.message))
const id = (seed) => 'vids_' + createHash('sha256').update('synthetic-' + seed).digest('hex')
const receipt = (kind, error, upstream, seed = kind) => ({
    id: id(seed), kind, status: 'failed', created_at: 1794310000, error,
    error_operation: 'generate', content_url: null, metadata: {}, upstream_error: upstream,
})
const speech = receipt('speech', 'upstream_http_400', { status: 'INVALID_ARGUMENT', reason: 'LANGUAGE_NOT_SUPPORTED' })
const image = receipt('image', 'upstream_http_400', { status: 'INVALID_ARGUMENT', reason: 'LANGUAGE_NOT_SUPPORTED' })
const video = receipt('video', 'upstream_http_403', { status: 'PERMISSION_DENIED', reason: 'CROSS_PRODUCT_SHARING_DISABLED' })
// Distinct physical receipt from the same submitted job; returned only after polling.
const final = receipt('speech', 'upstream_http_403', { status: 'PERMISSION_DENIED', reason: 'CROSS_PRODUCT_SHARING_DISABLED' }, 'speech-dispatch-final')
const item = (status, extra = {}) => ({
    key: 'job_synthetic_dispatch:0', index: 0, kind: 'speech', status, ...extra,
})
const pendingJob = {
    id: 'job_synthetic_dispatch', state: 'running', provider: 'vids', created_at: 1794310000,
    request_id: 'request_synthetic_dispatch', items: [item('generating', { receipt: id('speech-dispatch-base') })],
}
const finalJob = {
    ...pendingJob, state: 'failed',
    items: [item('failed', { receipt: final.id, vids_receipt: final, message: '生成失败' })],
}
let submittedOnce = false, polls = 0
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    reads.push({ method: request.method(), path: url.pathname })
    if (request.method() === 'POST') {
        writes.push({ path: url.pathname, body: request.postData() })
        if (url.pathname === '/api/admin/studio/jobs') {
            submittedOnce = true
            return json(route, { job: pendingJob })
        }
        return json(route, { detail: 'Synthetic browser check forbids other writes' }, 503)
    }
    if (url.pathname === '/api/stats') return json(route, { total_tokens: 1 })
    if (url.pathname === '/api/admin/vids/status') return json(route, {
        configured: true,
        available: { image: 1, video: 1, speech: 1 },
        recoverable: { image: 0, video: 0, speech: 0 },
        quota: {},
    })
    if (url.pathname === '/api/admin/vids/v1/models') return json(route, {
        object: 'list',
        data: [
            { id: 'vids-image', kind: 'image' },
            { id: 'vids-speech', kind: 'speech' },
            { id: 'vids-video', kind: 'video' },
        ],
    })
    if (url.pathname === '/api/admin/studio/jobs') {
        if (!submittedOnce) return json(route, { jobs: [] })
        polls += 1
        return json(route, { jobs: [polls >= 2 ? finalJob : pendingJob] })
    }
    for (const current of [speech, image, video, final]) {
        if (url.pathname === `/api/admin/vids/v1/vids/generations/${current.id}`) return json(route, current)
    }
    return json(route, { detail: `Unexpected fixture read ${url.pathname}` }, 503)
})
const record = (message) => { checks.push(message); console.log('PASS ' + message) }
const cardText = () => page.locator('.gallery-card').first().innerText()
async function load(current, expected, absent = []) {
    await page.waitForFunction(() => !document.querySelector('.receipt-recovery .el-button.is-loading'))
    const input = page.getByPlaceholder('粘贴 Vids 收据 ID，读取原任务与成品')
    await input.fill(current.id)
    await page.getByRole('button', { name: '恢复历史收据' }).click()
    await page.waitForFunction(({ expected, absent }) => {
        const text = document.querySelector('.gallery-card')?.innerText || ''
        return text.includes(expected) && absent.every((value) => !text.includes(value))
    }, { expected, absent })
    const text = await cardText()
    assert.ok(text.includes(expected), text)
    for (const value of absent) assert.ok(!text.includes(value), `${value}\n${text}`)
    return text
}
try {
    await page.goto(`${base}/#/studio?platform=vids`, { waitUntil: 'domcontentloaded' })
    await page.locator('.studio-receipt-recovery').waitFor()
    await page.waitForFunction(() => /\d/.test(document.querySelector('.model-field .field-hint')?.innerText || ''))
    assert.equal(await page.getByRole('combobox', { name: /账号|account/i }).count(), 0)
    record('Studio readiness reflects the synthetic account pool and offers no account picker')
    await page.getByText('恢复已有 Vids 收据', { exact: true }).click()
    await load(speech, 'LANGUAGE_NOT_SUPPORTED', ['CROSS_PRODUCT_SHARING_DISABLED'])
    record('Studio speech receipt retains the upstream language reason')
    await page.screenshot({ path: resolve(output, 'studio-speech-language.png'), fullPage: true })
    await load(image, 'LANGUAGE_NOT_SUPPORTED', ['CROSS_PRODUCT_SHARING_DISABLED'])
    record('Studio image receipt retains the real upstream reason')
    await page.screenshot({ path: resolve(output, 'studio-image-boundary.png'), fullPage: true })
    await load(video, 'CROSS_PRODUCT_SHARING_DISABLED', ['LANGUAGE_NOT_SUPPORTED'])
    record('Studio video receipt retains the upstream account-permission reason')
    await page.screenshot({ path: resolve(output, 'studio-video-entitlement.png'), fullPage: true })
    const beforeReloadReads = reads.length
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('.gallery-card').first().waitFor()
    const restored = await cardText()
    assert.ok(restored.includes('CROSS_PRODUCT_SHARING_DISABLED'), restored)
    assert.equal(writes.length, 0, JSON.stringify(writes))
    assert.ok(reads.length > beforeReloadReads)
    record('Refresh preserves the receipt result and never posts a generation request')
    await page.locator('#studio-tab-speech').click()
    const prompt = page.locator('.studio-prompt-input textarea')
    await prompt.fill('Synthetic English speech used only to prove automatic dispatch.')
    const submit = page.getByRole('button', { name: '生成我的声线' })
    const postsBeforeSubmit = writes.length
    const [request] = await Promise.all([
        page.waitForRequest((entry) => entry.method() === 'POST' && new URL(entry.url()).pathname === '/api/admin/studio/jobs'),
        submit.click(),
    ])
    const submission = JSON.parse(request.postData())
    assert.equal(submission.provider, 'vids')
    assert.equal(submission.count, 1)
    for (const key of Object.keys(submission)) assert.ok(!/account|token/i.test(key), key)
    assert.deepEqual(Object.keys(submission.body).sort(), ['kind', 'prompt', 'voice'])
    assert.equal(submission.body.kind, 'speech')
    assert.ok(!/token_id|account_id/.test(JSON.stringify(submission)), JSON.stringify(submission))
    await page.waitForFunction((expected) => Object.values(localStorage).some((value) => value.includes(expected)), final.id)
    assert.ok((await cardText()).includes('CROSS_PRODUCT_SHARING_DISABLED'))
    const stored = await page.evaluate(() => Object.values(localStorage).join('\n'))
    assert.ok(stored.includes(final.id), 'final physical receipt persisted')
    assert.equal(writes.length, postsBeforeSubmit + 1, JSON.stringify(writes))
    assert.equal(await page.getByRole('combobox', { name: /账号|account/i }).count(), 0)
    assert.equal(await page.getByRole('dialog').count(), 0)
    await page.screenshot({ path: resolve(output, 'studio-auto-dispatch.png'), fullPage: true })
    record('One submit click dispatches Vids automatically, polls to the final physical receipt and sends no account field')
    await writeFile(resolve(output, 'browser-result.json'), JSON.stringify({
        checks, errors, writes, reads: reads.length, polls,
        receipts: [speech.id, image.id, video.id, final.id],
    }, null, 2))
    assert.deepEqual(errors, [])
    assert.equal(writes.length, 1, JSON.stringify(writes))
    console.log(`PASS ${checks.length} browser checks; 1 synthetic intercepted submit; ${polls} polls; ${reads.length} intercepted reads`)
} finally {
    await page.close().catch(() => {})
    await context.close().catch(() => {})
    await browser.close().catch(() => {})
    await new Promise((done) => server.close(done))
}