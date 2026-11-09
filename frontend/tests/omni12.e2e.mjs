import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { extname, resolve, sep } from 'node:path'
import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import { chromium } from 'playwright'

// Only synthetic model capabilities and intercepted requests. No backend or
// provider runs, no account credentials or generation quota are used.
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/omni12-ui')
const build = resolve(process.env.OMNI12_UI_BUILD_DIR || resolve(root, 'static/app'))
await mkdir(output, { recursive: true })
const catalogSource = await readFile(resolve(root, 'static/js/model_catalog.js'), 'utf8')
const catalog = JSON.parse(catalogSource.slice(catalogSource.indexOf('{'), catalogSource.lastIndexOf('}') + 1))
assert.ok(catalog['omni_1.2_flash'], 'Generate the updated model fallback before running this check')
const server = createServer(async (request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname
    const target = pathname.startsWith('/static/app/assets/')
        ? resolve(build, 'assets', decodeURIComponent(pathname.slice('/static/app/assets/'.length)))
        : resolve(build, 'index.html')
    if (!target.startsWith(build + sep)) return response.writeHead(404).end()
    try {
        response.writeHead(200, { 'Content-Type': {
            '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
        }[extname(target)] || 'application/octet-stream' })
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
const browserServer = await chromium.launchServer({ executablePath, headless: true })
const browser = await chromium.connect(browserServer.wsEndpoint())
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', reducedMotion: 'reduce' })
const page = await context.newPage()
page.setDefaultTimeout(15000)
const errors = [], writes = [], checks = []
page.on('pageerror', (error) => errors.push(error.message))
const json = (route, data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (url.pathname === '/health') return json(route, { backend_running: true, has_active_tokens: true, remote_browser_configured: true })
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') {
        if (request.method() === 'POST' && url.pathname === '/api/admin/studio/jobs') {
            writes.push(request.postDataJSON())
            const body = writes.at(-1), id = `studio_synthetic_${writes.length}`
            return json(route, { job: { id, request_id: body.request_id, state: 'queued', provider: 'flow', kind: 'video',
                total: 1, created_at: 1700000000, title: 'Synthetic queued request',
                items: [{ id: `${id}:0`, key: `${id}:0`, index: 0, provider: 'flow', kind: 'video', status: 'queued', outputs: [] }],
            } }, 202)
        }
        throw new Error(`Unexpected fixture mutation ${request.method()} ${url.pathname}`)
    }
    if (url.pathname === '/api/admin/studio/models') return json(route, { data: Object.entries(catalog).map(([id, entry]) => ({ id, ...entry })) })
    if (url.pathname === '/api/admin/studio/jobs') return json(route, { jobs: [] })
    if (url.pathname === '/api/admin/vids/v1/vids/generations/vids_' + 'f'.repeat(64) + '/content')
        return route.fulfill({ contentType: 'video/mp4', body: Buffer.from([0, 0, 0, 24, 102, 116, 121, 112, 105, 115, 111, 109]) })
    if (url.pathname === '/api/admin/vids/v1/vids/generations/vids_' + 'f'.repeat(64))
        return json(route, { id: 'vids_' + 'f'.repeat(64), kind: 'video', status: 'completed', content_url: `/api/admin/vids/v1/vids/generations/vids_${'f'.repeat(64)}/content` })
    if (url.pathname === '/api/profile-transfer/jobs' || url.pathname === '/api/plugin/import/jobs') return json(route, { jobs: [] })
    if (url.pathname === '/api/stats') return json(route, { total_tokens: 0, active_tokens: 0 })
    if (url.pathname === '/api/admin/network') return json(route, { mode: 'direct', reachable: true })
    if (url.pathname === '/api/admin/release') return json(route, { version: 'synthetic', entries: [], update_docs: {} })
    return json(route, {})
})
const editor = page.locator('.studio-editor')
const field = (label) => editor.locator('.el-form-item').filter({ has: page.locator('.el-form-item__label').filter({ hasText: new RegExp(`^${label}$`) }) })
const select = async (label, option) => {
    await field(label).locator('.el-select').click()
    await page.getByRole('option', { name: option, exact: true }).click()
    await page.waitForFunction(() => !document.querySelector('.el-select__wrapper.is-focused[aria-expanded="true"]'))
}
const options = async (label) => {
    await field(label).locator('.el-select').click()
    const controlled = await field(label).getByRole('combobox').getAttribute('aria-controls')
    const values = await page.locator(`#${controlled} .el-select-dropdown__item`).allTextContents()
    await page.keyboard.press('Escape')
    return values.map((value) => value.trim())
}
const check = (message) => { checks.push(message); console.log('PASS ' + message) }
const noOverflow = async () => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4m0AAAAASUVORK5CYII=', 'base64')
try {
    await page.goto(`${base}/__omni12_test__#/studio`, { waitUntil: 'domcontentloaded' })
    await page.locator('#studio-tab-video').click()
    await field('生成模型').getByRole('combobox').waitFor()
    assert.match(await field('生成模型').innerText(), /Omni 1\.2 Flash/)
    await field('画面描述').getByRole('textbox').fill('Synthetic motion fixture')
    await select('输出时长', '15 秒')
    await editor.getByText(/当前单条约 60 点/).waitFor()
    await page.getByRole('button', { name: '提交 1 个视频', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.studio-editor .is-loading') === null)
    assert.equal(writes[0].body.model, 'omni_1.2_flash')
    assert.equal(writes[0].body.generationConfig.videoConfig.durationSeconds, 15)
    check('Omni 1.2 is the new-video default and submits an exact 15-second contract with subscription/point hints')

    await select('创作方式', '首尾帧')
    const lengths = await options('输出时长')
    assert.ok(lengths.includes('10 秒') && !lengths.includes('15 秒'), JSON.stringify(lengths))
    await editor.getByText(/首尾帧支持 3–10 秒/).waitFor()
    check('First/last-frame selector follows observed 3–10-second model capability limits')

    await select('创作方式', '编辑素材')
    const end = field('片段终点（毫秒）').getByRole('spinbutton')
    assert.equal(await end.getAttribute('max'), '15000')
    await end.fill('15000'); await end.blur()
    await editor.locator('.material-section input[type="file"]').setInputFiles({ name: 'synthetic.mp4', mimeType: 'video/mp4', buffer: Buffer.from('synthetic video bytes') })
    await page.waitForFunction(() => !document.querySelector('.studio-editor-form.is-disabled'))
    await page.getByRole('button', { name: '提交 1 个视频', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.studio-editor .is-loading') === null)
    assert.equal(writes[1].body.generationConfig.videoConfig.trimEndMs, 15000)
    assert.equal(writes[1].body.generationConfig.videoConfig.durationSeconds, undefined)
    check('Omni 1.2 editing exposes 15-second trim bounds and sends source trim rather than generation duration')

    await select('创作方式', '参考图视频')
    const board = page.getByRole('region', { name: '参考图片素材区', exact: true })
    await board.locator('input[type="file"]').setInputFiles(Array.from({ length: 3 }, (_, i) => ({ name: `synthetic-${i}.png`, mimeType: 'image/png', buffer: image })))
    await page.waitForFunction(() => document.querySelectorAll('.reference-item').length === 3)
    await select('创作方式', '视频续写')
    assert.equal(await board.locator('.reference-item').count(), 3)
    assert.equal(await editor.getByText('源视频媒体 ID', { exact: true }).count(), 0)
    await editor.locator('.material-section input[type="file"]').setInputFiles({ name: 'original-synthetic.mp4', mimeType: 'video/mp4', buffer: Buffer.from('synthetic original video bytes') })
    await editor.locator('.source-video video').waitFor()
    await editor.locator('.source-video video').evaluate((video) => {
        Object.defineProperty(video, 'duration', { configurable: true, value: 10 })
        video.dispatchEvent(new Event('loadedmetadata'))
    })
    await select('追加时长', '3 秒')
    await page.getByRole('button', { name: '提交 1 个视频', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.studio-editor .is-loading') === null)
    assert.equal(writes[2].body.generationConfig.videoConfig.mode, 'extend')
    assert.equal(writes[2].body.messages[0].content.filter((item) => item.type === 'image_url').length, 3)
    assert.match(writes[2].body.messages[0].content.at(-1).video_url.url, /^data:video\/mp4;base64,/)
    assert.equal(writes[2].body.generationConfig.videoConfig.trimStartMs, undefined)
    check('Omni Extend retains three references and uploads the full original MP4 without a technical ID field')
    await noOverflow()
    await editor.scrollIntoViewIfNeeded()
    await page.screenshot({ path: resolve(output, 'desktop-omni12.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'mobile-omni12.png'), fullPage: true })
    await page.setViewportSize({ width: 1440, height: 1000 })
    await field('生成模型').locator('.el-select').click()
    await page.getByRole('option', { name: /Omni 1\.1 Flash/ }).click()
    const oldLengths = await options('输出时长')
    assert.deepEqual(oldLengths, ['4 秒', '6 秒', '8 秒', '10 秒'])
    await editor.getByText(/12 月 5 日停用/).waitFor()
    const beforeSelection = writes.length
    await page.evaluate(() => {
        const receipt = 'vids_' + 'f'.repeat(64)
        localStorage.setItem('lay4flow_studio_receipts:v2:creator', JSON.stringify([{ provider: 'vids', kind: 'video', status: 'completed',
            started: Date.now(), title: 'Existing original fixture', receipt,
            outputs: [{ kind: 'video', path: `/api/admin/vids/v1/vids/generations/${receipt}/content` }],
        }]))
    })
    await page.reload({ waitUntil: 'domcontentloaded' })
    const oldVideo = page.locator('.gallery-card').filter({ hasText: 'Existing original fixture' }).first()
    await oldVideo.getByRole('button', { name: '续写视频', exact: true }).click()
    await editor.locator('.source-video').filter({ hasText: 'Existing original fixture.mp4' }).waitFor()
    assert.match(await field('生成模型').innerText(), /Omni 1\.2 Flash/)
    assert.equal(await editor.getByText('源视频媒体 ID', { exact: true }).count(), 0)
    assert.equal(writes.length, beforeSelection)
    check('Existing completed video card prepares an Omni 1.2 Extend source and editable prompt without any generation POST')
    assert.deepEqual(errors, [])
    check('Legacy Omni 1.1 remains explicitly selectable with its original duration contract; desktop/mobile fit without page errors')
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, writes, errors }, null, 2))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, writes, errors }, null, 2))
    throw error
} finally {
    await context.close()
    let cleanupTimer
    const graceful = (async () => { await browser.close(); await browserServer.close() })().catch(() => {})
    const forced = new Promise((done) => { cleanupTimer = setTimeout(() => {
        if (process.platform === 'win32') {
            const child = spawn('taskkill', ['/PID', String(browserServer.process().pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
            child.once('exit', done); child.once('error', done)
        } else { browserServer.process().kill('SIGKILL'); done() }
    }, 5000) })
    await Promise.race([graceful, forced]); clearTimeout(cleanupTimer)
    server.closeAllConnections(); await new Promise((done) => server.close(done))
}
