import assert from 'node:assert/strict'
import { chromium } from 'playwright'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Synthetic account/proxy UI regression; every API and external request is intercepted.
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/quick-binding-review')
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38127'
const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_BROWSER_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
})
const context = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
    reducedMotion: 'reduce',
    locale: 'zh-CN',
})
const page = await context.newPage()
page.setDefaultTimeout(15000)
page.setDefaultNavigationTimeout(30000)
await mkdir(output, { recursive: true })
const urlA = 'socks5://plain-user:plain-password@192.0.2.11:1080'
const urlB = 'socks5://second-user:second-password@192.0.2.12:1080'
const proxies = [
    { id: 1, proxy_url: urlA, note: '创作出口 A', enabled: true, last_check_ok: true },
    { id: 2, proxy_url: urlB, note: '创作出口 B', enabled: true, last_check_ok: true },
    { id: 3, proxy_url: 'socks5://paused:pass@192.0.2.13:1080', note: '已停用出口', enabled: false },
]
Object.assign(proxies[0], {
    circuit_state: 'ready',
    inflight: 2,
    success_count: 8,
    failure_count: 2,
    success_rate: 0.8,
    consecutive_failures: 0,
})
Object.assign(proxies[1], {
    circuit_state: 'cooling',
    inflight: 0,
    success_count: 5,
    failure_count: 3,
    success_rate: 0.625,
    consecutive_failures: 3,
    cooldown_until: '2099-01-01T00:00:00Z',
})
const accounts = [
    {
        id: 11,
        email: 'free@example.test',
        name: '待绑定账号',
        proxy_pool_id: null,
        proxy_url: '',
        browser_profile_source: 'cookies',
    },
    {
        id: 12,
        email: 'retry@example.test',
        name: '重试账号',
        proxy_pool_id: null,
        proxy_url: '',
        browser_profile_source: 'cookies',
    },
    {
        id: 13,
        email: 'cloak@example.test',
        name: '持久账号',
        proxy_pool_id: 2,
        proxy_url: urlB,
        browser_profile_source: 'cloak',
    },
].map((row) => ({
    ...row,
    is_active: true,
    st: `plain-st-${row.id}`,
    at: `plain-at-${row.id}`,
    login_password: `login-pass-${row.id}`,
    login_account: row.email,
    remark: '',
    image_enabled: true,
    video_enabled: true,
    auto_refresh_enabled: true,
    refresh_interval_minutes: 120,
    submission_proxy_mode: 'fixed',
    at_expires: '2099-01-01T00:00:00Z',
    credits: 100,
    browser_profile_status: 'ready',
}))
const writes = [],
    errors = [],
    unexpected = [],
    checks = []
