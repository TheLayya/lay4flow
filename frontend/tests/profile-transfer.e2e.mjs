import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// All requests are synthetic; no real Google session or runtime account is used.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38126'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/profile-transfer-ui')
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
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
const page = await context.newPage()
page.setDefaultTimeout(12000)
const requests = [],
    errors = [],
    checks = []
page.on('pageerror', (error) => errors.push(error.message))
let completeReady = false
let importJob = null
let retryCount = 0
const accounts = []
const bundle = {
    format: 'lay4flow-cloak-session',
    version: 1,
    account: { email: 'direct@example.test', is_active: true },
    browser: { fingerprint_seed: 41010 },
    storage_state: { cookies: [], origins: [] },
}
const receipt = {
    success: true,
    session_id: 'synthetic-session',
    token_id: 71,
    viewer_id: 'synthetic-viewer',
    viewer_url: '/api/profile-browser/71',
    expires_at: '2099-01-01T00:00:00Z',
}
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(),
        url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    requests.push({ method: request.method(), path: url.pathname, body: request.postDataJSON() })
    if (url.pathname === '/health') return json(route, { backend_running: true, has_active_tokens: true })
    if (url.pathname === '/api/stats')
        return json(route, { total_tokens: accounts.length, active_tokens: accounts.length })
    if (url.pathname === '/api/tokens') return json(route, accounts)
    if (url.pathname === '/api/token-refresh/config')
        return json(route, { config: { protocol_refresh_enabled: false } })
    if (url.pathname.startsWith('/api/admin/vids/')) return json(route, { accounts: [] })
    if (url.pathname === '/api/proxy/pool')
        return json(route, {
            entries: [
                {
                    id: 17,
                    enabled: true,
                    last_check_ok: true,
                    note: '目标出口',
                    proxy_url: 'socks5://destination.example.test:1080',
                },
            ],
        })
    if (url.pathname === '/api/profile-login') return json(route, receipt)
    if (url.pathname === '/api/profile-browser/71')
        return route.fulfill({ contentType: 'text/html', body: '<p>Synthetic Cloak login</p>' })
    if (url.pathname === '/api/profile-login/synthetic-session/complete') {
        if (!completeReady)
            return json(
                route,
                { detail: { error_code: 'login_required', message: 'Complete synthetic Google login first' } },
                409,
            )
        accounts.push({
            id: 71,
            email: 'direct@example.test',
            is_active: true,
            credits: 1050,
            browser_profile_source: 'cloak',
            browser_profile_status: 'ready',
            refresh_status: 'ready',
        })
        return json(route, { success: true, token_id: 71, email: 'direct@example.test', is_active: true })
    }
    if (url.pathname === '/api/tokens/71/profile-export') return json(route, { success: true, bundle })
    if (url.pathname === '/api/profile-transfer/jobs') {
        if (request.method() === 'GET') return json(route, { success: true, jobs: importJob ? [importJob] : [] })
        importJob = {
            id: 'synthetic-background-import',
            state: 'running',
            request_id: request.postDataJSON().request_id,
            added: 0,
            updated: 0,
            failed: 0,
            total: 1,
            current: 1,
            elapsed_seconds: 3,
            can_retry: false,
            created_at: new Date().toISOString(),
            items: [
                {
                    email: bundle.account.email,
                    state: 'running',
                    stage: 'oauth_renewal',
                    message: 'Google 登录态续签',
                    elapsed_seconds: 3,
                },
            ],
        }
        return json(route, { success: true, job: importJob }, 202)
    }
    if (url.pathname === '/api/profile-transfer/jobs/synthetic-background-import/retry') {
        retryCount++
        importJob = {
            ...importJob,
            state: 'running',
            can_retry: false,
            failed: 0,
            items: [
                {
                    email: bundle.account.email,
                    state: 'running',
                    stage: 'cold_check_1',
                    message: '首次重开校验',
                    elapsed_seconds: 2,
                },
            ],
        }
        return json(route, { success: true, job: importJob })
    }
    return json(route, { detail: `Unexpected fixture request ${url.pathname}` }, 503)
})
const loginDialog = () => page.getByRole('dialog').filter({ hasText: 'Cloak 登录账号' })
const record = (text) => {
    checks.push(text)
    console.log(`PASS ${text}`)
}
try {
    await page.goto(`${base}/login#/accounts`)
    await page.locator('.accounts-view h1').waitFor()
    await page.locator('.page-actions').getByRole('button', { name: '添加账号', exact: true }).click()
    await loginDialog().getByPlaceholder('name@gmail.com').fill('direct@example.test')
    const popupPromise = context.waitForEvent('page')
    await loginDialog().getByRole('button', { name: '打开 Cloak 登录', exact: true }).click()
    const popup = await popupPromise
    await popup.waitForURL(`${base}/api/profile-browser/71?viewer_id=synthetic-viewer`)
    await loginDialog().getByRole('button', { name: '完成登录并添加', exact: true }).waitFor()
    assert.ok(await page.evaluate(() => !!sessionStorage.getItem('lay4flow.profileLogin')))
    record('Add account directly opens a scoped Cloak viewer without extension installation')

    await page.reload()
    await page.getByRole('button', { name: '查看登录进度', exact: true }).click()
    await loginDialog().getByRole('button', { name: '完成登录并添加', exact: true }).click()
    await loginDialog().getByText('Complete synthetic Google login first', { exact: false }).waitFor()
    assert.ok(await page.evaluate(() => !!sessionStorage.getItem('lay4flow.profileLogin')))
    completeReady = true
    await loginDialog().getByRole('button', { name: '完成登录并添加', exact: true }).click()
    await loginDialog().waitFor({ state: 'hidden' })
    await page.locator('.accounts-table').getByText('direct@example.test', { exact: true }).waitFor()
    assert.equal(await page.evaluate(() => sessionStorage.getItem('lay4flow.profileLogin')), null)
    record('Reload resumes login; rejected verification keeps it open and successful verification adds the account')

    await page.getByRole('button', { name: '更多账号操作', exact: true }).click()
    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('menuitem', { name: '导出全部账号', exact: true }).click()
    const download = await downloadPromise
    const exported = await readFile(await download.path(), 'utf8')
    assert.deepEqual(JSON.parse(exported), { format: 'lay4flow-profile-bundle', version: 1, accounts: [bundle] })
    await page.getByRole('button', { name: '更多账号操作', exact: true }).click()
    await page.getByRole('menuitem', { name: '导入 JSON', exact: true }).click()
    const importDialog = page.getByRole('dialog').filter({ hasText: '导入账号 Profile' })
    await importDialog.getByPlaceholder('粘贴导出的 Profile JSON').fill(exported)
    await importDialog.locator('.el-select').click()
    await page.getByRole('option', { name: /#17/ }).click()
    await importDialog.getByRole('button', { name: '后台导入', exact: true }).click()
    await importDialog.waitFor({ state: 'hidden' })
    await page.locator('.profile-import-jobs').waitFor()
    const importRequest = requests.find((item) => item.path === '/api/profile-transfer/jobs' && item.method === 'POST')
    assert.deepEqual(importRequest.body.bundles, [bundle])
    assert.equal(importRequest.body.proxy_pool_id, 17)
    assert.equal(typeof importRequest.body.request_id, 'string')
    await page.screenshot({ path: resolve(output, 'profile-import-progress.png'), fullPage: true })
    record('Submitting returns promptly, closes the dialog and displays a server-side import task')

    await page.locator('.main-nav a[href="#/proxies"]').click()
    await page.locator('.profile-import-notice').getByText('账号正在后台导入', { exact: false }).waitFor()
    importJob = {
        ...importJob,
        state: 'failed',
        failed: 1,
        can_retry: true,
        elapsed_seconds: 10,
        items: [
            {
                email: bundle.account.email,
                state: 'failed',
                stage: 'oauth_renewal',
                reason: 'transient_error',
                message: 'Google 登录态续签失败：网络请求超时；目标资料已保留',
                elapsed_seconds: 10,
            },
        ],
    }
    await page.locator('.profile-import-notice').getByText('后台导入已结束', { exact: false }).waitFor()
    await page.locator('.profile-import-notice a').click()
    await page.locator('.profile-import-jobs').waitFor()
    await page.reload()
    const task = page.locator('.profile-import-job')
    await task.waitFor()
    if ((await task.getAttribute('open')) === null) await task.locator('summary').click()
    await task.getByText('Google 登录态续签失败：网络请求超时；目标资料已保留', { exact: false }).waitFor()
    record('Navigation and reload recover progress and the specific failed result without resubmitting JSON')

    await task.getByRole('button', { name: /重试失败/ }).click()
    await task.getByText('首次重开校验', { exact: false }).waitFor()
    assert.equal(retryCount, 1)
    importJob = {
        ...importJob,
        state: 'completed',
        failed: 0,
        added: 1,
        can_retry: false,
        items: [
            {
                email: bundle.account.email,
                state: 'succeeded',
                stage: 'completed',
                message: '导入完成',
                elapsed_seconds: 7,
            },
        ],
    }
    await task.getByText('导入完成', { exact: false }).first().waitFor()
    assert.equal(
        requests.filter((item) => item.path === '/api/profile-transfer/jobs' && item.method === 'POST').length,
        1,
    )
    assert.equal(requests.filter((item) => item.path === '/api/profile-transfer/import').length, 0)
    assert.ok(requests.every((item) => !item.path.includes('/plugin/')))
    await page.screenshot({ path: resolve(output, 'profile-import-result.png'), fullPage: true })
    record('Retry operates only on failed server-side items and completes without repeating successful imports')
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, errors, requests }, null, 2))
} catch (error) {
    await writeFile(
        resolve(output, 'failure.json'),
        JSON.stringify({ message: error.message, requests, body: await page.locator('body').innerText() }, null, 2),
    )
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    throw error
} finally {
    await browser.close()
}
