import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// The native contract is mocked. No real account or browser process is opened.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38139'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/native-profile-ui')
await mkdir(output, { recursive: true })
const executablePath = [process.env.PLAYWRIGHT_BROWSER_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe']
    .filter(Boolean)
    .find(existsSync)
assert.ok(executablePath)
const browser = await chromium.launch({ executablePath, headless: true })
const checks = []
const errors = []
const record = (text) => { checks.push(text); console.log(`PASS ${text}`) }
try {
    for (const shell of [true, false]) {
        const context = await browser.newContext({ viewport: { width: 1366, height: 700 }, locale: 'zh-CN' })
        if (shell) await context.addInitScript(() => { window.lay4flowDesktop = { runtime: 'native', version: 'fixture' } })
        const writes = []
        const requests = []
        const row = { id: 71, email: 'native@example.test', is_active: true, credits: 120, browser_profile_source: 'cloak', browser_profile_status: 'ready', refresh_status: 'ready' }
        await context.route('**/*', async (route) => {
            const request = route.request(), url = new URL(request.url())
            if (url.origin !== base) return route.abort()
            requests.push(url.pathname)
            if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
            if (request.method() !== 'GET') {
                writes.push({ path: url.pathname, query: url.search, method: request.method() })
                if (url.pathname === '/api/profile-login') return route.fulfill({ json: { success: true, session_id: 'native-login', token_id: 71, viewer_id: 'native-owned', presentation: 'native' } })
                return route.fulfill({ json: { success: true, status: 'ready', viewer_id: 'native-owned', presentation: 'native', message: '已保存原生窗口凭据' } })
            }
            const fixtures = {
                '/health': { backend_running: true, has_active_tokens: true, remote_browser_configured: true, viewer_presentation: 'native' },
                '/api/tokens': [row],
                '/api/profile-transfer/jobs': { jobs: [] },
                '/api/token-refresh/config': { config: { protocol_refresh_enabled: true } },
                '/api/proxy/pool': { entries: [] },
                '/api/admin/network': { mode: 'direct', message: 'Synthetic native network' },
            }
            return route.fulfill({ json: fixtures[url.pathname] || { accounts: [], entries: [], data: [] } })
        })
        const page = await context.newPage()
        page.setDefaultTimeout(12000)
        page.on('pageerror', (error) => errors.push(error.message))
        await page.goto(`${base}/static/app/#/accounts`)
        await page.locator('.accounts-view h1').waitFor()
        await page.getByRole('button', { name: '添加账号', exact: true }).first().click()
        const dialog = page.getByRole('dialog', { name: 'Cloak 登录账号' })
        await dialog.getByPlaceholder('name@gmail.com').fill('native@example.test')
        await dialog.getByRole('button', { name: '打开本地 Cloak 登录', exact: true }).click()
        await dialog.getByText('本地 Cloak 窗口已打开', { exact: false }).waitFor()
        assert.equal(context.pages().length, 1)
        await dialog.getByRole('button', { name: '继续登录', exact: true }).click()
        await page.waitForFunction(() => document.body.textContent.includes('已显示本地 Cloak 登录窗口'))
        assert.ok(writes.some((request) => request.path === '/api/tokens/71/browser/focus' && request.query === '?viewer_id=native-owned'))
        await dialog.getByRole('button', { name: '取消本次登录', exact: true }).click()
        await dialog.waitFor({ state: 'hidden' })
        await page.locator('.accounts-table').getByRole('button', { name: '浏览器', exact: true }).click()
        const bar = page.locator('.native-browser-bar')
        await bar.getByText('native@example.test 的本地 Cloak 窗口已打开', { exact: true }).waitFor()
        await bar.getByRole('button', { name: '显示窗口', exact: true }).click()
        await bar.getByRole('button', { name: '刷新凭据', exact: true }).click()
        await page.screenshot({ path: resolve(output, `native-account-${shell ? 'desktop' : 'browser'}.png`) })
        await bar.getByRole('button', { name: '保存并关闭后刷新', exact: true }).click()
        await bar.waitFor({ state: 'hidden' })
        assert.equal(context.pages().length, 1)
        assert.ok(!requests.some((path) => path.includes('profile-browser-assets') || path.endsWith('/ws')))
        assert.ok(writes.some((request) => request.path === '/api/tokens/71/refresh-at'))
        record(`${shell ? 'Native desktop bridge' : 'Normal browser with native backend'} logs in and manages Cloak inline without noVNC or popups`)
        await context.close()
    }
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'report.json'), JSON.stringify({ checks, errors }, null, 2))
} finally { await browser.close() }