let rejectRetry = true
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
page.on('pageerror', (error) => errors.push(error.message))
await context.addInitScript(() => {
    window.bindingCopies = []
    Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: async (value) => window.bindingCopies.push(value) },
    })
})
await context.route('**/*', async (route) => {
    const request = route.request(),
        url = new URL(request.url())
    if (url.origin !== base) {
        unexpected.push(request.url())
        return route.abort()
    }
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    if (request.method() === 'PUT' && /^\/api\/tokens\/\d+$/.test(url.pathname)) {
        const id = Number(url.pathname.split('/').at(-1)),
            body = request.postDataJSON()
        writes.push({ id, body })
        if (id === 12 && rejectRetry) return json(route, { detail: '模拟保存失败，请重试' }, 400)
        const account = accounts.find((row) => row.id === id)
        const proxy = proxies.find((row) => row.id === body.proxy_pool_id)
        if (proxy && account.browser_profile_source === 'cloak' && account.proxy_url !== proxy.proxy_url)
            return json(route, { detail: 'Cloak 固定使用原出口' }, 400)
        Object.assign(account, body, proxy ? { proxy_url: proxy.proxy_url } : {})
        return json(route, { success: true })
    }
    if (request.method() === 'PUT' && /^\/api\/proxy\/pool\/\d+$/.test(url.pathname)) {
        const id = Number(url.pathname.split('/').at(-1)),
            body = request.postDataJSON()
        writes.push({ proxyId: id, body })
        Object.assign(
            proxies.find((row) => row.id === id),
            body,
        )
        return json(route, { success: true })
    }
    if (!['GET', 'HEAD'].includes(request.method())) {
        unexpected.push(`${request.method()} ${url.pathname}`)
        return json(route, {}, 503)
    }
    if (url.pathname === '/health') return json(route, { backend_running: true, has_active_tokens: true })
    if (url.pathname === '/api/stats') return json(route, { total_tokens: 3, active_tokens: 3 })
    if (url.pathname === '/api/tokens') return json(route, accounts)
    if (url.pathname === '/api/profile-transfer/jobs') return json(route, { success: true, jobs: [] })
    if (url.pathname === '/api/proxy/pool')
        return json(route, {
            success: true,
            entries: proxies.map((row) => ({
                ...row,
                bound_account_count: accounts.filter((account) => account.proxy_pool_id === row.id).length,
            })),
        })
    if (url.pathname === '/api/token-refresh/config') return json(route, { config: { protocol_refresh_enabled: true } })
    if (url.pathname === '/api/admin/vids/accounts/detail' || url.pathname === '/api/admin/vids/onboard/candidates')
        return json(route, { accounts: [] })
    unexpected.push(`GET ${url.pathname}`)
    return json(route, {}, 404)
})
const record = (message) => {
    checks.push(message)
    console.log('PASS ' + message)
}
const visible = (selector) => page.locator(selector + ':visible')
const noOverflow = async () =>
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
try {
    await page.goto(`${base}/login#/proxies`, { waitUntil: 'domcontentloaded' })
    await visible('.pool-bind-trigger').filter({ hasText: '绑定 0' }).first().click()
    const panel = visible('.proxy-inline-editor')
    await panel.waitFor()
    assert.match(await panel.innerText(), /plain-password/)
    assert.equal(
        await panel.locator('input:disabled').count(),
        0,
        'View mode uses compact values instead of disabled forms',
    )
    await panel.getByRole('button', { name: '复制地址', exact: true }).click()
    assert.equal((await page.evaluate(() => window.bindingCopies)).at(-1), urlA)
    assert.match(await visible('.pool-table-dense').innerText(), /近期成功 80%/)
    assert.match(await visible('.pool-table-dense').innerText(), /熔断冷却/)
    assert.match(await visible('.pool-table-dense').innerText(), /最早.+重探/)
    const box = panel.locator('.inline-binding .el-select')
    await box.click()
    await page.getByRole('option').filter({ hasText: 'free@example.test' }).click()
    await page.getByRole('option').filter({ hasText: 'retry@example.test' }).click()
    assert.equal(
        await page
            .getByRole('option')
            .filter({ hasText: 'cloak@example.test' })
            .getAttribute('class')
            .then((value) => value.includes('is-disabled')),
        false,
        'Cloak accounts remain selectable for server-side verified switching',
    )
    await page.keyboard.press('Escape')
    await panel.getByRole('button', { name: /绑定所选 2 个/ }).click()
    await panel.locator('.bind-results').filter({ hasText: '模拟保存失败' }).waitFor()
    assert.deepEqual(writes.slice(0, 2), [
        { id: 11, body: { proxy_pool_id: 1 } },
        { id: 12, body: { proxy_pool_id: 1 } },
    ])
    assert.equal(await panel.locator('.bound-account-row').count(), 1)
    record(
        'Proxy row expands to plain connection details and batch binding; partial failures keep only the retry selection',
    )
    rejectRetry = false
    await panel.getByRole('button', { name: /绑定所选 1 个/ }).click()
    await panel.locator('.bound-account-row').nth(1).waitFor()
    await box.click()
    await page.getByRole('option').filter({ hasText: 'cloak@example.test' }).click()
    await page.keyboard.press('Escape')
    const beforeCancel = writes.length
    await panel.getByRole('button', { name: /绑定所选 1 个/ }).click()
    const confirmation = page.getByRole('dialog', { name: '确认切换 Cloak 代理' })
    await confirmation.waitFor()
    assert.match(await confirmation.innerText(), /Cookie.*seed.*Profile/)
    await confirmation.getByRole('button', { name: '保留原绑定', exact: true }).click()
    assert.equal(writes.length, beforeCancel)
    await panel.getByRole('button', { name: /绑定所选 1 个/ }).click()
    await confirmation.getByRole('button', { name: '验证并切换', exact: true }).click()
    await panel.locator('.bind-results').filter({ hasText: 'Cloak 固定使用原出口' }).waitFor()
    assert.equal(accounts.find((item) => item.id === 13).proxy_pool_id, 2)
    record('Cloak switch confirmation cancels without a write and failed verification preserves the original binding')
    const bound = panel.locator('.bound-account-row').filter({ hasText: 'free@example.test' })
    await bound.locator('summary').click()
    assert.match(await bound.innerText(), /plain-st-11/)
    await bound.getByRole('textbox', { name: '账号备注', exact: true }).fill('原位编辑备注')
    await bound.getByRole('button', { name: '保存设置', exact: true }).click()
    assert.deepEqual(writes.at(-1), { id: 11, body: { remark: '原位编辑备注' } })
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'proxy-binding-1600.png'), animations: 'disabled' })
    record('Bound account expands in place for plain credentials, copying and changed-fields-only settings')
    await page.goto(`${base}/login#/accounts`, { waitUntil: 'domcontentloaded' })
    const row = page.locator('.accounts-table .el-table__row').filter({ hasText: 'free@example.test' })
    await row.locator('.account-proxy-trigger').click()
    const quick = visible('.account-quick-panel[data-account-id="11"]')
    await quick.waitFor()
    assert.match(await quick.innerText(), /plain-password/)
    await quick.locator('.quick-proxy-picker:not(.quick-submission-picker) .el-select').click()
    await page.getByRole('option').filter({ hasText: '创作出口 B' }).click()
    await quick.locator('.quick-bind-save').click()
    await quick.getByRole('status').filter({ hasText: '已绑定代理 #2' }).waitFor()
    assert.equal(new URL(page.url()).hash, '#/accounts')
    await quick.getByRole('button', { name: '复制代理密码', exact: true }).click()
    assert.equal((await page.evaluate(() => window.bindingCopies)).at(-1), 'second-password')
    await quick.locator('.quick-credentials summary').click()
    assert.match(await quick.innerText(), /plain-at-11/)
    await quick.screenshot({ path: resolve(output, 'account-binding-1600.png'), animations: 'disabled' })
    record('Account row binds a new proxy and copies decoded credentials without a drawer, dialog or route change')
    await quick.locator('.quick-submission-picker .el-select').click()
    await page.getByRole('option', { name: '代理池随机（每次生成取健康出口）', exact: true }).click()
    await quick.getByRole('button', { name: '保存策略', exact: true }).click()
    await quick.getByRole('status').filter({ hasText: '生成出口已设置为代理池随机' }).waitFor()
    assert.deepEqual(writes.at(-1), { id: 11, body: { submission_proxy_mode: 'pool_random' } })
    assert.equal(accounts.find((item) => item.id === 11).proxy_pool_id, 2)
    assert.equal(accounts.find((item) => item.id === 11).proxy_url, urlB)
    await page.locator('.el-message').last().waitFor({ state: 'hidden' })
    await quick.screenshot({ path: resolve(output, 'submission-strategy-1600.png'), animations: 'disabled' })
    record('Submission strategy saves separately and the account retains its fixed login exit')
    await page.setViewportSize({ width: 1440, height: 900 })
    await noOverflow()
    await visible('.account-quick-panel[data-account-id="11"]').screenshot({
        path: resolve(output, 'submission-strategy-1440.png'),
        animations: 'disabled',
    })
    await page.setViewportSize({ width: 390, height: 844 })
    await noOverflow()
    await visible('.account-quick-panel[data-account-id="11"]').waitFor()
    await visible('.account-quick-panel[data-account-id="11"]').screenshot({
        path: resolve(output, 'account-binding-390.png'),
        animations: 'disabled',
    })
    await page.goto(`${base}/login#/proxies`, { waitUntil: 'domcontentloaded' })
    await visible('.pool-bind-trigger').first().click()
    await visible('.proxy-inline-editor').waitFor()
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'proxy-binding-390.png'), animations: 'disabled' })
    record('390px account and proxy rows keep inline binding and copy controls inside the viewport')
    assert.deepEqual(errors, [])
    assert.deepEqual(unexpected, [])
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify({ checks, writes, errors, unexpected }, null, 2),
    )
} finally {
    await browser.close()
}
