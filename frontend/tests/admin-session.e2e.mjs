import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Use only scripts/preview_frontend.py: temporary data, synthetic credentials.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38128'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/admin-session-review')
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
let context, activePage
const checks = [],
    errors = [],
    requests = []
const record = (message) => {
    checks.push(message)
    console.log(`PASS ${message}`)
}
async function open(cookies = []) {
    const created = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
    await created.addCookies(cookies)
    await created.route('**/*', (route) => {
        const request = route.request(),
            url = new URL(request.url())
        if (url.origin !== base) return route.abort()
        if (!['GET', 'HEAD'].includes(request.method()) && !['/api/login', '/api/logout'].includes(url.pathname))
            return route.abort()
        return route.continue()
    })
    const page = await created.newPage()
    activePage = page
    page.on('pageerror', (error) => errors.push(error.message))
    page.on('response', (response) => {
        if (new URL(response.url()).pathname.startsWith('/api/'))
            requests.push({ path: new URL(response.url()).pathname, status: response.status() })
    })
    page.setDefaultTimeout(15000)
    return { context: created, page }
}
try {
    let page
    ;({ context, page } = await open())
    await page.goto(`${base}/#/login`)
    await page.getByPlaceholder('输入你的账号').fill('preview-admin')
    await page.getByPlaceholder('输入登录密码').fill('preview-password')
    await page.getByPlaceholder('输入登录密码').blur()
    await page.getByRole('button', { name: '进入灵感工坊', exact: true }).click()
    await page.locator('.dashboard-page h1').waitFor()
    const cookie = (await context.cookies()).find((item) => item.name === 'lay4flow_remote_admin_session')
    assert.ok(cookie?.httpOnly)
    assert.ok(cookie.expires > Date.now() / 1000 + 29 * 86400)
    assert.equal(await page.evaluate(() => localStorage.getItem('token')), null)
    record('Login uses a persistent 30-day HttpOnly cookie without storing a bearer token')

    await context.close()
    ;({ context, page } = await open([cookie]))
    await page.goto(`${base}/#/accounts`)
    await page.locator('.accounts-view h1').waitFor()
    await page.reload()
    await page.locator('.accounts-view h1').waitFor()
    record('A new browser context and page reload retain the admin login')

    await context.route('**/api/tokens', (route) =>
        route.fulfill({
            status: 401,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'Synthetic upstream account requires reconnection' }),
        }),
    )
    await page.reload()
    await page
        .locator('.account-alert')
        .filter({ hasText: 'Synthetic upstream account requires reconnection' })
        .waitFor()
    assert.match(page.url(), /#\/accounts/)
    assert.equal((await context.request.get(`${base}/api/stats`)).status(), 200)
    record('An account-level 401 shows its error and preserves the valid admin session')
    await page.screenshot({ path: resolve(output, 'business-error-keeps-login.png'), fullPage: true })

    assert.equal((await context.request.post(`${base}/api/logout`)).status(), 200)
    const revoked = await context.request.get(`${base}/api/stats`)
    assert.equal(revoked.status(), 401)
    assert.equal(revoked.headers()['x-admin-session'], 'expired')
    await page.goto(`${base}/#/proxies`)
    await page.getByPlaceholder('输入你的账号').waitFor()
    record('Explicit logout revokes the persisted session and true admin expiry returns to sign-in')
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, errors }, null, 2))
} catch (error) {
    await activePage?.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(
        resolve(output, 'failure.json'),
        JSON.stringify(
            { message: error.message, errors, requests, body: await activePage?.locator('body').innerText() },
            null,
            2,
        ),
    )
    throw error
} finally {
    await context?.close()
    await browser.close()
}
