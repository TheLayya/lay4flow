import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Synthetic browser regression. Every mutation below is intercepted locally.
// It never changes the real proxy pool or service configuration.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38126'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/proxy-pool-review')
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
]
    .filter(Boolean)
    .find(existsSync)
assert.ok(executablePath, 'Install Chrome/Edge or set PLAYWRIGHT_BROWSER_PATH')
await mkdir(output, { recursive: true })

const rows = Array.from({ length: 31 }, (_, index) => ({
    id: index + 1,
    proxy_url: `socks5://fixture-${String(index + 1).padStart(2, '0')}.pool.test:${6000 + index}`,
    note: `出口 ${String(index + 1).padStart(2, '0')}`,
    enabled: index !== 4,
    bound_account_count: index === 1 ? 5 : 0,
    last_check_ok: index === 6 ? false : true,
    last_check_at: `2026-10-16T02:${String(index).padStart(2, '0')}:00Z`,
}))
rows[0].proxy_url = 'socks5h://old%40user:old%3Ap%2Fa%40ss@fixture-01.pool.test:6000'
const cloneRows = () => rows.map((row) => ({ ...row }))
const reads = {
    '/health': { status: 'ok', backend_running: true, has_active_tokens: true },
    '/api/stats': { total_tokens: 31, active_tokens: 30, today_images: 0, today_videos: 0, today_errors: 0 },
    '/api/admin/config': {
        admin_username: 'fixture-admin',
        api_key: 'fixture-key',
        error_ban_threshold: 5,
        debug_enabled: false,
    },
    '/api/config/generation': { config: { image_timeout: 300, video_timeout: 1500, max_retries: 3 } },
    '/api/call-logic/config': { config: { call_mode: 'default' } },
    '/api/token-refresh/config': { config: { protocol_refresh_enabled: true, refresh_interval_minutes: 120 } },
    '/api/proxy/config': {
        proxy_enabled: true,
        proxy_url: 'http://fixture.proxy.test:8000',
        media_proxy_enabled: false,
        media_proxy_url: '',
    },
    '/api/cache/config': { config: { enabled: true, timeout: 7200, base_url: '', effective_base_url: '' } },
    '/api/captcha/config': {
        captcha_method: 'remote_browser',
        remote_browser_base_url: 'https://captcha.example.test',
        remote_browser_api_key: 'synthetic-key',
        remote_browser_timeout: 60,
    },
}
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
})
context.setDefaultTimeout(15000)
const page = await context.newPage()
await page.addInitScript(() => {
    window.fixtureCopies = []
    Object.defineProperty(navigator, 'clipboard', {
        value: {
            writeText: async (value) => {
                window.fixtureCopies.push(value)
            },
        },
    })
})
const checks = [],
    errors = [],
    mutations = [],
    batchResponses = []
let releaseDeferredBatch
let holdNextBatch = false
let heldBatch
page.on('pageerror', (error) => errors.push(error.message))
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
const record = (value) => {
    checks.push(value)
    console.log(`PASS ${value}`)
}

