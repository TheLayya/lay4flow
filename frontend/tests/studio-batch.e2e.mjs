import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// The preview and all generated media are synthetic. Every API is intercepted.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38129'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(process.env.STUDIO_BATCH_OUTPUT || resolve(root, 'tmp/studio-batch-review'))
await mkdir(output, { recursive: true })
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    '/usr/bin/chromium',
]
    .filter(Boolean)
    .find(existsSync)
assert.ok(executablePath)
const catalogSource = await readFile(resolve(root, 'static/js/model_catalog.js'), 'utf8')
const catalog = Object.entries(
    JSON.parse(catalogSource.slice(catalogSource.indexOf('{'), catalogSource.lastIndexOf('}') + 1)),
).map(([id, value]) => ({ id, ...value }))
const image = await readFile(resolve(root, 'frontend/src/assets/koharu-banner.webp'))
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: 'zh-CN',
    reducedMotion: 'reduce',
})
const page = await context.newPage()
page.setDefaultTimeout(15000)
const errors = [],
    requests = [],
    checks = [],
    jobs = []
page.on('pageerror', (error) => errors.push(error.message))
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(),
        url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (url.pathname === '/__batch__/result.webp') return route.fulfill({ contentType: 'image/webp', body: image })
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    if (request.method() === 'POST') requests.push({ path: url.pathname, body: request.postDataJSON() })
    if (url.pathname === '/health')
        return json(route, { backend_running: true, has_active_tokens: true, remote_browser_configured: true })
    if (url.pathname === '/api/stats') return json(route, { total_tokens: 1, active_tokens: 1 })
    if (url.pathname === '/api/profile-transfer/jobs') return json(route, { success: true, jobs: [] })
    if (url.pathname === '/api/admin/studio/models') return json(route, { data: catalog })
    if (url.pathname === '/api/admin/vids/status')
        return json(route, {
            configured: true,
            available: { image: 1, video: 1, speech: 1 },
            recoverable: {},
            quota: {},
        })
    if (url.pathname === '/api/admin/vids/v1/models') return json(route, { data: [] })
    if (url.pathname === '/api/admin/studio/jobs') {
        if (request.method() === 'GET') return json(route, { success: true, jobs: [...jobs].reverse() })
        const payload = request.postDataJSON(),
            index = jobs.length
        const kind =
            payload.provider === 'vids'
                ? payload.body.kind
                : payload.body.generationConfig?.videoConfig
                  ? 'video'
                  : 'image'
        const prompt = payload.provider === 'vids' ? payload.body.prompt : payload.body.messages.at(-1).content
        const job = {
            id: `studio_${String(index).padStart(32, '0')}`,
            request_id: payload.request_id,
            provider: payload.provider,
            kind,
            model: payload.body.model || `vids-${kind}`,
            prompt: typeof prompt === 'string' ? prompt : prompt.find((item) => item.type === 'text').text,
            state: 'running',
            status: 'running',
            total: payload.count,
            completed: 0,
            failed: 0,
            created_at: Math.floor(Date.now() / 1000),
        }
        job.items = Array.from({ length: payload.count }, (_, itemIndex) => ({
            id: `${job.id}:${itemIndex}`,
            key: `${job.id}:${itemIndex}`,
            index: itemIndex,
            provider: job.provider,
            kind,
            model: job.model,
            status: itemIndex ? 'queued' : 'generating',
            message: itemIndex ? '已加入后台队列' : '正在后台生成',
            text: '',
            progressLog: '',
            outputs: [],
            receipt: '',
        }))
        jobs.push(job)
        return json(route, { success: true, job }, 202)
    }
    if (url.pathname === '/api/admin/studio/media/archive')
        return json(route, { id: 'a'.repeat(64), url: '/__batch__/result.webp', kind: 'image', cached: true })
    if (url.pathname.startsWith('/api/admin/vids/v1/vids/generations/'))
        return json(route, { id: url.pathname.split('/').at(-1), status: 'unknown' })
    return json(route, { data: [], accounts: [] })
})
const record = (message) => {
    checks.push(message)
    console.log(`PASS ${message}`)
}
const promptInput = () => page.locator('.studio-editor textarea')
async function submit(count, label) {
    await page.locator('#studio-generation-count').fill(String(count))
    await page.locator('#studio-generation-count').blur()
    await page.getByRole('button', { name: `提交 ${count} ${label}`, exact: true }).click()
    await page.getByRole('button', { name: `提交 ${count} ${label}`, exact: true }).waitFor()
}
try {
    await page.goto(`${base}/manage#/studio`)
    await page.locator('.studio-view h1').waitFor()
    await promptInput().fill('合成图片批次 A')
    await submit(3, '张图片')
    assert.equal(jobs.length, 1)
    assert.equal(jobs[0].total, 3)
    assert.equal(await promptInput().isEnabled(), true)
    await promptInput().fill('合成图片批次 B，第一批仍在生成')
    await submit(2, '张图片')
    assert.equal(jobs.length, 2)
    assert.equal(jobs[0].state, 'running')
    record('Image quantity creates independent tasks and a second batch submits while the first is still running')

    await page.locator('[data-kind="video"]').click()
    await promptInput().fill('合成视频批次，海边镜头')
    await submit(4, '个视频')
    assert.equal(jobs[2].kind, 'video')
    assert.equal(jobs[2].total, 4)
    record('Video has the same quantity control and remains editable after acknowledgement')

    await page.reload()
    await page.locator('.studio-view h1').waitFor()
    await page.waitForFunction(() => document.querySelectorAll('.gallery-card').length >= 9)
    assert.equal(requests.filter((request) => request.path === '/api/admin/studio/jobs').length, 3)
    record('Reload restores every queued item without submitting another paid request')

    await page.goto(`${base}/manage#/studio?platform=vids`)
    await page.locator('[data-kind="image"]').click()
    await promptInput().fill('合成 Vids 图片批次')
    await submit(2, '张图片')
    assert.equal(jobs[3].provider, 'vids')
    assert.equal(requests[3].body.body.n, 1)
    await page.locator('[data-kind="video"]').click()
    await promptInput().fill('合成 Vids 视频批次')
    await submit(3, '个视频')
    assert.equal(jobs[4].provider, 'vids')
    assert.equal(jobs[4].kind, 'video')
    assert.equal(jobs[4].total, 3)
    record('Vids image and video quantities create batches without multiplying the per-request image count')

    const failed = jobs[2]
    failed.items.forEach((item) => Object.assign(item, {
        status: 'failed', message: 'HTTP Error 403: reCAPTCHA evaluation failed',
        retry_trace: { attempts: 3, max_attempts: 3, history: [{ attempt: 1, error: 'HTTP Error 403', success: false }] },
    }))
    Object.assign(failed, { state: 'failed', status: 'failed', failed: failed.total })
    await page.getByText('已尝试 3 / 3 次', { exact: false }).first().waitFor()
    const failedCard = page.locator('.gallery-card').filter({ hasText: '已尝试 3 / 3 次' }).first()
    await failedCard.getByRole('button', { name: '记录', exact: true }).click()
    await page.locator('.studio-json').getByText('"max_attempts": 3', { exact: false }).waitFor()
    await page.locator('.studio-json').locator('xpath=ancestor::div[contains(@class,"el-drawer")]').getByLabel('关闭此对话框').click()
    record('A failed batch card and its request details expose the actual configured retry attempts')

    for (const job of jobs.slice(0, 2)) {
        job.items.forEach((item) =>
            Object.assign(item, {
                status: 'completed',
                outputs: [
                    { kind: 'image', url: `${base}/__batch__/result.webp`, sourceURL: `${base}/__batch__/result.webp` },
                ],
                completed_at: Math.floor(Date.now() / 1000),
            }),
        )
        Object.assign(job, { state: 'completed', status: 'completed', completed: job.total })
    }
    await page.waitForFunction(
        () =>
            document.querySelectorAll('.gallery-card').length >= 14 &&
            document.querySelectorAll('.gallery-media .el-image').length >= 5,
    )
    for (const [width, height] of [
        [1440, 1000],
        [390, 844],
    ]) {
        await page.setViewportSize({ width, height })
        await page.screenshot({ path: resolve(output, `studio-batch-${width}.png`), fullPage: true })
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
    }
    assert.equal(
        requests.every((request) => request.path === '/api/admin/studio/jobs'),
        true,
    )
    assert.deepEqual(errors, [])
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify(
            { checks, errors, submitted: requests.map(({ body }) => ({ provider: body.provider, count: body.count })) },
            null,
            2,
        ),
    )
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(
        resolve(output, 'failure.json'),
        JSON.stringify(
            { message: error.message, errors, requests, body: await page.locator('body').innerText() },
            null,
            2,
        ),
    )
    throw error
} finally {
    await browser.close()
}
