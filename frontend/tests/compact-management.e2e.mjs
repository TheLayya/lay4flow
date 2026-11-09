import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { extname, resolve } from 'node:path'
import { createServer } from 'node:http'
import { chromium } from 'playwright'
import { spawn } from 'node:child_process'

// Full application shell with synthetic records. All APIs are intercepted;
// non-loopback requests are blocked. No account, profile or provider is used.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const root = resolve(frontend, '..')
const output = resolve(frontend, '../tmp/compact-management-ui')
await mkdir(output, { recursive: true })
const server = createServer(async (request, response) => {
    const path = new URL(request.url, 'http://127.0.0.1').pathname
    const target = path.startsWith('/static/app/assets/')
        ? resolve(root, '.' + decodeURIComponent(path))
        : resolve(root, 'static/app/index.html')
    if (
        !target.startsWith(resolve(root, 'static/app') + '/') &&
        !target.startsWith(resolve(root, 'static/app') + '\\')
    ) {
        response.writeHead(404).end()
        return
    }
    try {
        const mime =
            {
                '.html': 'text/html; charset=utf-8',
                '.js': 'text/javascript',
                '.css': 'text/css',
                '.svg': 'image/svg+xml',
            }[extname(target)] || 'application/octet-stream'
        response.writeHead(200, { 'Content-Type': mime })
        response.end(await readFile(target))
    } catch {
        response.writeHead(404).end()
    }
})
await new Promise((done) => server.listen(0, '127.0.0.1', done))
const base = `http://127.0.0.1:${server.address().port}`
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/chromium',
].find((path) => path && existsSync(path))
assert.ok(executablePath, 'Install Chromium or set PLAYWRIGHT_BROWSER_PATH')
const browserServer = await chromium.launchServer({ executablePath, headless: true })
const browser = await chromium.connect(browserServer.wsEndpoint())
const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
})
const page = await context.newPage()
page.setDefaultTimeout(15000)
const checks = [],
    errors = [],
    unexpected = [],
    reads = [],
    writes = [],
    measurements = []