await context.route('**/*', async (route) => {
    const request = route.request(),
        url = new URL(request.url()),
        method = request.method()
    if (url.origin !== base) return route.abort()
    if (url.pathname === '/api/proxy/pool' && method === 'GET')
        return json(route, { success: true, entries: cloneRows() })
    if (url.pathname === '/api/tokens' && method === 'GET')
        return json(route, [
            {
                id: 8,
                email: 'bound@example.test',
                proxy_pool_id: 2,
                proxy_url: rows[1].proxy_url,
                is_active: true,
                browser_profile_source: 'cloak',
            },
        ])
    if (url.pathname === '/api/proxy/pool/batch' && method === 'POST') {
        const body = request.postDataJSON() || {},
            ids = (body.ids || []).map(Number),
            action = body.action
        if (holdNextBatch) {
            holdNextBatch = false
            heldBatch = body
            await new Promise((resolve) => {
                releaseDeferredBatch = resolve
            })
        }
        const failed =
            action === 'check' ? ids.filter((id) => id === 7) : action === 'delete' ? ids.filter((id) => id === 11) : []
        if (action === 'enable' || action === 'disable')
            rows.forEach((row) => {
                if (ids.includes(row.id)) row.enabled = action === 'enable'
            })
        if (action === 'delete')
            ids.filter((id) => !failed.includes(id)).forEach((id) => {
                const index = rows.findIndex((row) => row.id === id)
                if (index >= 0) rows.splice(index, 1)
            })
        const results = ids.map((id) => ({
            id,
            success: !failed.includes(id),
            message: failed.includes(id) ? `出口 #${id} 模拟失败` : '完成',
            enabled: action === 'enable',
        }))
        const response = { success: failed.length === 0, results, succeeded: ids.length - failed.length }
        batchResponses.push({ body, response })
        return json(route, response, failed.length ? 207 : 200)
    }
    if (url.pathname.match(/^\/api\/proxy\/pool\/\d+$/) && method === 'PUT') {
        const id = Number(url.pathname.split('/').at(-1)),
            body = request.postDataJSON() || {}
        mutations.push({ method, path: url.pathname, body })
        if (!/^(https?|socks5|socks5h):\/\/[^\s]+$/i.test(String(body.proxy_url || '')))
            return json(route, { success: false, message: '代理地址格式不正确' }, 422)
        const row = rows.find((item) => item.id === id)
        if (row) Object.assign(row, body)
        return json(route, { success: true, entry: row })
    }
    if (url.pathname.match(/^\/api\/proxy\/pool\/\d+$/) && method === 'DELETE') {
        mutations.push({ method, path: url.pathname })
        return json(route, { success: true })
    }
    if (url.pathname.match(/^\/api\/proxy\/pool$/) && method === 'POST') {
        mutations.push({ method, path: url.pathname, body: request.postDataJSON() })
        return json(route, { success: true, id: 99 })
    }
    if (!['GET', 'HEAD'].includes(method)) {
        mutations.push({ method, path: url.pathname })
        return json(route, { detail: 'unexpected mutation' }, 503)
    }
    if (reads[url.pathname]) return json(route, reads[url.pathname])
    if (url.pathname.startsWith('/api/')) return json(route, { detail: `unexpected read ${url.pathname}` }, 404)
    return route.continue()
})

const panel = () => page.locator('.proxy-pool-panel')
const visibleRows = () =>
    page.locator('.pool-table-wrap:visible tbody tr.el-table__row, .pool-mobile-list:visible .pool-mobile-card')
const noOverflow = async () => {
    const value = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, viewport: innerWidth }))
    assert.ok(value.document <= value.viewport + 1, `horizontal overflow ${JSON.stringify(value)}`)
}
const equalHeight = async (selector, label) => {
    const values = await page
        .locator(selector)
        .evaluateAll((items) => items.map((item) => Math.round(item.getBoundingClientRect().height)))
    assert.ok(values.length > 1, `${label} needs multiple cards`)
    assert.ok(Math.max(...values) - Math.min(...values) <= 4, `${label} cards misaligned: ${values}`)
}

