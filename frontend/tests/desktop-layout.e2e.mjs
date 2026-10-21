import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Isolated preview only. The desktop bridge and all business data are synthetic.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38135'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/desktop-layout-review')
await mkdir(output, { recursive: true })
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    '/usr/bin/chromium',
]
    .filter(Boolean)
    .find(existsSync)
assert.ok(executablePath)
const browser = await chromium.launch({ executablePath, headless: true })
const results = [],
    errors = [],
    writes = []
const version = '1.3.1',
    latest = '1.3.2'
const proxyUrl = 'socks5h://fixture-user:fixture-password@192.0.2.1:1080'
const accounts = Array.from({ length: 35 }, (_, index) => ({
    id: index + 1,
    email: `fixture-${index + 1}@example.test`,
    name: `合成账号 ${index + 1}`,
    remark: '布局验收',
    is_active: true,
    credits: 120,
    image_enabled: true,
    video_enabled: true,
    proxy_pool_id: 1,
    proxy_url: proxyUrl,
    submission_proxy_mode: 'fixed',
    st: `fixture-st-${index + 1}`,
    at_expires: '2099-01-01T00:00:00Z',
    browser_profile_source: 'cookies',
    browser_profile_status: 'ready',
    refresh_status: 'idle',
    auto_refresh_enabled: true,
    refresh_interval_minutes: 120,
}))
const pool = accounts.map((row) => ({
    id: row.id,
    proxy_url: `socks5h://fixture:password@192.0.2.${row.id}:1080`,
    note: `合成出口 ${row.id}`,
    enabled: true,
    last_check_ok: true,
    bound_accounts: row.id === 1 ? 35 : 0,
    circuit_state: 'ready',
}))
const logs = Array.from({ length: 10 }, (_, index) => ({
    id: index + 1,
    token_id: 1,
    token_email: accounts[0].email,
    operation: 'image_generation',
    status_code: 200,
    created_at: new Date(Date.now() - index * 60000).toISOString(),
}))
try {
    for (const [width, height] of [
        [1100, 650],
        [1366, 700],
        [1440, 800],
        [1920, 1080],
    ]) {
        const context = await browser.newContext({
            viewport: { width, height },
            locale: 'zh-CN',
            reducedMotion: 'reduce',
        })
        await context.addInitScript(() => {
            window.lay4flowDesktop = Object.freeze({ version: 'fixture-desktop' })
            window.desktopMessages = []
            window.desktopListeners = new Set()
            Object.defineProperty(window, 'chrome', {
                configurable: true,
                value: {
                    webview: {
                        postMessage: (value) => window.desktopMessages.push(value),
                        addEventListener: (type, listener) => {
                            if (type === 'message') window.desktopListeners.add(listener)
                        },
                        removeEventListener: (type, listener) => window.desktopListeners.delete(listener),
                    },
                },
            })
        })
        let signedIn = false
        await context.route('**/*', (route) => {
            const request = route.request(),
                url = new URL(request.url())
            if (url.origin !== base) return route.abort()
            if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
            const json = (value, status = 200) => route.fulfill({ json: value, status })
            if (request.method() !== 'GET') {
                writes.push({ method: request.method(), path: url.pathname })
                if (url.pathname === '/api/login' && request.method() === 'POST') {
                    signedIn = true
                    return json({ success: true, username: 'fixture-admin' })
                }
                return json({ detail: 'Layout test blocks writes' }, 500)
            }
            if (url.pathname === '/api/stats' && !signedIn) return json({ detail: 'Login required' }, 401)
            const fixtures = {
                '/health': { version, backend_running: true, has_active_tokens: true, remote_browser_configured: true },
                '/api/stats': {
                    total_tokens: 35,
                    active_tokens: 35,
                    today_images: 12,
                    today_videos: 3,
                    today_errors: 0,
                    total_images: 100,
                    total_videos: 20,
                },
                '/api/tokens': accounts,
                '/api/logs': logs,
                '/api/profile-transfer/jobs': { jobs: [] },
                '/api/token-refresh/config': {
                    config: { protocol_refresh_enabled: true, refresh_interval_minutes: 120 },
                },
                '/api/proxy/pool': { success: true, entries: pool },
                '/api/admin/vids/accounts/detail': { accounts: [] },
                '/api/admin/vids/onboard/candidates': { accounts: [] },
                '/api/admin/vids/status': {
                    configured: true,
                    configured_count: 1,
                    available: { image: 1, video: 1 },
                    quota: {},
                },
                '/api/admin/network': { mode: 'direct', message: 'Synthetic network status', upstream: null },
                '/api/release': {
                    version,
                    release_date: '2026-10-22',
                    entries: [{ version, date: '2026-10-22', items: ['桌面窗口自适应', '更新日志集中展示'] }],
                    update_docs: { windows: '/docs/WINDOWS_INSTALL.md', docker: '/docs/DOCKER_UPDATE.md' },
                },
                '/api/release/check': {
                    status: 'update_available',
                    message: `发现 v${latest}`,
                    latest: { version: latest },
                },
            }
            return json(fixtures[url.pathname] || { accounts: [], data: [], entries: [] })
        })
        const page = await context.newPage()
        page.setDefaultTimeout(15000)
        page.on('pageerror', (error) => errors.push(error.message))
        const key = `${width}x${height}`
        async function verify(label, selectors = []) {
            const values = await page.evaluate((selectors) => {
                const main = document.querySelector('.workspace-main > .main-content')
                return {
                    viewport: [innerWidth, innerHeight],
                    doc: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
                    main: main ? [main.clientHeight, main.scrollHeight] : null,
                    elements: selectors.map((selector) => {
                        const element = document.querySelector(selector),
                            r = element?.getBoundingClientRect()
                        return {
                            selector,
                            x: r?.x,
                            y: r?.y,
                            right: r?.right,
                            bottom: r?.bottom,
                            height: r?.height,
                            width: r?.width,
                        }
                    }),
                }
            }, selectors)
            assert.ok(values.doc[0] <= width + 1, `${label} horizontal overflow: ${JSON.stringify(values)}`)
            assert.ok(values.doc[1] <= height + 1, `${label} document overflow: ${JSON.stringify(values)}`)
            if (values.main)
                assert.ok(values.main[1] <= values.main[0] + 1, `${label} main overflow: ${JSON.stringify(values)}`)
            for (const element of values.elements) {
                assert.ok(element.height > 0 && element.width > 0, `${label} missing ${element.selector}`)
                assert.ok(
                    element.y >= -1 && element.bottom <= height + 1 && element.x >= -1 && element.right <= width + 1,
                    `${label} hidden ${element.selector}: ${JSON.stringify(values)}`,
                )
            }
            results.push({ label, key, values })
            await page.screenshot({ path: resolve(output, `${label}-${key}.png`), animations: 'disabled' })
        }
        await page.goto(`${base}/login#/login`)
        await page.getByLabel('管理员账号', { exact: true }).waitFor()
        await verify('login', ['.login-form-wrap', '.login-submit', '.login-release-trigger'])
        await page.getByLabel('管理员账号', { exact: true }).fill('fixture-admin')
        await page.getByLabel('登录密码', { exact: true }).fill('fixture-password')
        await page.getByRole('button', { name: '进入灵感工坊', exact: true }).click()
        await page.locator('.dashboard-page h1').waitFor()
        await verify('dashboard-recent', ['.dashboard-overview-tabs', '.recent-activity', '.version-trigger'])
        await page.locator('.dashboard-overview-tabs').getByRole('button', { name: '趋势与额度', exact: true }).click()
        await verify('dashboard-overview', ['.dashboard-middle', '.version-trigger'])
        for (const path of ['accounts', 'proxies']) {
            await page.goto(`${base}/manage#/${path}`)
            await page.locator(`.${path === 'accounts' ? 'accounts-view' : 'proxy-pool-view'} h1`).waitFor()
            await page
                .locator(path === 'accounts' ? '.accounts-table .el-table__row' : '.pool-table-dense .el-table__row')
                .first()
                .waitFor()
            await verify(
                path,
                path === 'accounts'
                    ? ['.account-toolbar', '.table-footer', '.version-trigger']
                    : ['.pool-heading-actions', '.pool-toolbar', '.pool-pagination', '.version-trigger'],
            )
            const scrolling = await page
                .locator(
                    path === 'accounts'
                        ? '.accounts-table .el-scrollbar__wrap'
                        : '.pool-table-dense .el-scrollbar__wrap',
                )
                .evaluateAll((elements) => elements.some((element) => element.scrollHeight > element.clientHeight + 1))
            assert.equal(scrolling, true, `${path} rows must scroll locally`)
        }
        const versionTrigger = page.getByRole('button', { name: '版本与更新', exact: true })
        await versionTrigger.click()
        const drawer = page.locator('.release-history-drawer')
        await drawer.getByText(`当前 v${version}`, { exact: true }).waitFor()
        await drawer.getByRole('button', { name: '检查更新', exact: true }).click()
        await drawer.getByRole('button', { name: '立即更新', exact: true }).click()
        assert.deepEqual(await page.evaluate(() => window.desktopMessages), [{ type: 'apply-update', version: latest }])
        assert.equal(await drawer.getByRole('button', { name: '立即更新', exact: true }).isEnabled(), false)
        assert.equal(await page.evaluate(() => window.desktopMessages.length), 1)
        await page.evaluate(() =>
            window.desktopListeners.forEach((listener) =>
                listener({
                    data: { type: 'desktop-update-status', state: 'failed', message: '合成下载失败，原版本保留' },
                }),
            ),
        )
        await drawer.getByText('合成下载失败，原版本保留', { exact: true }).waitFor()
        await drawer.getByRole('button', { name: '立即更新', exact: true }).click()
        assert.equal(await page.evaluate(() => window.desktopMessages.length), 2)
        await drawer.getByRole('button', { name: /close|关闭/i }).click()
        assert.equal(await versionTrigger.locator('.version-update-dot').count(), 1)
        await verify('update-version-dot', ['.version-trigger'])
        await context.close()
    }
    assert.deepEqual(errors, [])
    assert.ok(writes.every((write) => write.method === 'POST' && write.path === '/api/login'))
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ results, errors, writes }, null, 2))
    console.log(`PASS ${results.length} layout checks, local table scrolling and mocked WebView update retries`)
} finally {
    await browser.close()
}
