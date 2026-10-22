import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Every API request is intercepted. This test only reads the served frontend;
// profiles, accounts, upstream quotas and generation providers are never touched.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38126'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, '../../output/accounts-glance')
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
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
})
context.setDefaultTimeout(12000)
const page = await context.newPage()
const checks = [],
    errors = [],
    blockedMutations = [],
    apiReads = []
let tokenReadVersion = 0
page.on('pageerror', (error) => errors.push(error.message))
const record = (label) => {
    checks.push(label)
    console.log(`PASS ${label}`)
}
const quotaTime = '2026-10-16T02:12:00Z'
const expires = '2026-10-17T03:14:00Z'
const quota = (available, total, extra = {}) => ({
    available,
    total,
    queried_at: quotaTime,
    stale: false,
    error: null,
    ...extra,
})
const token = (id, email, extra = {}) => ({
    id,
    email,
    name: email.split('@')[0],
    remark: '只读浏览器回归数据',
    is_active: true,
    credits: 1200,
    image_enabled: true,
    video_enabled: true,
    image_count: 28,
    video_count: 7,
    today_error_count: 0,
    at_expired: false,
    at_expires: expires,
    browser_profile_source: 'cloak',
    browser_profile_status: 'ready',
    refresh_status: 'idle',
    protocol_mode: 'session',
    auto_refresh_enabled: true,
    refresh_interval_minutes: 120,
    last_refresh_at: quotaTime,
    next_refresh_at: '2026-10-16T04:12:00Z',
    current_project_id: `fixture-project-${id}`,
    current_project_name: `项目 ${id}`,
    created_at: '2026-10-14T02:00:00Z',
    last_used_at: quotaTime,
    ...extra,
})
const vidsAccount = (token_id, email, extra = {}) => ({
    token_id,
    email,
    enabled: true,
    image_enabled: true,
    video_enabled: true,
    ready: true,
    session_state: 'ready',
    identity_verified: true,
    vids_identity: email,
    last_quota: { image: quota(19, 30), video: quota(427, 500) },
    ...extra,
})
const accounts = [
    token(41, 'healthy@example.test'),
    token(42, 'attention@example.test', {
        at_expired: true,
        refresh_status: 'login_required',
        refresh_error: 'Flow 会话待重新登录',
    }),
    token(43, 'paused@example.test'),
    token(44, 'notjoined@example.test'),
    token(46, 'zero@example.test', { credits: 0 }),
    token(47, 'unknown@example.test', { credits: null }),
]
const vidsAccounts = [
    vidsAccount(41, 'healthy@example.test'),
    vidsAccount(42, 'attention@example.test', {
        ready: false,
        identity_verified: false,
        session_state: 'identity_mismatch',
        runtime_error: 'Vids 身份与账号不一致，请重新连接',
        last_quota: {
            image: quota(12, 30, { stale: true, error: 'session_identity_mismatch' }),
            video: quota(321, 500, { stale: true, error: 'session_identity_mismatch' }),
        },
    }),
    vidsAccount(43, 'paused@example.test', { enabled: false }),
    vidsAccount(45, 'vidsonly@example.test'),
    vidsAccount(46, 'zero@example.test', { last_quota: { image: quota(0, 30), video: quota(0, 500) } }),
    vidsAccount(47, 'unknown@example.test', { last_quota: {} }),
]
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', (route) => {
    const request = route.request(),
        url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!['GET', 'HEAD'].includes(request.method())) {
        blockedMutations.push(`${request.method()} ${url.pathname}`)
        return json(route, { detail: 'Account glance test blocks all writes' }, 503)
    }
    if (!url.pathname.startsWith('/api/')) return route.continue()
    apiReads.push(url.pathname + url.search)
    if (url.pathname === '/api/stats')
        return json(route, { total_tokens: 6, active_tokens: 6, today_images: 0, today_videos: 0, today_errors: 0 })
    if (url.pathname === '/api/tokens') {
        tokenReadVersion++
        return json(route, accounts.map((row) => ({ ...row, remark: `只读刷新 ${tokenReadVersion}` })))
    }
    if (url.pathname === '/api/admin/vids/accounts/detail') return json(route, { accounts: vidsAccounts })
    if (url.pathname === '/api/admin/vids/onboard/candidates') return json(route, { accounts: [] })
    if (url.pathname === '/api/token-refresh/config')
        return json(route, { config: { protocol_refresh_enabled: false } })
    return json(route, { detail: `Unexpected API read ${url.pathname}` }, 404)
})
const drawer = page.locator('.account-drawer:visible')
async function close() {
    if (!(await drawer.count())) return
    await drawer.locator('.el-drawer__close-btn').click()
    await drawer.waitFor({ state: 'hidden' })
}
async function waitText(locator, text) {
    await locator.filter({ hasText: text }).first().waitFor({ state: 'visible' })
}
async function open(email, mobile = false) {
    await close()
    const area = page.locator(mobile ? '.mobile-accounts' : '.accounts-table')
    await area.locator('.account-identity').filter({ hasText: email }).click()
    await drawer.waitFor({ state: 'visible' })
    await waitText(drawer, email)
    await drawer.locator('.el-drawer__body').evaluate((element) => {
        element.scrollTop = 0
    })
    assert.equal(await drawer.getByRole('tab').count(), 0, 'Account overview must not hide data behind tabs')
    await drawer.locator('.account-balances').waitFor()
}
async function noOverflow() {
    const bounds = await page.evaluate(() => {
        const body = document.querySelector('.account-drawer .el-drawer__body')
        return {
            viewport: innerWidth,
            document: document.documentElement.scrollWidth,
            body: body?.clientWidth,
            content: body?.scrollWidth,
        }
    })
    assert.ok(bounds.document <= bounds.viewport + 1, `Document overflows: ${JSON.stringify(bounds)}`)
    if (bounds.body) assert.ok(bounds.content <= bounds.body + 1, `Drawer overflows: ${JSON.stringify(bounds)}`)
}
async function inFirstScreen(selector) {
    const bounds = await drawer.locator(selector).evaluate((element) => {
        const r = element.getBoundingClientRect(),
            body = element.closest('.el-drawer__body').getBoundingClientRect()
        return { top: r.top, bottom: r.bottom, bodyTop: body.top, bodyBottom: body.bottom, viewport: innerHeight }
    })
    assert.ok(
        bounds.top >= bounds.bodyTop - 1 && bounds.bottom <= Math.min(bounds.bodyBottom, bounds.viewport) + 1,
        `${selector} must be readable in the first desktop screen: ${JSON.stringify(bounds)}`,
    )
}
async function screenshot(name) {
    await noOverflow()
    await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: false, animations: 'disabled' })
}
try {
    await page.goto(`${base}/login#/accounts`, { waitUntil: 'domcontentloaded' })
    await page.locator('.accounts-view h1').waitFor()
    await waitText(page.locator('.accounts-table'), 'healthy@example.test')
    await page.evaluate(() => document.fonts.ready)
    const healthyRow = page
        .locator('.accounts-table .el-table__body-wrapper tbody tr')
        .filter({ hasText: 'healthy@example.test' })
        .first()
    await healthyRow.locator('.account-list-health').waitFor()
    assert.match(
        await healthyRow.locator('.account-list-health').innerText(),
        /Flow[\s\S]*会话正常[\s\S]*Vids[\s\S]*会话就绪/,
    )
    record('Desktop account list displays Flow and Vids health without opening details')

    // A background poll replaces the table data with fresh row objects.  The
    // current selection must survive that replacement, including Vids-only
    // rows which are merged into the account list on the client.
    const selectRow = async (email) => {
        const row = page.locator('.accounts-table .el-table__body-wrapper tbody tr').filter({ hasText: email }).first()
        await row.locator('td.el-table-column--selection .el-checkbox').click()
    }
    await selectRow('healthy@example.test')
    await selectRow('vidsonly@example.test')
    assert.match(await page.locator('.selection-bar').innerText(), /已选择\s*2\s*位成员/)
    const refreshedAccounts = page.waitForResponse(
        (response) => response.url() === `${base}/api/tokens` && response.request().method() === 'GET',
    )
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    await refreshedAccounts
    await healthyRow.locator('.account-identity').filter({ hasText: `只读刷新 ${tokenReadVersion}` }).waitFor()
    await page.locator('.selection-bar').filter({ hasText: '已选择' }).waitFor()
    assert.match(
        await page.locator('.selection-bar').innerText(),
        /已选择\s*2\s*位成员/,
        'Automatic account refresh must preserve selected Flow and Vids-only rows',
    )
    for (const email of ['healthy@example.test', 'vidsonly@example.test']) {
        const row = page.locator('.accounts-table .el-table__body-wrapper tbody tr').filter({ hasText: email }).first()
        assert.ok(await row.locator('td.el-table-column--selection input[type="checkbox"]').isChecked())
    }
    record('Automatic account refresh preserves selected Flow and Vids-only rows')

    const nextRead = tokenReadVersion + 1
    await page.locator('.page-heading').getByRole('button', { name: '刷新', exact: true }).click()
    await healthyRow.locator('.account-identity').filter({ hasText: `只读刷新 ${nextRead}` }).waitFor()
    assert.match(await page.locator('.selection-bar').innerText(), /已选择\s*2\s*位成员/)
    const accountSearch = page.locator('.account-toolbar input').first()
    await accountSearch.fill('healthy@example.test')
    await page
        .locator('.accounts-table .el-table__body-wrapper tbody tr')
        .filter({ hasText: 'healthy@example.test' })
        .waitFor()
    assert.match(await page.locator('.selection-bar').innerText(), /已选择\s*2\s*位成员/)
    await accountSearch.fill('')
    const selectedVids = page
        .locator('.accounts-table .el-table__body-wrapper tbody tr')
        .filter({ hasText: 'vidsonly@example.test' })
    await selectedVids.waitFor()
    assert.ok(await selectedVids.locator('td.el-table-column--selection input[type="checkbox"]').isChecked())
    record('Manual refresh and search filtering preserve the same selected account IDs')

    await open('healthy@example.test')
    const balances = drawer.locator('.account-balances')
    assert.match(
        await balances.innerText(),
        /Flow[\s\S]*1,200[\s\S]*Vids[\s\S]*19[\s\S]*30[\s\S]*Vids[\s\S]*427[\s\S]*500/,
    )
    const sessions = drawer.locator('.account-session-grid')
    await sessions.locator('.flow-session-summary').waitFor()
    await sessions.locator('.vids-session-summary').waitFor()
    assert.match(await sessions.innerText(), /会话正常/)
    assert.match(await sessions.innerText(), /会话就绪/)
    assert.match(await sessions.innerText(), /身份已验证/)
    assert.match(await sessions.innerText(), /2026[\s\S]*10[\s\S]*17/)
    for (const selector of ['.account-balances', '.account-session-grid']) await inFirstScreen(selector)
    for (const label of ['刷新会话', '查看浏览器', '重新连接']) {
        const button = drawer.getByRole('button', { name: label, exact: true })
        assert.ok(await button.isVisible(), `${label} must be directly available`)
        const bounds = await button.boundingBox()
        assert.ok(bounds && bounds.y + bounds.height <= 900, `${label} must be in the first desktop screen`)
    }
    await screenshot('healthy-desktop')
    record('One click opens both balances, sessions, identity, expiry and direct actions in the first desktop screen')

    await open('attention@example.test')
    assert.match(await drawer.locator('.flow-session-summary').innerText(), /需要重新登录|已过期|待重新登录/)
    assert.match(await drawer.locator('.vids-session-summary').innerText(), /身份待核对|身份.*不一致/)
    assert.match(await drawer.innerText(), /Vids 身份与账号不一致/)
    assert.match(await drawer.locator('.account-balances').innerText(), /历史|待更新|失败/)
    await screenshot('attention-desktop')
    await close()
    const attentionRow = page
        .locator('.accounts-table .el-table__body-wrapper tbody tr')
        .filter({ hasText: 'attention@example.test' })
        .first()
    assert.match(await attentionRow.locator('.account-list-health').innerText(), /需要重新登录|已过期|待重新登录/)
    assert.match(await attentionRow.locator('.account-list-health').innerText(), /身份待核对|身份.*不一致/)
    record('Flow expiry and Vids identity failures are visible in list and overview without switching tabs')

    await open('paused@example.test')
    assert.match(await drawer.locator('.vids-session-summary').innerText(), /暂停/)
    assert.match(await drawer.locator('.account-balances').innerText(), /暂停|历史/)
    record('Paused Vids account is not presented as an active ready session')

    await open('notjoined@example.test')
    assert.match(await drawer.innerText(), /未接入|尚未接入|还没.*Vids/)
    assert.ok(await drawer.getByRole('button', { name: /接入此账号|接入 Vids|连接 Vids/ }).isVisible())
    record('Account without Vids directly exposes connection state and onboarding action')

    await open('vidsonly@example.test')
    assert.match(await drawer.locator('.vids-session-summary').innerText(), /会话就绪/)
    const flowSummary = drawer.locator('.flow-session-summary')
    if (await flowSummary.count()) assert.doesNotMatch(await flowSummary.innerText(), /会话正常/)
    assert.equal(await drawer.getByRole('button', { name: '刷新会话', exact: true }).count(), 0)
    record('Vids-only account does not invent a healthy Flow session or offer Flow refresh')

    await open('zero@example.test')
    const zero = await drawer.locator('.account-balances').innerText()
    assert.match(zero, /Flow[\s\S]*\b0\b[\s\S]*Vids[\s\S]*\b0\b[\s\S]*30[\s\S]*Vids[\s\S]*\b0\b[\s\S]*500/)
    record('Zero balances remain explicit zero values')

    await open('unknown@example.test')
    const unknown = await drawer.locator('.account-balances').innerText()
    assert.doesNotMatch(unknown, /NaN|undefined|null/)
    assert.match(unknown, /—|暂无|待查询/)
    await screenshot('unknown-desktop')
    record('Unknown balances remain visibly unknown instead of turning into zero')

    await close()
    await page.setViewportSize({ width: 390, height: 844 })
    const mobile = page.locator('.mobile-account').filter({ hasText: 'healthy@example.test' })
    assert.ok(await mobile.getByRole('checkbox', { name: '选择 healthy@example.test', exact: true }).isChecked())
    const mobileVids = page.locator('.mobile-account').filter({ hasText: 'vidsonly@example.test' })
    assert.ok(await mobileVids.getByRole('checkbox', { name: '选择 vidsonly@example.test', exact: true }).isChecked())
    record('Desktop selection remains checked after switching to the mobile account list')
    await mobile.locator('.mobile-account-health').waitFor()
    assert.match(
        await mobile.locator('.mobile-account-health').innerText(),
        /Flow[\s\S]*会话正常[\s\S]*Vids[\s\S]*会话就绪/,
    )
    await open('healthy@example.test', true)
    assert.ok(await drawer.locator('.account-balances').isVisible())
    assert.ok(await drawer.locator('.flow-session-summary').isVisible())
    assert.ok(await drawer.locator('.vids-session-summary').isVisible())
    await screenshot('healthy-mobile-top')
    const body = drawer.locator('.el-drawer__body')
    await body.evaluate((element) => {
        element.scrollTop = element.scrollHeight
    })
    await drawer.locator('.account-overview-metadata').scrollIntoViewIfNeeded()
    const metadataBounds = await drawer.locator('.account-overview-metadata').boundingBox()
    assert.ok(
        metadataBounds && metadataBounds.y < 844,
        'Mobile overview metadata must be reachable with continuous scrolling',
    )
    await screenshot('healthy-mobile-bottom')
    record('Mobile list shows both health states and overview scrolls continuously without horizontal overflow')

    assert.ok(apiReads.some((path) => path.includes('/accounts/detail?refresh=true')))
    assert.equal(blockedMutations.length, 0, 'Viewing account overview must not attempt writes')
    assert.deepEqual(errors, [])
    record('Read-only regression performs no account, quota, profile or generation mutations')
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify({ base, checks, errors, blockedMutations, apiReads }, null, 2),
    )
    console.log(`Verified ${checks.length} account overview checks. Evidence: ${output}`)
} catch (error) {
    await page
        .screenshot({ path: resolve(output, 'failure.png'), fullPage: false, animations: 'disabled' })
        .catch(() => {})
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify({ base, checks, errors, blockedMutations, apiReads, failure: error.message }, null, 2),
    )
    throw error
} finally {
    await browser.close()
}
