import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { extname, resolve } from 'node:path'
import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import { chromium } from 'playwright'

// Synthetic bound account and receipt only. All API traffic is intercepted.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/vids-maintenance-ui')
await mkdir(output, { recursive: true })
// Run npm --prefix frontend run build first, or set VIDS_UI_BUILD_DIR to an
// isolated Vite build output. No application server or database is started.
const build = resolve(process.env.VIDS_UI_BUILD_DIR || resolve(frontend, '../static/app'))
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
const browserServer = await chromium.launchServer({ executablePath, headless: true })
const browser = await chromium.connect(browserServer.wsEndpoint())
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
const page = await context.newPage()
page.setDefaultTimeout(15000)
const errors = [], writes = [], checks = []
page.on('pageerror', (error) => errors.push(error.message))
let settings = { keepalive_enabled: true, keepalive_interval_minutes: 10, repair_cooldown_seconds: 300,
    consecutive_failure_threshold: 3, failure_cooldown_seconds: 300, rules: '' }
let protection = { status: 'cooldown', reason: 'upstream_http_429', until: '2026-11-09T06:00:00Z' }
const token = { id: 81, email: 'synthetic@example.test', is_active: false, image_enabled: true, video_enabled: true,
    credits: 100, image_count: 2, video_count: 3, refresh_status: 'idle', browser_profile_source: 'cloak',
    user_paygate_tier: 'PAYGATE_TIER_ONE', browser_profile_status: 'ready' }
const vidsAccount = () => ({ token_id: 81, email: token.email, enabled: false, ready: true,
    identity_verified: true, session_state: 'ready', vids_identity: token.email, protection,
    maintenance: { status: 'retry_wait', last_probe_at: '2026-11-09T03:00:00Z', last_success_at: '2026-11-09T02:50:00Z',
        next_probe_at: '2026-11-09T03:10:00Z', last_error: 'transport_failed' },
    stats: { image_count: 17, video_count: 8, speech_count: 4, error_count: 3, consecutive_error_count: protection ? 3 : 0,
        last_used_at: '2026-11-09T02:45:00Z' }, last_quota: {},
})
const log = (id, status_text, operation) => ({ id, status_text, operation, status_code: 202, token_id: 81,
    token_email: token.email, duration: 1, created_at: '2026-11-09T02:45:00Z', progress: 0,
    attempt_count: 1, retry_count: 0, account_count: 1, request_body: '{}', response_body: '{}' })
