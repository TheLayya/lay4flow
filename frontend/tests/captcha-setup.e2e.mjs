import assert from 'node:assert/strict'
import { chromium } from 'playwright'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Local synthetic setup: no request reaches the captcha provider or an upstream account.
const root = fileURLToPath(new URL('../..', import.meta.url))
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38127'
const output = resolve(root, 'tmp/captcha-setup-review')
const service = 'https://cap.tk218.top/'
await mkdir(output, { recursive: true })
const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_BROWSER_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
})
const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    reducedMotion: 'reduce',
    locale: 'zh-CN',
})
const page = await context.newPage()
page.setDefaultTimeout(15000)
const errors = [],
    writes = [],
    unexpected = [],
    checks = []
let configured = false
const modelSource = await import('node:fs/promises').then((fs) =>
    fs.readFile(resolve(root, 'static/js/model_catalog.js'), 'utf8'),
)
const models = Object.entries(
    JSON.parse(modelSource.slice(modelSource.indexOf('{'), modelSource.lastIndexOf('}') + 1)),
).map(([id, item]) => ({ id, ...item }))
const json = (route, value, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
page.on('pageerror', (error) => errors.push(error.message))
await context.route('**/*', async (route) => {
    const request = route.request(),
        url = new URL(request.url())
    if (url.origin !== base) {
        unexpected.push(request.url())
        return route.abort()
    }
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    if (url.pathname === '/api/captcha/config' && request.method() === 'POST') {
        const body = request.postDataJSON()
        writes.push(body)
        assert.equal(body.remote_browser_base_url, service)
        assert.equal(body.remote_browser_api_key, 'synthetic-captcha-key')
        configured = true
        return json(route, { success: true })
    }
    if (!['GET', 'HEAD'].includes(request.method())) {
        unexpected.push(`${request.method()} ${url.pathname}`)
        return json(route, {}, 503)
    }
    const reads = {
        '/health': { backend_running: true, has_active_tokens: true, remote_browser_configured: configured },
        '/api/stats': { total_tokens: 1, active_tokens: 1 },
        '/api/admin/config': {
            admin_username: 'demo',
            api_key: 'synthetic-api-key',
            error_ban_threshold: 3,
            debug_enabled: false,
        },
        '/api/config/generation': { config: { image_timeout: 300, video_timeout: 1500, max_retries: 3 } },
        '/api/call-logic/config': { config: { call_mode: 'default' } },
        '/api/token-refresh/config': { config: { protocol_refresh_enabled: true, refresh_interval_minutes: 120 } },
        '/api/cache/config': { config: { enabled: false, timeout: 7200, base_url: '', effective_base_url: '' } },
        '/api/captcha/config': {
            captcha_method: 'remote_browser',
            remote_browser_base_url: service.slice(0, -1),
            remote_browser_api_key: configured ? 'synthetic-captcha-key' : '',
            remote_browser_timeout: 60,
        },
        '/api/admin/studio/models': { object: 'list', data: models },
        '/api/admin/vids/status': {
            configured: true,
            configured_count: 1,
            available: { image: 1, video: 1, speech: 1 },
            recoverable: {},
            quota: {},
        },
        '/api/admin/vids/v1/models': {
            data: [
                {
                    id: 'vids-speech',
                    type: 'speech',
                    capabilities: { voices: ['Puck'], speech_response_formats: ['wav'] },
                },
            ],
        },
    }
    if (url.pathname === '/api/admin/studio/media/history') return json(route, { entries: [] })
    if (url.pathname in reads) return json(route, reads[url.pathname])
    unexpected.push(`GET ${url.pathname}`)
    return json(route, {}, 404)
})
const banner = () => page.locator('.captcha-setup-notice')
const record = (text) => {
    checks.push(text)
    console.log('PASS ' + text)
}
try {
    await page.goto(`${base}/login#/studio`, { waitUntil: 'domcontentloaded' })
    await banner().waitFor()
    assert.equal(await banner().getByRole('link').getAttribute('href'), service)
    assert.equal(await banner().getByRole('link').getAttribute('target'), '_blank')
    await page.locator('.studio-editor textarea').fill('Synthetic image prompt')
    await page.locator('.studio-captcha-setup').waitFor()
    assert.ok(await page.locator('.generate-area .el-button--primary').last().isDisabled())
    await page.screenshot({ path: resolve(output, 'flow-required-1440.png'), animations: 'disabled' })
    record('Missing key is visible globally and blocks Flow creation before any generation request')
    await banner().getByRole('button', { name: '填写打码密钥', exact: true }).click()
    await page.locator('[data-setting-card="captcha"]').waitFor()
    assert.equal(new URL(page.url()).hash, '#/settings?section=captcha')
    assert.ok((await page.locator('.captcha-service-address a').getAttribute('href')) === service)
    assert.equal(await page.locator('[data-setting-card="captcha"] input[placeholder*="your-browser"]').count(), 0)
    const key = page
        .locator('[data-setting-card="captcha"] .el-form-item')
        .filter({ hasText: '远程服务 API 密钥' })
        .locator('input')
    await key.fill('synthetic-captcha-key')
    assert.equal(await key.getAttribute('type'), 'text')
    await page.getByRole('button', { name: '保存验证码配置', exact: true }).click()
    await banner().waitFor({ state: 'hidden' })
    assert.equal(writes.length, 1)
    record(
        'Setup action opens the exact settings section; fixed service link, plain key and save immediately clear the global notice',
    )
    await page.setViewportSize({ width: 390, height: 844 })
    configured = false
    await page.goto(`${base}/login#/studio`, { waitUntil: 'domcontentloaded' })
    await page.reload({ waitUntil: 'domcontentloaded' })
    await banner().waitFor()
    await page.locator('.studio-captcha-setup').waitFor()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.screenshot({ path: resolve(output, 'flow-required-390.png'), animations: 'disabled' })
    await page.goto(`${base}/login#/studio?platform=vids`, { waitUntil: 'domcontentloaded' })
    await page.locator('.studio-editor .el-tag').filter({ hasText: 'VIDS' }).waitFor()
    assert.equal(await page.locator('.studio-captcha-setup').count(), 0)
    await page.goto(`${base}/api-guide#/guide`, { waitUntil: 'domcontentloaded' })
    await page.locator('.guide-view').waitFor()
    assert.equal(await banner().count(), 0)
    record('Mobile setup banner fits the viewport; Vids is independent and public documentation remains accessible')
    assert.deepEqual(errors, [])
    assert.deepEqual(unexpected, [])
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify({ checks, writes, errors, unexpected }, null, 2),
    )
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), animations: 'disabled' })
    await writeFile(
        resolve(output, 'failure.json'),
        JSON.stringify(
            { message: error.message, errors, writes, unexpected, text: await page.locator('body').innerText() },
            null,
            2,
        ),
    )
    throw error
} finally {
    await browser.close()
}
