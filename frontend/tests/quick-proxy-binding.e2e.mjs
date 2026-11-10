import assert from 'node:assert/strict'
import { chromium } from 'playwright'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Synthetic account/proxy UI regression; every API and external request is intercepted.
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/quick-binding-review')
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38127'
const entryPath = process.env.FRONTEND_PREVIEW_PATH || '/login'
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
    google_cookies: JSON.stringify({SID:`plain-cookie-${row.id}`}),
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
    if (request.method() === 'POST' && url.pathname === '/api/tokens/submission-proxy/batch') {
        const { ids, submission_proxy_mode: mode } = request.postDataJSON()
        const results = ids.map((id) => {
            writes.push({ id, body: { submission_proxy_mode: mode } })
            if (id === 12 && rejectRetry)
                return { id, success: false, status: 'failed', message: '模拟保存失败，请重试' }
            const account = accounts.find((row) => row.id === id)
            const status = account.submission_proxy_mode === mode ? 'unchanged' : 'updated'
            account.submission_proxy_mode = mode
            return { id, success: true, status, submission_proxy_mode: mode }
        })
        return json(route, { success: true, results })
    }
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
    if (url.pathname === '/api/admin/network') return json(route, { mode: 'direct', reachable: true })
    if (url.pathname === '/api/tokens') return json(route, accounts)
    if (url.pathname === '/api/profile-transfer/jobs' || url.pathname === '/api/plugin/import/jobs') return json(route, { success: true, jobs: [] })
    if (url.pathname === '/api/proxy/pool')
        return json(route, {
            success: true,
            entries: proxies.map((row) => ({
                ...row,
                bound_account_count: accounts.filter((account) => account.proxy_pool_id === row.id).length,
            })),
        })
    if (url.pathname === '/api/token-refresh/config') return json(route, { config: { protocol_refresh_enabled: true } })
    if (url.pathname === '/api/admin/vids/accounts/detail')
        return json(route, { accounts: [{ token_id: 99, email: 'vids-only@example.test', enabled: true }] })
    if (url.pathname === '/api/admin/vids/onboard/candidates') return json(route, { accounts: [] })
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
const accountBindings = () => accounts.map(({ submission_proxy_mode, ...account }) => ({ ...account }))
try {
    await page.goto(`${base}${entryPath}#/proxies`, { waitUntil: 'domcontentloaded' })
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
    assert.match(await visible('.pool-table-dense').innerText(), /成功率/)
    assert.match(await visible('.pool-table-dense').innerText(), /80%/)
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
    await page.goto(`${base}${entryPath}#/accounts`, { waitUntil: 'domcontentloaded' })
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
    await quick.getByRole('tab', { name: '账号信息', exact: true }).click()
    assert.match(await quick.innerText(), /plain-cookie-11/)
    assert.equal(await quick.locator('.quick-account-section details').count(),0)
    await quick.screenshot({ path: resolve(output, 'account-binding-1600.png'), animations: 'disabled' })
    record('Account row binds a new proxy and copies decoded credentials without a drawer, dialog or route change')
    await quick.getByRole('tab', { name: '代理', exact: true }).click()
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
    const cloakRow = page.locator('.accounts-table .el-table__row').filter({ hasText: 'cloak@example.test' })
    await cloakRow.locator('.account-proxy-trigger').click()
    const cloakQuick = visible('.account-quick-panel[data-account-id="13"]')
    await cloakQuick.waitFor()
    const beforeIndividual = accountBindings()
    await cloakQuick.locator('.quick-submission-picker .el-select').click()
    await page.getByRole('option', { name: '代理池随机（每次生成取健康出口）', exact: true }).click()
    assert.match(await cloakQuick.locator('.quick-strategy-hint').innerText(), /登录、刷新与 Profile.*固定绑定/)
    await cloakQuick.getByRole('button', { name: '保存策略', exact: true }).click()
    await cloakQuick.getByRole('status').filter({ hasText: '生成出口已设置为代理池随机' }).waitFor()
    assert.deepEqual(writes.at(-1), { id: 13, body: { submission_proxy_mode: 'pool_random' } })
    assert.deepEqual(accountBindings(), beforeIndividual)
    assert.match(await cloakRow.locator('.account-proxy-trigger').innerText(), /生成 · 代理池随机 · 登录 #2/)
    assert.equal(await cloakQuick.getByRole('button', { name: '保存策略', exact: true }).isDisabled(), true)
    assert.equal(await confirmation.isVisible(), false)
    record('Cloak individual random strategy saves only generation egress, displays the actual strategy and retains binding, Cookie and Profile')
    const selectAccount = async (email) => page.locator('.accounts-table .el-table__row')
        .filter({ hasText: email }).locator('.el-checkbox').first().check()
    const showBulk = async () => {
        await page.getByRole('button', { name: '批量修改提交代理', exact: true }).click()
        const dialog = page.getByRole('dialog', { name: '批量修改提交代理', exact: true })
        await dialog.waitFor()
        return dialog
    }
    await selectAccount('free@example.test')
    await selectAccount('retry@example.test')
    await selectAccount('cloak@example.test')
    await selectAccount('vids-only@example.test')
    const bindings = accountBindings()
    let dialog = await showBulk()
    assert.match(await dialog.innerText(), /可设置 3 个[\s\S]*跳过 1 个/)
    const beforeBulk = writes.length
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    assert.equal(writes.length, beforeBulk)
    dialog = await showBulk()
    await dialog.getByRole('button', { name: '应用到 3 个账号', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    await page.getByRole('alert').filter({ hasText: '提交代理：已修改 2 个，已是目标策略 1 个，跳过 1 个，失败 0 个' }).waitFor()
    assert.equal(accounts[0].submission_proxy_mode, 'fixed')
    assert.equal(accounts[1].submission_proxy_mode, 'fixed')
    assert.equal(accounts[2].submission_proxy_mode, 'fixed')
    assert.deepEqual(writes.slice(beforeBulk), [
        { id: 11, body: { submission_proxy_mode: 'fixed' } },
        { id: 13, body: { submission_proxy_mode: 'fixed' } },
    ])
    assert.deepEqual(accountBindings(), bindings)
    record('Bulk fixed strategy includes Cloak, skips only Vids-only and unchanged accounts, and cancel writes nothing')
    rejectRetry = true
    dialog = await showBulk()
    await dialog.locator('.el-select').click()
    await page.getByRole('option', { name: '代理池随机（每次生成取健康出口）', exact: true }).click()
    assert.match(await dialog.innerText(), /可设置 3 个[\s\S]*跳过 1 个/)
    await dialog.screenshot({ path: resolve(output, 'bulk-submission-strategy-1600.png'), animations: 'disabled' })
    const beforeRandom = writes.length
    await dialog.getByRole('button', { name: '应用到 3 个账号', exact: true }).click()
    const failure = page.getByRole('dialog', { name: '部分账号操作未完成', exact: true })
    await failure.waitFor()
    assert.match(await failure.innerText(), /retry@example\.test: 模拟保存失败/)
    assert.equal(accounts[0].submission_proxy_mode, 'pool_random')
    assert.equal(accounts[1].submission_proxy_mode, 'fixed')
    assert.equal(accounts[2].submission_proxy_mode, 'pool_random')
    assert.deepEqual(writes.slice(beforeRandom), [
        { id: 11, body: { submission_proxy_mode: 'pool_random' } },
        { id: 12, body: { submission_proxy_mode: 'pool_random' } },
        { id: 13, body: { submission_proxy_mode: 'pool_random' } },
    ])
    assert.deepEqual(accountBindings(), bindings)
    assert.match(await quick.locator('.quick-submission-picker').innerText(), /代理池随机/)
    await failure.getByRole('button', { name: '知道了', exact: true }).click()
    await failure.waitFor({ state: 'hidden' })
    rejectRetry = false
    const beforeBulkRetry = writes.length
    dialog = await showBulk()
    await page.setViewportSize({ width: 390, height: 844 })
    await noOverflow()
    await dialog.screenshot({ path: resolve(output, 'bulk-submission-strategy-390.png'), animations: 'disabled' })
    await dialog.getByRole('button', { name: '应用到 3 个账号', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    await page.getByRole('alert').filter({ hasText: '提交代理：已修改 1 个，已是目标策略 2 个，跳过 1 个，失败 0 个' }).waitFor()
    assert.equal(writes.length, beforeBulkRetry + 1)
    assert.equal(accounts[1].submission_proxy_mode, 'pool_random')
    assert.deepEqual(writes.slice(beforeBulkRetry), [
        { id: 12, body: { submission_proxy_mode: 'pool_random' } },
    ])
    assert.deepEqual(accountBindings(), bindings)
    record('Bulk random strategy includes Cloak, skips only Vids-only, exposes per-account failure and retries only unsaved accounts without changing bindings, Cookie or Profile')
    for (const email of ['free@example.test', 'retry@example.test', 'cloak@example.test'])
        await page.locator('.accounts-table .el-table__row').filter({ hasText: email })
            .locator('.el-checkbox').first().uncheck()
    assert.equal(await page.getByRole('button', { name: '批量修改提交代理', exact: true }).count(), 0)
    assert.equal(writes.length, beforeBulkRetry + 1)
    record('Vids-only selection hides the Flow strategy action and cannot submit')
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
    await page.goto(`${base}${entryPath}#/proxies`, { waitUntil: 'domcontentloaded' })
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
