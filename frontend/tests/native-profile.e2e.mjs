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
const record = (text) => {
    checks.push(text)
    console.log(`PASS ${text}`)
}
try {
    for (const shell of [true, false]) {
        const context = await browser.newContext({ viewport: { width: 1366, height: 700 }, locale: 'zh-CN' })
        if (shell)
            await context.addInitScript(() => {
                window.lay4flowDesktop = { runtime: 'native', version: 'fixture' }
            })
        const writes = []
        const requests = []
        let loginState = 'waiting'
        let starts = 0
        let retries = 0
        let viewerId = 'native-owned'
        const accounts = []
        const row = {
            id: 71,
            email: 'native@example.test',
            is_active: true,
            credits: 120,
            browser_profile_source: 'cloak',
            browser_profile_status: 'ready',
            refresh_status: 'ready',
        }
        await context.route('**/*', async (route) => {
            const request = route.request(),
                url = new URL(request.url())
            if (url.origin !== base) return route.abort()
            requests.push(url.pathname)
            if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
            const receipt = () => ({
                success: true,
                session_id: 'native-login',
                token_id: 71,
                viewer_id: viewerId,
                presentation: 'native',
                expires_at: '2099-01-01T00:00:00Z',
            })
            if (url.pathname === '/api/profile-login/native-login' && request.method() === 'GET') {
                assert.equal(url.searchParams.get('viewer_id'), viewerId)
                if (loginState === 'error')
                    return route.fulfill({
                        json: {
                            success: true,
                            status: 'error',
                            error_code: 'profile_login_verification_failed',
                            reason: 'transient_error',
                            stage: 'session_probe',
                            network_step: 'flow_bootstrap',
                            retryable: true,
                            browser_open: false,
                            message: 'Flow 会话读取暂时失败，请继续登录重试',
                        },
                    })
                if (loginState === 'ready') {
                    if (!accounts.length) accounts.push(row)
                    return route.fulfill({
                        json: {
                            success: true,
                            status: 'ready',
                            result: { email: row.email, token_id: row.id, is_active: true },
                        },
                    })
                }
                return route.fulfill({
                    json: { success: true, status: 'waiting', message: '等待本地 Cloak 窗口完成 Google 登录' },
                })
            }
            if (request.method() !== 'GET') {
                writes.push({
                    path: url.pathname,
                    query: url.search,
                    method: request.method(),
                    body: request.postDataJSON(),
                })
                if (url.pathname === '/api/profile-login') {
                    starts++
                    loginState = 'waiting'
                    viewerId = 'native-owned'
                    return route.fulfill({ json: receipt() })
                }
                if (url.pathname === '/api/profile-login/native-login/retry') {
                    assert.deepEqual(request.postDataJSON(), { viewer_id: viewerId })
                    retries++
                    viewerId = 'native-retried'
                    loginState = 'waiting'
                    return route.fulfill({ json: receipt() })
                }
                return route.fulfill({
                    json: {
                        success: true,
                        status: 'ready',
                        viewer_id: viewerId,
                        presentation: 'native',
                        message: '已保存原生窗口凭据',
                    },
                })
            }
            const fixtures = {
                '/health': {
                    backend_running: true,
                    has_active_tokens: true,
                    remote_browser_configured: true,
                    viewer_presentation: 'native',
                },
                '/api/tokens': accounts,
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
        await page.goto(`${base}/#/accounts`)
        await page.locator('.accounts-view h1').waitFor()
        if (shell) {
            await page.locator('.page-actions').getByRole('button', { name: '扩展登录', exact: true }).click()
            const extension = page.getByRole('dialog', { name: '扩展登录 · 指纹浏览器' })
            await extension.waitFor()
            assert.equal(await extension.getByLabel('本机管理地址').inputValue(), `${base}/#/accounts`)
            assert.equal(await extension.getByRole('button', { name: '检测扩展', exact: true }).count(), 0)
            assert.equal(await extension.getByRole('button', { name: '开始扩展登录', exact: true }).count(), 0)
            assert.equal(await extension.getByText('登录环境', { exact: true }).count(), 0)
            await extension.getByRole('button', { name: '复制管理地址', exact: true }).waitFor()
            await extension.getByRole('button', { name: '刷新账号', exact: true }).click()
            assert.ok(!writes.some((request) => request.path === '/api/plugin/import/start'))
            await page.screenshot({ path: resolve(output, 'desktop-extension-login.png') })
            await extension.getByRole('button', { name: '关闭', exact: true }).click()
            await extension.waitFor({ state: 'hidden' })
            record('Desktop extension login shows its actual local address and refreshes accounts without an impossible WebView handshake')
        }
        await page.getByRole('button', { name: '添加账号', exact: true }).first().click()
        const dialog = page.getByRole('dialog', { name: 'Cloak 登录账号' })
        await dialog.getByRole('button', { name: '打开本地 Cloak 登录', exact: true }).waitFor()
        assert.equal(starts, 0)
        await dialog.getByRole('button', { name: '打开本地 Cloak 登录', exact: true }).click()
        await dialog.getByText('等待登录', { exact: true }).waitFor()
        assert.equal(await dialog.getByPlaceholder('name@gmail.com').count(), 0)
        assert.equal(await dialog.getByRole('button', { name: '完成登录并添加', exact: true }).count(), 0)
        assert.deepEqual(writes.find((request) => request.path === '/api/profile-login').body, {})
        assert.equal(context.pages().length, 1)
        loginState = 'error'
        await dialog.getByRole('button', { name: '刷新登录状态', exact: true }).click()
        await dialog.getByText('Flow 会话读取暂时失败，请继续登录重试', { exact: false }).waitFor()
        const stored = await page.evaluate(() => JSON.parse(sessionStorage.getItem('lay4flow.profileLogin')))
        assert.equal(stored.session_id, 'native-login')
        assert.equal(stored.viewer_id, 'native-owned')
        assert.equal(stored.browser_open, false)
        assert.equal('cookies' in stored, false)
        await page.reload()
        await page.getByRole('button', { name: '查看登录进度', exact: true }).click()
        await dialog.getByText('Flow 会话读取暂时失败，请继续登录重试', { exact: false }).waitFor()
        await dialog.getByRole('button', { name: '继续登录', exact: true }).click()
        await page.waitForFunction(
            () => JSON.parse(sessionStorage.getItem('lay4flow.profileLogin') || '{}').viewer_id === 'native-retried',
        )
        assert.equal(retries, 1)
        assert.equal(starts, 1)
        assert.ok(
            writes.some(
                (request) =>
                    request.path === '/api/tokens/71/browser/focus' && request.query === '?viewer_id=native-retried',
            ),
        )
        loginState = 'ready'
        await dialog.waitFor({ state: 'hidden' })
        await page.locator('.accounts-table').getByText(row.email, { exact: true }).waitFor()
        assert.equal(await page.evaluate(() => sessionStorage.getItem('lay4flow.profileLogin')), null)
        assert.equal(
            writes.some((request) => request.path.endsWith('/complete')),
            false,
        )
        record(
            `${shell ? 'Native desktop' : 'Native backend'} preserves an anonymous login receipt across reload and retries Flow verification with the same session`,
        )
        await page.getByRole('button', { name: '添加账号', exact: true }).first().click()
        await dialog.getByRole('button', { name: '打开本地 Cloak 登录', exact: true }).click()
        await dialog.getByText('等待登录', { exact: true }).waitFor()
        await dialog.getByRole('button', { name: '取消本次登录', exact: true }).click()
        await dialog.waitFor({ state: 'hidden' })
        assert.equal(starts, 2)
        assert.ok(
            writes.some((request) => request.path === '/api/profile-login/native-login' && request.method === 'DELETE'),
        )
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
        record(
            `${shell ? 'Native desktop bridge' : 'Normal browser with native backend'} logs in and manages Cloak inline without noVNC or popups`,
        )
        await context.close()
    }
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'report.json'), JSON.stringify({ checks, errors }, null, 2))
} finally {
    await browser.close()
}
