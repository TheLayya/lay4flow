import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { extname, resolve, sep } from 'node:path'
import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import { performance } from 'node:perf_hooks'
import { chromium } from 'playwright'

// All APIs are synthetic and intercepted. No backend, account, Profile,
// upstream quota or generation service is used by this performance check.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/accounts-performance-ui')
const build = resolve(process.env.ACCOUNTS_UI_BUILD_DIR || resolve(frontend, '../static/app'))
await mkdir(output, { recursive: true })
const server = createServer(async (request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname
    const target = pathname.startsWith('/static/app/assets/')
        ? resolve(build, 'assets', decodeURIComponent(pathname.slice('/static/app/assets/'.length)))
        : resolve(build, 'index.html')
    if (!target.startsWith(build + sep)) return response.writeHead(404).end()
    try {
        response.writeHead(200, { 'Content-Type': {
            '.html': 'text/html; charset=utf-8', '.js': 'text/javascript',
            '.css': 'text/css', '.svg': 'image/svg+xml',
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
assert.ok(executablePath, 'Install Chrome/Edge or set PLAYWRIGHT_BROWSER_PATH')
const browserServer = await chromium.launchServer({ executablePath, headless: true })
const browser = await chromium.connect(browserServer.wsEndpoint())
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', reducedMotion: 'reduce' })
const page = await context.newPage()
page.setDefaultTimeout(15000)
const defer = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }
const coldGate = defer(), reentryGate = defer()
const auxiliaryPaths = ['/api/admin/vids/accounts/detail', '/api/admin/vids/onboard/candidates', '/api/proxy/pool']
const checks = [], errors = [], reads = [], writes = [], metrics = {}
const pendingCold = new Set(), pendingReentry = new Set()
const coldAuxiliaryStarts = []
const tokens = Array.from({ length: 61 }, (_, index) => ({
    id: index + 1, email: `member-${String(index + 1).padStart(2, '0')}@example.invalid`,
    name: `Synthetic member ${index + 1}`, is_active: true, image_enabled: true, video_enabled: true,
    auto_refresh_enabled: true, refresh_interval_minutes: 30, credits: 100 + index,
    user_paygate_tier: 'PAYGATE_TIER_ONE', browser_profile_source: 'cloak',
    browser_profile_status: 'ready', refresh_status: 'idle', remark: 'Synthetic list data',
}))
let phase = 'cold', coldReleased = false, reentryReleased = false
let coldTokenAt, reentryTokenAt
page.on('pageerror', (error) => errors.push(error.message))
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
const record = (message) => { checks.push(message); console.log('PASS ' + message) }
const noOverflow = async () => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (url.pathname === '/health') return json(route, { status: 'ok', backend_running: true, has_active_tokens: true, remote_browser_configured: true })
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const requestPhase = phase
    reads.push({ path: url.pathname + url.search, phase: requestPhase, method: request.method() })
    if (request.method() === 'PUT' && url.pathname === '/api/tokens/1') {
        const body = request.postDataJSON()
        writes.push({ path: url.pathname, body })
        Object.assign(tokens[0], body)
        return json(route, { success: true })
    }
    if (request.method() !== 'GET') {
        writes.push({ path: url.pathname, unexpected: true })
        return json(route, { detail: 'Unexpected synthetic mutation' }, 503)
    }
    if (url.pathname === '/api/stats') return json(route, { total_tokens: tokens.length, active_tokens: tokens.length })
    if (url.pathname === '/api/admin/network') return json(route, { mode: 'direct', reachable: true })
    if (url.pathname === '/api/admin/release') return json(route, { version: 'fixture', entries: [], update_docs: {} })
    if (url.pathname === '/api/profile-transfer/jobs' || url.pathname === '/api/plugin/import/jobs') return json(route, { jobs: [] })
    if (url.pathname === '/api/tokens') {
        if (requestPhase === 'reentry') {
            pendingReentry.add(url.pathname)
            await reentryGate.promise
            reentryTokenAt = performance.now()
        } else coldTokenAt = performance.now()
        return json(route, tokens)
    }
    if (auxiliaryPaths.includes(url.pathname)) {
        if (requestPhase === 'cold') {
            pendingCold.add(url.pathname)
            coldAuxiliaryStarts.push(performance.now())
            await coldGate.promise
        } else if (requestPhase === 'reentry') {
            pendingReentry.add(url.pathname)
            await reentryGate.promise
        }
        if (url.pathname === '/api/admin/vids/accounts/detail') return json(route, { accounts: [{
            token_id: 1, email: tokens[0].email, enabled: true, image_enabled: true, video_enabled: true,
            ready: true, identity_verified: true, session_state: 'ready', vids_identity: tokens[0].email,
            last_quota: { image: { available: 20, total: 30 }, video: { available: 100, total: 200 } },
            stats: { image_count: 2, video_count: 3, speech_count: 0 },
        }] })
        if (url.pathname === '/api/admin/vids/onboard/candidates') return json(route, { accounts: [] })
        return json(route, { entries: [] })
    }
    return json(route, { detail: `Unexpected synthetic read: ${url.pathname}` }, 503)
})
try {
    const coldStart = performance.now()
    await page.goto(`${base}/__accounts_performance__#/accounts`, { waitUntil: 'domcontentloaded' })
    const firstRow = () => page.locator('.accounts-table .el-table__body tr').filter({ hasText: tokens[0].email }).first()
    await firstRow().locator('.account-identity').waitFor({ state: 'visible' })
    metrics.coldRouteToRowsMs = Math.round(performance.now() - coldStart)
    metrics.tokenResponseToRowsMs = Math.round(performance.now() - coldTokenAt)
    metrics.coldAssetsAndStartupMs = Math.round(coldTokenAt - coldStart)
    assert.equal(coldReleased, false)
    assert.equal(pendingCold.size, 3)
    assert.equal(await page.locator('.table-skeleton').count(), 0)
    // Prove optional requests remain pending for at least two seconds while
    // the table is usable; do not substitute fast fixture responses for them.
    const lastAuxiliaryStarted = Math.max(...coldAuxiliaryStarts)
    await new Promise((done) => setTimeout(done, Math.max(0, 2000 - (performance.now() - lastAuxiliaryStarted))))
    assert.equal(coldReleased, false)
    metrics.optionalRequestsHeldMs = Math.round(performance.now() - lastAuxiliaryStarted)
    assert.ok(metrics.optionalRequestsHeldMs >= 1990)
    // Shared-machine load affects wall-clock latency. The acceptance condition
    // is rows/actions succeeding before optional gates are released; record the
    // actual latency without a flaky absolute speed requirement.
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'desktop-optional-pending.png'), fullPage: true })
    record('Base account rows display while Vids/candidate/proxy requests remain blocked for at least 2 seconds')

    // Inline updates used to await the same all-endpoint load promise.
    await firstRow().getByRole('button', { name: `${tokens[0].email} 图片生成`, exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.accounts-table .capability-toggle')?.getAttribute('aria-pressed') === 'false')
    assert.equal(writes.length, 1)
    assert.deepEqual(writes[0], { path: '/api/tokens/1', body: { image_enabled: false } })
    assert.equal(coldReleased, false)
    record('Inline capability update completes while optional account requests are pending')
    const proxiesCompleted = page.waitForResponse((response) => response.url() === `${base}/api/proxy/pool`)
    coldReleased = true
    coldGate.resolve()
    await proxiesCompleted
    await page.waitForFunction(() => !document.querySelector('.page-actions .is-loading'))

    await page.locator('.account-surface .el-pagination__sizes').click()
    await page.getByRole('option', { name: '20条/页', exact: true }).click()
    await page.getByRole('button', { name: /下一页/ }).click()
    await page.getByText(tokens[20].email, { exact: true }).first().waitFor()
    await page.getByRole('button', { name: /上一页/ }).click()
    await firstRow().locator('.account-identity').click()
    await page.locator('.account-drawer:visible').waitFor()
    await page.keyboard.press('Escape')
    await page.locator('.account-drawer:visible').waitFor({ state: 'hidden' })
    record('Pagination and keyboard-close account details remain usable after progressive loading')

    await page.evaluate(() => { location.hash = '/guide' })
    await page.locator('.guide-view').waitFor({ state: 'visible' })
    await page.locator('.accounts-view').waitFor({ state: 'hidden' })
    tokens[0] = { ...tokens[0], email: 'updated-member@example.invalid', remark: 'Fresh response' }
    phase = 'reentry'
    const reentryStart = performance.now()
    await page.evaluate(() => { location.hash = '/accounts' })
    const cachedIdentity = page.locator('.accounts-table .account-identity').filter({ hasText: 'member-01@example.invalid' }).first()
    await cachedIdentity.waitFor({ state: 'visible' })
    metrics.reentryToCachedRowsMs = Math.round(performance.now() - reentryStart)
    assert.equal(reentryReleased, false)
    assert.equal(await page.locator('.table-skeleton').count(), 0)
    assert.equal(pendingReentry.size, 4)
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'desktop-cached-reentry.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'mobile-cached-reentry.png'), fullPage: true })
    record('Returning to accounts immediately shows the in-memory snapshot while all four fresh reads are blocked')
    reentryReleased = true
    reentryGate.resolve()
    const updatedIdentity = page.locator('.accounts-table .account-identity').filter({ hasText: tokens[0].email }).first()
    await updatedIdentity.waitFor({ state: 'visible' })
    metrics.freshResponseToUpdatedRowsMs = Math.round(performance.now() - reentryTokenAt)
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'mobile-refreshed.png'), fullPage: true })
    assert.equal(reads.filter((item) => item.path.includes('accounts/detail?refresh=true')).length, 0)
    assert.equal(writes.filter((item) => item.unexpected).length, 0)
    assert.deepEqual(errors, [])
    record('Fresh responses replace cached rows; repeated entry triggers no automatic upstream quota refresh')
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, metrics, reads, writes, errors }, null, 2))
    console.log('METRICS ' + JSON.stringify(metrics))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, metrics, reads, writes, errors }, null, 2))
    throw error
} finally {
    coldGate.resolve()
    reentryGate.resolve()
    let cleanupTimer
    const gracefulClose = (async () => { await context.close(); await browser.close(); await browserServer.close() })().catch(() => {})
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