try {
    await page.goto(`${base}/login#/proxies`, { waitUntil: 'domcontentloaded' })
    await page.locator('.proxy-pool-view h1').waitFor()
    await page
        .locator('.settings-skeleton')
        .waitFor({ state: 'hidden' })
        .catch(() => {})
    await panel().scrollIntoViewIfNeeded()
    await panel().waitFor({ state: 'visible' })
    assert.match(await panel().innerText(), /31/)
    assert.equal(await visibleRows().count(), 20, 'default page size is twenty')
    record('31 proxies render in a bounded twenty-row page')

    await noOverflow()
    assert.equal(await page.locator('.main-nav').getByRole('link', { name: '代理池', exact: true }).count(), 1)
    assert.match(await panel().innerText(), /5/)
    await page.screenshot({
        path: resolve(output, 'proxies-1920-overview.png'),
        fullPage: true,
        animations: 'disabled',
    })
    record('Dedicated sidebar page exposes account binding counts and inline connection details')

    const search = panel().locator('input[placeholder="搜索地址或备注"]')
    await search.fill('出口 21')
    await page.waitForFunction(
        () =>
            [...document.querySelectorAll('.pool-table-wrap tbody tr, .pool-mobile-list .pool-mobile-card')].filter(
                (item) => item.getClientRects().length,
            ).length === 1,
    )
    assert.match(await visibleRows().innerText(), /出口 21/)
    await search.fill('')
    const selects = panel().locator('.pool-toolbar .el-select')
    assert.equal(await selects.count(), 2, 'enabled and health filters are both visible')
    await selects.nth(0).click()
    await page.getByRole('option', { name: '已停用' }).click()
    assert.equal(await visibleRows().count(), 1)
    await selects.nth(0).click()
    await page.getByRole('option', { name: '全部状态' }).click()
    await selects.nth(1).click()
    await page.getByRole('option', { name: '连接异常' }).click()
    assert.equal(await visibleRows().count(), 1)
    await selects.nth(1).click()
    await page.getByRole('option', { name: '全部健康' }).click()
    record('Search and enabled/health filters isolate matching pool rows')

    const pageSize = panel().locator('.pool-pagination .el-select').first()
    await pageSize.click()
    await page.getByRole('option', { name: /^10/ }).last().click()
    assert.equal(await visibleRows().count(), 10)
    const first = panel()
        .locator('label:visible')
        .filter({ has: page.getByRole('checkbox', { name: '选择出口 1', exact: true }) })
    await first.click()
    await panel()
        .getByRole('button', { name: /下一页/ })
        .click()
    const second = panel()
        .locator('label:visible')
        .filter({ has: page.getByRole('checkbox', { name: '选择出口 11', exact: true }) })
    await second.click()
    assert.match(await panel().innerText(), /已选 2 个/)
    record('Selection survives moving between pages')

    await pageSize.click()
    await page.getByRole('option', { name: /^50/ }).last().click()
    assert.equal(await visibleRows().count(), 31)
    record('Page sizes 10, 20 and 50 expose all entries while retaining a fixed table viewport')
    const row1 = panel().getByRole('checkbox', { name: '选择出口 1', exact: true }).locator('xpath=ancestor::tr')
    await row1.getByRole('button', { name: '展开', exact: true }).click()
    const dialog = page.locator('.proxy-inline-editor:visible').last()
    await dialog.waitFor()
    assert.equal(
        await dialog.locator('.inline-fields').count(),
        0,
        'View mode displays plain information without disabled inputs',
    )
    assert.match(await dialog.locator('.connection-values').innerText(), /SOCKS5H/)
    assert.match(await dialog.locator('.connection-values').innerText(), /fixture-01\.pool\.test:6000/)
    assert.match(await dialog.locator('.connection-values').innerText(), /old@user/)
    assert.match(await dialog.locator('.connection-values').innerText(), /old:p\/a@ss/)
    await dialog.getByRole('button', { name: '复制地址', exact: true }).click()
    const valueField = (label) =>
        dialog.locator('.connection-values > div').filter({ has: page.locator('dt').getByText(label, { exact: true }) })
    await valueField('用户名').getByRole('button', { name: '复制', exact: true }).click()
    await valueField('密码').getByRole('button', { name: '复制', exact: true }).click()
    assert.deepEqual(await page.evaluate(() => window.fixtureCopies), [rows[0].proxy_url, 'old@user', 'old:p/a@ss'])
    await dialog.getByRole('button', { name: '编辑连接', exact: true }).click()
    const editorField = (label) =>
        dialog.locator('.el-form-item').filter({
            has: page.locator('.el-form-item__label').filter({ hasText: new RegExp(`^${label}$`) }),
        })
    const hostInput = editorField('IP / 主机').locator('input'),
        portInput = editorField('端口').locator('input'),
        usernameInput = editorField('用户名').locator('input'),
        passwordInput = editorField('密码').locator('input'),
        noteInput = editorField('备注').locator('input'),
        protocolSelect = editorField('协议').locator('.el-select')
    assert.equal(await hostInput.inputValue(), 'fixture-01.pool.test')
    assert.equal(await portInput.inputValue(), '6000')
    assert.equal(await usernameInput.inputValue(), 'old@user')
    assert.equal(await passwordInput.inputValue(), 'old:p/a@ss')
    assert.equal(await passwordInput.getAttribute('type'), 'text')
    assert.equal(await page.locator('.proxy-editor-dialog:visible').count(), 0)
    assert.match(await protocolSelect.innerText(), /SOCKS5H/i)
    assert.equal(await dialog.locator('input[placeholder*="socks5"]').count(), 0)
    await page.screenshot({ path: resolve(output, 'proxy-edit-fields.png'), animations: 'disabled' })
    record('Proxy editor exposes protocol, host, port, decoded username/password and note as separate fields')
    await hostInput.fill('bad host/path')
    await dialog.getByRole('button', { name: '保存修改' }).click()
    assert.ok(await dialog.isVisible())
    assert.equal(
        mutations.filter((item) => item.method === 'PUT').length,
        0,
        'Invalid proxy host must be rejected before sending a write',
    )
    await hostInput.fill('203.0.113.42')
    await portInput.fill('7123')
    await portInput.blur()
    await usernameInput.fill('fresh@user')
    await passwordInput.fill('new:p/a@ss')
    await protocolSelect.click()
    await page.getByRole('option', { name: 'HTTP', exact: true }).click()
    await noteInput.fill('编辑后的出口')
    await dialog.getByRole('button', { name: '保存修改' }).click()
    await dialog.getByRole('button', { name: '编辑连接', exact: true }).waitFor()
    assert.deepEqual(
        mutations.findLast((item) => item.method === 'PUT'),
        {
            method: 'PUT',
            path: '/api/proxy/pool/1',
            body: {
                proxy_url: 'http://fresh%40user:new%3Ap%2Fa%40ss@203.0.113.42:7123',
                note: '编辑后的出口',
            },
        },
    )
    await row1.getByRole('button', { name: '编辑' }).click()
    await dialog.waitFor()
    assert.equal(await hostInput.inputValue(), '203.0.113.42')
    assert.equal(await portInput.inputValue(), '7123')
    assert.equal(await usernameInput.inputValue(), 'fresh@user')
    assert.equal(await passwordInput.inputValue(), 'new:p/a@ss')
    assert.equal(await noteInput.inputValue(), '编辑后的出口')
    assert.match(await protocolSelect.innerText(), /HTTP/)
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    record('Proxy edit validates host and sends same-ID PUT with encoded credentials that round-trip correctly')

    await row1.getByRole('button', { name: '收起', exact: true }).click()
    const boundRow = panel().getByRole('checkbox', { name: '选择出口 2', exact: true }).locator('xpath=ancestor::tr')
    await boundRow.getByRole('button', { name: '展开', exact: true }).click()
    const boundDialog = page.locator('.proxy-inline-editor:visible').last()
    await boundDialog.getByText('bound@example.test', { exact: false }).waitFor()
    await boundDialog.locator('.bound-account-row summary').click()
    await boundDialog.locator('.bound-account-details').waitFor()
    assert.match(await boundDialog.locator('.bound-account-details').innerText(), /账号|ST|AT/)
    assert.equal(await page.locator('.proxy-editor-dialog:visible').count(), 0)
    await boundRow.getByRole('button', { name: '收起', exact: true }).click()
    record('Expanded bound proxy shows account identity inline; full URL and decoded credentials copy exactly')
    const header = panel()
        .locator('label:visible')
        .filter({ has: page.getByRole('checkbox', { name: '全选本页代理' }) })
    await header.click()
    await panel().getByRole('button', { name: '批量检测' }).click()
    await page.waitForTimeout(250)
    const latestCheck = batchResponses.findLast((item) => item.body.action === 'check')
    assert.ok(latestCheck)
    assert.ok(latestCheck.response.results.some((item) => item.id === 7 && !item.success))
    assert.match(await page.locator('.pool-batch-result').innerText(), /失败|模拟失败|出口 #7/)
    record('Bulk check keeps partial failure details visible')
    await panel().getByRole('button', { name: '批量停用' }).click()
    await page.waitForTimeout(150)
    assert.ok(rows.every((row) => !row.enabled))
    await panel().getByRole('button', { name: '批量启用' }).click()
    await page.waitForTimeout(150)
    assert.ok(rows.every((row) => row.enabled))
    record('Bulk enable and disable update all selected IDs')

    await panel().getByRole('button', { name: '清除选择' }).click()
    const selectRow = async (id) =>
        panel()
            .locator('label:visible')
            .filter({ has: page.getByRole('checkbox', { name: `选择出口 ${id}`, exact: true }) })
            .click()
    await selectRow(11)
    await selectRow(12)
    await panel().getByRole('button', { name: '批量删除' }).click()
    await page.getByRole('button', { name: '保留', exact: true }).click()
    assert.equal(
        batchResponses.filter((item) => item.body.action === 'delete').length,
        0,
        'Cancelling bulk delete sends no request',
    )
    record('Bulk deletion can be cancelled without sending a mutation')
    await panel().getByRole('button', { name: '批量删除' }).click()
    await page.getByRole('button', { name: '移除所选', exact: true }).click()
    await page.waitForTimeout(250)
    const latestDelete = batchResponses.findLast((item) => item.body.action === 'delete')
    assert.deepEqual(latestDelete?.body.ids, [11, 12])
    assert.ok(
        rows.some((row) => row.id === 11),
        'Failed deletion stays in the pool',
    )
    assert.ok(!rows.some((row) => row.id === 12), 'Successful deletion is removed')
    assert.match(await page.locator('.pool-batch-result').innerText(), /#11[\s\S]*模拟失败/)
    assert.match(
        await panel().innerText(),
        /已选 1 个/,
        'Only still-existing failed selection remains after partial deletion',
    )
    record('Bulk deletion requires confirmation, removes successes and retains failed row details')

    const beforeHeld = batchResponses.length
    holdNextBatch = true
    await panel().getByRole('button', { name: '批量启用' }).click()
    await page.waitForFunction(() => document.querySelector('.bulk-progress')?.textContent.includes('正在处理'))
    assert.ok(heldBatch)
    assert.equal(
        await panel().getByRole('button', { name: '批量启用' }).count(),
        0,
        'Bulk actions are replaced by progress while the request is pending',
    )
    assert.ok(
        await panel()
            .locator('.pool-selectionbar')
            .getByRole('button', { name: /选择全部/ })
            .isDisabled(),
    )
    assert.ok(await panel().locator('.pool-table-wrap').getByRole('button', { name: '编辑' }).first().isDisabled())
    assert.equal(batchResponses.length, beforeHeld, 'Only one held request is in flight')
    releaseDeferredBatch()
    await page.waitForFunction(() => !document.querySelector('.bulk-progress'))
    assert.equal(batchResponses.length, beforeHeld + 1)
    record('Pending bulk work locks repeat actions and row editing until the response finishes')

    await page
        .locator('.el-message:visible')
        .last()
        .waitFor({ state: 'hidden', timeout: 5000 })
        .catch(() => {})
    await page.setViewportSize({ width: 1920, height: 1080 })
    await page.evaluate(() => document.querySelector('.proxy-pool-view').scrollIntoView({ block: 'start' }))
    await page.screenshot({
        path: resolve(output, 'proxies-1920.png'),
        fullPage: false,
        animations: 'disabled',
    })

    await page.setViewportSize({ width: 768, height: 1024 })
    await panel().scrollIntoViewIfNeeded()
    await noOverflow()
    await page.screenshot({ path: resolve(output, 'proxies-768.png'), fullPage: false, animations: 'disabled' })
    record('Tablet proxy management has no horizontal overflow')
    await page.setViewportSize({ width: 390, height: 844 })
    await page.evaluate(() => document.querySelector('.proxy-pool-panel').scrollIntoView({ block: 'start' }))
    await noOverflow()
    assert.ok((await panel().locator('.pool-mobile-list:visible .pool-mobile-card').count()) > 0)
    assert.ok((await panel().locator('.pool-mobile-list:visible .row-actions button').count()) >= 3)
    const mobileList = await panel()
        .locator('.pool-mobile-list:visible')
        .evaluate((element) => ({ client: element.clientHeight, scroll: element.scrollHeight }))
    assert.ok(
        mobileList.client <= 365 && mobileList.scroll > mobileList.client,
        `Mobile list uses its own bounded scroll: ${JSON.stringify(mobileList)}`,
    )
    record('Mobile cards stay bounded with visible edit/check/delete actions')
    await page.screenshot({ path: resolve(output, 'proxies-390.png'), fullPage: false, animations: 'disabled' })
    await panel()
        .locator('.pool-mobile-list:visible .pool-mobile-card')
        .first()
        .getByRole('button', { name: '编辑' })
        .click()
    await dialog.waitFor()
    await noOverflow()
    const mobileEditorBounds = await dialog.boundingBox()
    assert.ok(mobileEditorBounds && mobileEditorBounds.x >= 0 && mobileEditorBounds.x + mobileEditorBounds.width <= 391)
    assert.equal(await hostInput.inputValue(), '203.0.113.42')
    assert.equal(await usernameInput.inputValue(), 'fresh@user')
    assert.equal(await passwordInput.getAttribute('type'), 'text')
    for (const input of [hostInput, portInput, usernameInput, passwordInput, noteInput])
        assert.ok(await input.isVisible())
    await page.screenshot({ path: resolve(output, 'proxy-edit-fields-390.png'), animations: 'disabled' })
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    record('Mobile inline editor keeps full credentials visible inside the viewport without navigation')
    assert.deepEqual(
        mutations.filter((item) => item.method !== 'PUT' || item.path !== '/api/proxy/pool/1'),
        [],
    )
    assert.deepEqual(errors, [])
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify({ base, checks, errors, mutations, batchResponses }, null, 2),
    )
    console.log(`Verified ${checks.length} proxy pool checks. Evidence: ${output}`)
} catch (error) {
    await page
        .screenshot({ path: resolve(output, 'failure.png'), fullPage: true, animations: 'disabled' })
        .catch(() => {})
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify({ base, checks, errors, mutations, batchResponses, failure: error.message }, null, 2),
    )
    throw error
} finally {
    await browser.close()
}