page.on('pageerror', (error) => errors.push(error.message))
const account = (id) => ({
    id,
    email: `member-${String(id).padStart(2, '0')}@example.test`,
    name: `Member ${id}`,
    remark: `Synthetic account ${id}`,
    st: `profile-session:synthetic-${id}`,
    is_active: id !== 2,
    credits: id * 100,
    user_paygate_tier: ['PAYGATE_TIER_NOT_PAID', 'PAYGATE_TIER_ONE', 'PAYGATE_TIER_TWO', null,
        'PAYGATE_TIER_UNSUBSCRIBED_WITH_CREDITS', 'PAYGATE_TIER_ZERO'][(id - 1) % 6],
    image_enabled: true,
    video_enabled: id !== 3,
    image_count: 20 + id,
    video_count: 5 + id,
    today_error_count: id === 3 ? 2 : 0,
    at_expired: false,
    at_expires: '2026-12-09T03:14:00Z',
    browser_profile_source: 'cloak',
    browser_profile_status: 'ready',
    refresh_status: 'idle',
    protocol_mode: 'session',
    auto_refresh_enabled: true,
    refresh_interval_minutes: 30,
    last_refresh_at: '2026-11-09T01:00:00Z',
    next_refresh_at: '2026-11-09T01:30:00Z',
    current_project_id: `synthetic-project-${id}`,
    current_project_name: `Synthetic project ${id}`,
    created_at: '2026-11-01T01:00:00Z',
    last_used_at: '2026-11-09T01:00:00Z',
    proxy_pool_id: id,
    proxy_url: `socks5h://synthetic-${id}.example.test:${6000 + id}`,
    submission_proxy_mode: 'fixed',
})
const accounts = Array.from({ length: 31 }, (_, index) => account(index + 1))
const proxies = accounts.map((row) => ({
    id: row.id,
    proxy_url: row.proxy_url,
    note: `Synthetic proxy ${row.id}`,
    enabled: row.id !== 2,
    bound_account_count: 1,
    last_check_ok: row.id !== 3,
    last_check_at: '2026-11-09T01:00:00Z',
    circuit_state: row.id === 3 ? 'cooling' : 'ready',
    inflight: row.id === 1 ? 2 : 0,
    consecutive_failures: row.id === 3 ? 3 : 0,
    success_count: 19,
    failure_count: 1,
    success_rate: 0.95,
    cooldown_until: row.id === 3 ? '2026-11-09T02:00:00Z' : null,
}))
const logs = accounts.map((row, index) => ({
    id: 101 + index,
    token_id: row.id,
    token_email: row.email,
    operation: index % 2 ? 'generate_video' : 'generate_image',
    status_code: index === 1 ? 429 : index === 2 ? 102 : 200,
    status_text: index === 1 ? 'failed' : index === 2 ? 'started' : 'completed',
    error_summary: index === 1 ? 'Synthetic upstream quota failure' : '',
    duration: 10 + index,
    progress: index === 2 ? 30 : 100,
    created_at: '2026-11-09T01:00:00Z',
    attempt_count: index === 0 ? 3 : 1,
    retry_count: index === 0 ? 2 : 0,
    account_count: index === 0 ? 2 : 1,
}))
const history = {
    version: 1,
    total_attempts: 3,
    retry_count: 2,
    account_count: 2,
    limit: 3,
    accounts_limit: 3,
    attempts: [
        {
            attempt: 1,
            account_id: 1,
            account_email: accounts[0].email,
            status: 'failed',
            stage: 'captcha',
            started_at: '2026-11-09T01:00:00Z',
            duration_ms: 2000,
            error_code: 'SYNTHETIC_CAPTCHA',
            error: 'Synthetic captcha failure',
            next_action: 'retry_same_account',
        },
        {
            attempt: 2,
            account_id: 1,
            account_email: accounts[0].email,
            status: 'failed',
            stage: 'submit',
            started_at: '2026-11-09T01:00:02Z',
            duration_ms: 3000,
            error_code: 'SYNTHETIC_QUOTA',
            error: 'Synthetic quota failure',
            rule_keyword: 'SYNTHETIC_QUOTA',
            rule_action: 'LIMIT',
            rule_seconds: 3600,
            next_action: 'switch_account',
        },
        {
            attempt: 3,
            account_id: 2,
            account_email: accounts[1].email,
            status: 'success',
            stage: 'submit',
            started_at: '2026-11-09T01:00:05Z',
            duration_ms: 5000,
            next_action: 'completed',
        },
    ],
}
const json = (route, value, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
await context.route('**/*', async (route) => {
    const request = route.request(),
        url = new URL(request.url()),
        method = request.method()
    if (url.origin !== base) {
        unexpected.push(`external ${request.url()}`)
        return route.abort()
    }
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    if (method !== 'GET') {
        const body = request.postDataJSON()
        writes.push({ method, path: url.pathname, body })
        if (method === 'PUT' && /^\/api\/tokens\/\d+$/.test(url.pathname)) {
            Object.assign(
                accounts.find((row) => url.pathname === `/api/tokens/${row.id}`),
                body,
            )
            return json(route, { success: true })
        }
        if (method === 'PUT' && /^\/api\/proxy\/pool\/\d+$/.test(url.pathname)) {
            Object.assign(
                proxies.find((row) => url.pathname === `/api/proxy/pool/${row.id}`),
                body,
            )
            return json(route, { success: true })
        }
        unexpected.push(`${method} ${url.pathname}`)
        return json(route, { detail: 'Unexpected synthetic write' }, 503)
    }
    reads.push(url.pathname)
    const fixtures = {
        '/health': {
            status: 'ok',
            backend_running: true,
            has_active_tokens: true,
            remote_browser_configured: true,
            version: '1.3.0',
        },
        '/api/stats': { total_tokens: accounts.length, active_tokens: accounts.length - 1 },
        '/api/admin/network': { reachable: true },
        '/api/admin/release': { version: '1.3.0', release_date: '2026-11-09', entries: [] },
        '/api/tokens': accounts,
        '/api/admin/vids/accounts/detail': {
            accounts: accounts.map((row) => ({
                token_id: row.id,
                email: row.email,
                enabled: true,
                ready: true,
                identity_verified: true,
                session_state: 'ready',
                image_enabled: true,
                video_enabled: true,
                last_quota: {
                    image: { available: 19, total: 30, queried_at: '2026-11-09T01:00:00Z', stale: false },
                    video: { available: 427, total: 500, queried_at: '2026-11-09T01:00:00Z', stale: false },
                },
            })),
        },
        '/api/admin/vids/onboard/candidates': { accounts: [] },
        '/api/profile-transfer/jobs': { success: true, jobs: [] },
        '/api/plugin/import/jobs': { success: true, jobs: [] },
        '/api/proxy/pool': { success: true, entries: proxies },
        '/api/logs': logs,
    }
    if (Object.hasOwn(fixtures, url.pathname)) return json(route, fixtures[url.pathname])
    const log = logs.find((row) => url.pathname === `/api/logs/${row.id}`)
    if (log)
        return json(route, {
            ...log,
            request_body: '{}',
            response_body: '{}',
            ...(log.id === 101 ? { attempt_history: history } : {}),
        })
    unexpected.push(`${method} ${url.pathname}`)
    return json(route, { detail: 'Unexpected synthetic read' }, 503)
})
const record = (value) => {
    checks.push(value)
    console.log('PASS ' + value)
}
const rows = (table) => table.locator('.el-table__body-wrapper tbody tr.el-table__row')
async function visit(name, selector) {
    await page.goto(`${base}/__management_test__#/${name}`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    const table = page.locator(selector)
    await rows(table).first().waitFor()
    await page.evaluate(() => document.fonts.ready)
    await page.waitForTimeout(180)
    return table
}
async function geometry(name, table, width, height, dark = false) {
    await page.setViewportSize({ width, height })
    await page.evaluate((dark) => document.documentElement.classList.toggle('dark', dark), dark)
    await page.waitForTimeout(200)
    const value = await table.evaluate((element) => {
        const cells = [...element.querySelectorAll('.el-table__body-wrapper tbody tr.el-table__row')]
        const wrap = element.querySelector('.el-scrollbar__wrap')
        const rect = element.getBoundingClientRect()
        return {
            documentWidth: document.documentElement.scrollWidth,
            viewportWidth: innerWidth,
            tableVisible: !!element.getClientRects().length && getComputedStyle(element).display !== 'none',
            fixedPositions: [...element.querySelectorAll('td.el-table-fixed-column--right, td.el-table-fixed-column--left')].map((cell) => getComputedStyle(cell).position),
            tableTop: Math.round(rect.top),
            rowHeights: cells.map((row) => Math.round(row.getBoundingClientRect().height)),
            headers: [...element.querySelectorAll('thead th')].map((cell) => cell.innerText.trim()).filter(Boolean),
            scrollWidth: wrap?.scrollWidth,
            clientWidth: wrap?.clientWidth,
            dark: document.documentElement.classList.contains('dark'),
        }
    })
    measurements.push({ name, width, height, ...value })
    assert.ok(value.documentWidth <= width + 1, `${name} page horizontally overflows: ${JSON.stringify(value)}`)
    assert.ok(value.tableVisible, `${name} must retain a table at ${width}px`)
    assert.ok(value.rowHeights.length > 0, `${name} needs visible records`)
    assert.ok(Math.max(...value.rowHeights) <= 66, `${name} rows exceed compact density: ${value.rowHeights}`)
    assert.ok(
        value.tableTop < (width < 768 ? 650 : 450),
        `${name} records displaced below oversized content: ${value.tableTop}`,
    )
    if (width < 768) {
        assert.ok(value.fixedPositions.every((position) => position === 'static'), `${name} narrow fixed columns must scroll with the table`)
        assert.ok(value.scrollWidth > value.clientWidth + 30, `${name} narrow table must scroll horizontally`)
        const wrap = table.locator('.el-scrollbar__wrap').first()
        await wrap.evaluate((element) => {
            element.scrollLeft = element.scrollWidth
        })
        assert.ok(
            await wrap.evaluate((element) => element.scrollLeft > 0),
            `${name} horizontal table scroll remains usable`,
        )
    }
    await page.screenshot({
        path: resolve(output, `${name}-${width}${dark ? '-dark' : ''}.png`),
        fullPage: true,
        animations: 'disabled',
    })
}
async function accountViewport(table, height) {
    await page.setViewportSize({ width: 1440, height })
    await page.waitForTimeout(150)
    const value = await table.evaluate((element) => {
        const surface = element.closest('.account-surface')
        const footer = surface.querySelector('.table-footer').getBoundingClientRect()
        const main = document.querySelector('.main-content').getBoundingClientRect()
        const body = element.querySelector('.el-table__body-wrapper').getBoundingClientRect()
        const visibleRows = [...element.querySelectorAll('tbody tr.el-table__row')].filter((row) => {
            const rect = row.getBoundingClientRect()
            return rect.top >= body.top && rect.bottom <= body.bottom + 1
        }).length
        return { height: innerHeight, tableHeight: element.getBoundingClientRect().height, footerBottom: footer.bottom,
            mainBottom: main.bottom, visibleRows, documentHeight: document.documentElement.scrollHeight }
    })
    measurements.push({ name: 'account-viewport', ...value })
    assert.ok(value.mainBottom - value.footerBottom <= 25, `Account pagination leaves unused space: ${JSON.stringify(value)}`)
    assert.ok(value.footerBottom <= height, 'Account pagination must stay visible')
    assert.ok(value.documentHeight <= height + 1, 'Desktop account rows must scroll inside the table')
    await page.screenshot({ path: resolve(output, `accounts-height-${height}.png`), fullPage: true })
    return value
}
try {
    const accountsTable = await visit('accounts', '.accounts-table')
    assert.equal(await rows(accountsTable).count(), 31)
    assert.equal(await accountsTable.getByRole('columnheader', { name: '套餐', exact: true }).count(), 1)
    for (const [index, label] of ['Free', 'Pro', 'Ultra', '未知', '未订阅（有积分）', '其他等级'].entries())
        assert.equal(await rows(accountsTable).nth(index).locator('.account-plan-tag').innerText(), label)
    await page.getByRole('combobox', { name: '筛选套餐', exact: true }).locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]').click()
    await page.getByRole('option', { name: 'Pro', exact: true }).click()
    assert.equal(await rows(accountsTable).count(), 5)
    assert.ok((await rows(accountsTable).locator('.account-plan-tag').allInnerTexts()).every((value) => value === 'Pro'))
    await page.getByRole('combobox', { name: '筛选套餐', exact: true }).locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]').click()
    await page.getByRole('option', { name: '全部套餐', exact: true }).click()
    record('Account plan column shows Free, Pro, Ultra and honest unknown/special states; plan filtering uses verified tier fields')
    const short = await accountViewport(accountsTable, 700)
    const tall = await accountViewport(accountsTable, 1500)
    assert.ok(tall.tableHeight > short.tableHeight + 700, 'Tall viewports must allocate their extra height to accounts')
    assert.ok(tall.visibleRows > short.visibleRows + 12, 'Tall viewports must show more actual accounts')
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.locator('.account-surface .el-pagination__sizes').click()
    await page.getByRole('option', { name: '20条/页', exact: true }).click()
    await geometry('accounts', accountsTable, 1440, 1000)
    const search = page.getByPlaceholder('搜索邮箱、备注或账号 ID')
    await search.fill(accounts[20].email)
    assert.equal(await rows(accountsTable).count(), 1)
    await search.fill('')
    await page
        .getByRole('combobox', { name: '筛选状态', exact: true })
        .locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]')
        .click()
    await page.getByRole('option', { name: '已暂停', exact: true }).click()
    assert.equal(await rows(accountsTable).count(), 1)
    await page
        .getByRole('combobox', { name: '筛选状态', exact: true })
        .locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]')
        .click()
    await page.getByRole('option', { name: '所有状态', exact: true }).click()
    const firstAccount = rows(accountsTable).filter({ hasText: accounts[0].email })
    await firstAccount.locator('td.el-table-column--selection .el-checkbox').click()
    await page.getByRole('button', { name: /下一页/ }).click()
    assert.match(await page.locator('.selection-bar').innerText(), /1/)
    await page.getByRole('button', { name: /上一页/ }).click()
    assert.ok(await firstAccount.locator('td.el-table-column--selection input').isChecked())
    const remark = firstAccount.getByRole('textbox', { name: `${accounts[0].email} 备注`, exact: true })
    const remarkSaved = page.waitForResponse(
        (response) => response.url() === `${base}/api/tokens/1` && response.request().method() === 'PUT',
    )
    await remark.fill('Synthetic inline update')
    await remark.press('Enter')
    await remarkSaved
    assert.equal(accounts[0].remark, 'Synthetic inline update')
    const identity = firstAccount.locator('.account-identity')
    await identity.focus()
    await page.keyboard.press('Enter')
    const accountDrawer = page.locator('.account-drawer:visible')
    await accountDrawer.waitFor()
    assert.match(await accountDrawer.innerText(), /member-01@example.test/)
    await page.keyboard.press('Escape')
    await accountDrawer.waitFor({ state: 'hidden' })
    record(
        'Account search, state filter, pagination, cross-page selection, inline remark edit and keyboard detail access remain usable',
    )
    await geometry('accounts', accountsTable, 390, 844)
    await geometry('accounts', accountsTable, 1440, 1000, true)

    const proxyTable = await visit('proxies', '.pool-table-dense')
    assert.equal(await rows(proxyTable).count(), 20)
    await geometry('proxies', proxyTable, 1440, 1000)
    const proxySearch = page.getByPlaceholder('搜索地址或备注')
    await proxySearch.fill('Synthetic proxy 21')
    assert.equal(await rows(proxyTable).count(), 1)
    await proxySearch.fill('')
    await page
        .getByRole('combobox', { name: '健康状态', exact: true })
        .locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]')
        .click()
    await page.getByRole('option', { name: '连接异常', exact: true }).click()
    assert.equal(await rows(proxyTable).count(), 1)
    await page
        .getByRole('combobox', { name: '健康状态', exact: true })
        .locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]')
        .click()
    await page.getByRole('option', { name: '全部健康', exact: true }).click()
    const firstProxy = rows(proxyTable).filter({ hasText: 'Synthetic proxy 1' }).first()
    await page.getByRole('checkbox', { name: '选择出口 1', exact: true }).locator('xpath=ancestor::label').click()
    await page.getByRole('button', { name: /下一页/ }).click()
    assert.match(await page.locator('.pool-selectionbar').innerText(), /已选 1/)
    await page.getByRole('button', { name: /上一页/ }).click()
    const expand = firstProxy.getByRole('button', { name: '展开', exact: true })
    await expand.focus()
    await page.keyboard.press('Enter')
    await page.locator('.proxy-inline-editor:visible').waitFor()
    await firstProxy.getByRole('button', { name: '收起', exact: true }).click()
    record('Proxy search, health filter, pagination, cross-page selection and keyboard inline details remain usable')
    await geometry('proxies', proxyTable, 390, 844)
    await geometry('proxies', proxyTable, 1440, 1000, true)

    const logsTable = await visit('logs', '.logs-table')
    assert.equal(await rows(logsTable).count(), 20)
    await geometry('logs', logsTable, 1440, 1000)
    const logSearch = page.getByPlaceholder('搜索账号、请求 ID 或错误内容')
    await logSearch.fill(accounts[20].email)
    assert.equal(await rows(logsTable).count(), 1)
    await logSearch.fill('')
    await page
        .getByRole('combobox', { name: '筛选请求状态', exact: true })
        .locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]')
        .click()
    await page.getByRole('option', { name: '生成失败', exact: true }).click()
    assert.equal(await rows(logsTable).count(), 1)
    await page
        .getByRole('combobox', { name: '筛选请求状态', exact: true })
        .locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]')
        .click()
    await page.getByRole('option', { name: '全部状态', exact: true }).click()
    await page.getByRole('button', { name: /下一页/ }).click()
    assert.equal(await rows(logsTable).count(), 11)
    await page.getByRole('button', { name: /上一页/ }).click()
    const request = logsTable.getByRole('button', { name: 'REQUEST #101', exact: true })
    await request.focus()
    await page.keyboard.press('Enter')
    const logDrawer = page.locator('.el-drawer:visible')
    await logDrawer.locator('.attempt-item').nth(2).waitFor()
    assert.equal(await logDrawer.locator('.attempt-item').count(), 3)
    assert.match(await logDrawer.innerText(), /SYNTHETIC_QUOTA/)
    assert.match(await logDrawer.innerText(), /切换账号/)
    assert.match(await logDrawer.innerText(), /member-02@example.test/)
    await page.screenshot({ path: resolve(output, 'logs-attempt-history.png'), fullPage: true })
    await page.keyboard.press('Escape')
    await logDrawer.waitFor({ state: 'hidden' })
    record('Log search, status filter, pagination and keyboard request details retain all retry history')
    await geometry('logs', logsTable, 390, 844)
    await geometry('logs', logsTable, 1440, 1000, true)
    record(
        'All three management pages retain compact desktop rows, narrow horizontally scrollable tables and dark mode',
    )
    assert.deepEqual(errors, [])
    assert.deepEqual(unexpected, [])
    assert.deepEqual(
        writes,
        [{ method: 'PUT', path: '/api/tokens/1', body: { remark: 'Synthetic inline update' } }],
        'Only the intercepted synthetic remark is changed',
    )
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify({ checks, measurements, reads, writes, errors, unexpected }, null, 2),
    )
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true }).catch(() => {})
    await writeFile(
        resolve(output, 'failure.json'),
        JSON.stringify({ message: error.message, checks, measurements, reads, writes, errors, unexpected }, null, 2),
    )
    throw error
} finally {
    await context.close()
    // Some local Chrome builds leave the graceful Playwright close pending.
    // Bound cleanup and terminate only this test's own browser process tree.
    let cleanupTimer
    const gracefulClose = (async () => {
        await browser.close()
        await browserServer.close()
    })().catch(() => {})
    const forceClose = new Promise((done) => {
        cleanupTimer = setTimeout(() => {
            const pid = browserServer.process().pid
            if (process.platform === 'win32') {
                const cleanup = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], {
                    windowsHide: true,
                    stdio: 'ignore',
                })
                cleanup.once('exit', done)
                cleanup.once('error', done)
            } else {
                browserServer.process().kill('SIGKILL')
                done()
            }
        }, 5000)
    })
    await Promise.race([gracefulClose, forceClose])
    clearTimeout(cleanupTimer)
    server.closeAllConnections()
    await new Promise((done) => server.close(done))
}