const logs = [log(901, 'unknown', 'vids_generate_image'), log(902, 'download_failed', 'vids_generate_video'),
    { ...log(903, 'completed', 'vids_generate_speech'), status_code: 200 }]
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (url.pathname === '/health') return json(route, { status: 'ok', backend_running: true, has_active_tokens: true, remote_browser_configured: true })
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() === 'POST') {
        const body = request.postDataJSON()
        writes.push({ path: url.pathname, body })
        if (url.pathname === '/api/admin/vids/config') {
            if (body.rules.endsWith('|RETRY')) return json(route, { detail: '第 1 行：Vids 仅支持 LIMIT、DEACTIVE、DISABLE' }, 422)
            settings = { ...body }
            return json(route, { success: true, config: settings })
        }
        if (url.pathname === '/api/admin/vids/accounts/81/protection/clear') {
            protection = null
            return json(route, { success: true })
        }
        return json(route, { detail: 'Unexpected fixture write' }, 503)
    }
    if (request.method() !== 'GET') return json(route, { detail: 'Unexpected fixture mutation' }, 503)
    if (url.pathname === '/api/stats') return json(route, { total_tokens: 1, active_tokens: 0, today_images: 0, today_videos: 0, today_errors: 0 })
    if (url.pathname === '/api/admin/network') return json(route, { mode: 'direct', reachable: true })
    if (url.pathname === '/api/admin/vids/config') return json(route, { config: settings })
    if (url.pathname === '/api/admin/config') return json(route, { admin_username: 'synthetic-admin' })
    if (url.pathname === '/api/config/generation') return json(route, { config: { image_timeout: 300, video_timeout: 1500, max_retries: 3 } })
    if (url.pathname === '/api/account-error-policy') return json(route, { config: { rules: '', consecutive_failure_threshold: 3, failure_cooldown_seconds: 300, max_account_attempts: 3 } })
    if (url.pathname === '/api/call-logic/config') return json(route, { config: { call_mode: 'default' } })
    if (url.pathname === '/api/cache/config') return json(route, { config: { enabled: true, timeout: 7200, base_url: '' } })
    if (url.pathname === '/api/captcha/config') return json(route, { remote_browser_api_key: 'synthetic-key', remote_browser_timeout: 60 })
    if (url.pathname === '/api/admin/release') return json(route, { version: 'fixture', entries: [], update_docs: {} })
    if (url.pathname === '/api/tokens') return json(route, [token])
    if (url.pathname === '/api/admin/vids/accounts/detail') return json(route, { accounts: [vidsAccount()] })
    if (url.pathname === '/api/admin/vids/onboard/candidates') return json(route, { accounts: [] })
    if (url.pathname === '/api/proxy/pool') return json(route, { entries: [] })
    if (url.pathname === '/api/profile-transfer/jobs' || url.pathname === '/api/plugin/import/jobs') return json(route, { jobs: [] })
    if (url.pathname === '/api/logs') return json(route, logs)
    if (url.pathname === '/api/logs/901') return json(route, logs[0])
    return json(route, { detail: `Unexpected fixture read ${url.pathname}` }, 503)
})
const record = (message) => { checks.push(message); console.log('PASS ' + message) }
const noOverflow = async () => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
try {
    await page.goto(`${base}/__vids_test__#/settings`, { waitUntil: 'domcontentloaded' })
    await page.locator('.settings-skeleton').waitFor({ state: 'hidden' })
    await page.getByRole('tab', { name: '自定义规则', exact: true }).click()
    const card = page.locator('[data-setting-card="vidsConfig"]')
    await card.getByRole('heading', { name: 'Vids 会话维护与账号保护' }).waitFor()
    const input = card.getByRole('textbox', { name: 'Vids 自定义错误规则', exact: true })
    assert.equal(await input.inputValue(), '')
    await input.fill('synthetic failure|RETRY')
    await card.getByRole('button', { name: '保存 Vids 配置' }).click()
    await card.getByText('第 1 行：Vids 仅支持 LIMIT、DEACTIVE、DISABLE', { exact: true }).waitFor()
    assert.equal(await input.inputValue(), 'synthetic failure|RETRY')
    await card.getByText('未保存', { exact: true }).waitFor()
    record('Vids rule validation stays in its own editor and preserves the rejected draft')
    await input.fill('synthetic failure|LIMIT|5m')
    await card.getByRole('button', { name: '保存 Vids 配置' }).click()
    await card.getByText('未保存', { exact: true }).waitFor({ state: 'hidden' })
    assert.equal(settings.rules, 'synthetic failure|LIMIT|5m')
    assert.equal(writes.every((item) => item.path === '/api/admin/vids/config'), true)
    await page.waitForFunction(() => !document.querySelector('.el-message'))
    await card.scrollIntoViewIfNeeded()
    await page.screenshot({ path: resolve(output, 'desktop-config.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await card.scrollIntoViewIfNeeded()
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'mobile-config.png'), fullPage: true })
    record('Independent Vids config saves all maintenance/protection fields and fits mobile')

    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.goto(`${base}/__vids_test__#/accounts`, { waitUntil: 'domcontentloaded' })
    const row = page.locator('.accounts-table .el-table__body tr').filter({ hasText: token.email }).first()
    await row.getByRole('button', { name: `${token.email} 成员 #81`, exact: true }).click()
    const drawer = page.locator('.account-drawer:visible')
    const session = drawer.getByRole('region', { name: 'Vids 会话' })
    await session.getByText('维护诊断：transport_failed', { exact: true }).waitFor()
    await session.getByText('上次维护成功', { exact: true }).waitFor()
    await session.getByText('下次维护', { exact: true }).waitFor()
    const stats = session.locator('[aria-label="Vids 生成统计"]')
    assert.match(await stats.textContent(), /Vids 累计图片17.*Vids 累计视频8.*Vids 累计语音4/s)
    await page.screenshot({ path: resolve(output, 'desktop-account.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'mobile-account.png'), fullPage: true })
    await session.getByRole('button', { name: '解除 Vids 保护', exact: true }).click()
    await session.getByText('生成冷却中', { exact: true }).waitFor({ state: 'hidden' })
    assert.equal(token.is_active, false)
    assert.equal(writes.at(-1).path, '/api/admin/vids/accounts/81/protection/clear')
    record('Vids account details show maintenance/protection/provider stats and clear preserves manual disable')

    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.goto(`${base}/__vids_test__#/logs`, { waitUntil: 'domcontentloaded' })
    await page.getByText('Vids 图片生成', { exact: true }).first().waitFor()
    await page.getByText('受理结果待确认', { exact: true }).first().waitFor()
    await page.getByText('作品下载待恢复', { exact: true }).first().waitFor()
    assert.equal(await page.locator('.inline-progress').count(), 0)
    await page.getByRole('button', { name: 'REQUEST #901', exact: true }).click()
    await page.getByText('尝试 1 次 · 重试 0 次', { exact: false }).waitFor()
    await page.screenshot({ path: resolve(output, 'desktop-log.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'mobile-log.png'), fullPage: true })
    record('Vids unresolved/download receipts display needs-attention states and retain one attempt with zero retries')
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, writes, errors }, null, 2))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, errors, writes }, null, 2))
    throw error
} finally {
    await context.close()
    let cleanupTimer
    const gracefulClose = (async () => { await browser.close(); await browserServer.close() })().catch(() => {})
    const forceClose = new Promise((done) => {
        cleanupTimer = setTimeout(() => {
            const pid = browserServer.process().pid
            if (process.platform === 'win32') {
                const cleanup = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
                cleanup.once('exit', done)
                cleanup.once('error', done)
            } else { browserServer.process().kill('SIGKILL'); done() }
        }, 5000)
    })
    await Promise.race([gracefulClose, forceClose])
    clearTimeout(cleanupTimer)
    server.closeAllConnections()
    await new Promise((done) => server.close(done))
}
